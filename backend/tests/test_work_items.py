"""Jira cache, work items and intake (design 03 §2, §7, §8; plan P1)."""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, update

from app.db import get_sessionmaker
from app.models import JiraIssue, Project, User, WorkItem
from app.plans.work_items import parse_jira_time, run_intake
from app.tasks import app as tasks
from tests.connector_fakes import fixture, ok, setup_integration

JIRA = "https://jira.example.com"


async def _alice_with_jira(make_user, *, intake: bool = True) -> User:
    alice, _ = await setup_integration(make_user, "jira", JIRA, {"token": "pat"})
    async with get_sessionmaker()() as db:
        await db.execute(update(User).where(User.id == alice.id).values(jira_intake_enabled=intake))
        db.add(Project(owner_id=alice.id, visibility="private", name="payments", repo_url="https://g/t/payments.git",
                       host="gitlab", status="ready"))  # fmt: skip
        await db.commit()
    return alice


async def _items(owner_id: int) -> list[WorkItem]:
    async with get_sessionmaker()() as db:
        return list(
            await db.scalars(select(WorkItem).where(WorkItem.owner_id == owner_id).order_by(WorkItem.created_at))
        )


def test_parse_jira_time() -> None:
    assert parse_jira_time("2026-10-20T09:12:00.000+0200") == datetime(2026, 10, 20, 7, 12, tzinfo=UTC)
    assert parse_jira_time("2026-10-20T07:12:00Z") == datetime(2026, 10, 20, 7, 12, tzinfo=UTC)
    assert parse_jira_time(None).tzinfo is not None


async def test_intake_creates_work_items_once_and_guesses_the_repository(make_user, api) -> None:
    alice = await _alice_with_jira(make_user)
    api.on("/rest/api/2/search", ok(fixture("jira", "intake.json")))
    first = await run_intake(alice.id)
    assert (first.issues, len(first.created), first.failed) == (2, 2, [])
    q = api.requests[0].url.params
    assert (
        q["jql"] == "(assignee = currentUser() AND resolution = Unresolved) AND updated >= -30d ORDER BY updated DESC"
    )
    assert q["maxResults"] == "50" and "components" in q["fields"]
    items = await _items(alice.id)
    async with get_sessionmaker()() as db:
        issues = {i.id: i for i in await db.scalars(select(JiraIssue))}
        payments = await db.scalar(select(Project.id).where(Project.name == "payments"))
    by_key = {issues[w.jira_issue_id].key: w for w in items}
    assert by_key["PAY-12"].project_id == payments  # component "Payments" matched case-insensitively
    assert by_key["OPS-7"].project_id is None and {w.state for w in items} == {"new"}
    pay = next(i for i in issues.values() if i.key == "PAY-12")
    assert pay.fields["links"] == [{"type": "blocks", "key": "PAY-13"}] and pay.priority == "High"
    assert pay.updated_remote == datetime(2026, 10, 20, 7, 12, tzinfo=UTC)

    changed = json.loads(fixture("jira", "intake.json"))
    changed["issues"][0]["fields"]["summary"] = "Retry webhook delivery (renamed)"
    api.routes["/rest/api/2/search"] = [ok(changed)]
    second = await run_intake(alice.id)
    assert second.created == [] and len(await _items(alice.id)) == 2  # once per issue
    async with get_sessionmaker()() as db:
        assert (
            await db.scalar(select(JiraIssue.summary).where(JiraIssue.key == "PAY-12"))
            == "Retry webhook delivery (renamed)"
        )
        assert await db.scalar(select(func.count()).select_from(JiraIssue)) == 2


async def test_intake_is_opt_in_and_uses_the_users_jql(make_user, api, task_queue) -> None:
    alice = await _alice_with_jira(make_user, intake=False)
    assert (await run_intake(alice.id)).issues == 0 and api.requests == []
    assert await tasks.jira_intake_tick(timestamp=0) == 0
    async with get_sessionmaker()() as db:
        await db.execute(update(User).values(jira_intake_enabled=True, jira_intake_jql="project = PAY"))
        await db.commit()
    assert await tasks.jira_intake_tick(timestamp=0) == 1
    [job] = [j for j in task_queue.jobs.values() if j["task_name"] == "jira_intake"]
    assert job["lock"] == f"intake:{alice.id}" and job["args"] == {"user_id": alice.id}
    api.on("/rest/api/2/search", ok({"issues": [], "total": 0}))
    await tasks.jira_intake(user_id=alice.id)
    assert api.requests[-1].url.params["jql"].startswith("(project = PAY) AND updated >= -30d")


async def test_intake_survives_a_jira_failure(make_user, api) -> None:
    alice = await _alice_with_jira(make_user)
    api.on("/rest/api/2/search", ok({}, status=401))
    r = await run_intake(alice.id)
    assert r.failed == ["Jira"] and r.created == []


async def test_add_a_work_item_by_key(make_user, client_for, api) -> None:
    await _alice_with_jira(make_user, intake=False)
    c = await client_for("alice", create=False)
    issue = json.loads(fixture("jira", "intake.json"))["issues"][0]
    api.on("/rest/api/2/issue/PAY-12", ok(issue))
    api.on("/rest/api/2/issue/NOPE-1", ok({}, status=404))
    r = await c.post("/api/work-items", json={"jira_key": " pay-12 "})
    assert r.status_code == 201, r.text
    w = r.json()
    assert (w["key"], w["state"], w["project"]) == ("PAY-12", "new", None)
    assert w["jira_url"] == f"{JIRA}/browse/PAY-12" and w["labels"] == ["mwb-plan"]
    assert w["description"].startswith("Webhooks are dropped")
    dup = await c.post("/api/work-items", json={"jira_key": "PAY-12"})
    assert dup.status_code == 409 and dup.json()["work_item_id"] == w["id"]
    assert (await c.post("/api/work-items", json={"jira_key": "NOPE-1"})).status_code == 404
    bad = await c.post("/api/work-items", json={"jira_key": "not a key"})
    assert bad.status_code == 422 and bad.json()["field"] == "jira_key"
    items = (await c.get("/api/work-items")).json()
    assert [i["key"] for i in items["items"]] == ["PAY-12"] and items["next_cursor"] is None


async def test_work_items_need_jira_credentials(client_for) -> None:
    c = await client_for("dave")
    r = await c.post("/api/work-items", json={"jira_key": "PAY-1"})
    assert r.status_code == 409 and "Settings → Integrations" in r.json()["detail"]


async def test_work_items_are_owner_only(make_user, client_for, api) -> None:
    alice = await _alice_with_jira(make_user)
    api.on("/rest/api/2/search", ok(fixture("jira", "intake.json")))
    await run_intake(alice.id)
    item = (await _items(alice.id))[0]
    bob = await client_for("bob")
    assert (await bob.get("/api/work-items")).json()["items"] == []
    assert (await bob.get(f"/api/work-items/{item.id}")).status_code == 404
    r = await bob.patch(f"/api/work-items/{item.id}", json={"state": "dismissed"})
    assert r.status_code == 404
    c = await client_for("alice", create=False)
    assert len((await c.get("/api/work-items")).json()["items"]) == 2


async def test_assign_dismiss_and_restore(make_user, client_for, api) -> None:
    alice = await _alice_with_jira(make_user)
    api.on("/rest/api/2/search", ok(fixture("jira", "intake.json")))
    await run_intake(alice.id)
    c = await client_for("alice", create=False)
    bob = await make_user("bob")
    async with get_sessionmaker()() as db:
        ops = next(w for w in await db.scalars(select(WorkItem)) if w.project_id is None)
        mine = await db.scalar(select(Project.id).where(Project.name == "payments"))
        theirs = Project(owner_id=bob.id, visibility="private", name="bobs", repo_url="https://g/b.git", host="gitlab")
        db.add(theirs)
        await db.commit()
    r = await c.patch(f"/api/work-items/{ops.id}", json={"project_id": theirs.id})
    assert r.status_code == 422 and r.json()["field"] == "project_id"  # not visible to alice
    r = await c.patch(f"/api/work-items/{ops.id}", json={"project_id": mine})
    assert r.status_code == 200 and r.json()["project"]["name"] == "payments"
    assert (await c.patch(f"/api/work-items/{ops.id}", json={"state": "dismissed"})).json()["state"] == "dismissed"
    assert ops.id not in [i["id"] for i in (await c.get("/api/work-items")).json()["items"]]
    dismissed = (await c.get("/api/work-items", params={"state": "dismissed"})).json()["items"]
    assert [i["id"] for i in dismissed] == [str(ops.id)]
    assert (await c.patch(f"/api/work-items/{ops.id}", json={"state": "new"})).json()["state"] == "new"
    async with get_sessionmaker()() as db:
        await db.execute(update(WorkItem).where(WorkItem.id == ops.id).values(state="approved"))
        await db.commit()
    assert (await c.patch(f"/api/work-items/{ops.id}", json={"state": "dismissed"})).status_code == 409
    assert (await c.patch(f"/api/work-items/{ops.id}", json={"project_id": None})).status_code == 409


async def test_intake_settings_on_the_profile(client_for) -> None:
    c = await client_for("alice")
    r = await c.patch(
        "/api/auth/me",
        json={"jira_intake_enabled": True, "jira_intake_jql": " project = PAY ", "jira_autoplan_label": "mwb-plan"},
    )
    assert r.status_code == 200
    assert (r.json()["jira_intake_enabled"], r.json()["jira_intake_jql"], r.json()["jira_autoplan_label"]) == (
        True, "project = PAY", "mwb-plan",
    )  # fmt: skip
    assert (await c.patch("/api/auth/me", json={"jira_autoplan_label": "has space"})).status_code == 422


async def test_work_item_state_check(make_user) -> None:
    from sqlalchemy.exc import IntegrityError

    alice = await make_user("alice")
    async with get_sessionmaker()() as db:
        from app.models import Integration

        i = Integration(kind="jira", name="J", base_url=JIRA)
        db.add(i)
        await db.flush()
        issue = JiraIssue(owner_id=alice.id, integration_id=i.id, key="A-1", summary="s", status="x",
                          updated_remote=datetime.now(UTC), fields={})  # fmt: skip
        db.add(issue)
        await db.flush()
        db.add(WorkItem(owner_id=alice.id, jira_issue_id=issue.id))
        await db.commit()
    with pytest.raises(IntegrityError):
        async with get_sessionmaker()() as db:
            await db.execute(update(WorkItem).values(state="shipped"))
            await db.commit()
    with pytest.raises(IntegrityError):  # one item per (owner, issue, repository), "no repository" included
        async with get_sessionmaker()() as db:
            db.add(WorkItem(owner_id=alice.id, jira_issue_id=issue.id))
            await db.commit()

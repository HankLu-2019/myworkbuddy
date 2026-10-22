"""Approve, reject, revise, user revisions and the revision diff (design 03 §6, §8; plan P4)."""

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.agents.seeds import sync_system_agents
from app.db import get_sessionmaker
from app.knowledge.ingest.repos import cache_dir
from app.llm.models import set_model_override
from app.models import Approval, AuditLog, Plan, PlanRevision, WorkItem
from app.plans import planner, repo
from app.plans.approvals import plan_revision_approved
from app.plans.diff import diff
from app.plans.schema import PlanDoc, approval_payload_hash, content_hash
from app.plans.validation import validate_plan
from tests.test_planner import KEY, Script, _work_item, plan_doc

CI = "ci_config:.gitlab-ci.yml"


async def seed_plan(
    make_user, tmp_path: Path, doc: dict[str, Any] | None = None
) -> tuple[WorkItem, Plan, PlanRevision]:
    """A work item whose plan waits for a decision on revision 1, validated against the real repository."""
    alice, item, sha = await _work_item(make_user, tmp_path)
    doc = doc or plan_doc(changes=[
        {"path": "app/webhooks.py", "action": "modify", "description": "Add retries."},
        {"path": ".gitlab-ci.yml", "action": "create", "description": "Run the retry tests."},
    ])  # fmt: skip
    existing = await repo.tracked_paths(cache_dir(item.project_id), sha)
    clean, validation = validate_plan(
        PlanDoc.model_validate(doc), jira_key=KEY, existing_paths=existing, visible_refs=[], optional_hosts=[]
    )
    content = clean.model_dump(mode="json")
    async with get_sessionmaker()() as db:
        plan = Plan(work_item_id=item.id, owner_id=alice.id, status="needs_decision", current_revision=1,
                    base_sha=sha, base_branch="main")  # fmt: skip
        db.add(plan)
        await db.flush()
        rev = PlanRevision(plan_id=plan.id, revision=1, content=content, content_hash=content_hash(content),
                           author="planner", validation=validation.model_dump())  # fmt: skip
        db.add(rev)
        w = await db.get(WorkItem, item.id)
        w.state, w.current_plan_id = "needs_decision", plan.id
        await db.commit()
    return item, plan, rev


async def _state(plan_id: uuid.UUID, item_id: uuid.UUID) -> tuple[str, str]:
    async with get_sessionmaker()() as db:
        return (await db.get(Plan, plan_id)).status, (await db.get(WorkItem, item_id)).state


# ---------- approve ----------


async def test_approve_binds_the_hash_and_the_acknowledgements(make_user, client_for, tmp_path) -> None:
    item, plan, rev = await seed_plan(make_user, tmp_path)
    assert rev.validation["ok"] and rev.validation["needs_ack"] == [CI]
    c = await client_for("alice", create=False)
    url = f"/api/plans/{plan.id}/approve"

    stale = await c.post(url, json={"revision": 2, "content_hash": rev.content_hash, "acknowledged": [CI]})
    assert stale.status_code == 409 and stale.json()["code"] == "plan_changed"
    wrong = await c.post(url, json={"revision": 1, "content_hash": "0" * 64, "acknowledged": [CI]})
    assert wrong.status_code == 409 and wrong.json()["code"] == "plan_changed"
    missing = await c.post(url, json={"revision": 1, "content_hash": rev.content_hash, "acknowledged": []})
    assert (
        missing.status_code == 422 and missing.json()["missing"] == [CI] and missing.json()["field"] == "acknowledged"
    )
    extra = await c.post(
        url, json={"revision": 1, "content_hash": rev.content_hash, "acknowledged": [CI, "large_change"]}
    )
    assert extra.status_code == 422 and extra.json()["unknown"] == ["large_change"]
    assert await _state(plan.id, item.id) == ("needs_decision", "needs_decision")

    r = await c.post(url, json={"revision": 1, "content_hash": rev.content_hash, "acknowledged": [CI, CI]})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    [decision] = r.json()["approvals"]
    assert decision["revision"] == 1 and decision["decision"] == "approved" and decision["acknowledged"] == [CI]
    assert await _state(plan.id, item.id) == ("approved", "approved")
    async with get_sessionmaker()() as db:
        row = await db.scalar(select(Approval))
        assert row.payload_hash == approval_payload_hash(rev.content_hash, [CI]) and row.subject_id == rev.id
        assert await plan_revision_approved(db, await db.get(PlanRevision, rev.id))
        audit = await db.scalar(select(AuditLog).where(AuditLog.action == "plan.approve"))
        assert audit.detail["payload_hash"] == row.payload_hash and audit.detail["acknowledged"] == [CI]

    again = await c.post(url, json={"revision": 1, "content_hash": rev.content_hash, "acknowledged": [CI]})
    assert again.status_code == 409 and again.json()["code"] == "plan_changed"


async def test_a_revision_with_errors_cannot_be_approved(make_user, client_for, tmp_path) -> None:
    item, plan, rev = await seed_plan(make_user, tmp_path, plan_doc(commit_message="no key"))
    c = await client_for("alice", create=False)
    r = await c.post(f"/api/plans/{plan.id}/approve", json={"revision": 1, "content_hash": rev.content_hash})
    assert r.status_code == 422 and r.json()["field"] == "validation"


async def test_a_tampered_revision_cannot_be_approved(make_user, client_for, tmp_path) -> None:
    item, plan, rev = await seed_plan(make_user, tmp_path)
    async with get_sessionmaker()() as db:  # simulate a row edited behind the app's back
        await db.execute(
            PlanRevision.__table__.update()
            .where(PlanRevision.id == rev.id)
            .values(content={**rev.content, "title": "Something else"})
        )
        await db.commit()
    c = await client_for("alice", create=False)
    r = await c.post(
        f"/api/plans/{plan.id}/approve", json={"revision": 1, "content_hash": rev.content_hash, "acknowledged": [CI]}
    )
    assert r.status_code == 409 and r.json()["code"] == "conflict"


# ---------- reject and revise ----------


async def test_reject_resets_the_work_item(make_user, client_for, tmp_path) -> None:
    item, plan, rev = await seed_plan(make_user, tmp_path)
    c = await client_for("alice", create=False)
    r = await c.post(f"/api/plans/{plan.id}/reject", json={"revision": 1, "comment": "Wrong approach"})
    assert r.status_code == 200 and r.json()["approvals"][0]["decision"] == "rejected"
    assert await _state(plan.id, item.id) == ("rejected", "new")
    async with get_sessionmaker()() as db:
        assert not await plan_revision_approved(db, await db.get(PlanRevision, rev.id))
    # A rejected plan is replaced by starting a new one.
    assert (await c.post(f"/api/plans/{plan.id}/revise", json={"revision": 1, "feedback": "x"})).status_code == 409
    new = await c.post(f"/api/work-items/{item.id}/plans")
    assert new.status_code == 202 and (await _state(plan.id, item.id)) == ("superseded", "planning")


async def test_revise_with_feedback_drafts_the_next_revision(
    make_user, client_for, tmp_path, task_queue, monkeypatch
) -> None:
    item, plan, rev = await seed_plan(make_user, tmp_path)
    async with get_sessionmaker()() as db:
        await sync_system_agents(db)
        await db.commit()
    c = await client_for("alice", create=False)
    too_long = await c.post(f"/api/plans/{plan.id}/revise", json={"revision": 1, "feedback": "x" * 4001})
    assert too_long.status_code == 422
    r = await c.post(f"/api/plans/{plan.id}/revise", json={"revision": 1, "feedback": "Use exponential backoff."})
    assert r.status_code == 202 and r.json()["status"] == "drafting"
    assert await _state(plan.id, item.id) == ("drafting", "planning")
    [job] = [j for j in task_queue.jobs.values() if j["task_name"] == "draft_plan"]
    assert job["args"]["feedback"] == "Use exponential backoff." and job["lock"] == f"project:{item.project_id}"

    async def no_fetch(db, project):
        await db.commit()
        return cache_dir(project.id)

    monkeypatch.setattr(planner, "sync_checkout", no_fetch)
    script = Script(plan_doc(approach="Exponential backoff."))
    set_model_override(script.model())
    assert await planner.draft_plan(plan.id, "Use exponential backoff.") == "needs_decision"
    prompt = script.prompts[0]
    assert "## The previous revision" in prompt and ".gitlab-ci.yml" in prompt
    assert "## The user's feedback on it\nUse exponential backoff." in prompt
    async with get_sessionmaker()() as db:
        new = await db.scalar(select(PlanRevision).where(PlanRevision.plan_id == plan.id, PlanRevision.revision == 2))
    assert new.change_note == "Use exponential backoff." and new.author == "planner"
    d = (await c.get(f"/api/plans/{plan.id}/diff", params={"from": 1, "to": 2})).json()
    assert {(f["field"], f.get("key"), f["kind"]) for f in d["fields"]} == {
        ("approach", None, "changed"),
        ("changes", ".gitlab-ci.yml", "removed"),
    }


# ---------- user revisions ----------


async def test_a_user_revision_is_validated_and_not_approved(make_user, client_for, tmp_path) -> None:
    item, plan, rev = await seed_plan(make_user, tmp_path)
    c = await client_for("alice", create=False)
    url = f"/api/plans/{plan.id}/revisions"
    edited = {**rev.content, "changes": [{"path": "./app//missing.py", "action": "modify", "description": "d"}]}

    stale = await c.post(url, json={"base_revision": 2, "content": edited})
    assert stale.status_code == 409 and stale.json()["code"] == "plan_changed"
    bad = await c.post(url, json={"base_revision": 1, "content": {**edited, "surprise": 1}})
    assert bad.status_code == 422  # extra fields are refused, like the planner's output

    r = await c.post(url, json={"base_revision": 1, "content": edited, "change_note": "Narrower"})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "needs_decision" and body["current_revision"] == 2 and body["approvals"] == []
    new = body["revision"]
    assert new["author"] == "user" and new["change_note"] == "Narrower"
    assert new["content"]["changes"][0]["path"] == "app/missing.py"  # normalised
    assert new["validation"]["ok"] is False and new["validation"]["issues"][0]["field"] == "changes[0].path"
    assert await _state(plan.id, item.id) == ("needs_decision", "needs_decision")

    # The old revision can no longer be approved; the new one is not approved until someone approves it.
    old = await c.post(
        f"/api/plans/{plan.id}/approve", json={"revision": 1, "content_hash": rev.content_hash, "acknowledged": [CI]}
    )
    assert old.status_code == 409
    fixed = {**rev.content, "title": "Retry webhooks, narrower"}
    r3 = (await c.post(url, json={"base_revision": 2, "content": fixed})).json()["revision"]
    assert r3["revision"] == 3 and r3["validation"]["ok"] and r3["validation"]["needs_ack"] == [CI]
    async with get_sessionmaker()() as db:
        rows = list(await db.scalars(select(PlanRevision).where(PlanRevision.plan_id == plan.id)))
        assert not any([await plan_revision_approved(db, r) for r in rows])
    ok = await c.post(
        f"/api/plans/{plan.id}/approve", json={"revision": 3, "content_hash": r3["content_hash"], "acknowledged": [CI]}
    )
    assert ok.status_code == 200
    assert (await c.post(url, json={"base_revision": 3, "content": fixed})).status_code == 409  # approved: no edits


async def test_without_a_checkout_paths_are_not_checked(make_user, client_for, tmp_path) -> None:
    import shutil

    item, plan, rev = await seed_plan(make_user, tmp_path)
    shutil.rmtree(cache_dir(item.project_id))
    c = await client_for("alice", create=False)
    edited = {**rev.content, "changes": [{"path": "app/missing.py", "action": "modify", "description": "d"}]}
    v = (await c.post(f"/api/plans/{plan.id}/revisions", json={"base_revision": 1, "content": edited})).json()[
        "revision"
    ]["validation"]
    assert v["ok"] is True and any("weren't checked" in i["message"] for i in v["issues"])


# ---------- history, diff and access ----------


async def test_revision_history_and_diff(make_user, client_for, tmp_path) -> None:
    item, plan, rev = await seed_plan(make_user, tmp_path)
    c = await client_for("alice", create=False)
    for n in range(1, 4):
        content = {**rev.content, "title": f"Title {n}"}
        assert (
            await c.post(f"/api/plans/{plan.id}/revisions", json={"base_revision": n, "content": content})
        ).status_code == 201
    page1 = (await c.get(f"/api/plans/{plan.id}/revisions", params={"limit": 3})).json()
    assert [r["revision"] for r in page1["items"]] == [4, 3, 2] and page1["next_cursor"]
    page2 = (await c.get(f"/api/plans/{plan.id}/revisions", params={"cursor": page1["next_cursor"]})).json()
    assert [r["revision"] for r in page2["items"]] == [1] and page2["next_cursor"] is None
    assert (await c.get(f"/api/plans/{plan.id}/revisions/1")).json()["content_hash"] == rev.content_hash
    assert (await c.get(f"/api/plans/{plan.id}/revisions/9")).status_code == 404
    d = (await c.get(f"/api/plans/{plan.id}/diff", params={"from": 1, "to": 4})).json()
    assert d["fields"] == [
        {"field": "title", "key": None, "kind": "changed", "before": "Retry webhooks", "after": "Title 3"}
    ]


async def test_plans_are_owner_only(make_user, client_for, tmp_path) -> None:
    item, plan, rev = await seed_plan(make_user, tmp_path)
    bob = await client_for("bob")
    body = {"revision": 1, "content_hash": rev.content_hash, "acknowledged": [CI]}
    for method, path, payload in [
        ("post", "approve", body),
        ("post", "reject", {"revision": 1}),
        ("post", "revise", {"revision": 1, "feedback": "x"}),
        ("post", "revisions", {"base_revision": 1, "content": rev.content}),
        ("get", "revisions", None),
        ("get", "revisions/1", None),
        ("get", "diff?from=1&to=1", None),
    ]:
        r = await getattr(bob, method)(f"/api/plans/{plan.id}/{path}", **({"json": payload} if payload else {}))
        assert r.status_code == 404, path
    assert await _state(plan.id, item.id) == ("needs_decision", "needs_decision")


# ---------- diff ----------


def test_diff_matches_list_items_by_key() -> None:
    a = plan_doc(
        changes=[
            {"path": "a.py", "action": "modify", "description": "1"},
            {"path": "b.py", "action": "modify", "description": "2"},
        ],
        verification=["pytest", "ruff check"],
        sandbox={"optional_hosts": ["pypi.org"], "cpus": 2},
    )
    b = plan_doc(
        changes=[
            {"path": "b.py", "action": "modify", "description": "2"},
            {"path": "a.py", "action": "modify", "description": "1, changed"},
            {"path": "c.py", "action": "create", "description": "3"},
        ],
        verification=["pytest", "mypy"],
        sandbox={"optional_hosts": ["pypi.org"], "cpus": 4},
    )
    out = [(f.field, f.key, f.kind) for f in diff(a, b)]
    assert out == [
        ("changes", "a.py", "changed"),
        ("changes", "c.py", "added"),
        ("changes", None, "reordered"),
        ("verification", "ruff check", "removed"),
        ("verification", "mypy", "added"),
        ("sandbox.cpus", None, "changed"),
    ]
    assert diff(a, a) == []
    assert [(f.field, f.kind) for f in diff({"verification": ["a", "b"]}, {"verification": ["b", "a"]})] == [
        ("verification", "reordered")
    ]

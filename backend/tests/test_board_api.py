"""The API the board and work item hub use (design 03 §8–9, 07 §4.9–4.10; plan P5)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from app.chat.context import context_message
from app.db import get_sessionmaker
from app.models import ChatSession, PlanRevision, User, WorkItem
from tests.test_plan_decisions import seed_plan


async def test_board_rows_carry_plan_state_and_cost(make_user, client_for, tmp_path) -> None:
    item, plan, rev = await seed_plan(make_user, tmp_path)
    async with get_sessionmaker()() as db:
        await db.execute(update(PlanRevision).values(cost_usd=0.0125))
        await db.commit()
    c = await client_for("alice", create=False)
    [row] = (await c.get("/api/work-items")).json()["items"]
    assert (row["plan_status"], row["plan_error"], row["cost_usd"]) == ("needs_decision", None, 0.0125)
    assert (await c.get(f"/api/work-items/{item.id}")).json()["cost_usd"] == 0.0125
    assert (await c.get(f"/api/plans/{plan.id}")).json()["revision"]["id"] == str(rev.id)


async def test_done_shows_the_last_two_weeks(make_user, client_for, tmp_path) -> None:
    item, _, _ = await seed_plan(make_user, tmp_path)
    c = await client_for("alice", create=False)
    async with get_sessionmaker()() as db:
        await db.execute(update(WorkItem).values(state="done", updated_at=datetime.now(UTC) - timedelta(days=15)))
        await db.commit()
    assert (await c.get("/api/work-items")).json()["items"] == []
    assert len((await c.get("/api/work-items", params={"state": "done"})).json()["items"]) == 1
    async with get_sessionmaker()() as db:
        await db.execute(update(WorkItem).values(updated_at=datetime.now(UTC) - timedelta(days=13)))
        await db.commit()
    assert len((await c.get("/api/work-items")).json()["items"]) == 1


async def test_refresh_from_jira(make_user, client_for, tmp_path, task_queue) -> None:
    await seed_plan(make_user, tmp_path)  # alice has a Jira token
    c = await client_for("alice", create=False)
    off = await c.post("/api/work-items/refresh")
    assert off.status_code == 409 and off.json()["field"] == "jira_intake_enabled"
    async with get_sessionmaker()() as db:
        await db.execute(update(User).values(jira_intake_enabled=True))
        await db.commit()
    r = await c.post("/api/work-items/refresh")
    assert r.status_code == 202 and r.json() == {"queued": True}
    assert (await c.post("/api/work-items/refresh")).json() == {"queued": False}  # already waiting
    assert [j["task_name"] for j in task_queue.jobs.values()] == ["jira_intake"]
    bob = await client_for("bob")
    async with get_sessionmaker()() as db:
        await db.execute(update(User).where(User.username == "bob").values(jira_intake_enabled=True))
        await db.commit()
    no_token = await bob.post("/api/work-items/refresh")
    assert no_token.status_code == 409 and no_token.json()["field"] == "jira"


async def test_home_lists_plans_waiting_for_a_decision(make_user, client_for, tmp_path) -> None:
    item, _, _ = await seed_plan(make_user, tmp_path)
    c = await client_for("alice", create=False)
    home = (await c.get("/api/home")).json()
    assert home["board_needs"] == 1
    [card] = [n for n in home["needs_you"] if n["kind"] == "plan_ready"]
    assert card["message"] == "The plan for PAY-12 needs a decision" and card["href"] == f"/work/{item.id}/plan"
    bob = await client_for("bob")
    assert (await bob.get("/api/home")).json()["board_needs"] == 0


async def test_discuss_a_plan_revision_in_chat(make_user, client_for, tmp_path) -> None:
    item, plan, rev = await seed_plan(make_user, tmp_path)
    c = await client_for("alice", create=False)
    r = await c.post(
        "/api/chat/sessions",
        json={"title": "Discuss PAY-12", "context": [{"type": "plan_revision", "id": str(rev.id), "label": "forged"}]},
    )
    assert r.status_code == 201
    s = r.json()
    assert s["project_id"] == item.project_id  # the plan's repository is in focus
    assert s["context"] == [
        {
            "type": "plan_revision",
            "id": str(rev.id),
            "label": "Plan for PAY-12 · revision 1",
            "href": f"/work/{item.id}/plan",
        }
    ]
    async with get_sessionmaker()() as db:
        session = await db.get(ChatSession, s["id"])
        alice = await db.get(User, session.owner_id)
        block = await context_message(db, alice, session.context)
    assert block and '"untrusted": true' in block and f"plan_revision:{rev.id}" in block and "Retry webhooks" in block

    bob = await client_for("bob")
    denied = await bob.post("/api/chat/sessions", json={"context": [{"type": "plan_revision", "id": str(rev.id)}]})
    assert denied.status_code == 404
    bad = await c.post("/api/chat/sessions", json={"context": [{"type": "plan_revision", "id": "nope"}]})
    assert bad.status_code == 422

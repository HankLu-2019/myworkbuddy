# ruff: noqa: F811 - pytest fixtures imported from the job tests
"""The agent can ask (design 08 §F3; plan F3)."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, update

from app.config import get_settings
from app.db import get_sessionmaker
from app.jobs import pipeline, track
from app.models import Artifact, InboxItem, Job, JobEvent, OutboundEmail
from tests import fake_runner
from tests.test_job_pipeline import approved_ticket, forge_url, job_row, runner, start  # noqa: F401 - fixtures

QUESTION = "Should a 429 be retried too, or only 502, 503 and 504?\n\n- A: also 429\n- B: only 5xx"


async def asking(make_user, client_for, forge_url, runner, **kw) -> tuple[dict, str]:
    t = await approved_ticket(make_user, forge_url)
    job_id = await start(client_for, t)
    runner.agent = [fake_runner.ask(QUESTION, **kw)]
    assert await pipeline.advance(uuid.UUID(job_id)) == "needs_input"
    return t, job_id


async def test_a_question_waits_for_the_answer_without_spending_an_attempt(
    make_user, client_for, forge_url, runner, task_queue
) -> None:
    t, job_id = await asking(make_user, client_for, forge_url, runner, change=True)
    c = await client_for("alice", create=False)
    body = (await c.get(f"/api/jobs/{job_id}")).json()
    assert body["status"] == "needs_input" and body["questions"] == 1 and body["attempt"] == 1
    assert body["question"]["text"] == QUESTION and body["question"]["number"] == 1
    async with get_sessionmaker()() as db:
        art = await db.get(Artifact, uuid.UUID(body["question"]["patch_artifact_id"]))
        assert art.key.endswith("patch-r0-a1-q1.diff")
        titles = list(await db.scalars(select(InboxItem.title).where(InboxItem.owner_id == t["user"].id)))
        emails = list(await db.scalars(select(OutboundEmail.subject).where(OutboundEmail.owner_id == t["user"].id)))
    assert "PAY-12: the agent has a question" in titles and "PAY-12: the agent has a question" in emails
    assert job_id in runner.workspaces  # kept while waiting
    needs = (await c.get("/api/home")).json()["needs_you"]
    assert [n["action"] for n in needs if n["kind"] == "job_question"] == ["Answer"]
    patch = (await c.get(f"/api/artifacts/{art.id}")).text
    assert "Retry transient failures" in patch  # what the agent did before it asked
    seen = {}

    def agent(ws: Path, env: dict):
        seen["answer"] = (ws / ".mwb" / "answer.md").read_text()
        seen["prompt"] = (ws / ".mwb" / "prompt.md").read_text()
        seen["question_left"] = (ws / ".mwb" / "question.md").exists()
        return fake_runner.planned_change(ws, env)

    runner.agent = [agent]
    assert (await c.post(f"/api/jobs/{job_id}/answer", json={"answer": ""})).status_code == 422
    r = await c.post(f"/api/jobs/{job_id}/answer", json={"answer": "B: only 5xx."})
    assert r.status_code == 200 and r.json()["status"] == "running" and r.json()["question"] is None, r.text
    assert await pipeline.advance(uuid.UUID(job_id)) == "awaiting_review"
    assert seen["answer"] == f"# Your question\n\n{QUESTION}\n\n# The reviewer's answer\n\nB: only 5xx.\n"
    assert "read .mwb/answer.md first" in seen["prompt"] and "may ask 2 more question(s)" in seen["prompt"]
    assert seen["question_left"] is False
    job = await job_row(job_id)
    assert job.attempt == 1 and job.questions == 1
    assert (await c.post(f"/api/jobs/{job_id}/answer", json={"answer": "again"})).status_code == 409


async def test_questions_past_the_cap_are_ignored(make_user, client_for, forge_url, runner, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "job_max_questions", 1)
    _t, job_id = await asking(make_user, client_for, forge_url, runner)
    c = await client_for("alice", create=False)
    seen = {}

    def again(ws: Path, env: dict):
        seen["prompt"] = (ws / ".mwb" / "prompt.md").read_text()
        return fake_runner.ask("And 408?", change=True)(ws, env)

    runner.agent = [again]
    assert (await c.post(f"/api/jobs/{job_id}/answer", json={"answer": "Only 5xx."})).status_code == 200
    assert await pipeline.advance(uuid.UUID(job_id)) == "awaiting_review"
    assert "can't ask more questions" in seen["prompt"]
    async with get_sessionmaker()() as db:
        texts = [
            e.data.get("text") for e in await db.scalars(select(JobEvent).where(JobEvent.job_id == uuid.UUID(job_id)))
        ]
    assert "The agent asked past its question limit; ignored." in texts
    assert (await job_row(job_id)).questions == 1


async def test_a_waiting_job_can_be_cancelled_or_expires(make_user, client_for, forge_url, runner) -> None:
    _t, job_id = await asking(make_user, client_for, forge_url, runner)
    c = await client_for("alice", create=False)
    bob = await client_for("bob")
    assert (await bob.post(f"/api/jobs/{job_id}/answer", json={"answer": "x"})).status_code == 404
    r = await c.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert job_id not in runner.workspaces


async def test_a_question_left_unanswered_is_discarded(make_user, client_for, forge_url, runner) -> None:
    _t, job_id = await asking(make_user, client_for, forge_url, runner)
    days = get_settings().job_review_days
    async with get_sessionmaker()() as db:
        old = datetime.now(UTC) - timedelta(days=days - 0.5)
        await db.execute(update(Job).where(Job.id == uuid.UUID(job_id)).values(updated_at=old))
        await db.commit()
    assert (await track.cleanup())["reminded"] == 1
    async with get_sessionmaker()() as db:
        old = datetime.now(UTC) - timedelta(days=days + 0.1)
        await db.execute(update(Job).where(Job.id == uuid.UUID(job_id)).values(updated_at=old))
        await db.commit()
    assert (await track.cleanup())["discarded"] == 1
    assert (await job_row(job_id)).status == "discarded" and job_id not in runner.workspaces

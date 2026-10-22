"""Jobs: the state machine, its events and the read API (design 04 §2–3, §8; plan X3)."""

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db import get_sessionmaker
from app.enums import ApprovalDecision, ApprovalSubject, JobStatus
from app.jobs import state
from app.jobs.state import TRANSITIONS, InvalidTransition, transition
from app.models import AuditLog, Job, JobEvent, WorkItem
from app.plans import approvals
from tests.test_plan_decisions import seed_plan

S = JobStatus


async def make_job(make_user, tmp_path: Path, status: JobStatus = S.created) -> Job:
    item, plan, rev = await seed_plan(make_user, tmp_path)
    async with get_sessionmaker()() as db:
        approval = await approvals.record(
            db, owner_id=plan.owner_id, subject_type=ApprovalSubject.plan_revision, subject_id=rev.id,
            payload_hash="0" * 64, decision=ApprovalDecision.approved,
        )  # fmt: skip
        job = Job(owner_id=plan.owner_id, work_item_id=item.id, plan_revision_id=rev.id, plan_approval_id=approval.id,
                  project_id=item.project_id, base_branch="main", base_sha="abc1234", branch_name="mwb/PAY-12-retry",
                  status=status, policy={"max_attempts": 3, "timeout_min": 45})  # fmt: skip
        db.add(job)
        await db.commit()
        return job


FIELDS = ("owner_id", "work_item_id", "plan_revision_id", "plan_approval_id", "base_branch", "base_sha",
          "branch_name", "policy")  # fmt: skip


def _clone(job: Job, status: str) -> Job:
    return Job(**{c: getattr(job, c) for c in FIELDS}, status=status)


async def _set(job_id: uuid.UUID, status: JobStatus) -> None:
    async with get_sessionmaker()() as db:
        await db.execute(update(Job).where(Job.id == job_id).values(status=status))
        await db.commit()


async def test_every_allowed_transition_works_and_logs_an_event(make_user, tmp_path) -> None:
    job = await make_job(make_user, tmp_path)
    pairs = [(a, b) for a, nexts in TRANSITIONS.items() for b in nexts]
    assert len(pairs) == 30
    for current, new in pairs:
        await _set(job.id, current)
        moved = await transition(job.id, current, new, detail={"why": "test"})
        assert moved is not None and moved.status == new, (current, new)
    async with get_sessionmaker()() as db:
        events = list(await db.scalars(select(JobEvent).where(JobEvent.job_id == job.id).order_by(JobEvent.seq)))
        audits = await db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.target_id == str(job.id)))
    assert [e.seq for e in events] == list(range(1, 31))
    assert [(e.data["from"], e.data["to"]) for e in events] == [(str(a), str(b)) for a, b in pairs]
    assert all(e.type == "status" and e.data["why"] == "test" for e in events) and audits == 30


async def test_every_other_transition_is_rejected(make_user, tmp_path) -> None:
    job = await make_job(make_user, tmp_path)
    for current in S:
        for new in S:
            if new in TRANSITIONS[current]:
                continue
            with pytest.raises(InvalidTransition):
                await transition(job.id, current, new)


async def test_a_job_in_another_status_is_left_alone(make_user, tmp_path) -> None:
    job = await make_job(make_user, tmp_path, S.running)
    assert await transition(job.id, S.created, S.preparing) is None  # someone else moved it already
    assert await transition(uuid.uuid4(), S.created, S.preparing) is None
    moved = await transition(job.id, [S.created, S.running], S.cancelled)
    assert moved.status == S.cancelled and moved.finished_at is not None
    async with get_sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(JobEvent)) == 1


async def test_timestamps_and_work_item_follow_the_job(make_user, tmp_path) -> None:
    job = await make_job(make_user, tmp_path)
    j = await transition(job.id, S.created, S.preparing)
    assert j.started_at is not None and j.finished_at is None

    async def item_state() -> str:
        async with get_sessionmaker()() as db:
            return await db.scalar(select(WorkItem.state).where(WorkItem.id == job.work_item_id))

    assert await item_state() == "in_progress"
    for current, new, expected in [
        (S.preparing, S.running, "in_progress"), (S.running, S.verifying, "in_progress"),
        (S.verifying, S.awaiting_review, "in_progress"), (S.awaiting_review, S.approved_push, "in_progress"),
        (S.approved_push, S.pushing, "in_progress"), (S.pushing, S.pushed, "in_review"),
        (S.pushed, S.merged, "done"),
    ]:  # fmt: skip
        await transition(job.id, current, new)
        assert await item_state() == expected, new


async def test_one_active_job_per_work_item(make_user, tmp_path) -> None:
    job = await make_job(make_user, tmp_path, S.awaiting_review)
    async with get_sessionmaker()() as db:
        twin = _clone(job, "created")
        db.add(twin)
        with pytest.raises(IntegrityError):
            await db.commit()
    await transition(job.id, S.awaiting_review, S.discarded)
    async with get_sessionmaker()() as db:
        db.add(_clone(job, "created"))
        await db.commit()  # the old one is finished: a new one may start


async def test_events_replay_and_stream_until_the_job_rests(make_user, client_for, tmp_path, monkeypatch) -> None:
    from app.jobs import routes

    monkeypatch.setattr(routes, "EVENT_POLL_S", 0.01)
    job = await make_job(make_user, tmp_path)
    await transition(job.id, S.created, S.preparing)
    await state.record_event(job.id, "agent_text", {"text": "Reading the task"})
    await transition(job.id, S.preparing, S.running)
    c = await client_for("alice", create=False)

    async def move_on() -> None:  # the job moves on while the stream is open
        await asyncio.sleep(0.2)
        await transition(job.id, S.running, S.verifying)
        await transition(job.id, S.verifying, S.awaiting_review)

    def parse(text: str) -> list[tuple[str | None, str, dict]]:
        out = []
        for block in text.strip().split("\n\n"):
            fields = dict(line.split(": ", 1) for line in block.splitlines() if not line.startswith(":"))
            if "event" in fields:
                out.append((fields.get("id"), fields["event"], json.loads(fields["data"])))
        return out

    mover = asyncio.create_task(move_on())
    r = await c.get(f"/api/jobs/{job.id}/events", params={"after": 1})
    await mover
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = parse(r.text)
    assert [(i, e) for i, e, _ in events] == [
        ("2", "agent_text"),
        ("3", "status"),
        ("4", "status"),
        ("5", "status"),
        (None, "end"),
    ]
    assert events[0][2]["text"] == "Reading the task" and events[-1][2] == {"status": "awaiting_review"}
    # Reconnecting with Last-Event-ID replays only what came after it, then ends.
    r = await c.get(f"/api/jobs/{job.id}/events", headers={"Last-Event-ID": "4"})
    assert [ln for ln in r.text.splitlines() if ln.startswith("id: ")] == ["id: 5"]


async def test_jobs_are_owner_only_and_paginated(make_user, client_for, tmp_path) -> None:
    job = await make_job(make_user, tmp_path)
    c = await client_for("alice", create=False)
    page = (await c.get("/api/jobs")).json()
    [row] = page["items"]
    assert (
        row["id"] == str(job.id) and row["key"] == "PAY-12" and row["status"] == "created" and row["max_attempts"] == 3
    )
    assert (await c.get(f"/api/jobs/{job.id}")).json()["branch_name"] == "mwb/PAY-12-retry"
    assert (await c.get("/api/jobs", params={"status": "running"})).json()["items"] == []
    bob = await client_for("bob")
    assert (await bob.get(f"/api/jobs/{job.id}")).status_code == 404
    assert (await bob.get(f"/api/jobs/{job.id}/events")).status_code == 404
    assert (await bob.get("/api/jobs")).json()["items"] == []


async def test_internal_active_jobs_needs_the_runner_token(make_user, anon, tmp_path, monkeypatch) -> None:
    from pydantic import SecretStr

    active = await make_job(make_user, tmp_path, S.running)
    async with get_sessionmaker()() as db:
        db.add(_clone(active, "failed"))
        await db.commit()
    assert (await anon.get("/internal/jobs/active")).status_code == 401  # no token configured
    monkeypatch.setattr(get_settings(), "runner_token", SecretStr("r" * 32))
    assert (await anon.get("/internal/jobs/active", headers={"Authorization": "Bearer wrong"})).status_code == 401
    r = await anon.get("/internal/jobs/active", headers={"Authorization": f"Bearer {'r' * 32}"})
    assert r.json() == {"jobs": [str(active.id)]}

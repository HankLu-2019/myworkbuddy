"""The job state machine and its event log (design 04 §3).

Every transition is a short transaction: lock the job row, check its current status is one the caller expected,
update it, append a `job_events` row and audit. A job found in any other status is left alone and the caller
gets None: the step it was about to take no longer applies (rule R4, idempotent tasks). Asking for a transition
the machine doesn't have is a programming error (InvalidTransition).
"""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import audit
from app.db import get_sessionmaker
from app.enums import JobEventType, JobStatus, WorkItemState
from app.models import ACTIVE_JOB_STATUSES, Job, JobEvent, WorkItem

S = JobStatus
TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    S.created: frozenset({S.preparing, S.cancelled, S.failed}),
    S.preparing: frozenset({S.running, S.failed, S.cancelled}),
    S.running: frozenset({S.verifying, S.failed, S.timed_out, S.budget_exceeded, S.cancelled}),
    S.verifying: frozenset({S.running, S.awaiting_review, S.failed, S.cancelled}),
    S.awaiting_review: frozenset({S.running, S.approved_push, S.discarded}),
    S.approved_push: frozenset({S.pushing, S.failed}),
    S.pushing: frozenset({S.pushed, S.failed}),
    S.pushed: frozenset({S.merged, S.closed, S.running}),
    S.merged: frozenset(),
    S.closed: frozenset(),
    S.failed: frozenset(),
    S.timed_out: frozenset(),
    S.budget_exceeded: frozenset(),
    S.cancelled: frozenset(),
    S.discarded: frozenset(),
}
ACTIVE = frozenset(JobStatus(s) for s in ACTIVE_JOB_STATUSES)
TERMINAL = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)
# Statuses in which the job is doing something on its own (the SSE stream keeps going; the Job tab polls).
WORKING = frozenset({S.created, S.preparing, S.running, S.verifying, S.approved_push, S.pushing})

# What the work item becomes when its job reaches a status (03 §10).
WORK_ITEM_STATE: dict[JobStatus, WorkItemState] = {
    S.preparing: WorkItemState.in_progress,
    S.running: WorkItemState.in_progress,
    S.pushed: WorkItemState.in_review,
    S.merged: WorkItemState.done,
    S.closed: WorkItemState.approved,
    S.failed: WorkItemState.approved,
    S.timed_out: WorkItemState.approved,
    S.budget_exceeded: WorkItemState.approved,
    S.cancelled: WorkItemState.approved,
    S.discarded: WorkItemState.approved,
}


class InvalidTransition(ValueError):
    pass


def allowed(current: JobStatus | str, new: JobStatus | str) -> bool:
    return JobStatus(new) in TRANSITIONS[JobStatus(current)]


async def add_event(db: AsyncSession, job_id: uuid.UUID, type_: JobEventType | str, data: dict[str, Any]) -> int:
    """Append an event; the caller holds the job row lock (or this takes it) so sequence numbers don't collide."""
    await db.execute(select(Job.id).where(Job.id == job_id).with_for_update())
    seq = (
        int(await db.scalar(select(func.coalesce(func.max(JobEvent.seq), 0)).where(JobEvent.job_id == job_id)) or 0) + 1
    )
    db.add(JobEvent(job_id=job_id, seq=seq, type=JobEventType(type_), data=data))
    await db.flush()
    return seq


async def record_event(job_id: uuid.UUID, type_: JobEventType | str, data: dict[str, Any]) -> int:
    """`add_event` in its own short transaction (agent text, tool use, check results)."""
    async with get_sessionmaker()() as db:
        seq = await add_event(db, job_id, type_, data)
        await db.commit()
        return seq


async def transition_in(
    db: AsyncSession,
    job_id: uuid.UUID,
    expected: JobStatus | Iterable[JobStatus],
    new: JobStatus,
    *,
    actor_id: int | None = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
    **fields: Any,
) -> Job | None:
    """The transition inside the caller's transaction (the caller commits). None if the job wasn't in `expected`."""
    wanted = {JobStatus(expected)} if isinstance(expected, str) else {JobStatus(e) for e in expected}
    bad = [e for e in wanted if not allowed(e, new)]
    if bad:
        raise InvalidTransition(f"{', '.join(sorted(bad))} → {new} is not a job transition")
    job = await db.scalar(
        select(Job).where(Job.id == job_id).with_for_update().execution_options(populate_existing=True)
    )
    if job is None or job.status not in wanted:
        return None
    previous = job.status
    now = datetime.now(UTC)
    values: dict[str, Any] = {"status": new, "updated_at": now, **fields}
    if new == S.preparing and job.started_at is None:
        values["started_at"] = now
    if new in TERMINAL:
        values["finished_at"] = now
    await db.execute(update(Job).where(Job.id == job_id).values(**values))
    await db.refresh(job)
    data = {"from": previous, "to": str(new), **(detail or {})}
    await add_event(db, job_id, JobEventType.status, data)
    if (state := WORK_ITEM_STATE.get(new)) is not None:
        await db.execute(update(WorkItem).where(WorkItem.id == job.work_item_id).values(state=state, updated_at=now))
    await audit(
        db, f"job.{new}", user_id=actor_id, target_type="job",
        target_id=str(job_id), detail=data, ip=ip,
    )  # fmt: skip
    return job


async def transition(
    job_id: uuid.UUID, expected: JobStatus | Iterable[JobStatus], new: JobStatus, **kw: Any
) -> Job | None:
    """`transition_in` in its own short transaction."""
    async with get_sessionmaker()() as db:
        job = await transition_in(db, job_id, expected, new, **kw)
        if job is None:
            await db.rollback()
            return None
        await db.commit()
        return job

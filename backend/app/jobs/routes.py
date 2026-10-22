"""Jobs API (design 04 §8): list, detail, live events. Owner only (others' jobs are 404). Creating, cancelling
and the review actions arrive with the pipeline (X4–X6). `/internal/jobs/active` is for the runner only."""

import asyncio
import json
import secrets
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Header, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from app import pagination
from app.config import get_settings
from app.db import get_sessionmaker
from app.deps import DB, CurrentAuth
from app.errors import AppError
from app.jobs.state import ACTIVE, WORKING
from app.models import JiraIssue, Job, JobEvent, User, WorkItem

router = APIRouter(tags=["jobs"])

JobStatusOut = Literal[
    "created", "preparing", "running", "verifying", "awaiting_review", "approved_push", "pushing", "pushed",
    "merged", "closed", "failed", "timed_out", "budget_exceeded", "cancelled", "discarded",
]  # fmt: skip
EVENT_POLL_S = 1.0
HEARTBEAT_S = 15.0


class JobOut(BaseModel):
    id: uuid.UUID
    work_item_id: uuid.UUID
    key: str
    summary: str
    status: JobStatusOut
    failed_stage: str | None
    attempt: int
    review_round: int
    max_attempts: int
    branch_name: str
    base_branch: str
    base_sha: str
    cost_usd: float
    error_code: str | None
    error: str | None
    result: dict | None
    push: dict | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime


class JobPage(BaseModel):
    items: list[JobOut]
    next_cursor: str | None


def job_out(job: Job, key: str, summary: str) -> JobOut:
    push = dict(job.push or {})
    push.pop("payload", None)  # the approved payload is shown by the push preview, not here
    return JobOut(
        id=job.id, work_item_id=job.work_item_id, key=key, summary=summary, status=job.status,
        failed_stage=job.failed_stage, attempt=job.attempt, review_round=job.review_round,
        max_attempts=int((job.policy or {}).get("max_attempts", 3)), branch_name=job.branch_name,
        base_branch=job.base_branch, base_sha=job.base_sha, cost_usd=float(job.cost_usd or 0),
        error_code=job.error_code, error=job.error, result=job.result, push=push or None,
        created_at=job.created_at, started_at=job.started_at, finished_at=job.finished_at, updated_at=job.updated_at,
    )  # fmt: skip


def _query(user: User):
    return (
        select(Job, JiraIssue.key, JiraIssue.summary)
        .join(WorkItem, WorkItem.id == Job.work_item_id)
        .join(JiraIssue, JiraIssue.id == WorkItem.jira_issue_id)
        .where(Job.owner_id == user.id)
    )


async def owned_job(db: DB, job_id: uuid.UUID, user: User) -> tuple[Job, str, str]:
    row = (await db.execute(_query(user).where(Job.id == job_id))).first()
    if row is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "Job not found")
    return row[0], row[1], row[2]


@router.get("/api/jobs", response_model=JobPage)
async def list_jobs(
    auth: CurrentAuth,
    db: DB,
    status_: list[JobStatusOut] | None = Query(None, alias="status"),
    work_item_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
) -> JobPage:
    q = _query(auth.user)
    if status_:
        q = q.where(Job.status.in_(status_))
    if work_item_id:
        q = q.where(Job.work_item_id == work_item_id)
    rows = list((await db.execute(pagination.page(q, Job.created_at, Job.id, cursor, limit))).all())
    jobs, next_cursor = pagination.result([r[0] for r in rows], limit)
    keep = {j.id for j in jobs}
    return JobPage(items=[job_out(*r) for r in rows if r[0].id in keep], next_cursor=next_cursor)


@router.get("/api/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: uuid.UUID, auth: CurrentAuth, db: DB) -> JobOut:
    return job_out(*await owned_job(db, job_id, auth.user))


def _sse(seq: int | None, event: str, data: dict) -> str:
    head = f"id: {seq}\n" if seq is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def _events_after(job_id: uuid.UUID, after: int) -> tuple[list[JobEvent], str | None]:
    """The status first, then the events: a status change commits with its event, so every event up to the status
    read here is included (reading events first could miss the one that ends the stream)."""
    async with get_sessionmaker()() as db:
        current = await db.scalar(select(Job.status).where(Job.id == job_id))
        events = list(
            await db.scalars(
                select(JobEvent)
                .where(JobEvent.job_id == job_id, JobEvent.seq > after)
                .order_by(JobEvent.seq)
                .limit(500)
            )
        )
        await db.commit()
        return events, current


@router.get("/api/jobs/{job_id}/events")
async def job_events(
    job_id: uuid.UUID,
    auth: CurrentAuth,
    db: DB,
    request: Request,
    after: int = Query(0, ge=0),
    last_event_id: str | None = Header(None),
) -> StreamingResponse:
    """SSE: every event after `after` (or Last-Event-ID on reconnect), then new ones as they happen, until the job
    stops working on its own (awaiting review, pushed, or finished); then `event: end`."""
    await owned_job(db, job_id, auth.user)
    await db.commit()  # rule R1: the stream outlives this transaction
    if last_event_id and last_event_id.isdigit():
        after = max(after, int(last_event_id))

    async def stream():
        seen = after
        idle = 0.0
        while True:
            events, current = await _events_after(job_id, seen)
            for e in events:
                seen = e.seq
                yield _sse(e.seq, e.type, {"seq": e.seq, "at": e.at.isoformat(), **e.data})
            if current is None or (current not in WORKING and len(events) < 500):
                yield _sse(None, "end", {"status": current})
                return
            if await request.is_disconnected():
                return
            if events:
                idle = 0.0
            else:
                idle += EVENT_POLL_S
                if idle >= HEARTBEAT_S:
                    idle = 0.0
                    yield ": keep-alive\n\n"
            await asyncio.sleep(EVENT_POLL_S)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


# ---------- internal (runner) ----------


@router.get("/internal/jobs/active", include_in_schema=False)
async def active_jobs(db: DB, authorization: str | None = Header(None)) -> dict[str, list[str]]:
    """The jobs whose sandboxes must be kept (04 §4 orphan sweep). Bearer MWB_RUNNER_TOKEN; Caddy doesn't route it."""
    token = get_settings().runner_token.get_secret_value()
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not token or not secrets.compare_digest(given.encode(), token.encode()):
        raise AppError(status.HTTP_401_UNAUTHORIZED, "not_signed_in", "Runner token required")
    ids = await db.scalars(select(Job.id).where(Job.status.in_([str(s) for s in ACTIVE])))
    return {"jobs": [str(i) for i in ids]}

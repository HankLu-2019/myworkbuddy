"""Jobs API (design 04 §8): list, detail, live events. Owner only (others' jobs are 404). Creating, cancelling,
the review actions and the push gate (X4–X6). `/internal/jobs/active` is for the runner only."""

import asyncio
import json
import secrets
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Header, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app import pagination
from app.config import get_settings
from app.connectors.base import Connection, ConnectorError
from app.db import get_sessionmaker
from app.deps import DB, CurrentAuth
from app.enums import JobStatus as S
from app.errors import AppError
from app.jobs.push import PushPayload
from app.jobs.state import ACTIVE, WORKING, transition_in
from app.models import Artifact, JiraIssue, Job, JobEvent, User, WorkItem

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


class JobIn(BaseModel):
    plan_revision_id: uuid.UUID


@router.post("/api/work-items/{work_item_id}/jobs", response_model=JobOut, status_code=201)
async def start_job(work_item_id: uuid.UUID, body: JobIn, auth: CurrentAuth, db: DB, request: Request) -> JobOut:
    """Start a job from the ticket's approved plan revision (04 §5.1). The pipeline runs in the background."""
    from app.deps import client_ip
    from app.jobs.create import create_job

    await db.commit()  # create_job uses its own short transactions around network calls (R1)
    job_id = await create_job(auth.user, work_item_id, body.plan_revision_id, client_ip(request))
    return job_out(*await owned_job(db, job_id, auth.user))


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


# ---------- actions (04 §5.5, §8) ----------


class ChangesIn(BaseModel):
    feedback: str = Field(min_length=1, max_length=4000)


MAX_REVIEW_ROUNDS = 5


async def _job_or_404(db: DB, job_id: uuid.UUID, user: User) -> Job:
    return (await owned_job(db, job_id, user))[0]


@router.post("/api/jobs/{job_id}/cancel", response_model=JobOut)
async def cancel_job(job_id: uuid.UUID, auth: CurrentAuth, db: DB, request: Request) -> JobOut:
    """Stop a job that's still working: kill its run, remove its workspace and key."""
    from app.deps import client_ip
    from app.jobs.pipeline import cleanup
    from app.jobs.runner_client import kill_run

    job = await _job_or_404(db, job_id, auth.user)
    run_id = job.current_run
    moved = await transition_in(
        db, job.id, [S.created, S.preparing, S.running, S.verifying], S.cancelled, actor_id=auth.user.id,
        ip=client_ip(request), error_code="cancelled", error="Cancelled by its owner.", current_run=None,
    )  # fmt: skip
    if moved is None:
        raise AppError(
            status.HTTP_409_CONFLICT, "conflict", f"A job that is {job.status.replace('_', ' ')} can't be cancelled."
        )
    await db.commit()
    if run_id:
        try:
            await kill_run(run_id, remove=True)  # no transaction open (R1)
        except Exception:  # noqa: BLE001 - the runner's sweep removes it anyway
            pass
    await cleanup(job.id)
    return job_out(*await owned_job(db, job_id, auth.user))


@router.post("/api/jobs/{job_id}/request-changes", response_model=JobOut)
async def request_changes(job_id: uuid.UUID, body: ChangesIn, auth: CurrentAuth, db: DB, request: Request) -> JobOut:
    """Another round: the feedback goes to the agent as .mwb/feedback.md (04 §5.5)."""
    from app.deps import client_ip
    from app.jobs import runner_client
    from app.jobs.pipeline import defer_advance, overlay
    from app.jobs.task_files import prompt_md, review_feedback_md

    job = await _job_or_404(db, job_id, auth.user)
    if job.status == S.pushed:
        return await _changes_after_push(db, job, body, auth.user, client_ip(request))
    if job.status != S.awaiting_review:
        raise AppError(status.HTTP_409_CONFLICT, "conflict", "Changes can be requested while the job waits for review.")
    if job.review_round >= MAX_REVIEW_ROUNDS:
        raise AppError(
            status.HTTP_409_CONFLICT,
            "conflict",
            f"This job has had {MAX_REVIEW_ROUNDS} review rounds; start a new job.",
        )
    await db.commit()
    files = {
        ".mwb/feedback.md": review_feedback_md(body.feedback),
        ".mwb/prompt.md": prompt_md(retry=False, review_feedback=True),
    }
    try:
        await runner_client.put_content(job.id, overlay(files))  # network: nothing open (R1)
    except runner_client.RunnerError as exc:
        raise AppError(
            status.HTTP_502_BAD_GATEWAY, "internal", f"The sandbox couldn't take the feedback: {exc.message}"
        ) from None
    moved = await transition_in(
        db, job.id, S.awaiting_review, S.running, actor_id=auth.user.id, ip=client_ip(request),
        review_round=job.review_round + 1, attempt=1, result=None, current_run=None,
        detail={"note": f"Changes requested (round {job.review_round + 1}).", "feedback_chars": len(body.feedback)},
    )  # fmt: skip
    if moved is None:
        raise AppError(status.HTTP_409_CONFLICT, "plan_changed", "The job changed meanwhile. Reload.")
    await db.commit()
    await defer_advance(job.id)
    return job_out(*await owned_job(db, job_id, auth.user))


async def _changes_after_push(db: DB, job: Job, body: "ChangesIn", user: User, ip: str | None) -> JobOut:
    note = f"Changes requested after the push (round {job.review_round + 1})."
    return await _round_after_push(db, job, body.feedback, user, ip, kind="review", note=note)


async def _round_after_push(
    db: DB, job: Job, feedback: str, user: User, ip: str | None, *, kind: str, note: str
) -> JobOut:
    """A new round after a push (04 §6): the workspace and key are gone, so the job prepares again from the pushed
    patch plus the feedback, with a new LLM key; the next approved push updates the same branch or change.
    `kind` is "review" (the user's words, under a reviewer header) or "ci" (feedback.md as the server built it)."""
    from app.jobs import keys
    from app.jobs.pipeline import defer_advance
    from app.llm.keys import GatewayAdminError

    if job.review_round >= MAX_REVIEW_ROUNDS:
        raise AppError(
            status.HTTP_409_CONFLICT,
            "conflict",
            f"This job has had {MAX_REVIEW_ROUNDS} review rounds; start a new job.",
        )
    patch_id = (job.result or {}).get("patch_artifact_id")
    if not patch_id:
        raise AppError(status.HTTP_409_CONFLICT, "conflict", "This job has no pushed changes to build on.")
    policy, job_id, round_ = job.policy, job.id, job.review_round
    await db.commit()
    try:
        await keys.create(job_id, user.id, policy)  # network: nothing open (R1)
    except (GatewayAdminError, ConnectorError):
        raise AppError(
            status.HTTP_502_BAD_GATEWAY, "llm_unavailable", "The LLM gateway couldn't create a key for this job."
        ) from None
    push = {**(job.push or {}), "resume": {"patch_artifact_id": patch_id, "feedback": feedback, "kind": kind}}
    moved = await transition_in(
        db, job_id, S.pushed, S.preparing, actor_id=user.id, ip=ip, review_round=round_ + 1, attempt=1,
        result=None, current_run=None, error=None, error_code=None, push=push,
        detail={"note": note, "feedback_chars": len(feedback)},
    )  # fmt: skip
    if moved is None:
        raise AppError(status.HTTP_409_CONFLICT, "plan_changed", "The job changed meanwhile. Reload.")
    await db.commit()
    await defer_advance(job_id)
    return job_out(*await owned_job(db, job_id, user))


class FixCiIn(BaseModel):
    note: str | None = Field(None, max_length=4000)


@router.post("/api/jobs/{job_id}/fix-ci", response_model=JobOut)
async def fix_ci(job_id: uuid.UUID, body: FixCiIn, auth: CurrentAuth, db: DB, request: Request) -> JobOut:
    """A new round whose feedback is the failing pipeline's logs, fetched here, never sent by the client (08 §F1)."""
    from app.deps import client_ip
    from app.integrations.service import get_credentials
    from app.jobs import ci
    from app.knowledge.ingest.repos import project_integration
    from app.models import Integration, Project

    job = await _job_or_404(db, job_id, auth.user)
    pipeline = (job.push or {}).get("pipeline") or {}
    if (
        job.status != S.pushed
        or pipeline.get("status") != "failed"
        or pipeline.get("sha") != job.push.get("pushed_sha")
    ):
        raise AppError(status.HTTP_409_CONFLICT, "conflict", "There's no failed pipeline for the pushed changes.")
    if job.review_round >= MAX_REVIEW_ROUNDS:
        raise AppError(
            status.HTTP_409_CONFLICT,
            "conflict",
            f"This job has had {MAX_REVIEW_ROUNDS} review rounds; start a new job.",
        )
    project = await db.get(Project, job.project_id) if job.project_id else None
    integration = await project_integration(db, project) if project else None
    if project is None or integration is None:
        raise AppError(status.HTTP_409_CONFLICT, "conflict", "The ticket's repository is no longer available.")
    conn = Connection(auth.user.id, integration, await get_credentials(db, auth.user.id, integration))
    jenkins_conns = []
    for j in await db.scalars(select(Integration).where(Integration.kind == "jenkins", Integration.enabled)):
        try:
            creds = await get_credentials(db, auth.user.id, j)
        except AppError:
            continue
        if creds:
            jenkins_conns.append(Connection(auth.user.id, j, creds))
    await db.commit()  # rule R1: the logs are fetched with nothing open
    try:
        logs = await ci.failure_logs(job, pipeline, conn, project, jenkins_conns)
    finally:
        for c in (conn, *jenkins_conns):
            await c.close()
    feedback = ci.failure_feedback(pipeline, logs, body.note)
    job = await _job_or_404(db, job_id, auth.user)
    return await _round_after_push(db, job, feedback, auth.user, client_ip(request), kind="ci",
                                   note=f"Fixing the failed pipeline (round {job.review_round + 1}).")  # fmt: skip


@router.get("/api/jobs/{job_id}/push-preview", response_model=PushPayload)
async def push_preview(job_id: uuid.UUID, auth: CurrentAuth, db: DB) -> dict:
    """The exact outbound content (04 §5.6); the texts are editable before approval."""
    from app.jobs import push

    return await push.preview(db, await _job_or_404(db, job_id, auth.user))


@router.post("/api/jobs/{job_id}/approve-push", response_model=JobOut)
async def approve_push(job_id: uuid.UUID, body: PushPayload, auth: CurrentAuth, db: DB, request: Request) -> JobOut:
    """Bind the (possibly edited) payload to an approval and queue the push (rule R5)."""
    from app.deps import client_ip
    from app.jobs import push
    from app.jobs.pipeline import defer_advance

    job = await _job_or_404(db, job_id, auth.user)
    await push.approve(db, job, body, auth.user, client_ip(request))
    await db.commit()
    await defer_advance(job.id)
    return job_out(*await owned_job(db, job_id, auth.user))


@router.post("/api/jobs/{job_id}/retry-push", response_model=JobOut)
async def retry_push(job_id: uuid.UUID, auth: CurrentAuth, db: DB, request: Request) -> JobOut:
    """Push again after a failure, with the approval already given (08 §F5)."""
    from app.deps import client_ip
    from app.jobs import push
    from app.jobs.pipeline import defer_advance

    job = await _job_or_404(db, job_id, auth.user)
    await push.retry(db, job, auth.user, client_ip(request))
    await db.commit()
    await defer_advance(job.id)
    return job_out(*await owned_job(db, job_id, auth.user))


@router.post("/api/jobs/{job_id}/discard", response_model=JobOut)
async def discard_job(job_id: uuid.UUID, auth: CurrentAuth, db: DB, request: Request) -> JobOut:
    """Throw the result away: workspace and key go, the ticket is back to approved."""
    from app.deps import client_ip
    from app.jobs.pipeline import cleanup

    job = await _job_or_404(db, job_id, auth.user)
    moved = await transition_in(
        db, job.id, S.awaiting_review, S.discarded, actor_id=auth.user.id, ip=client_ip(request)
    )
    if moved is None:
        raise AppError(status.HTTP_409_CONFLICT, "conflict", "Only a job waiting for review can be discarded.")
    await db.commit()
    await cleanup(job.id)
    return job_out(*await owned_job(db, job_id, auth.user))


def _artifact_response(art: Artifact, filename: str | None = None) -> Response:
    from app.jobs.artifacts import path_of

    path = path_of(art)
    if not path.is_file():
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "That file is no longer kept.")
    name = filename or art.key.rsplit("/", 1)[-1]
    headers = {"Content-Disposition": f'inline; filename="{name}"', "X-Content-Type-Options": "nosniff"}
    return Response(path.read_bytes(), media_type=art.mime, headers=headers)


@router.get("/api/jobs/{job_id}/patch")
async def job_patch(job_id: uuid.UUID, auth: CurrentAuth, db: DB) -> Response:
    job = await _job_or_404(db, job_id, auth.user)
    art_id = (job.result or {}).get("patch_artifact_id")
    art = await db.get(Artifact, uuid.UUID(art_id)) if art_id else None
    if art is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "This job has no patch yet.")
    return _artifact_response(art, f"{job.branch_name.replace('/', '-')}.diff")


@router.get("/api/artifacts/{artifact_id}")
async def get_artifact(artifact_id: uuid.UUID, auth: CurrentAuth, db: DB) -> Response:
    art = await db.get(Artifact, artifact_id)
    if art is None or art.owner_id != auth.user.id:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "File not found")
    return _artifact_response(art)


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

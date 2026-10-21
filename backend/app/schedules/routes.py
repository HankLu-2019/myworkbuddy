"""Schedules and runs API (design 02 §7). Owner only: other users' schedules and runs are 404."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app import pagination
from app.audit.service import audit
from app.deps import DB, CurrentAuth, client_ip
from app.errors import AppError
from app.models import Agent, Run, Schedule, User
from app.schedules import cron, proposals
from app.schedules.service import (
    ScheduleFields,
    create_manual_run,
    create_test_run,
    min_interval,
    now,
    validate_schedule,
)

router = APIRouter(prefix="/api", tags=["schedules"])

NotifyModeT = Literal["always", "on_change", "on_alert"]
RunStatusT = Literal["queued", "running", "succeeded", "failed", "timed_out", "skipped", "cancelled"]


class ScheduleIn(BaseModel):
    name: str = Field(max_length=200)
    agent: str = Field(max_length=64, description="Agent slug")
    cron: str = Field(max_length=200)
    timezone: str | None = Field(default=None, max_length=64, description="IANA name; default: yours")
    prompt: str = Field(default="", max_length=4000)
    inputs: dict[str, Any] = {}
    enabled: bool = True
    notify_mode: NotifyModeT = "on_change"
    notify_email: bool = True
    max_runtime_s: int = Field(default=300, ge=30, le=1800)


class ScheduleUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    agent: str | None = Field(default=None, max_length=64)
    cron: str | None = Field(default=None, max_length=200)
    timezone: str | None = Field(default=None, max_length=64)
    prompt: str | None = Field(default=None, max_length=4000)
    inputs: dict[str, Any] | None = None
    enabled: bool | None = None
    notify_mode: NotifyModeT | None = None
    notify_email: bool | None = None
    max_runtime_s: int | None = Field(default=None, ge=30, le=1800)


class AgentRef(BaseModel):
    slug: str
    name: str


class ScheduleOut(BaseModel):
    id: uuid.UUID
    name: str
    agent: AgentRef
    prompt: str
    inputs: dict[str, Any]
    cron: str
    description: str
    timezone: str
    enabled: bool
    notify_mode: NotifyModeT
    notify_email: bool
    max_runtime_s: int
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_status: str | None
    consecutive_failures: int
    paused_reason: str | None
    created_at: datetime
    updated_at: datetime


class SchedulePage(BaseModel):
    items: list[ScheduleOut]
    next_cursor: str | None


class PreviewIn(BaseModel):
    cron: str = Field(max_length=200)
    timezone: str | None = Field(default=None, max_length=64)


class PreviewOut(BaseModel):
    valid: bool
    error: str | None
    description: str | None
    next: list[str]


class RunOut(BaseModel):
    id: uuid.UUID
    schedule_id: uuid.UUID | None
    schedule_name: str
    agent: str
    trigger: Literal["schedule", "manual"]
    status: RunStatusT
    scheduled_for: datetime
    started_at: datetime | None
    finished_at: datetime | None
    changed: bool | None
    notified: bool
    headline: str | None
    report_status: Literal["ok", "warning", "alert"] | None
    error_code: str | None
    error: str | None
    cost_usd: Decimal | None
    created_at: datetime


class RunDetail(RunOut):
    report: dict[str, Any] | None
    fingerprints: list[str]
    tokens_in: int | None
    tokens_out: int | None
    session_id: uuid.UUID | None
    prompt: str
    inputs: dict[str, Any]


class RunPage(BaseModel):
    items: list[RunOut]
    next_cursor: str | None


class RunStarted(BaseModel):
    run_id: uuid.UUID


def _not_found(what: str = "Schedule") -> AppError:
    return AppError(status.HTTP_404_NOT_FOUND, "not_found", f"{what} not found")


async def _owned(db: DB, schedule_id: uuid.UUID, user: User, *, lock: bool = False) -> Schedule:
    q = select(Schedule).where(Schedule.id == schedule_id, Schedule.owner_id == user.id)
    s = await db.scalar(q.with_for_update() if lock else q)
    if s is None:
        raise _not_found()
    return s


def _out(s: Schedule, agent: Agent) -> ScheduleOut:
    return ScheduleOut(
        id=s.id, name=s.name, agent=AgentRef(slug=agent.slug, name=agent.name), prompt=s.prompt, inputs=s.inputs,
        cron=s.cron, description=cron.describe(s.cron), timezone=s.timezone, enabled=s.enabled,
        notify_mode=s.notify_mode, notify_email=s.notify_email, max_runtime_s=s.max_runtime_s,
        next_run_at=s.next_run_at, last_run_at=s.last_run_at, last_status=s.last_status,
        consecutive_failures=s.consecutive_failures, paused_reason=s.paused_reason, created_at=s.created_at,
        updated_at=s.updated_at,
    )  # fmt: skip


async def _schedule_out(db: DB, s: Schedule) -> ScheduleOut:
    await db.refresh(s)
    agent = await db.get(Agent, s.agent_id)
    assert agent is not None
    return _out(s, agent)


def _run_fields(r: Run) -> dict[str, Any]:
    report = r.report or {}
    return {
        "id": r.id, "schedule_id": r.schedule_id, "schedule_name": r.config.get("name", ""),
        "agent": r.config.get("agent_slug", ""), "trigger": r.trigger, "status": r.status,
        "scheduled_for": r.scheduled_for, "started_at": r.started_at, "finished_at": r.finished_at,
        "changed": r.changed, "notified": r.notified, "headline": report.get("headline"),
        "report_status": report.get("status"), "error_code": r.error_code, "error": r.error,
        "cost_usd": r.cost_usd, "created_at": r.created_at,
    }  # fmt: skip


# ---------- schedules ----------


@router.get("/schedules", response_model=SchedulePage)
async def list_schedules(
    auth: CurrentAuth, db: DB, cursor: str | None = None, limit: int = Query(50, ge=1, le=100)
) -> SchedulePage:
    q = select(Schedule, Agent).join(Agent, Agent.id == Schedule.agent_id).where(Schedule.owner_id == auth.user.id)
    rows = (await db.execute(pagination.page(q, Schedule.created_at, Schedule.id, cursor, limit))).all()
    items, next_cursor = pagination.result([s for s, _ in rows], limit)
    agents = {s.id: a for s, a in rows}
    return SchedulePage(items=[_out(s, agents[s.id]) for s in items], next_cursor=next_cursor)


@router.post("/schedules", response_model=ScheduleOut, status_code=201)
async def create_schedule(body: ScheduleIn, auth: CurrentAuth, db: DB, request: Request) -> ScheduleOut:
    values = await validate_schedule(db, auth.user, ScheduleFields(**body.model_dump()))
    s = Schedule(owner_id=auth.user.id, **values)
    db.add(s)
    await db.flush()
    await audit(
        db, "schedule.create", user_id=auth.user.id, target_type="schedule", target_id=str(s.id),
        detail={"agent": body.agent, "cron": values["cron"]}, ip=client_ip(request),
    )  # fmt: skip
    await db.commit()
    return await _schedule_out(db, s)


@router.post("/schedules/preview", response_model=PreviewOut)
async def preview_cron(body: PreviewIn, auth: CurrentAuth) -> PreviewOut:
    p = cron.preview(
        body.cron, body.timezone or auth.user.timezone, min_interval_min=min_interval(auth.user), now=now()
    )
    return PreviewOut(valid=p.valid, error=p.error, description=p.description, next=p.next)


class ProposalOut(BaseModel):
    proposal_id: uuid.UUID
    status: Literal["pending", "accepted", "dismissed", "expired"]
    name: str
    agent: str
    cron: str
    timezone: str
    prompt: str
    inputs: dict[str, Any]
    notify_mode: NotifyModeT
    description: str
    next_runs: list[str]
    expires_at: datetime
    session_id: uuid.UUID


class AcceptIn(BaseModel):
    """Empty for Create; the edited form for Edit → Save (02 §6)."""

    name: str | None = Field(default=None, max_length=200)
    cron: str | None = Field(default=None, max_length=200)
    timezone: str | None = Field(default=None, max_length=64)
    prompt: str | None = Field(default=None, max_length=4000)
    inputs: dict[str, Any] | None = None
    enabled: bool | None = None
    notify_mode: NotifyModeT | None = None
    notify_email: bool | None = None
    max_runtime_s: int | None = Field(default=None, ge=30, le=1800)


# ---------- proposals (before /schedules/{id} so the paths don't collide) ----------


async def _proposal_out(db: DB, p: Any) -> ProposalOut:
    payload = p.payload
    tz = cron.zone(payload["timezone"])
    return ProposalOut(
        proposal_id=p.id, status=p.status, name=payload["name"], agent=payload["agent"], cron=payload["cron"],
        timezone=payload["timezone"], prompt=payload.get("prompt", ""), inputs=payload.get("inputs", {}),
        notify_mode=payload.get("notify_mode", "on_change"), description=cron.describe(payload["cron"]),
        next_runs=[t.astimezone(tz).isoformat() for t in cron.occurrences(payload["cron"], tz, now(), 3)],
        expires_at=p.expires_at, session_id=p.session_id,
    )  # fmt: skip


@router.get("/schedules/proposals/{proposal_id}", response_model=ProposalOut)
async def get_proposal(proposal_id: uuid.UUID, auth: CurrentAuth, db: DB) -> ProposalOut:
    return await _proposal_out(db, await proposals.owned_pending(db, proposal_id, auth.user))


@router.post("/schedules/proposals/{proposal_id}/accept", response_model=ScheduleOut, status_code=201)
async def accept_proposal(
    proposal_id: uuid.UUID, auth: CurrentAuth, db: DB, request: Request, body: AcceptIn | None = None
) -> ScheduleOut:
    p = await proposals.owned_pending(db, proposal_id, auth.user, lock=True)
    overrides = body.model_dump(exclude_unset=True, exclude_none=True) if body else {}
    schedule = await proposals.accept(db, p, auth.user, overrides)
    await audit(
        db, "schedule.create", user_id=auth.user.id, target_type="schedule", target_id=str(schedule.id),
        detail={"proposal_id": str(p.id), "edited": sorted(overrides)}, ip=client_ip(request),
    )  # fmt: skip
    await db.commit()
    return await _schedule_out(db, schedule)


@router.post("/schedules/proposals/{proposal_id}/dismiss", status_code=204)
async def dismiss_proposal(proposal_id: uuid.UUID, auth: CurrentAuth, db: DB, request: Request) -> None:
    p = await proposals.owned_pending(db, proposal_id, auth.user, lock=True)
    p.status = "dismissed"
    await audit(
        db, "schedule_proposal.dismiss", user_id=auth.user.id, target_type="schedule_proposal",
        target_id=str(p.id), ip=client_ip(request),
    )  # fmt: skip
    await db.commit()


@router.post("/schedules/test", response_model=RunStarted, status_code=202)
async def test_run(body: ScheduleIn, auth: CurrentAuth, db: DB, request: Request) -> RunStarted:
    """Run an unsaved schedule once ("Run once now to test", 02 §6). Poll GET /api/runs/{id}."""
    from app.tasks.app import defer_run

    run = await create_test_run(db, auth.user, ScheduleFields(**body.model_dump()))
    await audit(
        db, "schedule.test", user_id=auth.user.id, target_type="run", target_id=str(run.id),
        detail={"agent": body.agent}, ip=client_ip(request),
    )  # fmt: skip
    await db.commit()
    await defer_run(run.id, None)
    return RunStarted(run_id=run.id)


@router.get("/schedules/{schedule_id}", response_model=ScheduleOut)
async def get_schedule(schedule_id: uuid.UUID, auth: CurrentAuth, db: DB) -> ScheduleOut:
    return await _schedule_out(db, await _owned(db, schedule_id, auth.user))


@router.patch("/schedules/{schedule_id}", response_model=ScheduleOut)
async def update_schedule(
    schedule_id: uuid.UUID, body: ScheduleUpdate, auth: CurrentAuth, db: DB, request: Request
) -> ScheduleOut:
    s = await _owned(db, schedule_id, auth.user, lock=True)
    agent = await db.get(Agent, s.agent_id)
    assert agent is not None
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    current = ScheduleFields(
        name=s.name, agent=agent.slug, cron=s.cron, timezone=s.timezone, prompt=s.prompt, inputs=s.inputs,
        enabled=s.enabled, notify_mode=s.notify_mode, notify_email=s.notify_email, max_runtime_s=s.max_runtime_s,
    )  # fmt: skip
    for key, value in changes.items():
        setattr(current, key, value)
    values = await validate_schedule(db, auth.user, current, exclude=s.id)
    timing = {"cron", "timezone", "enabled"}
    if s.enabled and values["enabled"] and not timing & changes.keys():
        values["next_run_at"] = s.next_run_at  # keep the slot; only timing changes move it
    if values["enabled"] and not s.enabled:
        s.consecutive_failures, s.paused_reason = 0, None  # resuming clears the pause
    for key, value in values.items():
        setattr(s, key, value)
    s.updated_at = now()
    await audit(
        db, "schedule.update", user_id=auth.user.id, target_type="schedule", target_id=str(s.id),
        detail={"fields": sorted(changes)}, ip=client_ip(request),
    )  # fmt: skip
    await db.commit()
    return await _schedule_out(db, s)


@router.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(schedule_id: uuid.UUID, auth: CurrentAuth, db: DB, request: Request) -> None:
    s = await _owned(db, schedule_id, auth.user, lock=True)
    await db.delete(s)  # runs keep their history (schedule_id set null); queued ones are skipped
    await audit(
        db, "schedule.delete", user_id=auth.user.id, target_type="schedule", target_id=str(schedule_id),
        ip=client_ip(request),
    )  # fmt: skip
    await db.commit()


@router.post("/schedules/{schedule_id}/run", response_model=RunStarted, status_code=202)
async def run_now(schedule_id: uuid.UUID, auth: CurrentAuth, db: DB, request: Request) -> RunStarted:
    from app.tasks.app import defer_run

    s = await _owned(db, schedule_id, auth.user)
    run = await create_manual_run(db, s)
    await audit(
        db, "schedule.run", user_id=auth.user.id, target_type="schedule", target_id=str(s.id),
        detail={"run_id": str(run.id)}, ip=client_ip(request),
    )  # fmt: skip
    await db.commit()
    await defer_run(run.id, s.id)  # if this fails, the tick re-defers the queued run
    return RunStarted(run_id=run.id)


@router.get("/schedules/{schedule_id}/runs", response_model=RunPage)
async def list_runs(
    schedule_id: uuid.UUID, auth: CurrentAuth, db: DB, cursor: str | None = None, limit: int = Query(50, ge=1, le=100)
) -> RunPage:
    await _owned(db, schedule_id, auth.user)
    q = select(Run).where(Run.schedule_id == schedule_id, Run.owner_id == auth.user.id)
    rows = list(await db.scalars(pagination.page(q, Run.created_at, Run.id, cursor, limit)))
    items, next_cursor = pagination.result(rows, limit)
    return RunPage(items=[RunOut(**_run_fields(r)) for r in items], next_cursor=next_cursor)


# ---------- runs ----------


@router.get("/runs/{run_id}", response_model=RunDetail)
async def get_run(run_id: uuid.UUID, auth: CurrentAuth, db: DB) -> RunDetail:
    r = await db.scalar(select(Run).where(Run.id == run_id, Run.owner_id == auth.user.id))
    if r is None:
        raise _not_found("Run")
    return RunDetail(
        **_run_fields(r), report=r.report, fingerprints=list(r.fingerprints), tokens_in=r.tokens_in,
        tokens_out=r.tokens_out, session_id=r.session_id, prompt=r.config.get("prompt", ""),
        inputs=r.config.get("inputs") or {},
    )  # fmt: skip

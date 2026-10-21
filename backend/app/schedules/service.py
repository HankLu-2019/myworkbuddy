"""Schedules and runs (design 02 §4).

- `validate_schedule()`: the one validation for create, update and (S6) chat proposals and test runs.
- `tick()`: due schedules → one `queued` run per fire time (FOR UPDATE SKIP LOCKED + a unique key), at most
  one catch-up run after downtime; re-defers runs stuck in `queued`; fails runs stuck in `running`.
- `execute_run()`: the `run_schedule` task body: short transaction to start, `execute_agent_run()` with no
  transaction open (rule R1), short transaction to store the result, decide on notifications and update the
  schedule, re-checking what it read.
"""

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import status
from jsonschema import Draft202012Validator
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.executor import RunOutcome, execute_agent_run
from app.config import get_settings
from app.db import get_sessionmaker
from app.enums import NotifyMode, RunStatus, RunTrigger
from app.errors import AppError
from app.inbox.email import app_url, queue_email
from app.inbox.service import add_item
from app.logs import log
from app.models import Agent, ChatSession, Run, Schedule, User
from app.schedules import cron

MAX_INPUT_BYTES = 16_000
TICK_BATCH = 100
REQUEUE_AFTER = timedelta(minutes=5)
STUCK_GRACE = timedelta(minutes=10)  # beyond max_runtime_s, a `running` run whose worker died is failed
PAUSE_AFTER_FAILURES = 3


def now() -> datetime:  # patched in tests
    return datetime.now(UTC)


def _invalid(message: str, field: str) -> AppError:
    return AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error", message, field=field)


# ---------- validation ----------


@dataclass
class ScheduleFields:
    name: str
    agent: str  # slug
    cron: str
    timezone: str | None = None  # default: the user's
    prompt: str = ""
    inputs: dict[str, Any] | None = None
    enabled: bool = True
    notify_mode: str = NotifyMode.on_change
    notify_email: bool = True
    max_runtime_s: int = 300


async def usable_agent(db: AsyncSession, user: User, slug: str) -> Agent:
    agent = await db.scalar(select(Agent).where(Agent.slug == slug, Agent.enabled, Agent.report))
    if agent is None or (agent.owner_id is not None and agent.owner_id != user.id):
        raise _invalid(f"There is no agent called {slug!r}.", "agent")
    return agent


def min_interval(user: User) -> int:
    s = get_settings()
    return s.schedule_min_interval_admin_min if user.is_admin else s.schedule_min_interval_min


async def validate_schedule(
    db: AsyncSession, user: User, f: ScheduleFields, *, exclude: uuid.UUID | None = None
) -> dict[str, Any]:
    """Column values for a schedule, or AppError(validation_error, field=...) / AppError(conflict) on the limit."""
    name = " ".join(f.name.split())
    if not 1 <= len(name) <= 120:
        raise _invalid("Give the schedule a name of 1–120 characters.", "name")
    if len(f.prompt) > 4000:
        raise _invalid("Instructions can be at most 4000 characters.", "prompt")
    if f.notify_mode not in NotifyMode.__members__:
        raise _invalid(f"notify_mode must be one of {', '.join(NotifyMode)}.", "notify_mode")
    if not 30 <= f.max_runtime_s <= 1800:
        raise _invalid("Maximum run time must be between 30 and 1800 seconds.", "max_runtime_s")
    agent = await usable_agent(db, user, f.agent)
    inputs = f.inputs or {}
    if len(json.dumps(inputs)) > MAX_INPUT_BYTES:
        raise _invalid("Inputs are too large.", "inputs")
    errors = sorted(Draft202012Validator(agent.inputs_schema or {}).iter_errors(inputs), key=lambda e: list(e.path))
    if errors:
        where = ".".join(str(p) for p in errors[0].absolute_path) or "inputs"
        raise _invalid(f"{where}: {errors[0].message}", f"inputs.{where}" if where != "inputs" else "inputs")
    tz = f.timezone or user.timezone
    try:
        expr = cron.check(f.cron, tz, min_interval_min=min_interval(user), now=now())
    except cron.CronError as exc:
        raise _invalid(str(exc), "timezone" if "timezone" in str(exc) else "cron") from None
    if f.enabled:
        q = select(func.count()).select_from(Schedule).where(Schedule.owner_id == user.id, Schedule.enabled)
        if exclude is not None:
            q = q.where(Schedule.id != exclude)
        limit = get_settings().max_schedules_per_user
        if (await db.scalar(q) or 0) >= limit:
            raise AppError(
                status.HTTP_409_CONFLICT, "conflict", f"You can have at most {limit} active schedules.", field="enabled"
            )
    return {
        "name": name,
        "agent_id": agent.id,
        "prompt": f.prompt,
        "inputs": inputs,
        "cron": expr,
        "timezone": tz,
        "enabled": f.enabled,
        "notify_mode": f.notify_mode,
        "notify_email": f.notify_email,
        "max_runtime_s": f.max_runtime_s,
        "next_run_at": cron.next_after(expr, tz, now()) if f.enabled else None,
    }


def snapshot(s: Schedule, agent: Agent) -> dict[str, Any]:
    """What a run executes with; editing the schedule later doesn't change a queued run."""
    return {
        "schedule_id": str(s.id),
        "name": s.name,
        "agent_id": agent.id,
        "agent_slug": agent.slug,
        "prompt": s.prompt,
        "inputs": s.inputs,
        "notify_mode": s.notify_mode,
        "notify_email": s.notify_email,
        "max_runtime_s": s.max_runtime_s,
    }


# ---------- the tick ----------


@dataclass
class TickResult:
    created: list[tuple[uuid.UUID, uuid.UUID | None]]  # (run id, schedule id) to defer
    requeue: list[tuple[uuid.UUID, uuid.UUID | None]]
    interrupted: int = 0
    disabled: int = 0


async def tick(at: datetime | None = None) -> TickResult:
    at = at or now()
    result = TickResult(created=[], requeue=[])
    async with get_sessionmaker()() as db:
        rows = (
            await db.execute(
                select(Schedule, User, Agent)
                .join(User, User.id == Schedule.owner_id)
                .join(Agent, Agent.id == Schedule.agent_id)
                .where(Schedule.enabled, Schedule.next_run_at <= at)
                .order_by(Schedule.next_run_at)
                .limit(TICK_BATCH)
                .with_for_update(of=Schedule, skip_locked=True)
            )
        ).all()
        for s, owner, agent in rows:
            if not owner.is_active:
                _pause(s, "The owner's account is deactivated.")
                result.disabled += 1
                continue
            fire = s.next_run_at
            assert fire is not None
            run_id = (
                await db.execute(
                    insert(Run)
                    .values(
                        schedule_id=s.id,
                        owner_id=s.owner_id,
                        agent_id=agent.id,
                        trigger=RunTrigger.schedule,
                        config=snapshot(s, agent),
                        scheduled_for=fire,
                        status=RunStatus.queued,
                    )  # fmt: skip
                    .on_conflict_do_nothing(index_elements=[Run.schedule_id, Run.scheduled_for])
                    .returning(Run.id)
                )
            ).scalar()
            if run_id is not None:
                result.created.append((run_id, s.id))
            try:
                s.next_run_at = cron.next_after(s.cron, s.timezone, max(at, fire))  # at most one catch-up run
            except cron.CronError as exc:
                _pause(s, f"The schedule's timing is no longer valid: {exc}")
                result.disabled += 1
        stale = await db.execute(
            select(Run.id, Run.schedule_id).where(Run.status == RunStatus.queued, Run.created_at < at - REQUEUE_AFTER)
        )
        result.requeue = [(rid, sid) for rid, sid in stale.all()]
        stuck = await db.execute(
            update(Run)
            .where(
                Run.status == RunStatus.running,
                Run.started_at
                < at - STUCK_GRACE - func.make_interval(0, 0, 0, 0, 0, 0, Run.config["max_runtime_s"].as_float()),
            )
            .values(
                status=RunStatus.failed,
                error_code="interrupted",
                error="The worker stopped during the run.",
                finished_at=at,
            )  # fmt: skip
        )
        result.interrupted = stuck.rowcount or 0  # type: ignore[attr-defined]
        await db.commit()
    return result


def _pause(s: Schedule, reason: str) -> None:
    s.enabled, s.next_run_at, s.paused_reason = False, None, reason


# ---------- manual runs ----------


async def create_manual_run(db: AsyncSession, s: Schedule) -> Run:
    agent = await db.get(Agent, s.agent_id)
    assert agent is not None
    run = Run(
        schedule_id=s.id, owner_id=s.owner_id, agent_id=agent.id, trigger=RunTrigger.manual,
        config=snapshot(s, agent), scheduled_for=now(), status=RunStatus.queued,
    )  # fmt: skip
    db.add(run)
    await db.flush()
    return run


# ---------- executing a run ----------


@dataclass
class _Started:
    run_id: uuid.UUID
    agent: Agent
    owner: User
    session_id: uuid.UUID
    config: dict[str, Any]


async def _start(run_id: uuid.UUID) -> _Started | None:
    async with get_sessionmaker()() as db:
        run = await db.scalar(select(Run).where(Run.id == run_id).with_for_update())
        if run is None or run.status != RunStatus.queued:
            return None  # already handled (rule R4)
        owner = await db.get(User, run.owner_id)
        agent = await db.get(Agent, run.agent_id)
        schedule = await db.get(Schedule, run.schedule_id) if run.schedule_id else None
        reason = None
        if owner is None or not owner.is_active:
            reason = "The owner's account is deactivated."
        elif agent is None or not agent.enabled:
            reason = "The agent is no longer available."
        elif run.config.get("schedule_id") and schedule is None:
            reason = "The schedule was deleted."
        elif run.trigger == RunTrigger.schedule and schedule is not None and not schedule.enabled:
            reason = "The schedule is paused."
        if reason:
            run.status, run.error, run.finished_at = RunStatus.skipped, reason, now()
            await db.commit()
            log.info("run.skipped", run_id=str(run_id), reason=reason)
            return None
        assert owner is not None and agent is not None
        try:
            local = run.scheduled_for.astimezone(cron.zone(owner.timezone))
        except cron.CronError:
            local = run.scheduled_for
        session = ChatSession(
            owner_id=owner.id, source="run", title=f"{run.config.get('name', agent.name)} · {local:%Y-%m-%d %H:%M}"
        )
        db.add(session)
        await db.flush()
        run.status, run.started_at, run.session_id = RunStatus.running, now(), session.id
        started = _Started(run.id, agent, owner, session.id, dict(run.config))
        await db.commit()
        db.expunge(agent)
        db.expunge(owner)
        return started


async def execute_run(run_id: uuid.UUID) -> str | None:
    """The `run_schedule` task. Returns the final status, or None when there was nothing to do."""
    started = await _start(run_id)
    if started is None:
        return None
    cfg = started.config
    outcome = await execute_agent_run(
        started.agent,
        started.owner,
        session_id=started.session_id,
        prompt=cfg.get("prompt", ""),
        inputs=cfg.get("inputs") or {},
        max_runtime_s=cfg.get("max_runtime_s", 300),
    )
    return await _finish(run_id, outcome)


def decide_notify(mode: str, status_: str, changed: bool, previous: list[str] | None, current: list[str]) -> str | None:
    """design 02 §4 table. Returns the inbox kind to create (alert, recovered, run) or None."""
    recovered = bool(previous) and not current and status_ == "ok"
    if mode == NotifyMode.always:
        return "recovered" if recovered else ("alert" if status_ != "ok" else "run")
    if mode == NotifyMode.on_change:
        if changed and status_ != "ok":
            return "alert"
        return "recovered" if recovered else None
    if mode == NotifyMode.on_alert:
        return "alert" if status_ == "alert" and changed else None
    return None  # pragma: no cover - CHECK constraint


async def _finish(run_id: uuid.UUID, outcome: RunOutcome) -> str:
    async with get_sessionmaker()() as db:
        run = await db.scalar(select(Run).where(Run.id == run_id).with_for_update())
        assert run is not None
        if run.status != RunStatus.running:  # cancelled or failed by the tick meanwhile: keep that
            return run.status
        schedule = (
            await db.scalar(select(Schedule).where(Schedule.id == run.schedule_id).with_for_update())
            if run.schedule_id
            else None
        )
        owner = await db.get(User, run.owner_id)
        assert owner is not None
        run.status, run.finished_at = outcome.status, now()
        run.tokens_in, run.tokens_out, run.cost_usd = outcome.tokens_in, outcome.tokens_out, outcome.cost_usd
        run.error_code, run.error = outcome.error_code, outcome.error
        if outcome.status == RunStatus.succeeded:
            await _succeeded(db, run, schedule, owner, outcome)
        elif schedule is not None:
            await _failed(db, run, schedule, owner, outcome)
        await db.commit()
        log.info(
            "run.finished", run_id=str(run_id), status=run.status, changed=run.changed, notified=run.notified,
            error_code=run.error_code,
        )  # fmt: skip
        return run.status


async def _previous(db: AsyncSession, run: Run) -> Run | None:
    if run.schedule_id is None:
        return None
    return await db.scalar(
        select(Run)
        .where(Run.schedule_id == run.schedule_id, Run.status == RunStatus.succeeded, Run.id != run.id)
        .order_by(Run.finished_at.desc().nulls_last(), Run.created_at.desc())
        .limit(1)
    )


async def _succeeded(db: AsyncSession, run: Run, schedule: Schedule | None, owner: User, outcome: RunOutcome) -> None:
    run.report, run.fingerprints = outcome.report, outcome.fingerprints
    previous = await _previous(db, run)
    run.changed = previous is None or set(previous.fingerprints) != set(outcome.fingerprints)
    report = outcome.report or {}
    status_ = report.get("status", "ok")
    name = run.config.get("name", "Run")
    if schedule is not None:
        schedule.last_run_at, schedule.last_status, schedule.consecutive_failures = run.finished_at, status_, 0
    if run.config.get("schedule_id") is None:
        return  # a test run of an unsaved schedule never notifies (02 §6)
    if run.trigger == RunTrigger.manual:
        kind: str | None = "alert" if status_ != "ok" else "run"  # a manual run always tells the user, in-app only
    else:
        kind = decide_notify(
            run.config.get("notify_mode", NotifyMode.on_change), status_, bool(run.changed),
            previous.fingerprints if previous else None, outcome.fingerprints,
        )  # fmt: skip
    if kind is None:
        return
    title = f"{name}: {'Recovered' if kind == 'recovered' else report.get('headline', 'Done')}"
    severity = {"alert": "alert", "warning": "warning"}.get(status_, "info") if kind != "recovered" else "info"
    add_item(
        db, owner.id, kind=kind, severity=severity, title=title, body_md=_body_md(report), link=f"/runs/{run.id}",
        run_id=run.id,
    )  # fmt: skip
    run.notified = True
    if run.trigger == RunTrigger.schedule and run.config.get("notify_email", True):
        await queue_email(
            db, owner, subject=_no_links(title), body=_email_body(run, report, kind), dedupe_key=f"run:{run.id}"
        )


async def _failed(db: AsyncSession, run: Run, schedule: Schedule, owner: User, outcome: RunOutcome) -> None:
    schedule.last_run_at, schedule.last_status = run.finished_at, run.status
    schedule.consecutive_failures += 1
    reason = None
    if outcome.error_code == "budget_exceeded":
        reason = f"Schedule {schedule.name} was paused because your AI budget is used up: {outcome.error}"
    elif schedule.consecutive_failures >= PAUSE_AFTER_FAILURES:
        reason = f"Schedule {schedule.name} was paused after {PAUSE_AFTER_FAILURES} failures: {outcome.error}"
    if reason is None or not schedule.enabled:
        return
    reason = _no_links(reason)
    _pause(schedule, reason)
    add_item(
        db, owner.id, kind="system", severity="warning", title=f"Schedule {schedule.name} was paused",
        body_md=reason, link=f"/schedules/{schedule.id}", run_id=run.id,
    )  # fmt: skip
    run.notified = True
    await queue_email(
        db, owner, subject=f"Schedule {schedule.name} was paused",
        body=f"{reason}\n\nOpen the schedule to resume it:\n{app_url(f'/schedules/{schedule.id}')}\n",
        dedupe_key=f"paused:{schedule.id}:{run.id}",
    )  # fmt: skip


def _body_md(report: dict[str, Any]) -> str:
    lines = [f"- **{f['severity']}**: {f['title']}" for f in report.get("findings", []) if f.get("verified")]
    return "\n".join(lines)


_URL = re.compile(r"\b(?:https?|ftp)://\S+|\bwww\.\S+", re.IGNORECASE)


def _no_links(text: str) -> str:
    """Finding titles come from the model, which read untrusted text: no links of theirs reach an email."""
    return _URL.sub("[link removed]", " ".join(text.split()))


def _email_body(run: Run, report: dict[str, Any], kind: str) -> str:
    """Plain text: headline, finding titles with severity, one link back into MyWorkBuddy (02 §5)."""
    headline = _no_links(report.get("headline", ""))
    lines = [headline if kind != "recovered" else "Everything that was reported is resolved.", ""]
    lines += [f"[{f['severity']}] {_no_links(f['title'])}" for f in report.get("findings", [])]
    lines += ["", f"Open the run: {app_url(f'/runs/{run.id}')}", ""]
    return "\n".join(lines)

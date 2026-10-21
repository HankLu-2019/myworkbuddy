"""Schedule validation (design 02 §4, §6): one check for create, update, chat proposals and test runs.

Kept apart from `app.schedules.service` because the chat tool `propose_schedule` (in the tool catalog) needs
it, and the service imports the agent executor, which imports the tool registry.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import status
from jsonschema import Draft202012Validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.enums import NotifyMode
from app.errors import AppError
from app.models import Agent, Schedule, User
from app.schedules import cron

MAX_INPUT_BYTES = 16_000


def now() -> datetime:  # patched in tests
    return datetime.now(UTC)


def _invalid(message: str, field: str) -> AppError:
    return AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error", message, field=field)


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

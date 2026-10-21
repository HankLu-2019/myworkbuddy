"""Schedules proposed in chat (design 02 §6).

The chat model has `propose_schedule`, a read-tier tool: it validates exactly like `POST /api/schedules`, stores
a pending proposal and returns a summary. Nothing is created until the user clicks Create (`accept`), which
re-validates the stored payload (limits may have changed) and creates the schedule from it. The model never sees
an endpoint or tool that accepts a proposal.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import status
from pydantic_ai import RunContext
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_sessionmaker
from app.enums import ProposalStatus
from app.errors import AppError
from app.models import Agent, Schedule, ScheduleProposal, User
from app.schedules import cron as cronlib
from app.schedules.service import ScheduleFields, now, validate_schedule

TTL = timedelta(hours=24)
MAX_PENDING_PER_SESSION = 10
PAYLOAD_KEYS = ("name", "agent", "cron", "timezone", "prompt", "inputs", "notify_mode")


def payload_fields(payload: dict[str, Any], **overrides: Any) -> ScheduleFields:
    values = {k: payload[k] for k in PAYLOAD_KEYS if k in payload} | overrides
    return ScheduleFields(**values)


async def describe(db: AsyncSession, values: dict[str, Any], count: int = 3) -> dict[str, Any]:
    tz = cronlib.zone(values["timezone"])
    agent = await db.get(Agent, values["agent_id"])
    return {
        "description": cronlib.describe(values["cron"]),
        "next_runs": [t.astimezone(tz).isoformat() for t in cronlib.occurrences(values["cron"], tz, now(), count)],
        "agent_name": agent.name if agent else "",
    }


async def _agent_list(db: AsyncSession, user: User) -> str:
    rows = await db.scalars(
        select(Agent)
        .where(Agent.enabled, Agent.report, (Agent.owner_id.is_(None)) | (Agent.owner_id == user.id))
        .order_by(Agent.slug)
    )
    return "; ".join(
        f"{a.slug} (inputs: {', '.join(sorted((a.inputs_schema or {}).get('properties', {}))) or 'none'})" for a in rows
    )


async def propose_schedule(
    ctx: RunContext[Any],
    name: str,
    agent: str,
    cron: str,
    prompt: str = "",
    inputs: dict[str, Any] | None = None,
    timezone: str | None = None,
    notify_mode: Literal["always", "on_change", "on_alert"] = "on_change",
) -> dict[str, Any]:
    """Propose a recurring check for the user to confirm. Nothing runs until they click Create.

    Args:
        name: Short name, e.g. "Prod pods health".
        agent: Agent slug: morning-brief, k8s-health, ci-failures or jira-digest (an error lists all, with inputs).
        cron: Standard 5-field cron in the user's timezone, e.g. "0 8 * * 1-5" for weekdays at 08:00.
        prompt: Extra instructions for the agent, in the user's words.
        inputs: The agent's inputs, e.g. {"namespaces": ["prod"]} for k8s-health or {"jql": "..."} for jira-digest.
        timezone: IANA timezone; leave empty for the user's own.
        notify_mode: on_change (default: tell the user when findings change), on_alert, or always.
    """
    deps = ctx.deps
    user: User = deps.user
    fields = ScheduleFields(
        name=name, agent=agent, cron=cron, timezone=timezone, prompt=prompt, inputs=inputs or {},
        notify_mode=notify_mode, enabled=True,
    )  # fmt: skip
    async with get_sessionmaker()() as db:
        try:
            values = await validate_schedule(db, user, fields)
        except AppError as exc:
            extra = getattr(exc, "extra", {})
            message = str(exc.detail)
            if extra.get("field") == "agent":
                message += f" Available agents: {await _agent_list(db, user)}."
            return {"error": "invalid_args", "field": extra.get("field"), "message": message}
        pending = await db.scalar(
            select(func.count())
            .select_from(ScheduleProposal)
            .where(ScheduleProposal.session_id == deps.session_id, ScheduleProposal.status == ProposalStatus.pending)
        )
        if (pending or 0) >= MAX_PENDING_PER_SESSION:
            return {"error": "invalid_args", "field": None, "message": "Too many open proposals in this chat."}
        payload = {
            "name": values["name"], "agent": agent, "cron": values["cron"], "timezone": values["timezone"],
            "prompt": prompt, "inputs": values["inputs"], "notify_mode": notify_mode,
        }  # fmt: skip
        proposal = ScheduleProposal(
            owner_id=user.id, session_id=deps.session_id, payload=payload, expires_at=now() + TTL
        )
        db.add(proposal)
        await db.flush()
        summary = await describe(db, values)
        await db.commit()
    card = {"proposal_id": str(proposal.id), **payload, **summary, "expires_at": proposal.expires_at.isoformat()}
    if hasattr(deps, "proposals"):
        deps.proposals.append(card)
    return {
        "proposal_id": card["proposal_id"],
        "description": summary["description"],
        "next_runs": summary["next_runs"],
    }


# ---------- the user's side ----------


async def owned_pending(
    db: AsyncSession, proposal_id: uuid.UUID, user: User, *, lock: bool = False
) -> ScheduleProposal:
    """404 for someone else's, an expired, or an already handled proposal."""
    q = select(ScheduleProposal).where(ScheduleProposal.id == proposal_id, ScheduleProposal.owner_id == user.id)
    p = await db.scalar(q.with_for_update() if lock else q)
    if p is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "Proposal not found")
    if p.status == ProposalStatus.pending and p.expires_at <= now():
        p.status = ProposalStatus.expired
        await db.commit()
    if p.status != ProposalStatus.pending:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", f"This proposal was {p.status}.")
    return p


async def accept(
    db: AsyncSession, proposal: ScheduleProposal, user: User, overrides: dict[str, Any] | None = None
) -> Schedule:
    """Create the schedule from the stored payload. `overrides` carries the user's own edits (Edit → Save)."""
    values = await validate_schedule(db, user, payload_fields(proposal.payload, **(overrides or {})))
    schedule = Schedule(owner_id=user.id, created_from_session=proposal.session_id, **values)
    db.add(schedule)
    await db.flush()
    proposal.status, proposal.schedule_id = ProposalStatus.accepted, schedule.id
    return schedule


async def expire_old(db: AsyncSession, at: datetime | None = None) -> int:
    result = await db.execute(
        update(ScheduleProposal)
        .where(ScheduleProposal.status == ProposalStatus.pending, ScheduleProposal.expires_at <= (at or now()))
        .values(status=ProposalStatus.expired)
    )
    return result.rowcount or 0  # type: ignore[attr-defined]

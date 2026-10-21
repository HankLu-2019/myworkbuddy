"""Agents API (design 02 §7): the agents a user can schedule, with their inputs schema."""

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import or_, select

from app.deps import DB, CurrentAuth
from app.models import Agent
from app.tools.registry import REGISTRY, verified_kinds

router = APIRouter(prefix="/api", tags=["agents"])


class AgentOut(BaseModel):
    id: int
    slug: str
    name: str
    description: str
    model_alias: Literal["chat", "fast", "planner"]
    tools: list[str]
    inputs_schema: dict[str, Any]
    system: bool  # a system agent (no owner)
    integrations: list[str]  # integration kinds its tools use
    missing: list[str]  # of those, kinds the caller has no verified credential for (their tools are left out)


@router.get("/agents", response_model=list[AgentOut])
async def list_agents(auth: CurrentAuth, db: DB) -> list[AgentOut]:
    rows = list(
        await db.scalars(
            select(Agent)
            .where(Agent.enabled, Agent.report, or_(Agent.owner_id.is_(None), Agent.owner_id == auth.user.id))
            .order_by(Agent.name)
        )
    )
    await db.commit()  # nothing to write; close the transaction before the next query session
    usable = await verified_kinds(auth.user.id)
    out = []
    for a in rows:
        kinds = sorted({k for t in a.tools if (spec := REGISTRY.get(t)) and (k := spec.integration_kind)})
        out.append(
            AgentOut(
                id=a.id,
                slug=a.slug,
                name=a.name,
                description=a.description,
                model_alias=a.model_alias,
                tools=list(a.tools),
                inputs_schema=a.inputs_schema,
                system=a.owner_id is None,
                integrations=kinds,
                missing=[k for k in kinds if k not in usable],
            )  # fmt: skip
        )
    return out

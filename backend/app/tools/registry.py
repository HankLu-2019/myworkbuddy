"""The only place agent tools are defined (design 02 §2).

Each ToolSpec says what a tool may do (tier), which integration it needs, whether its output is untrusted text
(then it is wrapped by app.llm.untrusted.wrap, rule R6) and how much of it may reach the model.
`toolset_for(agent, user)` gives an agent only the tools within its tier for which the user has a verified
credential. Tool functions never raise into the agent: errors come back as {"error": code, "message": ...}.
"""

import functools
import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy import select

from app.connectors.base import ConnectorError
from app.db import get_sessionmaker
from app.enums import Tier
from app.llm.untrusted import wrap
from app.logs import log
from app.models import Integration, User, UserCredential

TIER_ORDER = {Tier.read: 0, Tier.sandbox_write: 1, Tier.external_write: 2}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    tier: Tier
    integration_kind: str | None  # None = internal (knowledge, inbox)
    untrusted_output: bool  # True: the result is wrapped by app.llm.untrusted.wrap
    max_output_chars: int
    fn: Callable[..., Awaitable[Any]]  # (ctx: RunContext[deps with .user], **typed_args)


class AgentLike(Protocol):
    """What toolset_for needs from an agent: an `app.models.Agent` row, or a stand-in in tests."""

    tools: Iterable[str]
    max_tier: str


REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    if spec.name in REGISTRY:
        raise ValueError(f"tool {spec.name} registered twice")
    REGISTRY[spec.name] = spec
    return spec


def _guarded(spec: ToolSpec) -> Callable[..., Awaitable[Any]]:
    """The function the model calls: errors as data, output wrapped or capped. Keeps the signature and
    docstring, which pydantic-ai turns into the tool schema."""

    @functools.wraps(spec.fn)
    async def tool(ctx: RunContext[Any], *args: Any, **kwargs: Any) -> Any:
        try:
            result = await spec.fn(ctx, *args, **kwargs)
        except ConnectorError as exc:
            return {"error": exc.code, "message": exc.message}
        except Exception as exc:  # a bug in a connector must not end the run
            log.exception("tool.failed", tool=spec.name, error=type(exc).__name__)
            return {"error": "unavailable", "message": f"{spec.name} failed unexpectedly"}
        if spec.untrusted_output:
            return wrap(
                f"{spec.integration_kind or 'internal'}:{spec.name}", _dump(result), max_chars=spec.max_output_chars
            )
        text = _dump(result)
        if len(text) > spec.max_output_chars:
            return {"result": text[: spec.max_output_chars], "truncated": True}
        return result

    return tool


def _dump(result: Any) -> str:
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)


def tool_function(name: str) -> Callable[..., Awaitable[Any]]:
    """An internal tool's guarded function, for agents built without the table (chat)."""
    spec = REGISTRY[name]
    if spec.integration_kind is not None:
        raise ValueError(f"{name} needs an integration; use toolset_for")
    return spec.fn if not spec.untrusted_output else _guarded(spec)


async def verified_kinds(user_id: int) -> set[str]:
    """Integration kinds the user can use: every credential part verified (feeds need none)."""
    async with get_sessionmaker()() as db:
        rows = (
            await db.execute(
                select(Integration.kind, Integration.id, UserCredential.status)
                .join(
                    UserCredential,
                    (UserCredential.integration_id == Integration.id) & (UserCredential.user_id == user_id),
                    isouter=True,
                )
                .where(Integration.enabled)
            )
        ).all()
    from app.integrations.kinds import CREDENTIALS

    by_integration: dict[int, tuple[str, list[str | None]]] = {}
    for kind, iid, status in rows:
        by_integration.setdefault(iid, (kind, []))[1].append(status)
    kinds = set()
    for kind, statuses in by_integration.values():
        needed = len(CREDENTIALS.get(kind, {}))
        if needed == 0 or (statuses.count("ok") >= needed):
            kinds.add(kind)
    return kinds


async def toolset_for(agent: AgentLike, user: User) -> FunctionToolset:
    """The agent's tools within its tier and for which the user holds a verified credential (02 §2)."""
    max_tier = TIER_ORDER[Tier(agent.max_tier)]
    usable = await verified_kinds(user.id)
    toolset: FunctionToolset = FunctionToolset()
    mcp_names = [n for n in agent.tools if n.startswith("mcp.")]
    if mcp_names:  # 08 §F12: read tier only; a tool not enabled for this user is simply absent
        from app.mcp.service import tools_for

        for tool in await tools_for(user, mcp_names):
            toolset.add_tool(tool)
    for name in agent.tools:
        if name.startswith("mcp."):
            continue
        spec = REGISTRY.get(name)
        if spec is None:
            raise ValueError(f"unknown tool {name!r}")
        if TIER_ORDER[spec.tier] > max_tier:
            continue
        if spec.integration_kind is not None and spec.integration_kind not in usable:
            continue
        toolset.add_function(_guarded(spec), name=spec.name)
    return toolset


# Registration lives in app.tools.catalog; importing it here makes REGISTRY complete for every caller.
from app.tools import catalog  # noqa: E402,F401

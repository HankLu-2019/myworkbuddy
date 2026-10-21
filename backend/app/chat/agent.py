import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model

from app.db import get_sessionmaker
from app.knowledge import prompts
from app.knowledge.acl import can_view
from app.knowledge.wiki import find_page, read_page
from app.models import Project, User, WikiPage
from app.tools.knowledge import SCOPES  # noqa: E402,F401  (re-exported for app.knowledge.routes)
from app.tools.registry import tool_function

CONTEXT_PAGE_LIMIT = 6000
CHAT_TOOLS = ("search_knowledge", "get_session")


@dataclass
class ChatDeps:
    user: User
    session_id: uuid.UUID
    project_id: int | None
    citations: list[dict[str, Any]] = field(default_factory=list)


async def _instructions(ctx: RunContext[ChatDeps]) -> str:
    parts = [prompts.CHAT_SYSTEM]
    async with get_sessionmaker()() as db:
        me = await find_page(db, ctx.deps.user.id, "private", "me/how-i-work.md")
        if me:
            parts.append(f"## What you know about how this user works\n{read_page(me)[:CONTEXT_PAGE_LIMIT]}")
        if ctx.deps.project_id:
            project = await db.get(Project, ctx.deps.project_id)
            if project and can_view(project, ctx.deps.user):
                card = await db.get(WikiPage, project.card_page_id) if project.card_page_id else None
                # Only the user's own card goes into the system prompt; someone else's is untrusted text (R6)
                # and reaches the model through search, wrapped.
                if card and card.owner_id == ctx.deps.user.id:
                    parts.append(f"## Project in focus: {project.name}\n{read_page(card)[:CONTEXT_PAGE_LIMIT]}")
    return "\n\n".join(parts)


def build_agent(model: Model) -> Agent[ChatDeps, str]:
    """`model` comes from app.llm.client.model_for(user_id) (rule R11)."""
    return Agent(
        model,
        deps_type=ChatDeps,
        instructions=_instructions,
        tools=[tool_function(name) for name in CHAT_TOOLS],
    )

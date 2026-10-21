import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model
from sqlalchemy import select

from app.db import get_sessionmaker
from app.knowledge import prompts
from app.knowledge.acl import can_view
from app.knowledge.search import hybrid_search
from app.knowledge.wiki import find_page, read_page
from app.llm.untrusted import wrap
from app.models import ChatSession, Message, Project, User, WikiPage

SCOPES: dict[str, list[str] | None] = {
    "all": None,
    "wiki": ["wiki"],
    "sessions": ["episode"],
    "code": ["code", "repo_doc"],
    "documents": ["upload"],
}
CONTEXT_PAGE_LIMIT = 6000


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


async def search_knowledge(
    ctx: RunContext[ChatDeps],
    query: str,
    scope: Literal["all", "wiki", "sessions", "code", "documents"] = "all",
) -> list[dict[str, Any]]:
    """Search the user's knowledge base (their private items plus team-public ones).

    Args:
        query: keywords or a question; include exact identifiers like ticket keys or error names.
        scope: limit to wiki pages, past session summaries, code, or uploaded documents.
    """
    async with get_sessionmaker()() as db:
        hits = await hybrid_search(db, ctx.deps.user, query, source_types=SCOPES[scope], limit=8)
    results = []
    for h in hits:
        n = len(ctx.deps.citations) + 1
        ctx.deps.citations.append(
            {
                "n": n,
                "document_id": h.document_id,
                "title": h.title,
                "source_type": h.source_type,
                "source_ref": h.source_ref,
                "visibility": h.visibility,
                "meta": h.meta,
            }
        )
        results.append({"n": n, "title": h.title, "type": h.source_type, **wrap(_source(h), h.content, max_chars=1500)})
    return results


def _source(h) -> str:
    return f"{h.source_type}:{h.visibility}/{h.document_id}"


async def get_session(ctx: RunContext[ChatDeps], session_id: str) -> str | dict[str, Any]:
    """Read the full transcript of a past session (use when a session summary lacks detail).

    Args:
        session_id: the session id from a search result's reference ("session:<id>").
    """
    try:
        sid = uuid.UUID(session_id.removeprefix("session:"))
    except ValueError:
        return "Invalid session id"
    async with get_sessionmaker()() as db:
        session = await db.get(ChatSession, sid)
        if session is None or not can_view(session, ctx.deps.user):
            return "Session not found"
        messages = await db.scalars(select(Message).where(Message.session_id == sid).order_by(Message.id))
        text = "\n".join(f"{m.role.upper()}: {m.content}" for m in messages)
    return wrap(f"session:{sid}", text, max_chars=30000)


def build_agent(model: Model) -> Agent[ChatDeps, str]:
    """`model` comes from app.llm.client.model_for(user_id) (rule R11)."""
    return Agent(
        model,
        deps_type=ChatDeps,
        instructions=_instructions,
        tools=[search_knowledge, get_session],
    )

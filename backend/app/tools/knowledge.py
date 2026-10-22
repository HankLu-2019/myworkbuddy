"""Internal knowledge tools (design 02 §2 table: search_knowledge, get_session). They wrap untrusted text
themselves, per hit, because each hit has its own source."""

import uuid
from typing import Any, Literal

from pydantic_ai import RunContext
from sqlalchemy import select

from app.db import get_sessionmaker
from app.knowledge.acl import can_view
from app.knowledge.search import hybrid_search
from app.llm.untrusted import wrap
from app.models import ChatSession, Message

SCOPES: dict[str, list[str] | None] = {
    "all": None,
    "wiki": ["wiki"],
    "sessions": ["episode"],
    "code": ["code", "repo_doc"],
    "documents": ["upload"],
    "changes": ["change"],
}


async def search_knowledge(
    ctx: RunContext[Any],
    query: str,
    scope: Literal["all", "wiki", "sessions", "code", "documents", "changes"] = "all",
) -> list[dict[str, Any]]:
    """Search the user's knowledge base (their private items plus team-public ones).

    Args:
        query: keywords or a question; include exact identifiers like ticket keys or error names.
        scope: limit to wiki pages, past session summaries, code, uploaded documents, or merged changes.
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


async def get_session(ctx: RunContext[Any], session_id: str) -> str | dict[str, Any]:
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

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessagesTypeAdapter,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)
from pydantic_ai.run import AgentRunResultEvent
from pydantic_core import to_jsonable_python
from sqlalchemy import select, update

from app.audit.service import audit
from app.chat.agent import ChatDeps, build_agent
from app.db import get_sessionmaker
from app.deps import DB, CurrentAuth
from app.errors import AppError
from app.knowledge.acl import ensure_view, visible_filter
from app.knowledge.indexer import set_visibility
from app.llm import client as llm
from app.llm import usage
from app.logs import log
from app.models import ChatSession, Document, Episode, Message, Project, ToolCall, User
from app.tasks.app import build_episode_task, defer_once

router = APIRouter(prefix="/api/chat", tags=["chat"])

HISTORY_RUNS = 20
HISTORY_CHARS = 200_000


class SessionOut(BaseModel):
    id: uuid.UUID
    title: str
    source: str
    visibility: str
    project_id: int | None
    owner_id: int
    created_at: datetime
    updated_at: datetime


class SessionCreate(BaseModel):
    title: str | None = None
    project_id: int | None = None


class SessionUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    project_id: int | None = None
    visibility: Literal["private", "public"] | None = None


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    created_at: datetime
    citations: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=50_000)


def _out(s: ChatSession) -> SessionOut:
    return SessionOut(
        id=s.id,
        title=s.title,
        source=s.source,
        visibility=s.visibility,
        project_id=s.project_id,
        owner_id=s.owner_id,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


async def _owned(db: DB, session_id: uuid.UUID, user: User) -> ChatSession:
    session = await db.get(ChatSession, session_id)
    if session is None or session.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return session


async def _check_project(db: DB, project_id: int | None, user: User) -> None:
    if project_id is not None:
        ensure_view(await db.get(Project, project_id), user)


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(auth: CurrentAuth, db: DB, source: str | None = None, limit: int = 100) -> list[SessionOut]:
    q = select(ChatSession).where(ChatSession.owner_id == auth.user.id)
    if source:
        q = q.where(ChatSession.source == source)
    rows = await db.scalars(q.order_by(ChatSession.updated_at.desc()).limit(min(limit, 500)))
    return [_out(s) for s in rows]


@router.post("/sessions", response_model=SessionOut, status_code=201)
async def create_session(body: SessionCreate, auth: CurrentAuth, db: DB) -> SessionOut:
    await _check_project(db, body.project_id, auth.user)
    session = ChatSession(owner_id=auth.user.id, title=body.title or "New chat", project_id=body.project_id)
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return _out(session)


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def get_session(session_id: uuid.UUID, auth: CurrentAuth, db: DB) -> SessionOut:
    session = await db.get(ChatSession, session_id)
    ensure_view(session, auth.user)
    return _out(session)


@router.patch("/sessions/{session_id}", response_model=SessionOut)
async def update_session(session_id: uuid.UUID, body: SessionUpdate, auth: CurrentAuth, db: DB) -> SessionOut:
    session = await _owned(db, session_id, auth.user)
    if body.title is not None:
        session.title = body.title
    if "project_id" in body.model_fields_set:
        await _check_project(db, body.project_id, auth.user)
        session.project_id = body.project_id
    if body.visibility and body.visibility != session.visibility:
        session.visibility = body.visibility
        # The session's summary follows the session into (or out of) the team space.
        episode = await db.scalar(select(Episode).where(Episode.session_id == session.id))
        if episode:
            episode.visibility = body.visibility
            if episode.document_id and (doc := await db.get(Document, episode.document_id)):
                await set_visibility(db, doc, body.visibility, actor_id=auth.user.id)
        await audit(
            db,
            f"session.{'publish' if body.visibility == 'public' else 'unpublish'}",
            user_id=auth.user.id,
            target_type="session",
            target_id=session.id,
        )
    await db.commit()
    return _out(session)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: uuid.UUID, auth: CurrentAuth, db: DB) -> None:
    session = await _owned(db, session_id, auth.user)
    episode = await db.scalar(select(Episode).where(Episode.session_id == session.id))
    if episode and episode.document_id and (doc := await db.get(Document, episode.document_id)):
        await db.delete(doc)
    await db.delete(session)
    await audit(db, "session.delete", user_id=auth.user.id, target_type="session", target_id=session_id)
    await db.commit()


@router.post("/sessions/{session_id}/close", status_code=202)
async def close_session(session_id: uuid.UUID, auth: CurrentAuth, db: DB) -> dict[str, str]:
    session = await _owned(db, session_id, auth.user)
    session.closed_at = datetime.now(UTC)
    await db.commit()
    await defer_once(build_episode_task, f"episode:{session.id}", session_id=str(session.id))
    return {"status": "summarizing"}


@router.get("/sessions/{session_id}/messages", response_model=list[MessageOut])
async def list_messages(session_id: uuid.UUID, auth: CurrentAuth, db: DB) -> list[MessageOut]:
    session = await db.get(ChatSession, session_id)
    ensure_view(session, auth.user)
    messages = list(await db.scalars(select(Message).where(Message.session_id == session_id).order_by(Message.id)))
    tools: dict[int, list[dict[str, Any]]] = {}
    for t in await db.scalars(select(ToolCall).where(ToolCall.session_id == session_id).order_by(ToolCall.id)):
        tools.setdefault(t.message_id or 0, []).append({"name": t.tool_name, "args": t.args, "status": t.status})
    return [
        MessageOut(
            id=m.id,
            role=m.role,
            content=m.content,
            created_at=m.created_at,
            citations=_visible_citations(m.meta.get("citations", []), auth.user, session),
            tools=tools.get(m.id, []),
        )
        for m in messages
    ]


def _visible_citations(citations: list[dict[str, Any]], user: User, session: ChatSession) -> list[dict[str, Any]]:
    # A public session may cite the owner's private items; other viewers only see the public ones.
    if session.owner_id == user.id:
        return citations
    return [c for c in citations if c.get("visibility") == "public"]


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def _history(session_id: uuid.UUID) -> list:
    """Recent turns only, so long sessions stay inside the model's context window.

    Each stored run starts with the user's request, so dropping whole runs from the front keeps a valid sequence.
    """
    async with get_sessionmaker()() as db:
        raws = list(
            await db.scalars(
                select(Message.raw)
                .where(Message.session_id == session_id, Message.role == "assistant", Message.raw.is_not(None))
                .order_by(Message.id.desc())
                .limit(HISTORY_RUNS)
            )
        )
    kept, size = [], 0
    for raw in raws:  # newest first
        size += len(json.dumps(raw))
        if kept and size > HISTORY_CHARS:
            break
        kept.append(raw)
    return ModelMessagesTypeAdapter.validate_python([m for raw in reversed(kept) for m in raw])


async def _stream(session: ChatSession, user: User, content: str) -> AsyncIterator[str]:
    structlog.contextvars.bind_contextvars(session_id=str(session.id))
    maker = get_sessionmaker()
    async with maker() as db:
        db.add(Message(session_id=session.id, role="user", content=content))
        values: dict[str, Any] = {"updated_at": datetime.now(UTC)}
        if session.title == "New chat":
            values["title"] = " ".join(content.split())[:80]
        await db.execute(update(ChatSession).where(ChatSession.id == session.id).values(**values))
        await db.commit()

    deps = ChatDeps(user=user, session_id=session.id, project_id=session.project_id)
    text_parts: list[str] = []
    tool_rows: dict[str, int] = {}
    result = None
    log.info("chat.turn_started")
    error: dict[str, Any] | None = None
    tracked = usage.Usage()
    try:
        with usage.track() as tracked:
            history = await _history(session.id)
            agent = build_agent(await llm.model_for(user.id))
            async with (
                asyncio.timeout(llm.timeout_for("chat_turn")),
                agent.run_stream_events(
                    content, message_history=history, deps=deps, usage_limits=llm.limits_for("chat_turn")
                ) as events,
            ):
                async for ev in events:
                    if isinstance(ev, PartStartEvent) and isinstance(ev.part, TextPart) and ev.part.content:
                        text_parts.append(ev.part.content)
                        yield _sse("delta", {"text": ev.part.content})
                    elif isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, TextPartDelta):
                        text_parts.append(ev.delta.content_delta)
                        yield _sse("delta", {"text": ev.delta.content_delta})
                    elif isinstance(ev, FunctionToolCallEvent):
                        async with maker() as db:
                            row = ToolCall(
                                session_id=session.id,
                                call_id=ev.part.tool_call_id,
                                tool_name=ev.part.tool_name,
                                args=ev.part.args_as_dict(),
                            )
                            db.add(row)
                            await db.commit()
                            tool_rows[ev.part.tool_call_id] = row.id
                        log.info("chat.tool_call", tool=ev.part.tool_name)
                        yield _sse("tool_call", {"name": ev.part.tool_name, "args": ev.part.args_as_dict()})
                    elif isinstance(ev, FunctionToolResultEvent):
                        async with maker() as db:
                            await db.execute(
                                update(ToolCall)
                                .where(ToolCall.id == tool_rows.get(ev.part.tool_call_id))
                                .values(status="done", result=to_jsonable_python(ev.part.content))
                            )
                            await db.commit()
                        yield _sse("tool_result", {"name": ev.part.tool_name})
                    elif isinstance(ev, AgentRunResultEvent):
                        result = ev.result
    except AppError as exc:
        error = {"code": exc.code, "message": exc.detail}
    except Exception as exc:
        if llm.is_budget_error(exc):
            err = await llm.budget_error(user.id)
            log.warning("llm.budget_exceeded", user_id=user.id, purpose="chat_turn")
            error = {"code": err.code, "message": err.detail}
        elif isinstance(exc, TimeoutError):
            error = {"code": "llm_unavailable", "message": "The model took too long to answer. Try again."}
        elif isinstance(exc, UsageLimitExceeded):
            error = {
                "code": "llm_unavailable",
                "message": "This answer needed too many steps. Try a narrower question.",
            }
        else:
            log.exception("chat.failed")
            error = {"code": "llm_unavailable", "message": f"The model call failed: {exc}"[:500]}
    except BaseException as exc:  # cancellation (client went away) is otherwise silent
        log.warning("chat.cancelled", error=type(exc).__name__, partial_chars=len("".join(text_parts)))
        raise
    finally:
        message_id = await asyncio.shield(
            _save_assistant(session.id, text_parts, result, deps, list(tool_rows.values()), tracked)
        )
    if error:
        yield _sse("error", error)
    yield _sse("done", {"message_id": message_id, "citations": deps.citations})


async def _save_assistant(
    session_id: uuid.UUID,
    text_parts: list[str],
    result: Any,
    deps: ChatDeps,
    tool_ids: list[int],
    tracked: usage.Usage,
) -> int | None:
    if result is None and not "".join(text_parts).strip():
        return None  # the model produced nothing; the user's message stays so they can retry
    run_usage = result.usage if result else None
    async with get_sessionmaker()() as db:
        msg = Message(
            session_id=session_id,
            role="assistant",
            content=result.output if result else "".join(text_parts),
            raw=to_jsonable_python(result.new_messages()) if result else None,
            meta={"citations": deps.citations},
            tokens_in=run_usage.input_tokens if run_usage else None,
            tokens_out=run_usage.output_tokens if run_usage else None,
            cost_usd=tracked.cost_usd,
        )
        db.add(msg)
        await db.flush()
        if tool_ids:
            await db.execute(update(ToolCall).where(ToolCall.id.in_(tool_ids)).values(message_id=msg.id))
        await db.execute(update(ChatSession).where(ChatSession.id == session_id).values(updated_at=datetime.now(UTC)))
        await db.commit()
        if tracked.unpriced_call_ids:  # streamed calls: the gateway's spend log has their cost a bit later
            from app.tasks.app import defer_settle_costs

            await defer_settle_costs(msg.id, tracked.unpriced_call_ids)
        log.info(
            "chat.message_saved",
            cost_usd=float(tracked.cost_usd),
            message_id=msg.id,
            tokens_in=msg.tokens_in,
            tokens_out=msg.tokens_out,
            citations=len(deps.citations),
        )
        return msg.id


@router.post("/sessions/{session_id}/messages")
async def post_message(session_id: uuid.UUID, body: MessageIn, auth: CurrentAuth, db: DB) -> StreamingResponse:
    session = await _owned(db, session_id, auth.user)
    # The request's session stays checked out while the reply streams; end its read transaction now so it isn't
    # "idle in transaction" for minutes (rule R1; the server ends such sessions after 60 s).
    await db.commit()
    return StreamingResponse(
        _stream(session, auth.user, body.content),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/shared", response_model=list[SessionOut])
async def list_shared(auth: CurrentAuth, db: DB) -> list[SessionOut]:
    rows = await db.scalars(
        select(ChatSession)
        .where(visible_filter(ChatSession, auth.user), ChatSession.owner_id != auth.user.id)
        .order_by(ChatSession.updated_at.desc())
        .limit(100)
    )
    return [_out(s) for s in rows]

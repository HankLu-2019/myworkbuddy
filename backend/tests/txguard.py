"""Test guard for rule R1: fail any LLM or embedding call made while a DB transaction is open.

Transactions are tracked with engine events on every connection of the app's engine, together with the asyncio
task that opened them; the embedder and the test model are wrapped so each call checks first. A call is a
violation when a transaction is open in the calling task or in a task it was started from (a transaction left
open across `asyncio.gather` counts). A sibling task's own short transaction (a progress write while other files
are summarised) does not: that call is not inside it.
"""

import asyncio
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from pydantic_ai.models import Model
from pydantic_ai.models.wrapper import WrapperModel
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

_open: dict[int, asyncio.Task | None] = {}  # connection id -> the task that began its transaction
_where: dict[int, str] = {}  # connection id -> the app code that began it, for the violation message
_parents: "weakref.WeakKeyDictionary[asyncio.Task, asyncio.Task]" = weakref.WeakKeyDictionary()
violations: list[str] = []


def _current() -> asyncio.Task | None:
    try:
        return asyncio.current_task()
    except RuntimeError:  # no running loop: synchronous use
        return None


def install(engine: AsyncEngine) -> None:
    sync = engine.sync_engine

    @event.listens_for(sync, "begin")
    def _begin(conn) -> None:
        _open[id(conn)] = _current()
        _where[id(conn)] = _app_frame()

    @event.listens_for(sync, "commit")
    def _commit(conn) -> None:
        _open.pop(id(conn), None)

    @event.listens_for(sync, "rollback")
    def _rollback(conn) -> None:
        _open.pop(id(conn), None)


def _task_factory(loop: asyncio.AbstractEventLoop, coro, **kwargs: Any) -> asyncio.Task:
    task = asyncio.Task(coro, loop=loop, **kwargs)
    parent = asyncio.current_task(loop)
    if parent is not None:
        _parents[task] = parent
    return task


def track_tasks() -> None:
    """Record which task started which, so a parent's open transaction is seen from its children."""
    loop = asyncio.get_running_loop()
    if loop.get_task_factory() is not _task_factory:
        loop.set_task_factory(_task_factory)


def _lineage(task: asyncio.Task | None) -> set[asyncio.Task | None]:
    seen: set[asyncio.Task | None] = {None}  # a transaction begun outside any task is everyone's
    while task is not None and task not in seen:
        seen.add(task)
        task = _parents.get(task)
    return seen


def _app_frame() -> str:
    import traceback

    frames = [f for f in traceback.extract_stack(limit=40) if "/app/" in f.filename and "/tests/" not in f.filename]
    return f"{frames[-1].filename.split('/app/', 1)[1]}:{frames[-1].lineno}" if frames else "?"


def check(what: str) -> None:
    mine = _lineage(_current())
    held = [_where.get(c, "?") for c, owner in list(_open.items()) if owner in mine]
    if held:
        violations.append(
            f"{what} called with {len(held)} DB transaction(s) open (rule R1), begun at {', '.join(held)}"
        )


def reset() -> None:
    violations.clear()


class GuardedEmbedder:
    def __init__(self, inner: Any) -> None:
        self.inner = inner

    @property
    def model_id(self) -> str:
        return getattr(self.inner, "model_id", "hash-1024")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        check("embedding")
        return await self.inner.embed(texts)


class GuardedModel(WrapperModel):
    async def request(self, *args: Any, **kwargs: Any):
        check("LLM request")
        return await super().request(*args, **kwargs)

    @asynccontextmanager
    async def request_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        check("LLM stream")
        async with super().request_stream(*args, **kwargs) as stream:
            yield stream


def guard_model(model: Model) -> Model:
    return GuardedModel(model)

"""Test guard for rule R1: fail any LLM or embedding call made while a DB transaction is open.

Transactions are tracked with engine events on every connection of the app's engine; the embedder and the
test model are wrapped so each call checks the count first.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from pydantic_ai.models import Model
from pydantic_ai.models.wrapper import WrapperModel
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

_open: set[int] = set()
violations: list[str] = []


def install(engine: AsyncEngine) -> None:
    sync = engine.sync_engine

    @event.listens_for(sync, "begin")
    def _begin(conn) -> None:
        _open.add(id(conn))

    @event.listens_for(sync, "commit")
    def _commit(conn) -> None:
        _open.discard(id(conn))

    @event.listens_for(sync, "rollback")
    def _rollback(conn) -> None:
        _open.discard(id(conn))


def check(what: str) -> None:
    if _open:
        violations.append(f"{what} called with {len(_open)} DB transaction(s) open (rule R1)")


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

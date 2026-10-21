from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    # Load server-generated columns (created_at/updated_at) on INSERT/UPDATE via RETURNING; async sessions
    # can't lazy-load them later.
    __mapper_args__ = {"eager_defaults": True}


@lru_cache
def get_engine() -> AsyncEngine:
    s = get_settings()
    return create_async_engine(
        s.async_db_url,
        pool_pre_ping=True,
        pool_size=s.db_pool_size,
        max_overflow=5,
        pool_timeout=10,
        pool_recycle=1800,
        # api keeps statements short; the worker sets MWB_DB_STATEMENT_TIMEOUT_MS higher (design 01 §H6).
        connect_args={"server_settings": {"statement_timeout": str(s.db_statement_timeout_ms)}},
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session

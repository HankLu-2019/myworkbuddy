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
    return create_async_engine(get_settings().async_db_url, pool_pre_ping=True)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session

"""Readiness and worker heartbeat (design 01 §H14)."""

import asyncio
import os
import socket
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from app.config import get_settings
from app.db import get_sessionmaker
from app.logs import log
from app.models import SystemState

HEARTBEAT_KEY = "worker_heartbeat:default"
HEARTBEAT_INTERVAL = 30.0
READY_MAX_AGE = timedelta(minutes=2)  # /api/ready
HEALTHY_MAX_AGE = timedelta(minutes=3)  # `mwb worker-health` (container healthcheck)

_gateway_transport: httpx2.AsyncBaseTransport | None = None  # tests


def set_gateway_transport(transport: httpx2.AsyncBaseTransport | None) -> None:
    global _gateway_transport
    _gateway_transport = transport


async def put_state(key: str, value: dict[str, Any]) -> None:
    """Upsert one system_state row in its own short transaction."""
    async with get_sessionmaker()() as db:
        await db.execute(
            insert(SystemState)
            .values(key=key, value=value)
            .on_conflict_do_update(index_elements=["key"], set_={"value": value, "updated_at": datetime.now(UTC)})
        )
        await db.commit()


async def get_states(*keys: str) -> dict[str, dict[str, Any]]:
    async with get_sessionmaker()() as db:
        rows = await db.execute(select(SystemState.key, SystemState.value).where(SystemState.key.in_(keys)))
        return {k: v for k, v in rows}


async def beat() -> None:
    await put_state(
        HEARTBEAT_KEY, {"at": datetime.now(UTC).isoformat(), "host": socket.gethostname(), "pid": os.getpid()}
    )


async def heartbeat_loop() -> None:
    """Runs beside the worker in the same process. It doesn't use a job slot, so long jobs can't starve it, and it
    stops when the process does, which is exactly what the healthcheck should notice."""
    while True:
        try:
            await beat()
        except Exception as exc:  # the DB is briefly away: report it, try again
            log.warning("worker.heartbeat_failed", error=str(exc)[:200])
        await asyncio.sleep(HEARTBEAT_INTERVAL)


def heartbeat_age(state: dict[str, Any] | None, now: datetime | None = None) -> timedelta | None:
    if not state or "at" not in state:
        return None
    return (now or datetime.now(UTC)) - datetime.fromisoformat(state["at"])


async def check_db() -> str | None:
    try:
        async with get_sessionmaker()() as db:
            await db.execute(text("SELECT 1"))
        return None
    except Exception as exc:
        return f"database: {type(exc).__name__}"


async def check_gateway() -> str | None:
    url = f"{get_settings().gateway_url}/health/liveliness"
    try:
        async with httpx2.AsyncClient(timeout=2, transport=_gateway_transport) as client:
            resp = await client.get(url)
        return None if resp.status_code == 200 else f"llm gateway: HTTP {resp.status_code}"
    except Exception as exc:
        return f"llm gateway: {type(exc).__name__}"


async def check_worker() -> str | None:
    try:
        state = (await get_states(HEARTBEAT_KEY)).get(HEARTBEAT_KEY)
    except Exception:
        return "worker: heartbeat unreadable"
    age = heartbeat_age(state)
    if age is None:
        return "worker: no heartbeat yet"
    if age > READY_MAX_AGE:
        return f"worker: last heartbeat {int(age.total_seconds())} s ago"
    return None


async def readiness() -> dict[str, str]:
    """{check: "ok" | problem} for database, llm_gateway and worker."""
    db, gateway = await asyncio.gather(check_db(), check_gateway())
    worker = await check_worker() if db is None else "worker: database unavailable"
    return {"database": db or "ok", "llm_gateway": gateway or "ok", "worker": worker or "ok"}

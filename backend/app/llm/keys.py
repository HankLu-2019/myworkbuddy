"""Per-user LiteLLM virtual keys (design 01 §H12). The only module that uses the gateway master key.

Each user gets one key, alias `mwb-user-<id>`, limited to our model aliases and to the user's monthly budget.
The key is stored encrypted in `user_secrets` (kind `gateway_key`, hidden from the Settings list).

Rule R1: every gateway call happens with no DB transaction open. Creating a key is "read → close → create →
short write that re-checks": if another process stored a key meanwhile, ours is deleted and theirs is used.
Rule R7: the master key and user keys only travel in headers and request bodies, never in URLs or logs.
"""

import asyncio
import time
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx2
from sqlalchemy import select

from app.config import get_settings
from app.db import get_sessionmaker
from app.errors import AppError
from app.logs import log
from app.models import User, UserSecret
from app.secrets.service import get_secret, set_secret

KEY_KIND = "gateway_key"
MODELS = ["chat", "fast", "embed", "planner"]
INFO_TTL = 300.0  # spend is cached for 5 minutes

_transport: httpx2.AsyncBaseTransport | None = None  # tests route the gateway to the fake (tests/fakellm)
_locks: dict[int, asyncio.Lock] = {}
_info_cache: dict[int, tuple[float, "KeyInfo"]] = {}


def set_transport(transport: httpx2.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = transport
    _info_cache.clear()


def alias(user_id: int) -> str:
    return f"mwb-user-{user_id}"


def budget_for(user: User) -> Decimal:
    return Decimal(
        str(user.llm_budget_usd if user.llm_budget_usd is not None else get_settings().default_user_budget_usd)
    )


class GatewayAdminError(RuntimeError):
    pass


async def _admin(method: str, path: str, *, json: dict[str, Any] | None = None, params: dict | None = None) -> Any:
    s = get_settings()
    if not s.gateway_key:
        raise GatewayAdminError("MWB_GATEWAY_KEY (the LiteLLM master key) is not set")
    async with httpx2.AsyncClient(base_url=s.gateway_url, timeout=30, transport=_transport) as client:
        resp = await client.request(
            method, path, json=json, params=params, headers={"Authorization": f"Bearer {s.gateway_key}"}
        )
    if resp.status_code >= 400:
        # The body may echo the key we sent; never include it in the error.
        raise GatewayAdminError(f"gateway {method} {path} failed with HTTP {resp.status_code}")
    return resp.json() if resp.content else None


async def _generate(user: User) -> str:
    body = await _admin(
        "POST",
        "/key/generate",
        json={
            "key_alias": alias(user.id),
            "user_id": f"mwb:{user.id}",
            "models": MODELS,
            "max_budget": float(budget_for(user)),
            "budget_duration": "30d",
            "metadata": {"mwb_user": user.id},
        },
    )
    key = body.get("key") if isinstance(body, dict) else None
    if not key:
        raise GatewayAdminError("gateway /key/generate returned no key")
    return key


async def delete_key(user_id: int) -> None:
    """Delete the user's key(s) by alias; needs no secret, so it also works after the user row is gone."""
    await _admin("POST", "/key/delete", json={"key_aliases": [alias(user_id)]})
    _info_cache.pop(user_id, None)


async def _stored(user_id: int) -> str | None:
    async with get_sessionmaker()() as db:
        return await get_secret(db, user_id, KEY_KIND)


async def ensure_key(user_id: int) -> str:
    """The user's key, creating it on first use (lazily for users that existed before H12)."""
    key = await _stored(user_id)
    if key:
        return key
    lock = _locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        if key := await _stored(user_id):
            return key
        async with get_sessionmaker()() as db:
            user = await db.get(User, user_id)
            if user is None or not user.is_active:
                raise AppError(403, "forbidden", "This account is disabled")
        new_key = await _generate(user)  # no transaction open (rule R1)
        async with get_sessionmaker()() as db:
            existing = await db.scalar(
                select(UserSecret).where(UserSecret.user_id == user_id, UserSecret.kind == KEY_KIND).with_for_update()
            )
            if existing is None:
                await set_secret(db, user_id, KEY_KIND, new_key)
                await db.commit()
                log.info("llm.key_created", user_id=user_id)
                return new_key
        # Another process stored one first: keep theirs, drop ours (it was never handed out).
        log.info("llm.key_race_resolved", user_id=user_id)
        key = await _stored(user_id)
        await _admin("POST", "/key/delete", json={"keys": [new_key]})
        return key or new_key


async def create_for_new_user(user_id: int) -> None:
    """Called right after a user is created. Failure is logged; the key is then created on first use."""
    try:
        await ensure_key(user_id)
    except Exception as exc:
        log.warning("llm.key_create_failed", user_id=user_id, error=str(exc)[:300])


async def update_budget(user_id: int) -> None:
    key = await ensure_key(user_id)
    async with get_sessionmaker()() as db:
        user = await db.get(User, user_id)
        budget = budget_for(user)
    await _admin("POST", "/key/update", json={"key": key, "max_budget": float(budget)})
    _info_cache.pop(user_id, None)


async def rotate_key(user_id: int) -> None:
    """New key for the user, old one deleted (06-operations §8)."""
    old = await _stored(user_id)
    async with get_sessionmaker()() as db:
        user = await db.get(User, user_id)
    new_key = await _generate(user)
    async with get_sessionmaker()() as db:
        await set_secret(db, user_id, KEY_KIND, new_key)
        await db.commit()
    if old:
        await _admin("POST", "/key/delete", json={"keys": [old]})
    _info_cache.pop(user_id, None)


async def spend_for_call(call_id: str) -> Decimal | None:
    """Cost of one gateway call from LiteLLM's spend log (written a little after the call). None if not there yet."""
    body = await _admin("GET", "/spend/logs", params={"request_id": call_id})
    rows = body if isinstance(body, list) else (body or {}).get("data", []) if isinstance(body, dict) else []
    for row in rows:
        if isinstance(row, dict) and row.get("request_id") == call_id and row.get("spend") is not None:
            return Decimal(str(row["spend"]))
    return None


class KeyInfo(dict):
    """{"spend_usd", "budget_usd", "budget_reset_at", "fetched_at"}"""


async def key_info(user_id: int, *, refresh: bool = False) -> KeyInfo | None:
    """Spend and budget of the user's key, cached 5 minutes. Looked up by alias, so no key travels in a URL."""
    cached = _info_cache.get(user_id)
    if cached and not refresh and time.monotonic() - cached[0] < INFO_TTL:
        return cached[1]
    body = await _admin("GET", "/key/list", params={"key_alias": alias(user_id), "return_full_object": "true"})
    keys = [k for k in (body or {}).get("keys", []) if isinstance(k, dict) and k.get("key_alias") == alias(user_id)]
    if not keys:
        return None
    k = keys[0]
    info = KeyInfo(
        spend_usd=round(float(k.get("spend") or 0), 4),
        budget_usd=k.get("max_budget"),
        budget_reset_at=k.get("budget_reset_at"),
        fetched_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    _info_cache[user_id] = (time.monotonic(), info)
    return info


async def check_gateway() -> list[str]:
    """`mwb llm check`: create, read and delete a throwaway key against the real gateway; returns problems."""
    problems: list[str] = []
    probe = {"key_alias": "mwb-check", "models": MODELS, "max_budget": 0.01, "budget_duration": "30d"}
    try:
        body = await _admin("POST", "/key/generate", json=probe)
        if not (isinstance(body, dict) and body.get("key")):
            problems.append("/key/generate returned no key")
        listed = await _admin("GET", "/key/list", params={"key_alias": "mwb-check", "return_full_object": "true"})
        if not any(k.get("key_alias") == "mwb-check" for k in (listed or {}).get("keys", []) if isinstance(k, dict)):
            problems.append("/key/list?key_alias= did not return the new key with full objects")
        await _admin("POST", "/key/delete", json={"key_aliases": ["mwb-check"]})
    except GatewayAdminError as exc:
        problems.append(str(exc))
    return problems

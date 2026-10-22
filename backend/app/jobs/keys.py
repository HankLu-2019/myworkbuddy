"""The job's own LLM key (design 04 §5.1, §7): the only secret a sandbox ever holds.

Alias `mwb-job-<id>`, models `coder` and `fast`, a budget of min(MWB_JOB_BUDGET_USD, the owner's remaining
budget) and a lifetime of timeout × attempts × 1.5 + 30 minutes. Stored encrypted in `job_secrets`; deleted (at
the gateway and here) when the job leaves an active state. Network calls run with no transaction open (R1).
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select

from app.config import get_settings
from app.db import get_sessionmaker
from app.llm import keys as llm_keys
from app.models import JobSecret
from app.secrets.service import decrypt, encrypt

MODELS = ["coder", "fast"]


def alias(job_id: uuid.UUID) -> str:
    return f"mwb-job-{job_id}"


def lifetime_minutes(policy: dict[str, Any]) -> int:
    return int(int(policy.get("timeout_min", 45)) * int(policy.get("max_attempts", 3)) * 1.5 + 30)


async def remaining_budget(user_id: int) -> Decimal | None:
    """The owner's remaining monthly budget, or None if the gateway can't say (then only the job cap applies)."""
    info = await llm_keys.key_info(user_id, refresh=True)
    if not info or info.get("budget_usd") is None:
        return None
    return max(Decimal(str(info["budget_usd"])) - Decimal(str(info["spend_usd"])), Decimal(0))


async def create(job_id: uuid.UUID, user_id: int, policy: dict[str, Any]) -> Decimal:
    """Create the key and store it; returns its budget. Raises GatewayAdminError if the gateway refuses."""
    cap = Decimal(str(get_settings().job_budget_usd))
    left = await remaining_budget(user_id)
    budget = min(cap, left) if left is not None else cap
    body = await llm_keys._admin(
        "POST",
        "/key/generate",
        json={
            "key_alias": alias(job_id),
            "user_id": f"mwb:{user_id}",
            "models": MODELS,
            "max_budget": float(budget),
            "duration": f"{lifetime_minutes(policy)}m",
            "metadata": {"mwb_user": user_id, "mwb_job": str(job_id)},
        },
    )
    key = body.get("key") if isinstance(body, dict) else None
    if not key:
        raise llm_keys.GatewayAdminError("gateway /key/generate returned no key")
    async with get_sessionmaker()() as db:
        db.add(JobSecret(job_id=job_id, ciphertext=encrypt(key)))
        await db.commit()
    return budget


async def get(job_id: uuid.UUID) -> str | None:
    async with get_sessionmaker()() as db:
        row = await db.scalar(select(JobSecret.ciphertext).where(JobSecret.job_id == job_id))
    return decrypt(row) if row else None


async def spend(job_id: uuid.UUID) -> Decimal | None:
    """What the job's key has spent so far (the gateway's figure)."""
    body = await llm_keys._admin("GET", "/key/list", params={"key_alias": alias(job_id), "return_full_object": "true"})
    for k in (body or {}).get("keys", []):
        if isinstance(k, dict) and k.get("key_alias") == alias(job_id):
            return Decimal(str(k.get("spend") or 0))
    return None


async def remove(job_id: uuid.UUID) -> None:
    """Delete the key at the gateway (by alias: no secret needed) and the stored copy. Safe to repeat."""
    await llm_keys._admin("POST", "/key/delete", json={"key_aliases": [alias(job_id)]})
    async with get_sessionmaker()() as db:
        await db.execute(delete(JobSecret).where(JobSecret.job_id == job_id))
        await db.commit()

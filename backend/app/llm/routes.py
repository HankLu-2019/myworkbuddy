"""AI spend (design 01 §H12): your own in Settings, everyone's on the admin Users page. Cached 5 minutes."""

import asyncio

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from app.deps import DB, AdminAuth, CurrentAuth
from app.llm import keys
from app.logs import log
from app.models import User

router = APIRouter(prefix="/api", tags=["usage"])


class UsageOut(BaseModel):
    user_id: int
    spend_usd: float | None  # None: the gateway couldn't be asked
    budget_usd: float
    budget_reset_at: str | None
    fetched_at: str | None


async def _usage(user: User) -> UsageOut:
    budget = float(keys.budget_for(user))
    try:
        info = await keys.key_info(user.id)
    except Exception as exc:
        log.warning("llm.usage_unavailable", user_id=user.id, error=str(exc)[:200])
        info = None
    return UsageOut(
        user_id=user.id,
        spend_usd=info["spend_usd"] if info else None,
        budget_usd=budget,
        budget_reset_at=info["budget_reset_at"] if info else None,
        fetched_at=info["fetched_at"] if info else None,
    )


@router.get("/usage", response_model=UsageOut)
async def my_usage(auth: CurrentAuth, db: DB) -> UsageOut:
    await db.commit()  # rule R1: the lookup below is a network call
    return await _usage(auth.user)


@router.get("/admin/usage", response_model=list[UsageOut])
async def all_usage(auth: AdminAuth, db: DB) -> list[UsageOut]:
    users = list(await db.scalars(select(User).where(User.is_active).order_by(User.username)))
    await db.commit()
    return list(await asyncio.gather(*(_usage(u) for u in users)))

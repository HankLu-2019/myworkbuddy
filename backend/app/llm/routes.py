"""AI spend (design 01 §H12): your own in Settings, everyone's on the admin Users page. Cached 5 minutes."""

import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from app.config import get_settings
from app.deps import DB, AdminAuth, CurrentAuth
from app.llm import keys
from app.logs import log
from app.models import ChatSession, Message, User

router = APIRouter(prefix="/api", tags=["usage"])


class UsageOut(BaseModel):
    user_id: int
    spend_usd: float | None  # None: the gateway couldn't be asked
    budget_usd: float
    budget_reset_at: str | None
    fetched_at: str | None
    # Your own usage only: what chat replies cost this calendar month in your timezone (07-ux §4.12, Usage).
    # Schedules and jobs join it in Phases 2–4.
    chat_usd_month: float | None = None


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


def month_start(user: User, now: datetime | None = None) -> datetime:
    """Start of the current month in the user's timezone (rule R8), as a UTC instant."""
    try:
        tz = ZoneInfo(user.timezone or get_settings().default_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = UTC
    local = (now or datetime.now(UTC)).astimezone(tz)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


@router.get("/usage", response_model=UsageOut)
async def my_usage(auth: CurrentAuth, db: DB) -> UsageOut:
    chat = await db.scalar(
        select(func.coalesce(func.sum(Message.cost_usd), 0))
        .join(ChatSession, ChatSession.id == Message.session_id)
        .where(ChatSession.owner_id == auth.user.id, Message.created_at >= month_start(auth.user))
    )
    await db.commit()  # rule R1: the lookup below is a network call
    out = await _usage(auth.user)
    return out.model_copy(update={"chat_usd_month": round(float(chat or 0), 4)})


@router.get("/admin/usage", response_model=list[UsageOut])
async def all_usage(auth: AdminAuth, db: DB) -> list[UsageOut]:
    users = list(await db.scalars(select(User).where(User.is_active).order_by(User.username)))
    await db.commit()
    return list(await asyncio.gather(*(_usage(u) for u in users)))

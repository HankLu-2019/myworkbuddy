from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.logs import log
from app.models import AuditLog


async def audit(
    db: AsyncSession,
    action: str,
    *,
    user_id: int | None,
    target_type: str | None = None,
    target_id: str | int | None = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            detail=detail or {},
            ip=ip,
        )
    )
    log.info("audit", action=action, user_id=user_id, target_type=target_type, target_id=target_id)

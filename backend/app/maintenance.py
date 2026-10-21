"""Hourly housekeeping (design 01 §H11). Each step is a short statement in its own transaction."""

import shutil
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_

from app.auth.service import session_alive_after
from app.config import get_settings
from app.db import get_sessionmaker
from app.logs import log
from app.models import AuditLog, AuthSession, LoginAttempt

LOGIN_ATTEMPTS_KEEP = timedelta(days=1)
AUDIT_KEEP = timedelta(days=400)
JOBS_KEEP_HOURS = 30 * 24
TMP_KEEP_SECONDS = 24 * 3600


async def prune_database(now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(UTC)
    counts: dict[str, int] = {}
    async with get_sessionmaker()() as db:
        steps = {
            "login_attempts": delete(LoginAttempt).where(LoginAttempt.at < now - LOGIN_ATTEMPTS_KEEP),
            "sessions": delete(AuthSession).where(
                or_(AuthSession.expires_at <= now, AuthSession.last_seen_at <= session_alive_after(now))
            ),
            "audit_rows": delete(AuditLog).where(AuditLog.at < now - AUDIT_KEEP),
        }
        for name, stmt in steps.items():
            counts[name] = (await db.execute(stmt)).rowcount
            await db.commit()
    return counts


def prune_tmp(now: float | None = None) -> int:
    """Remove entries in /data/tmp older than a day (abandoned uploads, scratch checkouts)."""
    root = get_settings().data_dir / "tmp"
    if not root.exists():
        return 0
    cutoff = (now or time.time()) - TMP_KEEP_SECONDS
    removed = 0
    for entry in root.iterdir():
        try:
            if entry.lstat().st_mtime >= cutoff:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            removed += 1
        except OSError as exc:
            log.warning("maintenance.tmp_failed", path=str(entry), error=str(exc))
    return removed


def remove_user_files(user_id: int) -> list[str]:
    """A deleted user's private wiki repo (public pages stay in the shared repo)."""
    removed = []
    for path in [get_settings().wiki_dir / "users" / str(user_id)]:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            removed.append(str(path))
    return removed

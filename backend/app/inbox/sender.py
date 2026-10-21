"""Sending the email outbox (design 02 §5), run by the `send_emails` periodic task.

1. Short transaction: lock up to 50 due `pending` rows (FOR UPDATE SKIP LOCKED). Rows over the owner's daily
   cap (`MWB_MAX_EMAILS_PER_DAY`, counted in the owner's timezone) become `suppressed`, with one inbox notice per
   user per day. The rest are claimed: `attempts += 1` and `next_attempt_at` moves past a lease, so a worker that
   dies mid-send retries later instead of never.
2. No transaction: send each claimed row over SMTP (rule R1).
3. One short transaction per row: `sent`, or back to the backoff (1, 5, 15, 60, 240 min); after 6 attempts, or
   on a permanent SMTP refusal (5xx), `failed` with an inbox `system` item.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid, parseaddr
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosmtplib
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_sessionmaker
from app.enums import EmailStatus
from app.inbox.email import app_url
from app.inbox.service import add_item
from app.logs import log
from app.models import InboxItem, OutboundEmail, User

BATCH = 50
BACKOFF_MIN = (1, 5, 15, 60, 240)
MAX_ATTEMPTS = len(BACKOFF_MIN) + 1
LEASE = timedelta(minutes=10)
CAP_TITLE = "Email paused for today"
FAILED_TITLE = "An email couldn't be delivered"


def now() -> datetime:  # patched in tests
    return datetime.now(UTC)


@dataclass
class Claimed:
    id: int
    to_addr: str
    subject: str
    body_text: str
    dedupe_key: str
    attempts: int


@dataclass
class SendResult:
    sent: int = 0
    retrying: int = 0
    failed: int = 0
    suppressed: int = 0
    skipped: str | None = None  # why nothing was sent


class PermanentError(Exception):
    pass


def _local_midnight(user: User | None, at: datetime) -> datetime:
    try:
        tz = ZoneInfo(user.timezone) if user and user.timezone else ZoneInfo(get_settings().default_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    local = at.astimezone(tz)
    return datetime.combine(local.date(), time(0), tz).astimezone(UTC)


async def _sent_today(db: AsyncSession, owner_id: int, since: datetime) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(OutboundEmail)
            .where(
                OutboundEmail.owner_id == owner_id,
                OutboundEmail.status == EmailStatus.sent,
                OutboundEmail.sent_at >= since,
            )
        )
        or 0
    )


async def _cap_notice(db: AsyncSession, user: User, since: datetime) -> None:
    """One inbox notice per user per local day."""
    exists = await db.scalar(
        select(InboxItem.id).where(
            InboxItem.owner_id == user.id, InboxItem.title == CAP_TITLE, InboxItem.created_at >= since
        )
    )
    if exists is None:
        cap = get_settings().max_emails_per_day
        add_item(
            db, user.id, kind="system", severity="warning", title=CAP_TITLE,
            body_md=f"You've had {cap} emails today, the daily limit. Further notifications today are in your inbox "
            "only; email resumes tomorrow.",
            link="/inbox",
        )  # fmt: skip


async def claim(at: datetime) -> tuple[list[Claimed], int]:
    """Step 1. Returns the claimed rows and how many were suppressed."""
    cap = get_settings().max_emails_per_day
    claimed: list[Claimed] = []
    suppressed = 0
    async with get_sessionmaker()() as db:
        rows = list(
            await db.scalars(
                select(OutboundEmail)
                .where(OutboundEmail.status == EmailStatus.pending, OutboundEmail.next_attempt_at <= at)
                .order_by(OutboundEmail.next_attempt_at, OutboundEmail.id)
                .limit(BATCH)
                .with_for_update(skip_locked=True)
            )
        )
        counts: dict[int, int] = {}
        for row in rows:
            if row.owner_id is not None:
                user = await db.get(User, row.owner_id)
                since = _local_midnight(user, at)
                if row.owner_id not in counts:
                    counts[row.owner_id] = await _sent_today(db, row.owner_id, since)
                if counts[row.owner_id] >= cap:
                    row.status, row.last_error = EmailStatus.suppressed, f"Daily limit of {cap} emails reached"
                    suppressed += 1
                    if user is not None:
                        await _cap_notice(db, user, since)
                    continue
                counts[row.owner_id] += 1
            row.attempts += 1
            row.next_attempt_at = at + LEASE
            claimed.append(Claimed(row.id, row.to_addr, row.subject, row.body_text, row.dedupe_key, row.attempts))
        await db.commit()
    return claimed, suppressed


def build_message(c: Claimed) -> EmailMessage:
    s = get_settings()
    msg = EmailMessage()
    msg["From"] = s.smtp_from
    msg["To"] = c.to_addr
    msg["Subject"] = c.subject
    msg["Date"] = format_datetime(now())
    domain = parseaddr(s.smtp_from)[1].rpartition("@")[2] or "myworkbuddy"
    msg["Message-ID"] = make_msgid(idstring=f"mwb{c.id}", domain=domain)
    msg["Auto-Submitted"] = "auto-generated"
    msg["List-Unsubscribe"] = f"<{app_url('/settings/notifications')}>"
    if c.dedupe_key.startswith("run:"):
        msg["X-MWB-Run"] = c.dedupe_key.removeprefix("run:")
    msg.set_content(c.body_text)  # plain text only (02 §5)
    return msg


async def deliver(msg: EmailMessage) -> None:
    """Raises PermanentError for a 5xx refusal; anything else that fails is worth retrying."""
    s = get_settings()
    password = s.smtp_password.get_secret_value()
    try:
        await aiosmtplib.send(
            msg,
            hostname=s.smtp_host,
            port=s.smtp_port,
            start_tls=s.smtp_starttls,
            username=s.smtp_user or None,
            password=password or None,
            timeout=s.smtp_timeout_s,
        )
    except aiosmtplib.SMTPRecipientsRefused as exc:
        codes = [r.code for r in exc.recipients]
        if codes and all(500 <= c < 600 for c in codes):
            raise PermanentError(f"The mail server refused the address ({codes[0]})") from None
        raise
    except aiosmtplib.SMTPResponseException as exc:
        if 500 <= exc.code < 600 and exc.code not in (530, 535):  # auth problems are fixable config, not permanent
            raise PermanentError(f"The mail server refused the email ({exc.code})") from None
        raise


def _describe(exc: BaseException) -> str:
    """Errors from the SMTP library, without anything that could carry the password (rule R7)."""
    if isinstance(exc, PermanentError):
        return str(exc)
    if isinstance(exc, aiosmtplib.SMTPResponseException):
        return f"SMTP {exc.code}: {exc.message[:200]}"
    return type(exc).__name__


async def _record(c: Claimed, error: BaseException | None, at: datetime) -> str:
    async with get_sessionmaker()() as db:
        row = await db.scalar(select(OutboundEmail).where(OutboundEmail.id == c.id).with_for_update())
        if row is None or row.status != EmailStatus.pending:
            return "gone"
        if error is None:
            row.status, row.sent_at, row.last_error = EmailStatus.sent, at, None
            outcome = "sent"
        elif isinstance(error, PermanentError) or c.attempts >= MAX_ATTEMPTS:
            row.status, row.last_error = EmailStatus.failed, _describe(error)
            outcome = "failed"
            if row.owner_id is not None:
                add_item(
                    db, row.owner_id, kind="system", severity="warning", title=FAILED_TITLE,
                    body_md=f"“{row.subject}” to {row.to_addr} wasn't delivered: {row.last_error}. It's still in "
                    "your inbox; check your email address in Settings.",
                    link="/settings/profile",
                )  # fmt: skip
        else:
            row.last_error = _describe(error)
            row.next_attempt_at = at + timedelta(minutes=BACKOFF_MIN[c.attempts - 1])
            outcome = "retrying"
        await db.commit()
        return outcome


async def send_pending() -> SendResult:
    result = SendResult()
    if not get_settings().smtp_host:
        result.skipped = "MWB_SMTP_HOST is not set"
        return result
    claimed, result.suppressed = await claim(now())
    for c in claimed:
        error: BaseException | None = None
        try:
            await deliver(build_message(c))
        except Exception as exc:  # noqa: BLE001 - every failure is recorded on the row
            error = exc
            log.warning("email.send_failed", email_id=c.id, attempt=c.attempts, error=_describe(exc))
        outcome = await _record(c, error, now())
        if outcome in ("sent", "retrying", "failed"):
            setattr(result, outcome, getattr(result, outcome) + 1)
    if claimed or result.suppressed:
        log.info("email.batch", **{k: v for k, v in vars(result).items() if v})
    return result


async def outbox_status(db: AsyncSession) -> dict[str, object]:
    """For Admin → System: what's waiting and what went wrong lately."""
    since = now() - timedelta(hours=24)
    counts = {
        st: int(n)
        for st, n in await db.execute(
            select(OutboundEmail.status, func.count())
            .where((OutboundEmail.status == EmailStatus.pending) | (OutboundEmail.created_at >= since))
            .group_by(OutboundEmail.status)
        )
    }
    recent_failures = [
        {"id": i, "to": to, "subject": subj, "error": err, "at": at.isoformat()}
        for i, to, subj, err, at in await db.execute(
            select(
                OutboundEmail.id,
                OutboundEmail.to_addr,
                OutboundEmail.subject,
                OutboundEmail.last_error,
                OutboundEmail.created_at,
            )  # fmt: skip
            .where(OutboundEmail.status == EmailStatus.failed, OutboundEmail.created_at >= since)
            .order_by(OutboundEmail.id.desc())
            .limit(20)
        )
    ]
    return {
        "configured": bool(get_settings().smtp_host),
        "pending": counts.get("pending", 0),
        "sent_24h": counts.get("sent", 0),
        "failed_24h": counts.get("failed", 0),
        "suppressed_24h": counts.get("suppressed", 0),
        "recent_failures": recent_failures,
    }

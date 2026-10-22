"""Slack and Teams notifications (design 08 §F8), run by the `send_messages` periodic task.

1. `enqueue()`, one short transaction: for each enabled channel, the owner's inbox items after the channel's
   cursor that pass its filter (kinds, minimum severity) become `outbound_messages` rows (deduplicated per item and
   channel), and the cursor moves on. A new channel starts at the newest item, so old ones aren't replayed.
2. `send_pending()`: claim due rows (lease, `FOR UPDATE SKIP LOCKED`), suppress those past the owner's daily cap
   (`MWB_MAX_MESSAGES_PER_DAY`), then POST each with no transaction open (rule R1), then record the outcome in a
   short transaction: `sent`; a retry after 1, 5, 15, 60 minutes on 429/5xx/transport errors (5 attempts); or, when
   the webhook answers 401/403/404/410, the channel is disabled and the owner gets one inbox item.

The webhook URL is a secret (rule R7): stored encrypted, never logged, never returned (`url_hint` instead), and
errors are reported by status code only, because transport exceptions include the URL. Titles and details can carry
Jira or log text, so Slack's control characters (`<`, `>`, `&`) are escaped and Markdown links are broken, so a
message can't mention @channel or show a link other than ours.
"""

import fnmatch
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx2
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.config import get_settings
from app.db import get_sessionmaker
from app.enums import EmailStatus
from app.inbox.email import app_url
from app.inbox.sender import _local_midnight
from app.inbox.service import add_item
from app.integrations import http
from app.logs import log
from app.models import InboxItem, NotifyChannel, OutboundMessage, User
from app.secrets.service import decrypt

BATCH = 50
BACKOFF_MIN = (1, 5, 15, 60)
MAX_ATTEMPTS = len(BACKOFF_MIN) + 1
LEASE = timedelta(minutes=5)
SEVERITY_ORDER = {"info": 0, "warning": 1, "alert": 2}
GONE = {401, 403, 404, 410}
DETAIL_CHARS = 500
CAP_TITLE = "Chat notifications paused for today"
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def now() -> datetime:  # patched in tests
    return datetime.now(UTC)


class BadWebhook(ValueError):
    pass


def check_url(url: str) -> str:
    """An https URL whose host matches `MWB_NOTIFY_WEBHOOK_HOSTS`; returns the hint shown instead of it."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    patterns = [p.strip().lower() for p in get_settings().notify_webhook_hosts.split(",") if p.strip()]
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise BadWebhook("Use the https:// webhook URL the chat app gave you.")
    if not any(fnmatch.fnmatch(host, p) for p in patterns):
        raise BadWebhook(f"{host} isn't an allowed webhook host. Allowed: {', '.join(patterns)}.")
    return f"{host}/…{url.strip()[-4:]}"


def _slack(text: str) -> str:
    return CONTROL.sub("", text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _teams(text: str) -> str:
    return CONTROL.sub("", text).replace("](", "] (").replace("<", "‹").replace(">", "›")


def payload(kind: str, title: str, severity: str, link: str, details: str | None) -> dict[str, Any]:
    url = app_url(link)
    mark = {"alert": "🔴", "warning": "🟠"}.get(severity, "🔵")
    if kind == "slack":
        text = f"{mark} *{_slack(title)}*\n<{url}|Open in MyWorkBuddy>"
        if details:
            text += f"\n{_slack(details)}"
        return {"text": text}
    body: list[dict[str, Any]] = [
        {"type": "TextBlock", "text": f"{mark} {_teams(title)}", "weight": "Bolder", "wrap": True}
    ]
    if details:
        body.append({"type": "TextBlock", "text": _teams(details), "wrap": True})
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
        "body": body, "actions": [{"type": "Action.OpenUrl", "title": "Open in MyWorkBuddy", "url": url}],
    }  # fmt: skip
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                                                "content": card}]}  # fmt: skip


async def post(url: str, body: dict[str, Any]) -> tuple[int | None, str | None]:
    """(status, error), neither carrying the URL."""
    parsed = urlparse(url)
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    try:
        async with http.client(f"{parsed.scheme}://{parsed.netloc}") as c:
            code, _ = await http.request(c, "POST", path, json=body, limit=64 * 1024)
    except TimeoutError:
        return None, "timed out"
    except (httpx2.TransportError, http.ResponseTooLarge) as exc:
        return None, f"couldn't be reached ({type(exc).__name__})"
    if code >= 300:
        return code, f"answered {code}"
    return code, None


@dataclass
class Result:
    queued: int = 0
    sent: int = 0
    retrying: int = 0
    failed: int = 0
    suppressed: int = 0
    disabled: int = 0


async def enqueue() -> int:
    queued = 0
    async with get_sessionmaker()() as db:
        channels = list(
            await db.scalars(select(NotifyChannel).where(NotifyChannel.enabled).with_for_update(skip_locked=True))
        )
        for ch in channels:
            items = list(
                await db.scalars(
                    select(InboxItem)
                    .where(InboxItem.owner_id == ch.owner_id, InboxItem.id > ch.last_item_id)
                    .order_by(InboxItem.id)
                    .limit(200)
                )
            )
            if not items:
                continue
            ch.last_item_id = items[-1].id
            floor = SEVERITY_ORDER.get(ch.min_severity, 1)
            for it in items:
                if (ch.kinds and it.kind not in ch.kinds) or SEVERITY_ORDER.get(it.severity, 0) < floor:
                    continue
                details = (it.body_md or "")[:DETAIL_CHARS] if ch.include_details else None
                result = await db.execute(
                    insert(OutboundMessage).values(
                        owner_id=ch.owner_id, channel_id=ch.id, inbox_item_id=it.id, dedupe_key=f"{ch.id}:{it.id}",
                        title=it.title, severity=it.severity, link=it.link or "/inbox", details=details or None,
                        next_attempt_at=now(),
                    ).on_conflict_do_nothing(index_elements=["dedupe_key"])
                )  # fmt: skip
                queued += result.rowcount or 0
        await db.commit()
    return queued


async def _sent_today(db, owner_id: int, since: datetime) -> int:
    return int(await db.scalar(
        select(func.count()).select_from(OutboundMessage)
        .where(OutboundMessage.owner_id == owner_id, OutboundMessage.status == EmailStatus.sent,
               OutboundMessage.sent_at >= since)
    ) or 0)  # fmt: skip


async def _cap_notice(db, owner_id: int, since: datetime, cap: int) -> None:
    """One inbox notice per user per local day."""
    exists = await db.scalar(select(InboxItem.id).where(
        InboxItem.owner_id == owner_id, InboxItem.title == CAP_TITLE, InboxItem.created_at >= since))  # fmt: skip
    if exists is None:
        add_item(db, owner_id, kind="system", severity="info", title=CAP_TITLE,
                 body_md=f"You've had {cap} chat notifications today, the daily limit. The rest of today's are in your "
                 "inbox only.", link="/inbox")  # fmt: skip


async def send_pending() -> Result:
    out = Result(queued=await enqueue())
    cap = get_settings().max_messages_per_day
    claimed: list[tuple[int, int, str, str, dict[str, Any], int]] = []
    async with get_sessionmaker()() as db:
        rows = list(
            await db.scalars(
                select(OutboundMessage)
                .where(OutboundMessage.status == EmailStatus.pending, OutboundMessage.next_attempt_at <= now())
                .order_by(OutboundMessage.id)
                .limit(BATCH)
                .with_for_update(skip_locked=True)
            )
        )
        counts: dict[int, int] = {}
        for m in rows:
            ch = await db.get(NotifyChannel, m.channel_id)
            if ch is None or not ch.enabled:
                m.status, m.last_error = EmailStatus.failed, "the channel is disabled"
                out.failed += 1
                continue
            since = _local_midnight(await db.get(User, m.owner_id), now())
            if m.owner_id not in counts:
                counts[m.owner_id] = await _sent_today(db, m.owner_id, since)
            if counts[m.owner_id] >= cap:
                m.status, m.last_error = EmailStatus.suppressed, f"over the daily limit of {cap}"
                await _cap_notice(db, m.owner_id, since, cap)
                out.suppressed += 1
                continue
            counts[m.owner_id] += 1
            url = decrypt(ch.url_enc)
            if url is None:
                m.status, m.last_error = EmailStatus.failed, "the webhook URL can't be read"
                out.failed += 1
                continue
            m.attempts += 1
            m.next_attempt_at = now() + LEASE
            body = payload(ch.kind, m.title, m.severity, m.link, m.details)
            claimed.append((m.id, ch.id, ch.name, url, body, m.attempts))
        await db.commit()
    for msg_id, channel_id, name, url, body, attempts in claimed:
        status, error = await post(url, body)  # network: nothing open
        async with get_sessionmaker()() as db:
            m = await db.scalar(select(OutboundMessage).where(OutboundMessage.id == msg_id).with_for_update())
            if m is None or m.status != EmailStatus.pending:  # the channel was deleted meanwhile
                continue
            if error is None:
                m.status, m.sent_at, m.last_error = EmailStatus.sent, now(), None
                out.sent += 1
            elif status in GONE:
                m.status, m.last_error = EmailStatus.failed, error
                ch = await db.get(NotifyChannel, channel_id)
                if ch is not None and ch.enabled:
                    ch.enabled, ch.disabled_reason, ch.updated_at = False, f"The webhook {error}.", now()
                    add_item(db, m.owner_id, kind="system", severity="warning",
                             title=f"Notifications to {name} were turned off",
                             body_md=f"The webhook {error}. Add a new one in Settings → Notifications.",
                             link="/settings/notifications")  # fmt: skip
                    out.disabled += 1
                out.failed += 1
            elif attempts >= MAX_ATTEMPTS:
                m.status, m.last_error = EmailStatus.failed, error
                out.failed += 1
            else:
                m.last_error = error
                m.next_attempt_at = now() + timedelta(minutes=BACKOFF_MIN[min(attempts - 1, len(BACKOFF_MIN) - 1)])
                out.retrying += 1
            await db.commit()
        log.info("chat.sent" if error is None else "chat.send_failed", channel_id=channel_id, status=status)
    return out


async def start_cursor(db, owner_id: int) -> int:
    return int(await db.scalar(select(func.coalesce(func.max(InboxItem.id), 0)).where(InboxItem.owner_id == owner_id)))

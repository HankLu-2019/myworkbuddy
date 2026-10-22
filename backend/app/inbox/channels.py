"""Chat notification channels API (design 08 §F8): the caller's own Slack and Teams webhooks.

The URL is write-only: it is checked against `MWB_NOTIFY_WEBHOOK_HOSTS`, encrypted, and answered with a hint. Every
change is audited (rule R3); **Send test** posts outside any transaction (rule R1) and records the outcome.
"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.audit.service import audit
from app.deps import DB, CurrentAuth, client_ip
from app.errors import AppError
from app.inbox import chat
from app.models import NotifyChannel
from app.secrets.service import decrypt, encrypt

router = APIRouter(prefix="/api/notify-channels", tags=["inbox"])

MAX_CHANNELS = 10
ChannelKindL = Literal["slack", "teams"]
SeverityL = Literal["info", "warning", "alert"]
InboxKindL = Literal["run", "alert", "recovered", "system", "credential", "proposal", "plan", "job"]


class ChannelOut(BaseModel):
    id: int
    kind: ChannelKindL
    name: str
    url_hint: str
    kinds: list[InboxKindL]
    min_severity: SeverityL
    include_details: bool
    enabled: bool
    disabled_reason: str | None
    created_at: datetime


class ChannelIn(BaseModel):
    kind: ChannelKindL
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=12, max_length=1000)
    kinds: list[InboxKindL] = Field([], max_length=8)
    min_severity: SeverityL = "warning"
    include_details: bool = False


class ChannelPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    kinds: list[InboxKindL] | None = Field(None, max_length=8)
    min_severity: SeverityL | None = None
    include_details: bool | None = None
    enabled: bool | None = None


class ChannelTestResult(BaseModel):
    ok: bool
    status: int | None
    error: str | None


def _out(ch: NotifyChannel) -> ChannelOut:
    return ChannelOut.model_validate(ch, from_attributes=True)


async def _own(db, auth, channel_id: int) -> NotifyChannel:
    ch = await db.get(NotifyChannel, channel_id)
    if ch is None or ch.owner_id != auth.user.id:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "Channel not found")
    return ch


@router.get("", response_model=list[ChannelOut])
async def list_channels(auth: CurrentAuth, db: DB) -> list[ChannelOut]:
    rows = await db.scalars(
        select(NotifyChannel).where(NotifyChannel.owner_id == auth.user.id).order_by(NotifyChannel.id)
    )
    return [_out(ch) for ch in rows]


@router.post("", response_model=ChannelOut, status_code=201)
async def create_channel(body: ChannelIn, request: Request, auth: CurrentAuth, db: DB) -> ChannelOut:
    try:
        hint = chat.check_url(body.url)
    except chat.BadWebhook as exc:
        raise AppError(status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error", str(exc), field="url") from None
    count = await db.scalar(
        select(func.count()).select_from(NotifyChannel).where(NotifyChannel.owner_id == auth.user.id)
    )
    if (count or 0) >= MAX_CHANNELS:
        raise AppError(status.HTTP_409_CONFLICT, "conflict", f"You can have up to {MAX_CHANNELS} channels.")
    ch = NotifyChannel(
        owner_id=auth.user.id, kind=body.kind, name=body.name.strip(), url_enc=encrypt(body.url.strip()),
        url_hint=hint, kinds=sorted(set(body.kinds)), min_severity=body.min_severity,
        include_details=body.include_details, last_item_id=await chat.start_cursor(db, auth.user.id),
    )  # fmt: skip
    db.add(ch)
    await db.flush()
    await audit(db, "notify_channel.create", user_id=auth.user.id, target_type="notify_channel", target_id=ch.id,
                detail={"kind": ch.kind, "host": hint.split("/")[0]}, ip=client_ip(request))  # fmt: skip
    await db.commit()
    await db.refresh(ch)
    return _out(ch)


@router.patch("/{channel_id}", response_model=ChannelOut)
async def update_channel(
    channel_id: int, body: ChannelPatch, request: Request, auth: CurrentAuth, db: DB
) -> ChannelOut:
    ch = await _own(db, auth, channel_id)
    changes = body.model_dump(exclude_none=True)
    if "name" in changes:
        changes["name"] = changes["name"].strip()
    if "kinds" in changes:
        changes["kinds"] = sorted(set(changes["kinds"]))
    if changes.get("enabled") and not ch.enabled:  # turned back on: start from now, don't replay what was missed
        ch.last_item_id = await chat.start_cursor(db, auth.user.id)
        ch.disabled_reason = None
    for key, value in changes.items():
        setattr(ch, key, value)
    ch.updated_at = func.now()
    await audit(db, "notify_channel.update", user_id=auth.user.id, target_type="notify_channel", target_id=ch.id,
                detail={"fields": sorted(changes)}, ip=client_ip(request))  # fmt: skip
    await db.commit()
    await db.refresh(ch)
    return _out(ch)


@router.delete("/{channel_id}", status_code=204)
async def delete_channel(channel_id: int, request: Request, auth: CurrentAuth, db: DB) -> None:
    ch = await _own(db, auth, channel_id)
    await db.delete(ch)
    await audit(db, "notify_channel.delete", user_id=auth.user.id, target_type="notify_channel", target_id=channel_id,
                ip=client_ip(request))  # fmt: skip
    await db.commit()


@router.post("/{channel_id}/test", response_model=ChannelTestResult)
async def test_channel(channel_id: int, request: Request, auth: CurrentAuth, db: DB) -> ChannelTestResult:
    ch = await _own(db, auth, channel_id)
    url, kind = decrypt(ch.url_enc), ch.kind
    await db.commit()  # network next: nothing open (R1)
    if url is None:
        return ChannelTestResult(ok=False, status=None, error="The webhook URL can't be read. Add the channel again.")
    code, error = await chat.post(url, chat.payload(kind, "Test message from MyWorkBuddy", "info", "/inbox",
                                                    "Notifications will arrive here."))  # fmt: skip
    await audit(db, "notify_channel.test", user_id=auth.user.id, target_type="notify_channel", target_id=channel_id,
                detail={"ok": error is None, "status": code}, ip=client_ip(request))  # fmt: skip
    await db.commit()
    return ChannelTestResult(ok=error is None, status=code, error=f"The webhook {error}." if error else None)

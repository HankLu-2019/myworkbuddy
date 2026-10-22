"""Slack and Teams notifications (design 08 §F8; plan F8). Webhooks answered by an httpx2 MockTransport."""

import json
import logging
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from sqlalchemy import select, update
from structlog.testing import capture_logs

from app.config import get_settings
from app.db import get_sessionmaker
from app.inbox import chat
from app.inbox.service import add_item
from app.integrations import http
from app.models import AuditLog, InboxItem, NotifyChannel, OutboundMessage

SLACK = "https://hooks.slack.com/services/T000/B000/s3cr3tTOKENabcd"
TEAMS = "https://prod-01.westeurope.logic.azure.com/workflows/abc/triggers/manual/paths/invoke?sig=TEAMSSIGwxyz"
T0 = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


class Hooks:
    """Records posts; answers with queued status codes (default 200)."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.answers: list[int] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.posts.append((str(request.url), json.loads(request.content)))
        code = self.answers.pop(0) if self.answers else 200
        if code == 0:
            raise httpx2.ConnectError("connection refused", request=request)
        return httpx2.Response(code, text="ok")


@pytest.fixture
def hooks(monkeypatch):
    box = Hooks()
    http.set_transport(httpx2.MockTransport(box))
    s = get_settings()
    monkeypatch.setattr(s, "hostname", "https://mwb.example.com")
    monkeypatch.setattr(s, "max_messages_per_day", 50)
    clock = {"now": T0}
    monkeypatch.setattr(chat, "now", lambda: clock["now"])
    box.clock = clock  # type: ignore[attr-defined]
    yield box
    http.set_transport(None)


async def item(owner_id: int, title: str, *, kind: str = "job", severity: str = "warning", body: str = "") -> int:
    async with get_sessionmaker()() as db:
        it = add_item(db, owner_id, kind=kind, severity=severity, title=title, body_md=body, link="/jobs/1")
        await db.commit()
        return it.id


async def due() -> None:
    """Make every pending message due (the clock is fixed, the backoff isn't)."""
    async with get_sessionmaker()() as db:
        await db.execute(update(OutboundMessage).values(next_attempt_at=T0 - timedelta(seconds=1)))
        await db.commit()


async def messages() -> list[OutboundMessage]:
    async with get_sessionmaker()() as db:
        return list(await db.scalars(select(OutboundMessage).order_by(OutboundMessage.id)))


async def add_channel(c, url: str = SLACK, **kw) -> dict:
    body = {"kind": "slack", "name": "Team channel", "url": url} | kw
    r = await c.post("/api/notify-channels", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def test_a_channel_keeps_its_url_secret(client_for, hooks, caplog) -> None:
    caplog.set_level(logging.INFO)
    c = await client_for("alice")
    with capture_logs() as logs:
        out = await add_channel(c)
        listed = (await c.get("/api/notify-channels")).json()
        await item(c.user_id, "Job ready for review")
        await due()
        assert (await chat.send_pending()).sent == 1
    assert out["url_hint"] == "hooks.slack.com/…abcd" and "url" not in out
    assert "s3cr3t" not in json.dumps(listed) and "s3cr3t" not in json.dumps(logs) and "s3cr3t" not in caplog.text
    async with get_sessionmaker()() as db:
        ch = await db.get(NotifyChannel, out["id"])
        audits = list(await db.scalars(select(AuditLog).where(AuditLog.action.like("notify_channel.%"))))
    assert "s3cr3t" not in ch.url_enc and [a.action for a in audits] == ["notify_channel.create"]
    assert "s3cr3t" not in json.dumps([a.detail for a in audits])
    assert hooks.posts[0][0] == SLACK


async def test_only_allowed_https_webhook_hosts_are_accepted(client_for, hooks) -> None:
    c = await client_for("alice")
    for url in (
        "https://evil.example.com/services/x",
        "http://hooks.slack.com/services/T/B/x",
        "https://user:pw@hooks.slack.com/services/T/B/x",
        "https://hooks.slack.com.evil.com/services/x",
    ):
        r = await c.post("/api/notify-channels", json={"kind": "slack", "name": "x", "url": url})
        assert r.status_code == 422 and r.json()["field"] == "url", url
    await add_channel(c, TEAMS, kind="teams")
    bob = await client_for("bob")
    assert (await bob.get("/api/notify-channels")).json() == []
    [ch] = (await c.get("/api/notify-channels")).json()
    assert (await bob.patch(f"/api/notify-channels/{ch['id']}", json={"enabled": False})).status_code == 404
    assert (await bob.post(f"/api/notify-channels/{ch['id']}/test")).status_code == 404
    assert (await bob.delete(f"/api/notify-channels/{ch['id']}")).status_code == 404
    assert (await c.delete(f"/api/notify-channels/{ch['id']}")).status_code == 204


async def test_one_message_per_item_and_channel_matching_the_filter(client_for, hooks) -> None:
    c = await client_for("alice")
    await item(c.user_id, "Old item, before the channel existed")
    slack = await add_channel(c, kinds=["job"], min_severity="warning")
    await add_channel(c, TEAMS, kind="teams", name="Teams", min_severity="info", include_details=True)
    await item(c.user_id, "Job <!channel> ready", severity="warning", body="x" * 600)
    await item(c.user_id, "Plan ready", kind="plan", severity="info")
    await item(c.user_id, "Low job", severity="info")
    assert await chat.enqueue() == 4  # slack: the warning job; teams: all three
    assert await chat.enqueue() == 0  # the cursor moved; dedupe_key guards a replay
    result = await chat.send_pending()
    assert result.sent == 4 and len(hooks.posts) == 4
    slack_posts = [b for u, b in hooks.posts if u == SLACK]
    assert slack_posts == [
        {"text": "🟠 *Job &lt;!channel&gt; ready*\n<https://mwb.example.com/jobs/1|Open in MyWorkBuddy>"}
    ]
    teams = [b for u, b in hooks.posts if u == TEAMS]
    card = teams[0]["attachments"][0]["content"]
    assert card["actions"][0]["url"] == "https://mwb.example.com/jobs/1"
    assert card["body"][1]["text"] == "x" * 500  # details only where asked, capped
    assert all("Old item" not in json.dumps(b) for _, b in hooks.posts)
    assert (await chat.send_pending()).sent == 0  # nothing twice
    assert {m.channel_id for m in await messages()} == {slack["id"], slack["id"] + 1}


async def test_failures_retry_then_a_gone_webhook_turns_the_channel_off(client_for, hooks) -> None:
    c = await client_for("alice")
    ch = await add_channel(c)
    await item(c.user_id, "First")
    hooks.answers = [503, 0, 429, 500, 502]  # five failures: the last attempt gives up
    for _ in range(chat.MAX_ATTEMPTS):
        await due()
        await chat.send_pending()
    [m] = await messages()
    assert m.status == "failed" and m.attempts == chat.MAX_ATTEMPTS and m.last_error == "answered 502"
    hooks.answers = [404]
    await item(c.user_id, "Second")
    await due()
    assert (await chat.send_pending()).disabled == 1
    [listed] = (await c.get("/api/notify-channels")).json()
    assert listed["enabled"] is False and listed["disabled_reason"] == "The webhook answered 404."
    async with get_sessionmaker()() as db:
        notice = await db.scalar(
            select(InboxItem).where(InboxItem.title == "Notifications to Team channel were turned off")
        )
    assert notice is not None and notice.link == "/settings/notifications"
    await item(c.user_id, "While off")
    assert await chat.enqueue() == 0
    r = await c.patch(f"/api/notify-channels/{ch['id']}", json={"enabled": True})
    assert r.json()["enabled"] and r.json()["disabled_reason"] is None
    assert await chat.enqueue() == 0  # turning it back on doesn't replay what was missed
    await item(c.user_id, "Back on")
    assert await chat.enqueue() == 1


async def test_the_daily_limit_suppresses_and_notifies_once(client_for, hooks, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "max_messages_per_day", 2)
    c = await client_for("alice")
    await add_channel(c, min_severity="info")
    for n in range(4):
        await item(c.user_id, f"Item {n}")
    result = await chat.send_pending()
    assert (result.sent, result.suppressed) == (2, 2)
    async with get_sessionmaker()() as db:
        notices = list(await db.scalars(select(InboxItem).where(InboxItem.title == chat.CAP_TITLE)))
    assert len(notices) == 1
    await item(c.user_id, "Item 5")
    assert (await chat.send_pending()).suppressed >= 1
    async with get_sessionmaker()() as db:
        assert len(list(await db.scalars(select(InboxItem).where(InboxItem.title == chat.CAP_TITLE)))) == 1
    hooks.clock["now"] = T0 + timedelta(days=1)  # a new day
    await item(c.user_id, "Tomorrow")
    assert (await chat.send_pending()).sent == 1


async def test_send_test_posts_now_and_reports_the_answer(client_for, hooks) -> None:
    c = await client_for("alice")
    ch = await add_channel(c, TEAMS, kind="teams")
    r = await c.post(f"/api/notify-channels/{ch['id']}/test")
    assert r.status_code == 200 and r.json() == {"ok": True, "status": 200, "error": None}
    assert hooks.posts[0][0] == TEAMS and hooks.posts[0][1]["type"] == "message"
    hooks.answers = [403]
    r = await c.post(f"/api/notify-channels/{ch['id']}/test")
    assert r.json() == {"ok": False, "status": 403, "error": "The webhook answered 403."}
    hooks.answers = [0]
    r = await c.post(f"/api/notify-channels/{ch['id']}/test")
    assert r.json()["error"] == "The webhook couldn't be reached (ConnectError)." and "sig=" not in r.text
    async with get_sessionmaker()() as db:
        tests = list(await db.scalars(select(AuditLog).where(AuditLog.action == "notify_channel.test")))
    assert [t.detail["ok"] for t in tests] == [True, False, False]
    r = await c.patch(f"/api/notify-channels/{ch['id']}", json={"kinds": ["job", "job", "alert"], "name": " Ops "})
    assert r.json()["kinds"] == ["alert", "job"] and r.json()["name"] == "Ops"
    assert (await c.patch(f"/api/notify-channels/{ch['id']}", json={"kinds": ["nope"]})).status_code == 422

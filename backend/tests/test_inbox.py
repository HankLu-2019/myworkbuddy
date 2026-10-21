"""Inbox API and the email outbox sender (design 02 §5, §7; plan S5). A real SMTP server (aiosmtpd) on localhost."""

import socket
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from email.message import Message

import pytest
from aiosmtpd.controller import Controller
from sqlalchemy import func, select, update

from app.config import get_settings
from app.db import get_sessionmaker
from app.inbox import sender
from app.inbox.email import queue_email
from app.inbox.service import add_item
from app.models import InboxItem, OutboundEmail, User

T0 = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)  # 12:00 in Stockholm


class Mailbox:
    """Accepts mail, or answers with the queued SMTP replies first (e.g. '451 try later')."""

    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.replies: list[str] = []
        self.rcpt_replies: list[str] = []

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):  # noqa: N802
        if self.rcpt_replies:
            return self.rcpt_replies.pop(0)
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):  # noqa: N802
        if self.replies:
            return self.replies.pop(0)
        self.messages.append(message_from_bytes(envelope.content))
        return "250 OK"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def smtp(monkeypatch):
    box = Mailbox()
    port = _free_port()
    controller = Controller(box, hostname="127.0.0.1", port=port)
    controller.start()
    s = get_settings()
    for key, value in {
        "smtp_host": "127.0.0.1", "smtp_port": port, "smtp_starttls": False, "smtp_timeout_s": 5,
        "smtp_from": "MyWorkBuddy <mwb@example.com>", "hostname": "https://mwb.example.com", "max_emails_per_day": 3,
    }.items():  # fmt: skip
        monkeypatch.setattr(s, key, value)
    clock = {"now": T0}
    monkeypatch.setattr(sender, "now", lambda: clock["now"])
    box.clock = clock  # type: ignore[attr-defined]
    yield box
    controller.stop()


async def _user(make_user, name: str = "alice") -> User:
    user = await make_user(name, email=f"{name}@example.com")
    return user


async def _queue(user: User, key: str, subject: str = "Digest: 2 issues", body: str = "Hello\n") -> None:
    async with get_sessionmaker()() as db:
        db_user = await db.get(User, user.id)
        await queue_email(db, db_user, subject=subject, body=body, dedupe_key=key)
        await db.execute(update(OutboundEmail).values(next_attempt_at=T0 - timedelta(seconds=1)))
        await db.commit()


async def _emails() -> list[OutboundEmail]:
    async with get_sessionmaker()() as db:
        return list(await db.scalars(select(OutboundEmail).order_by(OutboundEmail.id)))


async def _inbox(owner_id: int) -> list[InboxItem]:
    async with get_sessionmaker()() as db:
        return list(await db.scalars(select(InboxItem).where(InboxItem.owner_id == owner_id).order_by(InboxItem.id)))


# ---------- sending ----------


async def test_sends_plain_text_with_the_headers(make_user, smtp) -> None:
    alice = await _user(make_user)
    run_id = "6f1c0a52-8b1e-4f53-9d2a-1f4e2c3b4a5d"
    await _queue(alice, f"run:{run_id}", body=f"2 issues\n\nOpen the run: https://mwb.example.com/runs/{run_id}\n")
    result = await sender.send_pending()
    assert (result.sent, result.failed, result.retrying) == (1, 0, 0)
    [msg] = smtp.messages
    assert msg["To"] == "alice@example.com" and msg["From"] == "MyWorkBuddy <mwb@example.com>"
    assert msg["Subject"] == "Digest: 2 issues" and msg["Auto-Submitted"] == "auto-generated"
    assert msg["X-MWB-Run"] == run_id and msg["List-Unsubscribe"] == "<https://mwb.example.com/settings/notifications>"
    assert msg.get_content_type() == "text/plain" and not msg.is_multipart()
    assert msg["Message-ID"].endswith("@example.com>")
    [row] = await _emails()
    assert row.status == "sent" and row.sent_at == T0 and row.attempts == 1
    assert (await sender.send_pending()).sent == 0 and len(smtp.messages) == 1  # sent once


async def test_dedupe_key_prevents_duplicates(make_user, smtp) -> None:
    alice = await _user(make_user)
    for _ in range(3):
        await _queue(alice, "run:same")
    assert len(await _emails()) == 1
    await sender.send_pending()
    assert len(smtp.messages) == 1


async def test_temporary_failures_back_off_then_succeed(make_user, smtp) -> None:
    alice = await _user(make_user)
    await _queue(alice, "run:x")
    smtp.replies = ["451 4.3.0 try again later", "451 4.3.0 try again later"]
    assert (await sender.send_pending()).retrying == 1
    [row] = await _emails()
    assert row.status == "pending" and row.attempts == 1 and row.next_attempt_at == T0 + timedelta(minutes=1)
    assert "451" in row.last_error
    assert (await sender.send_pending()).retrying == 0  # not due yet
    smtp.clock["now"] = T0 + timedelta(minutes=1)
    await sender.send_pending()
    [row] = await _emails()
    assert row.attempts == 2 and row.next_attempt_at == T0 + timedelta(minutes=6)  # +5 min
    smtp.clock["now"] = T0 + timedelta(minutes=6)
    assert (await sender.send_pending()).sent == 1 and len(smtp.messages) == 1


async def test_gives_up_after_six_attempts_and_tells_the_user(make_user, smtp) -> None:
    alice = await _user(make_user)
    await _queue(alice, "run:x", subject="Digest: down")
    smtp.replies = ["421 4.7.0 busy"] * 10
    at = T0
    for minutes in (*sender.BACKOFF_MIN, 0):
        await sender.send_pending()
        at += timedelta(minutes=minutes)
        smtp.clock["now"] = at
    [row] = await _emails()
    assert row.status == "failed" and row.attempts == 6 and smtp.messages == []
    [item] = await _inbox(alice.id)
    assert item.kind == "system" and item.title == sender.FAILED_TITLE and "Digest: down" in item.body_md


async def test_permanent_refusal_fails_at_once(make_user, smtp) -> None:
    alice = await _user(make_user)
    await _queue(alice, "run:x")
    smtp.rcpt_replies = ["550 5.1.1 no such user"]
    assert (await sender.send_pending()).failed == 1
    [row] = await _emails()
    assert row.status == "failed" and row.attempts == 1 and "refused the address (550)" in row.last_error


async def test_unreachable_server_is_retried_without_leaking_settings(make_user, smtp, monkeypatch) -> None:
    alice = await _user(make_user)
    await _queue(alice, "run:x")
    monkeypatch.setattr(get_settings(), "smtp_port", _free_port())  # nothing listens there
    assert (await sender.send_pending()).retrying == 1
    [row] = await _emails()
    assert row.status == "pending" and row.last_error in ("SMTPConnectError", "SMTPConnectTimeoutError", "OSError")


async def test_daily_cap_suppresses_and_notifies_once(make_user, smtp) -> None:
    alice = await _user(make_user)
    bob = await _user(make_user, "bob")
    for i in range(5):
        await _queue(alice, f"run:a{i}")
    await _queue(bob, "run:b0")
    result = await sender.send_pending()
    assert (result.sent, result.suppressed) == (4, 2)  # alice's cap is 3; bob is unaffected
    statuses = [(e.owner_id, e.status) for e in await _emails()]
    assert statuses.count((alice.id, "sent")) == 3 and statuses.count((alice.id, "suppressed")) == 2
    await _queue(alice, "run:a5")
    await sender.send_pending()
    notices = [i for i in await _inbox(alice.id) if i.title == sender.CAP_TITLE]
    assert len(notices) == 1 and notices[0].link == "/inbox"  # one notice however many were suppressed
    # The cap is per local day: after midnight in Stockholm, email resumes.
    smtp.clock["now"] = datetime(2026, 10, 21, 22, 30, tzinfo=UTC)  # 00:30 on the 22nd in Stockholm
    await _queue(alice, "run:next-day")
    async with get_sessionmaker()() as db:
        await db.execute(
            update(OutboundEmail)
            .where(OutboundEmail.dedupe_key == "run:next-day")
            .values(next_attempt_at=smtp.clock["now"])
        )
        await db.commit()
    assert (await sender.send_pending()).sent == 1


async def test_nothing_is_sent_without_an_smtp_host(make_user, smtp, monkeypatch) -> None:
    alice = await _user(make_user)
    await _queue(alice, "run:x")
    monkeypatch.setattr(get_settings(), "smtp_host", "")
    assert (await sender.send_pending()).skipped == "MWB_SMTP_HOST is not set"
    assert (await _emails())[0].status == "pending"


async def test_queue_skips_users_without_email_or_who_opted_out(make_user) -> None:
    nobody = await make_user("nobody")
    quiet = await make_user("quiet", email="quiet@example.com")
    async with get_sessionmaker()() as db:
        await db.execute(update(User).where(User.id == nobody.id).values(email=None))
        await db.execute(update(User).where(User.id == quiet.id).values(email_notifications=False))
        await db.commit()
        for u in (nobody, quiet):
            assert (
                await queue_email(db, await db.get(User, u.id), subject="s", body="b", dedupe_key=f"k{u.id}") is False
            )
        await db.commit()
    assert await _emails() == []


async def test_a_crashed_send_is_retried_after_the_lease(make_user, smtp, monkeypatch) -> None:
    alice = await _user(make_user)
    await _queue(alice, "run:x")
    claimed, _ = await sender.claim(T0)  # claimed, then the worker died before sending
    assert len(claimed) == 1 and (await sender.send_pending()).sent == 0
    smtp.clock["now"] = T0 + sender.LEASE
    assert (await sender.send_pending()).sent == 1


async def test_system_page_reports_the_outbox(client_for, smtp) -> None:
    admin = await client_for("root", role="admin")
    async with get_sessionmaker()() as db:
        root = await db.scalar(select(User).where(User.username == "root"))
        db.add(OutboundEmail(owner_id=root.id, to_addr="x@example.com", subject="s", body_text="b", dedupe_key="f",
                             status="failed", last_error="SMTP 550: no"))  # fmt: skip
        db.add(OutboundEmail(owner_id=root.id, to_addr="x@example.com", subject="s", body_text="b", dedupe_key="p"))
        await db.commit()
    body = (await admin.get("/api/admin/system")).json()
    assert body["outbox"]["failed_24h"] == 1 and body["outbox"]["pending"] == 1 and body["outbox"]["configured"]
    assert "1 email(s) couldn't be delivered in the last 24 hours" in body["alerts"]


# ---------- the inbox API ----------


async def test_inbox_list_unread_count_and_marking_read(client_for, make_user) -> None:
    c = await client_for("alice")
    other = await client_for("bob")
    async with get_sessionmaker()() as db:
        alice = await db.scalar(select(User).where(User.username == "alice"))
        bob = await db.scalar(select(User).where(User.username == "bob"))
        for i in range(5):
            add_item(db, alice.id, kind="alert", severity="alert", title=f"a{i}", link=f"/runs/{i}")
        add_item(db, bob.id, kind="system", severity="info", title="bob's")
        await db.commit()
    assert (await c.get("/api/inbox/unread-count")).json() == {"unread": 5}
    page1 = (await c.get("/api/inbox", params={"limit": 3})).json()
    page2 = (await c.get("/api/inbox", params={"limit": 3, "cursor": page1["next_cursor"]})).json()
    titles = [i["title"] for i in page1["items"] + page2["items"]]
    assert titles == ["a4", "a3", "a2", "a1", "a0"] and page2["next_cursor"] is None

    first = page1["items"][0]
    read = (await c.post(f"/api/inbox/{first['id']}/read")).json()
    assert read["read_at"] is not None
    assert (await c.post(f"/api/inbox/{first['id']}/read")).json()["read_at"] == read["read_at"]  # idempotent
    assert (await c.get("/api/inbox/unread-count")).json() == {"unread": 4}
    unread = (await c.get("/api/inbox", params={"unread": True})).json()["items"]
    assert first["id"] not in [i["id"] for i in unread] and len(unread) == 4

    bobs = (await other.get("/api/inbox")).json()["items"]
    assert [i["title"] for i in bobs] == ["bob's"]
    assert (await other.post(f"/api/inbox/{first['id']}/read")).status_code == 404
    assert (await c.post("/api/inbox/read-all")).json() == {"marked": 4}
    assert (await c.get("/api/inbox/unread-count")).json() == {"unread": 0}
    assert (await other.get("/api/inbox/unread-count")).json() == {"unread": 1}


async def test_marking_read_needs_csrf(client_for) -> None:
    c = await client_for("alice")
    r = await c.post("/api/inbox/read-all", headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403 and r.json()["code"] == "csrf_invalid"


async def test_profile_turns_email_notifications_off(client_for) -> None:
    c = await client_for("alice")
    assert (await c.get("/api/auth/me")).json()["user"]["email_notifications"] is True
    r = await c.patch("/api/auth/me", json={"email_notifications": False})
    assert r.status_code == 200 and r.json()["email_notifications"] is False
    async with get_sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(User).where(User.email_notifications.is_(False))) == 1


def test_smtp_settings_are_checked() -> None:
    from pydantic import SecretStr

    from app.config import Settings, runtime_problems

    base = {"secret_keys": get_settings().secret_keys or get_settings().secret_key}
    problems = runtime_problems(Settings(env="dev", smtp_host="mail", smtp_from="nobody", **base))
    assert any("MWB_SMTP_FROM must be an address" in p for p in problems)
    problems = runtime_problems(Settings(env="dev", smtp_user="u", **base))
    assert any("both MWB_SMTP_USER and MWB_SMTP_PASSWORD" in p for p in problems)
    with pytest.raises(ValueError, match="MWB_SMTP_STARTTLS must be true"):
        Settings(
            env="prod", hostname="https://x.example", cookie_secure=True, smtp_user="u",
            smtp_password=SecretStr("p"), smtp_starttls=False, **base,
        )  # fmt: skip

"""Chat robustness (design 01 §H13): one reply per session, keep-alive, partial and failed replies."""

import asyncio
import uuid

from app.chat import routes as chat_routes
from app.chat.routes import ReplyLock, _history
from app.llm.models import set_model_override
from tests.helpers import parse_sse, slow_then_text, text_then_failure


async def _session(client) -> str:
    return (await client.post("/api/chat/sessions", json={})).json()["id"]


async def _send(client, sid: str, text: str = "hello"):
    return await client.post(f"/api/chat/sessions/{sid}/messages", json={"content": text})


async def test_second_message_while_answering_is_refused(client_for) -> None:
    alice = await client_for("alice")
    sid = await _session(alice)
    set_model_override(slow_then_text(0.5))
    first, second = await asyncio.gather(_send(alice, sid, "one"), _send_after(alice, sid, "two", 0.1))
    assert first.status_code == 200
    assert second.status_code == 409 and second.json()["code"] == "session_busy"
    # The lock is released when the reply ends, so the next message goes through.
    assert (await _send(alice, sid, "three")).status_code == 200


async def _send_after(client, sid: str, text: str, delay: float):
    await asyncio.sleep(delay)
    return await _send(client, sid, text)


async def test_lock_is_per_session_and_released_after_errors(client_for) -> None:
    alice = await client_for("alice")
    a, b = await _session(alice), await _session(alice)
    held = ReplyLock(uuid.UUID(a))
    assert await held.acquire()
    try:
        assert (await _send(alice, a)).status_code == 409
        assert (await _send(alice, b)).status_code == 200  # other sessions are not blocked
    finally:
        await held.release()
    await held.release()  # idempotent
    set_model_override(text_then_failure("half"))
    assert (await _send(alice, a)).status_code == 200
    assert (await _send(alice, a)).status_code == 200  # a failed reply released the lock too


async def test_keep_alive_pings_while_the_model_is_slow(client_for, monkeypatch) -> None:
    alice = await client_for("alice")
    sid = await _session(alice)
    monkeypatch.setattr(chat_routes, "PING_SECONDS", 0.05)
    set_model_override(slow_then_text(0.4, "finally"))
    body = (await _send(alice, sid)).text
    assert body.count(": ping\n\n") >= 3
    events = parse_sse(body)  # pings are comments, not events
    assert [n for n, _ in events] == ["delta", "done"] and events[0][1] == {"text": "finally"}


async def test_mid_stream_failure_keeps_a_partial_reply(client_for) -> None:
    alice = await client_for("alice")
    sid = await _session(alice)
    set_model_override(text_then_failure("The executor ran out of"))
    events = parse_sse((await _send(alice, sid)).text)
    assert [n for n, _ in events] == ["delta", "error", "done"]
    assert events[1][1]["code"] == "llm_unavailable"
    messages = (await alice.get(f"/api/chat/sessions/{sid}/messages")).json()
    reply = messages[-1]
    assert reply["status"] == "partial" and reply["content"] == "The executor ran out of"
    assert reply["error_code"] == "llm_unavailable" and reply["id"] == events[-1][1]["message_id"]
    assert await _history(uuid.UUID(sid)) == []  # never sent back to the model


async def test_complete_replies_are_marked_and_kept_in_history(client_for) -> None:
    alice = await client_for("alice")
    sid = await _session(alice)
    set_model_override(slow_then_text(0, "all good"))
    await _send(alice, sid)
    [_, reply] = (await alice.get(f"/api/chat/sessions/{sid}/messages")).json()
    assert reply["status"] == "complete" and reply["error_code"] is None
    assert len(await _history(uuid.UUID(sid))) >= 2

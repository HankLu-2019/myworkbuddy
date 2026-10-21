import uuid

from sqlalchemy import select

from app.db import get_sessionmaker
from app.llm.models import set_model_override
from app.models import Message, ToolCall
from tests.helpers import failing_model, parse_sse, tool_then_text


async def _upload(client, name, body, public=False):
    doc = (await client.post("/api/knowledge/documents", files={"file": (name, body.encode(), "text/plain")})).json()
    if public:
        await client.post(f"/api/knowledge/documents/{doc['id']}/visibility", json={"visibility": "public"})
    return doc


async def test_chat_streams_answer_with_citations_and_history(client_for):
    alice = await client_for("alice")
    await _upload(alice, "oom.txt", "Spark executor OOMKilled: raise spark.executor.memory to 8g")
    sid = (await alice.post("/api/chat/sessions", json={})).json()["id"]

    set_model_override(tool_then_text("search_knowledge", {"query": "spark OOMKilled"}, "Raise executor memory [1]."))
    resp = await alice.post(f"/api/chat/sessions/{sid}/messages", json={"content": "Why does spark OOM?"})
    assert resp.status_code == 200
    events = parse_sse(resp.text)
    kinds = [e for e, _ in events]
    assert kinds[0] == "tool_call" and "tool_result" in kinds and kinds[-1] == "done"
    assert "".join(d["text"] for e, d in events if e == "delta") == "Raise executor memory [1]."
    done = events[-1][1]
    assert done["citations"][0]["title"] == "oom.txt"

    session = (await alice.get(f"/api/chat/sessions/{sid}")).json()
    assert session["title"] == "Why does spark OOM?"
    messages = (await alice.get(f"/api/chat/sessions/{sid}/messages")).json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["citations"][0]["source_type"] == "upload"
    assert messages[1]["tools"][0]["name"] == "search_knowledge"
    async with get_sessionmaker()() as db:
        call = await db.scalar(select(ToolCall))
        assert call.status == "done" and call.result[0]["title"] == "oom.txt"
        assistant = await db.scalar(select(Message).where(Message.role == "assistant"))
        assert assistant.raw and assistant.tokens_out is not None

    # Second turn replays the stored history to the model.
    seen: list[int] = []
    model = tool_then_text("search_knowledge", {"query": "memory"}, "Again.")
    inner = model.stream_function

    async def counting(messages, info):
        seen.append(len(messages))
        async for chunk in inner(messages, info):
            yield chunk

    model.stream_function = counting
    set_model_override(model)
    await alice.post(f"/api/chat/sessions/{sid}/messages", json={"content": "and now?"})
    assert seen[0] > 1


async def test_shared_session_hides_private_citations(client_for):
    alice = await client_for("alice")
    bob = await client_for("bob")
    await _upload(alice, "private.txt", "deploy secret runbook")
    await _upload(alice, "public.txt", "deploy team runbook", public=True)
    sid = (await alice.post("/api/chat/sessions", json={})).json()["id"]
    set_model_override(tool_then_text("search_knowledge", {"query": "deploy runbook"}, "See [1] [2]."))
    await alice.post(f"/api/chat/sessions/{sid}/messages", json={"content": "deploy?"})
    await alice.patch(f"/api/chat/sessions/{sid}", json={"visibility": "public"})

    mine = (await alice.get(f"/api/chat/sessions/{sid}/messages")).json()[1]["citations"]
    theirs = (await bob.get(f"/api/chat/sessions/{sid}/messages")).json()[1]["citations"]
    assert {c["title"] for c in mine} == {"private.txt", "public.txt"}
    assert {c["title"] for c in theirs} == {"public.txt"}


async def test_immediate_failure_saves_a_failed_reply_outside_model_history(client_for):
    alice = await client_for("alice")
    sid = (await alice.post("/api/chat/sessions", json={})).json()["id"]
    set_model_override(failing_model())
    events = parse_sse((await alice.post(f"/api/chat/sessions/{sid}/messages", json={"content": "hello"})).text)
    error = next(d for n, d in events if n == "error")
    assert error["code"] == "llm_unavailable" and "bedrock unavailable" in error["message"]
    assert events[-1][0] == "done" and events[-1][1]["message_id"] is not None
    messages = (await alice.get(f"/api/chat/sessions/{sid}/messages")).json()
    assert [(m["role"], m["status"]) for m in messages] == [("user", "complete"), ("assistant", "failed")]
    assert "bedrock unavailable" in messages[1]["content"] and messages[1]["error_code"] == "llm_unavailable"
    # The next turn's model history doesn't include the failed reply.
    from app.chat.routes import _history

    assert await _history(uuid.UUID(sid)) == []


async def test_session_management(client_for, task_queue):
    alice = await client_for("alice")
    sid = (await alice.post("/api/chat/sessions", json={"title": "First"})).json()["id"]
    await alice.post("/api/chat/sessions", json={})
    assert len((await alice.get("/api/chat/sessions")).json()) == 2
    assert (await alice.post("/api/chat/sessions", json={"project_id": 999})).status_code == 404

    renamed = await alice.patch(f"/api/chat/sessions/{sid}", json={"title": "Renamed"})
    assert renamed.json()["title"] == "Renamed"
    closed = await alice.post(f"/api/chat/sessions/{sid}/close")
    assert closed.status_code == 202
    assert [j["task_name"] for j in task_queue.jobs.values()] == ["build_episode"]
    # A second close while the first summary is still queued doesn't queue a duplicate.
    await alice.post(f"/api/chat/sessions/{sid}/close")
    assert len(task_queue.jobs) == 1

    assert (await alice.delete(f"/api/chat/sessions/{sid}")).status_code == 204
    assert len((await alice.get("/api/chat/sessions")).json()) == 1


async def test_history_is_windowed(client_for, monkeypatch):
    from app.chat import routes

    alice = await client_for("alice")
    sid = (await alice.post("/api/chat/sessions", json={})).json()["id"]
    monkeypatch.setattr(routes, "HISTORY_RUNS", 2)
    from pydantic_ai.models.test import TestModel

    set_model_override(TestModel(call_tools=[], custom_output_text="ok"))
    for i in range(4):
        await alice.post(f"/api/chat/sessions/{sid}/messages", json={"content": f"turn {i}"})
    history = await routes._history(__import__("uuid").UUID(sid))
    prompts = [p.content for m in history for p in m.parts if getattr(p, "part_kind", "") == "user-prompt"]
    assert prompts == ["turn 2", "turn 3"]

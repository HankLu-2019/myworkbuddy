"""LLM gateway: per-user keys, one client, cost, budgets, spend (design 01 §H12). Uses the in-process fake gateway."""

import asyncio
import json
import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent
from sqlalchemy import select, update

from app.db import get_sessionmaker
from app.errors import AppError
from app.llm import client as llm
from app.llm import keys
from app.llm.embeddings import set_embedder
from app.llm.models import set_model_override
from app.models import ChatSession, Message, User, UserSecret
from app.secrets.service import get_secret
from app.tasks import app as tasks_mod
from tests.fakellm import server as fake

APP_DIR = Path(__file__).parent.parent / "app"


def _key_of(user_id: int) -> dict:
    [rec] = [r for r in fake.KEYS.values() if r["key_alias"] == f"mwb-user-{user_id}"]
    return rec


async def _real_models() -> None:
    """Use the gateway path (the fake) instead of the TestModel override."""
    set_model_override(None)
    set_embedder(None)


# ---------- keys ----------


async def test_creating_a_user_creates_a_key_with_the_budget(client_for) -> None:
    admin = await client_for("root", role="admin")
    created = (await admin.post("/api/admin/users", json={"username": "carol"})).json()
    rec = _key_of(created["id"])
    assert rec["max_budget"] == 50 and rec["budget_duration"] == "30d"
    assert rec["models"] == ["chat", "fast", "embed", "planner"] and rec["user_id"] == f"mwb:{created['id']}"
    async with get_sessionmaker()() as db:
        assert (await get_secret(db, created["id"], "gateway_key")) == rec["token"]

    # The key is a system secret: not listed, not settable through the secrets API.
    kinds = [s["kind"] for s in (await admin.get("/api/secrets")).json()]
    assert "gateway_key" not in kinds
    assert (await admin.put("/api/secrets/gateway_key", json={"value": "x"})).status_code == 404

    r = await admin.patch(f"/api/admin/users/{created['id']}", json={"llm_budget_usd": "12.50"})
    assert r.status_code == 200 and r.json()["llm_budget_usd"] == 12.5
    assert _key_of(created["id"])["max_budget"] == 12.5
    assert (await admin.patch(f"/api/admin/users/{created['id']}", json={"llm_budget_usd": -1})).status_code == 422


async def test_existing_users_get_a_key_lazily_and_only_one(make_user) -> None:
    alice = await make_user("alice")  # created directly in the DB, like users from before H12
    assert not [r for r in fake.KEYS.values() if r["key_alias"] == f"mwb-user-{alice.id}"]
    a, b = await asyncio.gather(keys.ensure_key(alice.id), keys.ensure_key(alice.id))
    assert a == b == _key_of(alice.id)["token"]
    async with get_sessionmaker()() as db:
        assert len(list(await db.scalars(select(UserSecret).where(UserSecret.kind == "gateway_key")))) == 1


async def test_a_lost_race_between_processes_keeps_one_key(make_user, monkeypatch) -> None:
    alice = await make_user("alice")
    real_generate = keys._generate

    async def generate_while_other_process_stores(user):
        new = await real_generate(user)
        other = await real_generate(user)  # "another process" stores its key first
        async with get_sessionmaker()() as db:
            from app.secrets.service import set_secret

            await set_secret(db, user.id, "gateway_key", other)
            await db.commit()
        return new

    monkeypatch.setattr(keys, "_generate", generate_while_other_process_stores)
    key = await keys.ensure_key(alice.id)
    assert list(fake.KEYS) == [key]  # ours was deleted, theirs kept


async def test_rotate_and_check(make_user) -> None:
    alice = await make_user("alice")
    old = await keys.ensure_key(alice.id)
    await keys.rotate_key(alice.id)
    new = await keys.ensure_key(alice.id)
    assert new != old and old not in fake.KEYS and new in fake.KEYS
    assert await keys.check_gateway() == []
    assert not [r for r in fake.KEYS.values() if r["key_alias"] == "mwb-check"]


async def test_disabled_user_gets_no_key(make_user) -> None:
    alice = await make_user("alice")
    async with get_sessionmaker()() as db:
        await db.execute(update(User).where(User.id == alice.id).values(is_active=False))
        await db.commit()
    with pytest.raises(AppError) as err:
        await keys.ensure_key(alice.id)
    assert err.value.code == "forbidden"


# ---------- one client ----------


def test_every_llm_call_goes_through_the_client() -> None:
    """Rule R11: models are built only in app/llm/client.py, and agent runs use llm.run (chat streams with limits)."""
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        rel = path.relative_to(APP_DIR).as_posix()
        text = path.read_text()
        if rel != "llm/client.py" and re.search(r"OpenAIProvider\(|OpenAIChatModel\(", text):
            offenders.append(f"{rel}: builds a model")
        if re.search(r"\bagent\.run\(", text) and rel != "llm/client.py":
            offenders.append(f"{rel}: calls agent.run directly")
        if "get_chat_model" in text:
            offenders.append(f"{rel}: uses the removed get_chat_model")
    assert offenders == []


def test_limits_per_purpose() -> None:
    chat = llm.limits_for("chat_turn")
    assert (chat.request_limit, chat.total_tokens_limit) == (8, 200_000)
    assert llm.timeout_for("chat_turn") == 180
    summary = llm.limits_for("code_summary")
    assert (summary.request_limit, summary.total_tokens_limit) == (2, 30_000) and llm.timeout_for("code_summary") == 60
    assert set(llm.LIMITS) == {
        "chat_turn",
        "episode",
        "librarian_page",
        "project_card",
        "code_summary",
        "scheduled_run",
        "plan_draft",
    }


async def test_run_uses_the_users_key_and_records_cost(make_user) -> None:
    alice = await make_user("alice")
    await _real_models()
    agent = Agent(await llm.model_for(alice.id), instructions="Be brief.")
    result = await llm.run(agent, "hello", user_id=alice.id, purpose="episode")
    assert result.output == fake.PLAIN_ANSWER
    assert result.cost_usd == Decimal("0.001")
    assert _key_of(alice.id)["spend"] == 0.001


async def test_usage_limits_are_applied(make_user, monkeypatch) -> None:
    from pydantic_ai.exceptions import UsageLimitExceeded

    alice = await make_user("alice")
    await _real_models()
    monkeypatch.setitem(llm.LIMITS, "code_summary", (0, 10, 5))  # no request allowed at all
    agent = Agent(await llm.model_for(alice.id))
    with pytest.raises(UsageLimitExceeded):
        await llm.run(agent, "x", user_id=alice.id, purpose="code_summary")


async def test_timeout_is_applied(make_user, monkeypatch) -> None:
    alice = await make_user("alice")
    await _real_models()
    monkeypatch.setitem(llm.LIMITS, "code_summary", (2, 30_000, 0.2))
    agent = Agent(await llm.model_for(alice.id))
    with pytest.raises(TimeoutError):
        await llm.run(agent, "[[slow:2]] hi", user_id=alice.id, purpose="code_summary")


# ---------- budgets ----------


async def _exhaust(user_id: int) -> None:
    await keys.ensure_key(user_id)
    _key_of(user_id)["max_budget"] = 0.0


async def test_budget_error_maps_to_budget_exceeded(make_user) -> None:
    alice = await make_user("alice")
    await _real_models()
    await _exhaust(alice.id)
    agent = Agent(await llm.model_for(alice.id))
    with pytest.raises(AppError) as err:
        await llm.run(agent, "hello", user_id=alice.id, purpose="episode")
    assert err.value.code == "budget_exceeded" and err.value.status_code == 402
    assert "monthly AI budget (USD 50.00)" in err.value.detail


async def _chat(client, text: str) -> list[tuple[str, dict]]:
    session = (await client.post("/api/chat/sessions", json={})).json()
    resp = await client.post(f"/api/chat/sessions/{session['id']}/messages", json={"content": text})
    events = []
    for block in resp.text.strip().split("\n\n"):
        name = re.search(r"^event: (.+)$", block, re.M).group(1)
        data = json.loads(re.search(r"^data: (.+)$", block, re.M).group(1))
        events.append((name, data))
    return events


async def test_chat_shows_budget_exceeded(client_for) -> None:
    alice = await client_for("alice")
    await _real_models()
    await _exhaust(alice.user_id)
    events = await _chat(alice, "hello")
    error = next(d for n, d in events if n == "error")
    assert error["code"] == "budget_exceeded" and "An admin can raise it" in error["message"]
    assert events[-1][0] == "done"


async def test_chat_message_cost_is_settled_from_the_spend_log(client_for, task_queue) -> None:
    alice = await client_for("alice")
    await _real_models()
    events = await _chat(alice, "what is in my notes?")
    assert [n for n, _ in events][-1] == "done"
    message_id = events[-1][1]["message_id"]
    async with get_sessionmaker()() as db:
        msg = await db.get(Message, message_id)
        assert msg.cost_usd == 0  # streamed: the cost isn't in the headers
    [job] = [j for j in task_queue.jobs.values() if j["task_name"] == "settle_costs"]
    assert job["args"]["message_id"] == message_id and len(job["args"]["call_ids"]) == 2  # tool call + answer
    await tasks_mod.settle_costs(**job["args"])
    async with get_sessionmaker()() as db:
        assert (await db.get(Message, message_id)).cost_usd == Decimal("0.002")


async def test_settle_retries_calls_not_logged_yet(make_user, task_queue) -> None:
    alice = await make_user("alice")
    async with get_sessionmaker()() as db:
        s = ChatSession(owner_id=alice.id, title="x")
        db.add(s)
        await db.flush()
        m = Message(session_id=s.id, role="assistant", content="a", cost_usd=Decimal("0.5"))
        db.add(m)
        await db.commit()
    fake.SPEND_LOGS["c1"] = {"request_id": "c1", "spend": 0.25}
    await tasks_mod.settle_costs(message_id=m.id, call_ids=["c1", "c2"])
    async with get_sessionmaker()() as db:
        assert (await db.get(Message, m.id)).cost_usd == Decimal("0.75")
    [job] = [j for j in task_queue.jobs.values() if j["task_name"] == "settle_costs"]
    assert job["args"] == {"message_id": m.id, "call_ids": ["c2"], "attempt": 2}
    await tasks_mod.settle_costs(message_id=m.id, call_ids=["c2"], attempt=tasks_mod.SETTLE_ATTEMPTS)
    assert len([j for j in task_queue.jobs.values() if j["task_name"] == "settle_costs"]) == 1  # gave up


def test_background_tasks_do_not_retry_on_budget() -> None:
    strategy = tasks_mod.NoRetryOnBudget(max_attempts=2)
    job = SimpleNamespace(attempts=0, task_name="build_episode")
    budget = AppError(402, "budget_exceeded", "over")
    assert strategy.get_retry_decision(exception=budget, job=job) is None
    assert strategy.get_retry_decision(exception=RuntimeError("blip"), job=job) is not None
    assert tasks_mod.build_episode_task.retry_strategy.__class__ is tasks_mod.NoRetryOnBudget


async def test_episode_task_fails_on_budget(client_for) -> None:
    alice = await client_for("alice")
    await _real_models()
    async with get_sessionmaker()() as db:
        s = ChatSession(owner_id=alice.user_id, title="x")
        db.add(s)
        await db.flush()
        db.add_all(
            [
                Message(session_id=s.id, role="user", content="a"),
                Message(session_id=s.id, role="assistant", content="b"),
            ]
        )
        await db.commit()
    await _exhaust(alice.user_id)
    with pytest.raises(AppError) as err:
        await tasks_mod.build_episode_task(session_id=str(s.id))
    assert err.value.code == "budget_exceeded"


async def test_embeddings_use_the_owners_key(client_for) -> None:
    alice = await client_for("alice")
    await _real_models()
    r = await alice.post(
        "/api/knowledge/documents", files={"file": ("n.md", b"# Notes\n\nzebra text", "text/markdown")}
    )
    assert r.status_code == 201
    assert _key_of(alice.user_id)["spend"] > 0  # the probe uses the master key; the document's vectors the owner's


# ---------- spend ----------


async def test_spend_endpoints_are_cached(client_for) -> None:
    admin = await client_for("root", role="admin")
    alice = await client_for("alice")
    await keys.ensure_key(alice.user_id)
    _key_of(alice.user_id)["spend"] = 1.25
    mine = (await alice.get("/api/usage")).json()
    assert mine["spend_usd"] == 1.25 and mine["budget_usd"] == 50 and mine["budget_reset_at"]
    _key_of(alice.user_id)["spend"] = 2.0
    assert (await alice.get("/api/usage")).json()["spend_usd"] == 1.25  # cached for 5 minutes
    everyone = {u["user_id"]: u for u in (await admin.get("/api/admin/usage")).json()}
    assert everyone[alice.user_id]["spend_usd"] == 1.25
    assert everyone[admin.user_id]["spend_usd"] is None or everyone[admin.user_id]["spend_usd"] == 0
    assert (await alice.get("/api/admin/usage")).status_code == 403


async def test_spend_unavailable_is_not_an_error(client_for, monkeypatch) -> None:
    alice = await client_for("alice")

    async def down(*a, **k):
        raise keys.GatewayAdminError("down")

    monkeypatch.setattr(keys, "_admin", down)
    body = (await alice.get("/api/usage")).json()
    assert body["spend_usd"] is None and body["budget_usd"] == 50


# ---------- offboarding ----------


async def test_offboarding_deletes_the_key(client_for, task_queue) -> None:
    admin = await client_for("root", role="admin")
    alice = await client_for("alice")
    await keys.ensure_key(alice.user_id)
    r = await admin.delete(f"/api/admin/users/{alice.user_id}", params={"transfer_to": admin.user_id})
    assert r.status_code == 200
    [job] = [j for j in task_queue.jobs.values() if j["task_name"] == "delete_gateway_key"]
    await tasks_mod.delete_gateway_key(**job["args"])
    assert not [k for k in fake.KEYS.values() if k["key_alias"] == f"mwb-user-{alice.user_id}"]

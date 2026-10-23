"""Agents, the run report and execute_agent_run (design 02 §3, §4 steps 2–3; plan S3)."""

import asyncio
import dataclasses
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.agents import executor
from app.agents.executor import compose_prompt, execute_agent_run
from app.agents.report import (
    RunReport,
    check_report,
    evidence,
    fingerprint_id,
    is_verified,
    report_markdown,
    report_status,
)
from app.agents.seeds import SeedError, load_seeds, parse_seed, sync_system_agents
from app.db import get_sessionmaker
from app.errors import AppError
from app.llm import usage
from app.llm.models import set_model_override
from app.llm.untrusted import UNTRUSTED_NOTICE
from app.models import Agent, ChatSession, Message, ToolCall, User, UserCredential
from app.tools.registry import instructions_for, missing_tools_note
from tests.connector_fakes import fixture, kubeconfig, ok, setup_integration

K8S = "https://k8s.example.com:6443"
POD = "payments-7d9f-abcde"


# ---------- seeds ----------


def test_seed_files_are_valid_and_end_with_the_notice() -> None:
    seeds = {s.slug: s for s in load_seeds()}
    assert set(seeds) == {"morning-brief", "k8s-health", "ci-failures", "jira-digest", "planner"}
    for seed in seeds.values():
        assert seed.system_prompt.endswith(UNTRUSTED_NOTICE) and seed.max_tier == "read"
        if seed.slug != "planner":  # the planner returns a PlanDoc, not a run report (03 §5)
            assert seed.report and seed.inputs_schema["type"] == "object" and "Fingerprints" in seed.system_prompt
    assert seeds["planner"].report is False and seeds["planner"].model_alias == "planner"
    assert seeds["k8s-health"].tools == ["k8s_get", "k8s_events", "k8s_pod_logs", "search_knowledge"]
    assert seeds["k8s-health"].inputs_schema["required"] == ["namespaces"]


async def test_sync_is_idempotent_and_updates_on_change(make_user) -> None:
    seeds = load_seeds()
    async with get_sessionmaker()() as db:
        assert await sync_system_agents(db, seeds) == {"created": 5, "updated": 0, "disabled": 0, "unchanged": 0}
        await db.commit()
        before = {a.slug: a.updated_at for a in await db.scalars(select(Agent))}
        assert await sync_system_agents(db, seeds) == {"created": 0, "updated": 0, "disabled": 0, "unchanged": 5}
        await db.commit()
        assert {a.slug: a.updated_at for a in await db.scalars(select(Agent))} == before  # untouched rows

        changed = [
            dataclasses.replace(s, system_prompt=s.system_prompt + "\nBe brief.") if s.slug == "jira-digest" else s
            for s in seeds
        ]
        assert (await sync_system_agents(db, changed))["updated"] == 1
        await db.commit()
        digest = await db.scalar(select(Agent).where(Agent.slug == "jira-digest"))
        assert digest.system_prompt.endswith("Be brief.") and digest.updated_at > before["jira-digest"]

        assert (await sync_system_agents(db, changed[1:]))["disabled"] == 1  # a removed file disables its agent
        await db.commit()
        gone = await db.scalar(select(Agent).where(Agent.slug == changed[0].slug))
        assert gone.enabled is False
        assert (await sync_system_agents(db, changed))["updated"] == 1  # and coming back re-enables it
        await db.commit()
        assert (await db.scalar(select(Agent).where(Agent.slug == changed[0].slug))).enabled is True


async def test_sync_refuses_a_slug_taken_by_a_user_agent(make_user) -> None:
    alice = await make_user("alice")
    seed = load_seeds()[0]
    async with get_sessionmaker()() as db:
        db.add(Agent(**seed.values(), owner_id=alice.id))
        await db.commit()
        with pytest.raises(SeedError, match="a user agent already has this slug"):
            await sync_system_agents(db, [seed])


SEED_OK = """---
slug: demo
name: Demo
description: d
model_alias: fast
tools: [jira_search]
---
Prompt.
"""


@pytest.mark.parametrize(
    ("name", "text", "message"),
    [
        ("demo.md", "no frontmatter", "needs YAML frontmatter"),
        ("other.md", SEED_OK, "slug must be 'other'"),
        ("demo.md", SEED_OK.replace("[jira_search]", "[jira_delete]"), "unknown tool 'jira_delete'"),
        ("demo.md", SEED_OK.replace("model_alias: fast", "model_alias: coder"), "model_alias must be one of"),
        ("demo.md", SEED_OK.replace("name: Demo", "name: Demo\ncolour: red"), "unknown fields ['colour']"),
        ("demo.md", SEED_OK.replace("name: Demo", "name: Demo\nsystem_prompt: x"), "unknown fields"),
        ("demo.md", SEED_OK.replace("name: Demo", "name: Demo\ninputs_schema: {type: 12}"), "not a valid JSON Schema"),
        ("demo.md", SEED_OK.replace("name: Demo", "name: Demo\ninputs_schema: {type: array}"), "describe an object"),
        ("demo.md", SEED_OK.replace("name: Demo", "name: Demo\nmax_tier: admin"), "max_tier must be one of"),
        ("demo.md", SEED_OK.replace("[jira_search]", "[]"), "at least one tool"),
    ],
)
def test_bad_seed_files_fail_loudly(tmp_path: Path, name: str, text: str, message: str) -> None:
    path = tmp_path / name
    path.write_text(text)
    with pytest.raises(SeedError, match=message.replace("[", r"\[").replace("]", r"\]")):
        parse_seed(path)


def test_a_tool_above_the_agents_tier_is_refused(tmp_path: Path, monkeypatch) -> None:
    from app.enums import Tier
    from app.tools import registry

    spec = dataclasses.replace(registry.REGISTRY["jira_search"], name="jira_comment", tier=Tier.external_write)
    monkeypatch.setitem(registry.REGISTRY, "jira_comment", spec)
    path = tmp_path / "demo.md"
    path.write_text(SEED_OK.replace("[jira_search]", "[jira_comment]"))
    with pytest.raises(SeedError, match="above max_tier read"):
        parse_seed(path)


async def test_agent_checks_at_the_database(make_user) -> None:
    seed = load_seeds()[0]
    for column, value in [("model_alias", "coder"), ("max_tier", "root"), ("slug", "Bad Slug")]:
        with pytest.raises(IntegrityError):
            async with get_sessionmaker()() as db:
                db.add(Agent(**(seed.values() | {column: value})))
                await db.commit()


# ---------- the report ----------


def _report(*findings: tuple[str, str], status: str = "ok") -> RunReport:
    return RunReport.model_validate(
        {
            "status": status,
            "headline": "h",
            "summary_md": "s",
            "findings": [{"fingerprint": fp, "severity": sev, "title": fp, "detail": "d"} for fp, sev in findings],
        }
    )


@pytest.mark.parametrize(
    ("fingerprint", "id_part"),
    [
        ("k8s:prod/payments-7f9:CrashLoopBackOff", "prod/payments-7f9"),
        ("jira:SPIN-4412", "SPIN-4412"),
        ("jira:SPIN-4412:In Progress", "SPIN-4412"),
        ("feed:https://news.example.com/a", "https://news.example.com/a"),
        ("feed:https://news.example.com/a:new", "https://news.example.com/a"),
        ("plain", "plain"),
    ],
)
def test_fingerprint_id(fingerprint: str, id_part: str) -> None:
    assert fingerprint_id(fingerprint) == id_part


def test_fingerprints_must_come_from_tool_results() -> None:
    corpus = evidence(
        [
            {"items": [{"name": POD, "namespace": "prod"}]},
            {"source": "jira:jira_search", "untrusted": True, "content": '{"issues": [{"key": "SPIN-4412"}]}'},
            {"error": "not_found", "message": "GHOST-1 was not found"},  # errors echo arguments: not evidence
            {"pipeline_id": 9912, "failed_jobs": [{"name": "integration-tests"}]},
        ]
    )
    assert is_verified(f"k8s:prod/{POD}:CrashLoopBackOff", corpus)
    assert is_verified("jira:spin-4412", corpus)  # case-insensitive
    assert is_verified("gitlab:team/etl#9912:integration-tests", corpus) is False  # "team", "etl" never returned
    assert not is_verified("jira:GHOST-1", corpus)
    assert not is_verified("k8s:prod/ghost-pod:OOMKilled", corpus)
    assert not is_verified("k8s:pr:x", corpus)  # no token of 3+ characters
    assert not is_verified("k8s::x", corpus)


def test_status_is_recomputed_and_unverified_findings_are_kept() -> None:
    report = _report((f"k8s:prod/{POD}:CrashLoopBackOff", "warning"), ("k8s:prod/ghost:OOMKilled", "alert"))
    stored, fingerprints = check_report(report, evidence([{"items": [{"name": POD, "namespace": "prod"}]}]))
    assert stored["status"] == "alert"  # the model said ok; the findings say otherwise
    assert [f["verified"] for f in stored["findings"]] == [True, False]
    assert fingerprints == [f"k8s:prod/{POD}:CrashLoopBackOff"]
    assert "_(unverified)_" in report_markdown(stored) and stored["headline"] in report_markdown(stored)
    assert report_status([]) == "ok" and report_status(_report(("a:bcd", "info")).findings) == "ok"


def test_compose_prompt() -> None:
    assert compose_prompt("", {}) == "Run your task now."
    assert compose_prompt(" Check prod ", {"namespaces": ["prod"]}).startswith("Check prod\n\nInputs:\n```json\n{")


def test_nested_usage_tracks_add_up() -> None:
    with usage.track() as outer:
        with usage.track():
            usage.record({"x-litellm-response-cost": "0.25"})
        usage.record({"x-litellm-call-id": "c1"})
    assert outer.cost_usd == Decimal("0.25") and outer.calls == 2 and outer.unpriced_call_ids == ["c1"]


# ---------- execute_agent_run ----------


async def _k8s_agent(make_user) -> tuple[User, Agent, Any]:
    alice, _ = await setup_integration(make_user, "kubernetes", K8S, {"kubeconfig": kubeconfig()})
    async with get_sessionmaker()() as db:
        await sync_system_agents(db)
        agent = await db.scalar(select(Agent).where(Agent.slug == "k8s-health"))
        session = ChatSession(owner_id=alice.id, title="k8s-health run", source="run")
        db.add(session)
        await db.commit()
        db.expunge_all()
    return alice, agent, session.id


def _scripted(calls: list[tuple[str, dict[str, Any]]], report: dict[str, Any]) -> FunctionModel:
    """Calls each tool in turn, then returns `report` as the output."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        output = info.output_tools[0].name
        done = sum(isinstance(p, ToolReturnPart) and p.tool_name != output for m in messages for p in m.parts)
        if done < len(calls):
            name, args = calls[done]
            return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id=f"c{done}")])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, report, tool_call_id="out")])

    return FunctionModel(respond)


REPORT = {
    "status": "ok",
    "headline": "payments is crash-looping",
    "summary_md": "OOM since 2.3.",
    "findings": [
        {
            "fingerprint": f"k8s:prod/{POD}:CrashLoopBackOff",
            "severity": "alert",
            "title": "Crash loop",
            "detail": "137",
        },
        {"fingerprint": "k8s:prod/ghost-pod:OOMKilled", "severity": "warning", "title": "Invented", "detail": "x"},
    ],
}


async def test_run_checks_the_report_and_saves_the_transcript(make_user, api) -> None:
    alice, agent, session_id = await _k8s_agent(make_user)
    api.on("/api/v1/namespaces/prod/pods", ok(fixture("kubernetes", "pods.json")))
    set_model_override(_scripted([("k8s_get", {"kind": "pods", "namespace": "prod"})], REPORT))

    outcome = await execute_agent_run(
        agent, alice, session_id=session_id, prompt="Check", inputs={"namespaces": ["prod"]}
    )

    assert outcome.status == "succeeded" and outcome.error_code is None
    assert outcome.report["status"] == "alert"  # recomputed, not the model's "ok"
    assert outcome.fingerprints == [f"k8s:prod/{POD}:CrashLoopBackOff"]  # the invented pod is excluded
    assert [f["verified"] for f in outcome.report["findings"]] == [True, False]
    async with get_sessionmaker()() as db:
        user_msg, reply = list(
            await db.scalars(select(Message).where(Message.session_id == session_id).order_by(Message.id))
        )
        [call] = list(await db.scalars(select(ToolCall).where(ToolCall.session_id == session_id)))
    assert user_msg.role == "user" and '"namespaces"' in user_msg.content and user_msg.content.startswith("Check")
    assert reply.status == "complete" and "payments is crash-looping" in reply.content and reply.raw
    assert call.tool_name == "k8s_get" and call.status == "done" and call.message_id == reply.id
    assert call.result["items"][0]["name"] == POD


async def test_error_results_cannot_verify_a_fingerprint(make_user, api) -> None:
    alice, agent, session_id = await _k8s_agent(make_user)
    api.on("/api/v1/namespaces/prod/pods/ghost-pod/log", ok({}, status=404))
    report = REPORT | {"findings": [REPORT["findings"][1]]}
    set_model_override(_scripted([("k8s_pod_logs", {"namespace": "prod", "pod": "ghost-pod"})], report))
    outcome = await execute_agent_run(agent, alice, session_id=session_id)
    assert outcome.status == "succeeded" and outcome.fingerprints == [] and outcome.report["status"] == "warning"


async def test_tools_without_a_verified_credential_are_not_offered(make_user) -> None:
    alice, agent, session_id = await _k8s_agent(make_user)
    async with get_sessionmaker()() as db:
        await db.execute(update(UserCredential).values(status="invalid"))
        await db.commit()
    offered: list[set[str]] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        offered.append({t.name for t in info.function_tools})
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, REPORT | {"findings": []})])

    set_model_override(FunctionModel(respond))
    outcome = await execute_agent_run(agent, alice, session_id=session_id)
    assert offered == [{"search_knowledge"}] and outcome.report["status"] == "ok"


async def test_the_model_is_told_which_named_tools_this_run_lacks(make_user) -> None:
    # The morning brief names jira_my_open_issues in its prompt; without Jira the tool is left out, and the model
    # must be told, or it calls a tool that doesn't exist and the whole run fails.
    alice, _, session_id = await _k8s_agent(make_user)
    async with get_sessionmaker()() as db:
        brief = await db.scalar(select(Agent).where(Agent.slug == "morning-brief"))
        db.expunge_all()
    seen: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info.instructions or "")
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, REPORT | {"findings": []})])

    set_model_override(FunctionModel(respond))
    outcome = await execute_agent_run(brief, alice, session_id=session_id)

    assert outcome.status == "succeeded"
    [instructions] = seen
    assert instructions.endswith(UNTRUSTED_NOTICE)  # the notice stays the last instruction
    note = instructions.removeprefix(brief.system_prompt.removesuffix(UNTRUSTED_NOTICE).rstrip())
    assert note.startswith("\n\nNot available in this run")
    for tool in ("jira_my_open_issues", "gitlab_list_mrs", "jenkins_failed_builds", "gerrit_query", "feed_latest"):
        assert tool in note
    assert "search_knowledge" not in note  # needs no integration


async def test_no_note_when_every_named_tool_is_available(make_user) -> None:
    alice, agent, session_id = await _k8s_agent(make_user)
    seen: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info.instructions or "")
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, REPORT | {"findings": []})])

    set_model_override(FunctionModel(respond))
    await execute_agent_run(agent, alice, session_id=session_id)
    assert seen == [agent.system_prompt]
    assert missing_tools_note({}) is None


def test_the_note_goes_before_the_notice_or_at_the_end() -> None:
    missing = {"jira_search": "jira"}
    with_notice = instructions_for(f"Do things.\n\n{UNTRUSTED_NOTICE}", missing)
    assert with_notice.startswith("Do things.\n\nNot available") and with_notice.endswith(UNTRUSTED_NOTICE)
    assert instructions_for("Do things.", missing).endswith("which sources were skipped.")
    assert instructions_for("Do things.", {}) == "Do things."


async def test_timeout(make_user) -> None:
    alice, agent, session_id = await _k8s_agent(make_user)

    async def slow(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(5)
        raise AssertionError("not reached")  # pragma: no cover

    set_model_override(FunctionModel(slow))
    outcome = await execute_agent_run(agent, alice, session_id=session_id, max_runtime_s=0.2)
    assert (outcome.status, outcome.error_code) == ("timed_out", "timeout") and outcome.report is None
    async with get_sessionmaker()() as db:
        reply = await db.scalar(select(Message).where(Message.session_id == session_id, Message.role == "assistant"))
    assert reply.status == "failed" and reply.meta["error_code"] == "timeout"


async def test_invalid_report(make_user) -> None:
    alice, agent, session_id = await _k8s_agent(make_user)
    set_model_override(_scripted([], {"headline": "x" * 500}))  # keeps failing validation
    outcome = await execute_agent_run(agent, alice, session_id=session_id)
    assert (outcome.status, outcome.error_code) == ("failed", "invalid_report")


async def test_budget_exceeded_and_unexpected_errors(make_user, monkeypatch) -> None:
    alice, agent, session_id = await _k8s_agent(make_user)

    async def over_budget(*a, **k):
        raise AppError(402, "budget_exceeded", "You've reached your monthly AI budget (USD 5.00).")

    monkeypatch.setattr(executor.llm, "model_for", over_budget)
    outcome = await execute_agent_run(agent, alice, session_id=session_id)
    assert (outcome.status, outcome.error_code) == ("failed", "budget_exceeded") and "USD 5.00" in outcome.error

    async def broken(*a, **k):
        raise RuntimeError("secret-token-in-message")

    monkeypatch.setattr(executor.llm, "model_for", broken)
    outcome = await execute_agent_run(agent, alice, session_id=session_id)
    assert outcome.error_code == "llm_unavailable" and "secret-token" not in outcome.error


async def test_usage_limit(make_user, api) -> None:
    alice, agent, session_id = await _k8s_agent(make_user)
    api.on("/api/v1/namespaces/prod/pods", ok(fixture("kubernetes", "pods.json")))

    def forever(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("k8s_get", {"kind": "pods", "namespace": "prod"})])

    set_model_override(FunctionModel(forever))
    outcome = await execute_agent_run(agent, alice, session_id=session_id)
    assert (outcome.status, outcome.error_code) == ("failed", "usage_limit")


# ---------- API ----------


async def test_list_agents_shows_missing_integrations(client_for) -> None:
    c = await client_for("alice")
    async with get_sessionmaker()() as db:
        await sync_system_agents(db)
        await db.commit()
    agents = {a["slug"]: a for a in (await c.get("/api/agents")).json()}
    assert list(agents) == ["ci-failures", "jira-digest", "k8s-health", "morning-brief"]
    assert agents["k8s-health"]["integrations"] == ["kubernetes"] and agents["k8s-health"]["missing"] == ["kubernetes"]
    assert agents["jira-digest"]["inputs_schema"]["required"] == ["jql"] and agents["jira-digest"]["system"] is True
    assert "system_prompt" not in agents["jira-digest"]
    assert agents["morning-brief"]["integrations"] == ["feed", "gerrit", "gitlab", "jenkins", "jira"]
    assert json.dumps(agents)  # serialisable


async def test_disabled_and_other_users_agents_are_hidden(client_for, make_user) -> None:
    c = await client_for("alice")
    bob = await make_user("bob")
    seed = load_seeds()[0]
    async with get_sessionmaker()() as db:
        await sync_system_agents(db)
        db.add(Agent(**(seed.values() | {"slug": "bobs-agent"}), owner_id=bob.id))
        await db.execute(update(Agent).where(Agent.slug == "jira-digest").values(enabled=False))
        await db.commit()
    slugs = [a["slug"] for a in (await c.get("/api/agents")).json()]
    assert slugs == ["ci-failures", "k8s-health", "morning-brief"]

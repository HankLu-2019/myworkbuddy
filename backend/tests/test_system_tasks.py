"""Phase 2 system tasks and agent evals (design 02 §3, §9; plan S8)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import func, select, update

from app.agents import evals
from app.agents.seeds import load_seeds, sync_system_agents
from app.db import get_sessionmaker
from app.inbox.service import add_item
from app.knowledge.ingest import refresh
from app.llm.models import set_model_override
from app.models import Agent, ChatSession, InboxItem, OutboundEmail, Project, Run, ScheduleProposal, User
from app.schedules.retention import prune_runs
from app.tasks import app as tasks

NOW = datetime(2026, 10, 22, 3, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def agents() -> None:
    async with get_sessionmaker()() as db:
        await sync_system_agents(db)
        await db.commit()


# ---------- projects_refresh ----------


def test_head_from_ls_remote() -> None:
    out = "aaa111\tHEAD\nbbb222\trefs/heads/main\nccc333\trefs/heads/dev\n"
    assert refresh.head_from_ls_remote(out, None) == "aaa111"
    assert refresh.head_from_ls_remote(out, "dev") == "ccc333"
    assert refresh.head_from_ls_remote(out, "missing") is None


async def _project(
    owner: User, name: str, status: str = "ready", sha: str | None = "old", branch: str | None = None
) -> int:
    async with get_sessionmaker()() as db:
        p = Project(
            owner_id=owner.id, visibility="private", name=name, repo_url=f"https://gitlab.example.com/t/{name}.git",
            host="gitlab", status=status, last_ingested_sha=sha, branch=branch,
        )  # fmt: skip
        db.add(p)
        await db.commit()
        return p.id


async def test_projects_refresh_queues_moved_repositories(make_user, monkeypatch, task_queue) -> None:
    alice = await make_user("alice")
    moved = await _project(alice, "moved")
    same = await _project(alice, "same", sha="new")
    broken = await _project(alice, "broken")
    await _project(alice, "queued", status="queued")  # not ready: not checked
    branch = await _project(alice, "branch", branch="dev", sha="dev-old")
    asked: list[tuple[str, ...]] = []

    async def fake_git(*args: str, **kwargs: Any) -> str:
        asked.append(args)
        assert kwargs["timeout"] == refresh.LS_REMOTE_TIMEOUT and "auth_header" in kwargs
        if "broken" in args[2]:
            raise RuntimeError("git ls-remote failed: could not resolve host")
        return "new\tHEAD\ndev-new\trefs/heads/dev\n"

    monkeypatch.setattr(refresh, "run_git", fake_git)
    counts = await tasks.projects_refresh(timestamp=0)
    assert counts == {"checked": 4, "moved": 2, "queued": 2, "failed": 1}
    assert [a[3] for a in asked if a[0] == "ls-remote"] == ["HEAD", "HEAD", "HEAD", "refs/heads/dev"]
    jobs = sorted(j["args"]["project_id"] for j in task_queue.jobs.values() if j["task_name"] == "ingest_project")
    assert jobs == sorted([moved, branch])
    async with get_sessionmaker()() as db:
        assert (await db.get(Project, broken)).status == "ready"  # last night's index still stands
    assert same not in jobs


# ---------- runs_retention ----------


async def _run(owner: User, age_days: float, status: str = "succeeded") -> Run:
    async with get_sessionmaker()() as db:
        agent = await db.scalar(select(Agent).limit(1))
        session = ChatSession(owner_id=owner.id, source="run", title="t")
        db.add(session)
        await db.flush()
        at = NOW - timedelta(days=age_days)
        run = Run(owner_id=owner.id, agent_id=agent.id, trigger="schedule", status=status, config={},
                  scheduled_for=at, session_id=session.id)  # fmt: skip
        db.add(run)
        await db.flush()
        await db.execute(update(Run).where(Run.id == run.id).values(created_at=at))
        add_item(db, owner.id, kind="run", severity="info", title="t", run_id=run.id)
        await db.commit()
        return run


async def test_runs_retention_deletes_old_runs_with_their_transcripts(make_user) -> None:
    alice = await make_user("alice")
    old = await _run(alice, 200)
    recent = await _run(alice, 10)
    stuck = await _run(alice, 200, status="running")
    chat = None
    async with get_sessionmaker()() as db:
        chat = ChatSession(owner_id=alice.id, source="chat", title="keep me")
        db.add(chat)
        old_email = OutboundEmail(owner_id=alice.id, to_addr="a@x", subject="s", body_text="b", dedupe_key="old",
                                  status="sent", created_at=NOW - timedelta(days=100))  # fmt: skip
        pending_email = OutboundEmail(owner_id=alice.id, to_addr="a@x", subject="s", body_text="b", dedupe_key="p",
                                      created_at=NOW - timedelta(days=100))  # fmt: skip
        db.add_all([old_email, pending_email])
        await db.flush()
        db.add(ScheduleProposal(owner_id=alice.id, session_id=chat.id, payload={}, status="dismissed",
                                expires_at=NOW, created_at=NOW - timedelta(days=40)))  # fmt: skip
        await db.commit()
    counts = await prune_runs(NOW)
    assert counts == {"runs": 1, "run_sessions": 1, "emails": 1, "proposals": 1}
    async with get_sessionmaker()() as db:
        assert {r.id for r in await db.scalars(select(Run))} == {recent.id, stuck.id}
        assert await db.get(ChatSession, old.session_id) is None
        assert await db.get(ChatSession, chat.id) is not None
        assert await db.scalar(select(func.count()).select_from(InboxItem)) == 2
        assert [e.dedupe_key for e in await db.scalars(select(OutboundEmail))] == ["p"]
    assert await prune_runs(NOW) == {"runs": 0, "run_sessions": 0, "emails": 0, "proposals": 0}


# ---------- agent evals ----------


def test_every_eval_case_fits_its_agent() -> None:
    cases = evals.load_cases()
    agents = {s.slug: s for s in load_seeds()}
    reporting = {slug for slug, s in agents.items() if s.report}  # the planner has its own tests (test_planner)
    assert {c.agent for c in cases} == reporting  # every report agent has at least one case
    for case in cases:
        assert set(case.tools) <= set(agents[case.agent].tools), case.name
        assert case.expect_status in ("ok", "warning", "alert")
        for recordings in case.tools.values():
            assert all("result" in r for r in recordings)


# Arguments each tool requires, for calls whose recording doesn't specify them.
REQUIRED_ARGS: dict[str, dict[str, Any]] = {
    "k8s_get": {"kind": "pods"},
    "k8s_events": {"namespace": "prod"},
    "k8s_pod_logs": {"namespace": "prod", "pod": "x"},
    "gitlab_pipeline_status": {"project": "team/etl", "ref": "main"},
    "gitlab_job_log_tail": {"project": "team/etl", "job_id": 1},
    "gitlab_list_mrs": {"project": "team/etl"},
    "jira_search": {"jql": "x"},
    "jira_get_issue": {"key": "SPIN-4412"},
    "gerrit_query": {"query": "x"},
    "feed_latest": {"url": "https://status.example.com/feed.xml"},
    "search_knowledge": {"query": "x"},
    "get_session": {"session_id": "00000000-0000-0000-0000-000000000000"},
}


def _replaying_model(case: evals.Case, fingerprints: list[str], status: str = "alert") -> FunctionModel:
    """Calls each recorded tool once with its recorded args, then reports the given fingerprints."""
    calls = [(name, r.get("args") or {}) for name, recs in case.tools.items() for r in recs[:1]]

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        output = info.output_tools[0].name
        done = sum(isinstance(p, ToolReturnPart) and p.tool_name != output for m in messages for p in m.parts)
        if done < len(calls):
            name, args = calls[done]
            required = REQUIRED_ARGS.get(name, {})
            return ModelResponse(parts=[ToolCallPart(name, required | args, tool_call_id=f"c{done}")])
        severity = status if status != "ok" else "info"
        findings = [{"fingerprint": f, "severity": severity, "title": f, "detail": ""} for f in fingerprints]
        report = {"status": status, "headline": "h", "findings": findings, "summary_md": ""}
        return ModelResponse(parts=[ToolCallPart(output, report, tool_call_id="out")])

    return FunctionModel(respond)


async def _admin(make_user) -> User:
    return await make_user("evaluator", role="admin")


async def test_an_eval_passes_when_the_report_matches(make_user) -> None:
    await _admin(make_user)
    [case] = evals.load_cases(agent="k8s-health")[:1]
    set_model_override(_replaying_model(case, ["k8s:prod/payments-7d9f-abcde:OOMKilled"]))  # qualifier differs
    [result] = [r for r in await evals.run_evals("evaluator", "k8s-health") if r.case == case.name]
    assert result.passed and result.missing == [] and result.unexpected == []


async def test_invented_and_missed_findings_fail(make_user) -> None:
    await _admin(make_user)
    case = next(c for c in evals.load_cases(agent="k8s-health") if c.name == "crashloop")
    set_model_override(_replaying_model(case, ["k8s:prod/ghost-pod:CrashLoopBackOff"]))
    [result] = [r for r in await evals.run_evals("evaluator", "k8s-health") if r.case == "crashloop"]
    assert not result.passed and result.missing == ["prod/payments-7d9f-abcde"] and result.unverified == 1
    s = evals.summary([result])
    assert s["passed"] == 0 and s["failures"][0]["case"] == "k8s-health/crashloop"


async def test_every_case_can_pass_with_a_model_that_reports_what_it_should(make_user) -> None:
    """Guards the cases themselves: each expected fingerprint is verifiable from its recordings."""
    await _admin(make_user)
    for case in evals.load_cases():
        set_model_override(_replaying_model(case, case.expect_fingerprints, case.expect_status))
        [result] = [r for r in await evals.run_evals("evaluator", case.agent) if r.case == case.name]
        assert result.passed, (case.agent, case.name, result)


async def test_evals_need_a_user_and_report_errors(make_user, monkeypatch) -> None:
    with pytest.raises(evals.CaseError):
        await evals.run_evals("nobody")
    await _admin(make_user)

    async def broken(*a, **k):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(evals.llm, "model_for", broken)
    results = await evals.run_evals("evaluator", "jira-digest")
    assert results and all(not r.passed and "gateway down" in r.error for r in results)


async def test_nightly_evals_are_off_by_default_and_reported_on_system(client_for, monkeypatch) -> None:
    from app.config import get_settings
    from app.ops.health import put_state

    assert await tasks.agent_evals(timestamp=0) is None
    admin = await client_for("root", role="admin")
    await put_state("agent_evals", {"passed": 4, "total": 5, "cost_usd": 0.1, "failures": [{"case": "x"}]})
    body = (await admin.get("/api/admin/system")).json()
    assert body["agent_evals"]["passed"] == 4
    assert "Agent evals: 1 of 5 cases failed last night" in body["alerts"]
    monkeypatch.setattr(get_settings(), "agent_evals_user", "root")
    set_model_override(_replaying_model(evals.load_cases(agent="jira-digest")[0], ["jira:SPIN-4412"], "warning"))
    assert await tasks.agent_evals(timestamp=0) is not None

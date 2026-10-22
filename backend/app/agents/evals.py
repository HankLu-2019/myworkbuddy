"""Agent evals (design 02 §3, plan S8): recorded tool outputs → expected fingerprints, against the real model.

Each case in `app/agents/eval_cases/<agent slug>/<case>.yaml` gives the run's prompt and inputs, what each tool returns
(recorded connector output, replayed in place of the real connectors), and what the report must contain:

    inputs: {namespaces: [prod]}
    tools:
      k8s_get:
        - args: {kind: pods}          # optional: the first entry whose args are a subset of the call's
          result: {kind: pods, items: [...], truncated: false}
    expect:
      status: alert
      fingerprints: ["k8s:prod/payments-7d9f-abcde:CrashLoopBackOff"]

Fingerprints are compared by their id part (`app.agents.report.fingerprint_id`), because the qualifier is the
model's wording; a case passes when the verified fingerprints match the expected ones exactly and the status
matches. Untrusted tool output is wrapped exactly as in production. Runs use the evaluating user's gateway key,
limits and budget (rule R11), so `mwb eval-agents` and the nightly `agent_evals` task need a user.

The nightly task stores its results in the ops state `agent_evals`, shown on Admin → System. It's not a CI gate:
CI has no model. Tests run the same code with a scripted model.
"""

import functools
import inspect
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic_ai import Agent as PydanticAgent
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy import select

from app.agents.executor import RunDeps
from app.agents.report import RunReport, check_report, evidence, fingerprint_id
from app.db import get_sessionmaker
from app.llm import client as llm
from app.llm import usage
from app.llm.untrusted import wrap
from app.models import Agent, User
from app.tools.registry import REGISTRY, _dump

CASES_DIR = Path(__file__).parent / "eval_cases"
STATE_KEY = "agent_evals"


@dataclass(frozen=True)
class Case:
    agent: str
    name: str
    description: str
    prompt: str
    inputs: dict[str, Any]
    tools: dict[str, list[dict[str, Any]]]
    expect_status: str
    expect_fingerprints: list[str]


@dataclass
class CaseResult:
    agent: str
    case: str
    passed: bool
    status: str | None = None
    expected_status: str | None = None
    missing: list[str] = field(default_factory=list)  # expected ids not reported (verified)
    unexpected: list[str] = field(default_factory=list)  # verified ids that weren't expected
    unverified: int = 0
    error: str | None = None
    cost_usd: Decimal = Decimal(0)
    seconds: float = 0.0


class CaseError(ValueError):
    pass


def load_cases(directory: Path = CASES_DIR, agent: str | None = None) -> list[Case]:
    cases = []
    for path in sorted(directory.glob("*/*.yaml")):
        slug = path.parent.name
        if agent and slug != agent:
            continue
        doc = yaml.safe_load(path.read_text()) or {}
        expect = doc.get("expect") or {}
        if "status" not in expect or "fingerprints" not in expect:
            raise CaseError(f"{slug}/{path.name}: expect needs status and fingerprints")
        cases.append(
            Case(
                agent=slug,
                name=path.stem,
                description=doc.get("description", ""),
                prompt=doc.get("prompt", ""),
                inputs=doc.get("inputs") or {},
                tools=doc.get("tools") or {},
                expect_status=expect["status"],
                expect_fingerprints=list(expect["fingerprints"]),
            )  # fmt: skip
        )
    return cases


def _matches(recorded: dict[str, Any] | None, args: dict[str, Any]) -> bool:
    return all(args.get(k) == v for k, v in (recorded or {}).items())


def replay_toolset(tool_names: list[str], case: Case, results: list[Any]) -> FunctionToolset:
    """The agent's tools with the same names, docs and signatures, answering from the case's recordings."""
    functions = []
    for name in tool_names:
        spec = REGISTRY[name]
        recordings = case.tools.get(name, [])

        def make(spec=spec, recordings=recordings):
            @functools.wraps(spec.fn)
            async def tool(ctx: RunContext[Any], *args: Any, **kwargs: Any) -> Any:
                bound = inspect.signature(spec.fn).bind_partial(ctx, *args, **kwargs).arguments
                call = {k: v for k, v in bound.items() if k != "ctx"}
                hit = next((r for r in recordings if _matches(r.get("args"), call)), None)
                if hit is None:
                    return {"error": "not_found", "message": f"Nothing recorded for {spec.name}({call})"}
                result = hit["result"]
                if isinstance(result, dict) and "error" in result:
                    return result
                results.append(result)
                if spec.untrusted_output:
                    source = f"{spec.integration_kind or 'internal'}:{spec.name}"
                    return wrap(source, _dump(result), max_chars=spec.max_output_chars)
                return result

            return tool

        functions.append(make())
    return FunctionToolset(functions)


def score(case: Case, stored: dict[str, Any] | None, verified: list[str]) -> CaseResult:
    expected = {fingerprint_id(f) for f in case.expect_fingerprints}
    got = {fingerprint_id(f) for f in verified}
    status = (stored or {}).get("status")
    unverified = sum(1 for f in (stored or {}).get("findings", []) if not f.get("verified"))
    return CaseResult(
        agent=case.agent, case=case.name, passed=got == expected and status == case.expect_status, status=status,
        expected_status=case.expect_status, missing=sorted(expected - got), unexpected=sorted(got - expected),
        unverified=unverified,
    )  # fmt: skip


async def run_case(agent: Agent, user: User, case: Case) -> CaseResult:
    from app.agents.executor import compose_prompt

    results: list[Any] = []
    started = time.monotonic()
    with usage.track() as tracked:
        try:
            model = await llm.model_for(user.id, agent.model_alias)  # type: ignore[arg-type]
            runner = PydanticAgent(
                model,
                deps_type=RunDeps,
                instructions=agent.system_prompt,
                toolsets=[replay_toolset(list(agent.tools), case, results)],
                output_type=RunReport,
            )
            result = await llm.run(
                runner,
                compose_prompt(case.prompt, case.inputs),
                user_id=user.id,
                purpose="scheduled_run",
                deps=RunDeps(user=user, session_id=uuid.uuid4()),
            )
            stored, verified = check_report(result.output, evidence(results))
            out = score(case, stored, verified)
        except Exception as exc:  # an eval that errors is a failed eval, reported like the others
            out = CaseResult(agent=case.agent, case=case.name, passed=False, error=f"{type(exc).__name__}: {exc}"[:300])
    out.cost_usd, out.seconds = tracked.cost_usd, round(time.monotonic() - started, 1)
    return out


async def run_evals(username: str, agent: str | None = None) -> list[CaseResult]:
    async with get_sessionmaker()() as db:
        user = await db.scalar(select(User).where(User.username == username, User.is_active))
        if user is None:
            raise CaseError(f"No active user {username!r} to run the evals as")
        agents = {a.slug: a for a in await db.scalars(select(Agent).where(Agent.owner_id.is_(None)))}
        db.expunge_all()
    out = []
    for case in load_cases(agent=agent):
        if case.agent not in agents:
            out.append(
                CaseResult(agent=case.agent, case=case.name, passed=False, error="agent not found; run mwb sync-agents")
            )
            continue
        out.append(await run_case(agents[case.agent], user, case))
    return out


def summary(results: list[CaseResult]) -> dict[str, Any]:
    return {
        "passed": sum(r.passed for r in results),
        "total": len(results),
        "cost_usd": float(sum((r.cost_usd for r in results), Decimal(0))),
        "failures": [
            {"case": f"{r.agent}/{r.case}", "missing": r.missing, "unexpected": r.unexpected, "status": r.status,
             "expected_status": r.expected_status, "error": r.error}
            for r in results
            if not r.passed
        ],
    }  # fmt: skip

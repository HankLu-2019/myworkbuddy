"""`execute_agent_run()`: run a report agent once and say what happened (design 02 §4 "Executing a run", steps
2 and 3). Schedules (S4), manual runs and test runs (S6) all call it; they own the `runs` row and decide on
notifications from the returned outcome.

The caller creates the run session (`source='run'`) first. This function, with no transaction open across
network calls (rule R1):

1. saves the prompt as the session's user message;
2. runs the agent with the tools `toolset_for` allows, `output_type=RunReport`, the `scheduled_run` limits and
   the caller's timeout, saving each tool call as chat does;
3. checks the report (`app.agents.report.check_report`) against this run's tool results;
4. saves the report as the assistant message, with usage and cost.

It never raises for a failed run; the outcome carries `error_code`.
"""

import json
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from pydantic_ai import Agent as PydanticAgent
from pydantic_ai import RunContext
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.toolsets import WrapperToolset
from pydantic_core import to_jsonable_python
from sqlalchemy import func, update

from app.agents.report import RunReport, check_report, evidence, report_markdown
from app.db import get_sessionmaker
from app.errors import AppError
from app.llm import client as llm
from app.llm import usage
from app.logs import log
from app.models import Agent, ChatSession, Message, ToolCall, User
from app.tools.registry import toolset_for

RunStatus = Literal["succeeded", "failed", "timed_out"]
ERROR_CHARS = 500


@dataclass
class RunDeps:
    """What tools see as `ctx.deps` (the knowledge tools need `user` and `citations`, like chat)."""

    user: User
    session_id: uuid.UUID
    citations: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RunOutcome:
    status: RunStatus
    report: dict[str, Any] | None = None  # RunReport as stored: status recomputed, findings marked `verified`
    fingerprints: list[str] = field(default_factory=list)  # verified only, sorted
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: Decimal = Decimal(0)
    error_code: str | None = None  # budget_exceeded | timeout | usage_limit | invalid_report | llm_unavailable
    error: str | None = None


def compose_prompt(prompt: str, inputs: dict[str, Any]) -> str:
    """The user message: the schedule's own text and inputs (typed by the owner, so not untrusted)."""
    parts = [prompt.strip() or "Run your task now."]
    if inputs:
        parts.append("Inputs:\n```json\n" + json.dumps(inputs, indent=2, sort_keys=True) + "\n```")
    return "\n\n".join(parts)


async def execute_agent_run(
    agent: Agent,
    user: User,
    *,
    session_id: uuid.UUID,
    prompt: str = "",
    inputs: dict[str, Any] | None = None,
    max_runtime_s: float = 300,
) -> RunOutcome:
    """`agent` and `user` are loaded (and may be detached); `session_id` is the run session."""
    maker = get_sessionmaker()
    content = compose_prompt(prompt, inputs or {})
    async with maker() as db:
        db.add(Message(session_id=session_id, role="user", content=content))
        await db.commit()

    deps = RunDeps(user=user, session_id=session_id)
    recorder: _Recorder | None = None

    outcome = RunOutcome(status="failed")
    result = None
    with usage.track() as tracked:
        try:
            recorder = _Recorder(wrapped=await toolset_for(agent, user), session_id=session_id)
            model = await llm.model_for(user.id, agent.model_alias)  # type: ignore[arg-type]
            runner = PydanticAgent(
                model, deps_type=RunDeps, instructions=agent.system_prompt, toolsets=[recorder], output_type=RunReport
            )
            result = await llm.run(
                runner,
                content,
                user_id=user.id,
                purpose="scheduled_run",
                timeout=max_runtime_s,
                deps=deps,
            )
        except AppError as exc:
            outcome.error_code, outcome.error = exc.code, str(exc.detail)
        except TimeoutError:
            outcome.status = "timed_out"
            outcome.error_code, outcome.error = "timeout", f"The run took longer than {int(max_runtime_s)} s."
        except UsageLimitExceeded:
            outcome.error_code, outcome.error = "usage_limit", "The run needed more model calls than allowed."
        except UnexpectedModelBehavior as exc:
            outcome.error_code = "invalid_report"
            outcome.error = f"The model didn't return a valid report: {exc}"[:ERROR_CHARS]
        except Exception as exc:
            log.exception("agent_run.failed", agent=agent.slug)
            outcome.error_code = "llm_unavailable"
            outcome.error = f"The model call failed: {type(exc).__name__}"

    outcome.cost_usd = tracked.cost_usd
    if result is not None:
        outcome.status = "succeeded"
        outcome.report, outcome.fingerprints = check_report(
            result.output, evidence(recorder.results if recorder else [])
        )
        run_usage = result.usage
        outcome.tokens_in, outcome.tokens_out = run_usage.input_tokens, run_usage.output_tokens

    await _save(session_id, outcome, result, deps, recorder.rows if recorder else [])
    log.info(
        "agent_run.done",
        agent=agent.slug,
        status=outcome.status,
        error_code=outcome.error_code,
        findings=len((outcome.report or {}).get("findings", [])),
        verified=len(outcome.fingerprints),
        cost_usd=float(outcome.cost_usd),
    )
    return outcome


@dataclass
class _Recorder(WrapperToolset[Any]):
    """Saves each tool call to the run session as it happens (as chat does), and keeps the results that
    fingerprints are checked against. Each save is its own short transaction (rule R1)."""

    session_id: uuid.UUID | None = None
    results: list[Any] = field(default_factory=list)  # shared by copies made with dataclasses.replace
    rows: list[int] = field(default_factory=list)

    async def call_tool(self, name: str, tool_args: dict[str, Any], ctx: RunContext[Any], tool: Any) -> Any:
        maker = get_sessionmaker()
        async with maker() as db:
            row = ToolCall(session_id=self.session_id, call_id=ctx.tool_call_id, tool_name=name, args=tool_args)
            db.add(row)
            await db.commit()
            self.rows.append(row.id)
        status = "failed"
        result: Any = None
        try:
            result = await super().call_tool(name, tool_args, ctx, tool)
            status = "done"
            return result
        finally:
            stored = to_jsonable_python(result)
            if status == "done":
                self.results.append(stored)
            async with maker() as db:
                await db.execute(update(ToolCall).where(ToolCall.id == row.id).values(status=status, result=stored))
                await db.commit()


async def _save(session_id: uuid.UUID, outcome: RunOutcome, result: Any, deps: RunDeps, tool_ids: list[int]) -> None:
    async with get_sessionmaker()() as db:
        ok = outcome.report is not None
        msg = Message(
            session_id=session_id,
            role="assistant",
            content=report_markdown(outcome.report) if ok else (outcome.error or "The run failed."),
            raw=to_jsonable_python(result.new_messages()) if ok else None,
            meta={"citations": deps.citations, **({} if ok else {"error_code": outcome.error_code})},
            status="complete" if ok else "failed",
            tokens_in=outcome.tokens_in,
            tokens_out=outcome.tokens_out,
            cost_usd=outcome.cost_usd,
        )
        db.add(msg)
        await db.flush()
        if tool_ids:
            await db.execute(update(ToolCall).where(ToolCall.id.in_(tool_ids)).values(message_id=msg.id))
        await db.execute(update(ChatSession).where(ChatSession.id == session_id).values(updated_at=func.now()))
        await db.commit()

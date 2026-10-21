"""The one way to call an LLM (rule R11, design 01 §H12).

- `model_for(user_id, alias)`: a model that talks to the gateway with the user's own virtual key, so LiteLLM
  enforces their budget and records their spend.
- `limits_for(purpose)` / `timeout_for(purpose)`: request and token limits and a wall-clock timeout per purpose.
- `run(agent, prompt, user_id=..., purpose=...)`: runs an agent with those limits, records cost, and maps the
  gateway's budget error to `budget_exceeded`.
- `embedder_for(user_id)`: the embedder with the user's key.

Every response passes through one HTTP client whose hook feeds `app.llm.usage` (cost per call).
"""

import asyncio
from decimal import Decimal
from typing import Any, Literal

import httpx2
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from app.config import get_settings
from app.db import get_sessionmaker
from app.errors import AppError
from app.llm import embeddings, keys, models, usage
from app.logs import log
from app.models import User

Alias = Literal["chat", "fast", "planner", "coder"]
Purpose = Literal[
    "chat_turn", "episode", "librarian_page", "project_card", "code_summary", "scheduled_run", "plan_draft"
]

# purpose: (request_limit, total_tokens_limit, timeout seconds) — the table in design 01 §H12.
LIMITS: dict[str, tuple[int, int, float]] = {
    "chat_turn": (8, 200_000, 180),
    "episode": (2, 120_000, 120),
    "librarian_page": (2, 150_000, 180),
    "project_card": (2, 120_000, 180),
    "code_summary": (2, 30_000, 60),
    "scheduled_run": (12, 300_000, 600),
    "plan_draft": (20, 400_000, 600),
}

_transport: httpx2.AsyncBaseTransport | None = None
_http: httpx2.AsyncClient | None = None


def set_transport(transport: httpx2.AsyncBaseTransport | None) -> None:
    """Tests route model calls to the fake gateway (tests/fakellm) instead of the network."""
    global _transport, _http
    _transport, _http = transport, None


async def _on_response(response: httpx2.Response) -> None:
    usage.record(response.headers)


def _client() -> httpx2.AsyncClient:
    global _http
    if _http is None:
        _http = httpx2.AsyncClient(
            timeout=httpx2.Timeout(600, connect=10),
            event_hooks={"response": [_on_response]},
            transport=_transport,
        )
    return _http


def _model_name(alias: Alias) -> str:
    s = get_settings()
    return {"chat": s.chat_model, "fast": s.fast_model, "planner": s.planner_model, "coder": "coder"}[alias]


def limits_for(purpose: Purpose) -> UsageLimits:
    requests, tokens, _ = LIMITS[purpose]
    return UsageLimits(request_limit=requests, total_tokens_limit=tokens)


def timeout_for(purpose: Purpose) -> float:
    return LIMITS[purpose][2]


async def model_for(user_id: int, alias: Alias = "chat") -> Model:
    """A gateway model using the user's virtual key (created on first use). Tests get the override instead."""
    if (override := models.get_override()) is not None:
        return override
    key = await keys.ensure_key(user_id)
    provider = OpenAIProvider(base_url=f"{get_settings().gateway_url}/v1", api_key=key, http_client=_client())
    return OpenAIChatModel(_model_name(alias), provider=provider)


async def embedder_for(user_id: int) -> embeddings.Embedder:
    if (override := embeddings.get_override()) is not None:
        return override
    return embeddings.GatewayEmbedder(api_key=await keys.ensure_key(user_id))


def is_budget_error(exc: BaseException) -> bool:
    """LiteLLM answers an over-budget key with type `budget_exceeded` (newer) or an `ExceededTokenBudget`
    detail (older); status 400, 401 or 429 depending on the version."""
    if not isinstance(exc, ModelHTTPError):
        return False
    text = str(exc.body).lower() if exc.body is not None else ""
    return "budget_exceeded" in text or "exceededtokenbudget" in text or "budget has been exceeded" in text


async def budget_error(user_id: int) -> AppError:
    async with get_sessionmaker()() as db:
        user = await db.get(User, user_id)
        budget = keys.budget_for(user) if user else Decimal(0)
    return AppError(
        402,
        "budget_exceeded",
        f"You've reached your monthly AI budget (USD {budget:.2f}). An admin can raise it.",
        budget_usd=float(budget),
    )


async def run(agent: Agent, prompt: str, *, user_id: int, purpose: Purpose, **kwargs: Any) -> Any:
    """`agent.run` with the purpose's limits and timeout. Returns the result; `result.cost_usd` holds the cost.

    Raises AppError(budget_exceeded) when the user's key is over budget, and TimeoutError on timeout.
    """
    with usage.track() as u:
        try:
            async with asyncio.timeout(timeout_for(purpose)):
                result = await agent.run(prompt, usage_limits=limits_for(purpose), **kwargs)
        except ModelHTTPError as exc:
            if is_budget_error(exc):
                log.warning("llm.budget_exceeded", user_id=user_id, purpose=purpose)
                raise await budget_error(user_id) from None
            raise
    result.cost_usd = u.cost_usd  # type: ignore[attr-defined]
    log.info("llm.run", user_id=user_id, purpose=purpose, cost_usd=float(u.cost_usd), calls=u.calls)
    return result

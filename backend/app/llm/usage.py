"""Per-run LLM cost accounting (design 01 §H12).

The LLM HTTP client's response hook calls `record()` for every gateway response. Callers wrap a run in
`track()` and read the result afterwards. Uses a context variable, so concurrent runs don't mix.

LiteLLM puts the call's cost in `x-litellm-response-cost`. Streamed responses send their headers before the
cost is known, so for those only the call id (`x-litellm-call-id`) is kept, and `settle_costs` looks the cost up
in the gateway's spend log later.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


@dataclass
class Usage:
    cost_usd: Decimal = Decimal(0)
    calls: int = 0
    unpriced_call_ids: list[str] = field(default_factory=list)  # streamed calls; settled later


_current: ContextVar[Usage | None] = ContextVar("llm_usage", default=None)


@contextmanager
def track() -> Iterator[Usage]:
    """Nested tracks also add to the enclosing one, so a caller sees the cost of a run that raised."""
    usage = Usage()
    token = _current.set(usage)
    try:
        yield usage
    finally:
        _current.reset(token)
        if (outer := _current.get()) is not None:
            outer.cost_usd += usage.cost_usd
            outer.calls += usage.calls
            outer.unpriced_call_ids.extend(usage.unpriced_call_ids)


def record(headers: Mapping[str, str]) -> None:
    usage = _current.get()
    if usage is None:
        return
    usage.calls += 1
    raw = headers.get("x-litellm-response-cost")
    try:
        cost = Decimal(raw) if raw not in (None, "", "None") else None
    except InvalidOperation:
        cost = None
    if cost is not None and cost > 0:
        usage.cost_usd += cost
    elif call_id := headers.get("x-litellm-call-id"):
        usage.unpriced_call_ids.append(call_id)

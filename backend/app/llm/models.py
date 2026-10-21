"""Test override for LLM models. Real models come from `app.llm.client.model_for` (rule R11)."""

from collections.abc import Callable

from pydantic_ai.models import Model

_override: Model | None = None
# Tests install a wrapper that checks every model call (e.g. that no DB transaction is open, rule R1).
_override_wrapper: Callable[[Model], Model] | None = None


def set_override_wrapper(wrapper: Callable[[Model], Model] | None) -> None:
    global _override_wrapper
    _override_wrapper = wrapper


def set_model_override(model: Model | None) -> None:
    """Tests use this to swap the gateway model for a TestModel/FunctionModel."""
    global _override
    _override = _override_wrapper(model) if model is not None and _override_wrapper is not None else model


def get_override() -> Model | None:
    return _override

from collections.abc import Callable

from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from app.config import get_settings

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


def get_chat_model(alias: str | None = None) -> Model:
    if _override is not None:
        return _override
    s = get_settings()
    provider = OpenAIProvider(base_url=f"{s.gateway_url}/v1", api_key=s.gateway_key or "unused")
    return OpenAIChatModel(alias or s.chat_model, provider=provider)

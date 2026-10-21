import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCalls, FunctionModel


def scripted_model(text: Callable[[str], str] | str = "ok", structured: dict[str, Any] | None = None) -> FunctionModel:
    """A model that returns `structured` when the agent wants structured output, otherwise `text`.

    `text` may be a function of the last user prompt, so tests can assert what the agent was asked.
    """

    def last_prompt(messages: list[ModelMessage]) -> str:
        for part in reversed(messages[-1].parts):
            content = getattr(part, "content", None)
            if isinstance(content, str):
                return content
        return ""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if info.output_tools:
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, structured or {})])
        return ModelResponse(parts=[TextPart(text(last_prompt(messages)) if callable(text) else text)])

    return FunctionModel(respond)


def tool_then_text(tool: str, args: dict[str, Any], answer: str) -> FunctionModel:
    """Streaming model: first turn calls `tool`, second turn streams `answer` in two pieces."""

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        if len(messages) == 1 or not any(getattr(p, "tool_name", None) == tool for p in messages[-1].parts):
            from pydantic_ai.models.function import DeltaToolCall

            yield {0: DeltaToolCall(name=tool, json_args=json.dumps(args), tool_call_id="call-1")}
            return
        half = len(answer) // 2
        yield answer[:half]
        yield answer[half:]

    return FunctionModel(stream_function=stream)


def failing_model() -> FunctionModel:
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        raise RuntimeError("bedrock unavailable")
        yield ""  # pragma: no cover

    return FunctionModel(stream_function=stream)


def parse_sse(body: str) -> list[tuple[str, Any]]:
    events = []
    for block in body.strip().split("\n\n"):
        if all(line.startswith(":") for line in block.splitlines()):
            continue  # keep-alive comment (": ping")
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        events.append((lines.get("event"), json.loads(lines.get("data", "null"))))
    return events


def slow_then_text(delay: float, answer: str = "done") -> FunctionModel:
    """Streaming model that waits `delay` seconds before answering."""

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        import asyncio

        await asyncio.sleep(delay)
        yield answer

    return FunctionModel(stream_function=stream)


def text_then_failure(text: str) -> FunctionModel:
    """Streaming model that sends some text, then fails mid-reply."""

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        yield text
        raise RuntimeError("connection reset by gateway")

    return FunctionModel(stream_function=stream)

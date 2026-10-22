"""A deterministic Anthropic Messages endpoint (`POST /v1/messages`) for Claude Code in the sandbox (design 04 §4).

LiteLLM serves this API in production; the fake answers like this, streamed or not:

- Requests without a `Bash` tool (Claude Code's background calls, e.g. titles): a short text answer.
- The agent loop, driven by tool results in the conversation:
  1. read the task: `cat .mwb/TASK.md`;
  2. make the change: append a line to the first file the task plans to create or modify (`- modify \\`path\\``),
     or create `FAKE_AGENT.md` when there is no task, and write `.mwb/notes.md`;
  3. finish with a one-line summary.
- Markers in the first user message: `[[slow:<seconds>]]` sleeps before every answer (timeouts),
  `[[fail]]` answers HTTP 500.
- Keys and budgets are the gateway's (server.py `_key_for`): a key over its budget gets LiteLLM's HTTP 400
  `budget_exceeded`, which Claude Code doesn't retry.
"""

import asyncio
import json
import re
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

router = APIRouter()
PLANNED = re.compile(r"^- (create|modify) `([^`]+)`", re.M)
SLOW = re.compile(r"\[\[slow:(\d+(?:\.\d+)?)\]\]")


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    out = []
    for block in content or []:
        if block.get("type") == "text":
            out.append(block.get("text", ""))
        elif block.get("type") == "tool_result":
            inner = block.get("content")
            out += _texts(inner) if isinstance(inner, list) else [str(inner or "")]
    return out


def _tool_results(messages: list[dict[str, Any]]) -> list[str]:
    results = []
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            for block in m["content"]:
                if block.get("type") == "tool_result":
                    inner = block.get("content")
                    results.append("\n".join(_texts(inner)) if isinstance(inner, list) else str(inner or ""))
    return results


def first_user_text(messages: list[dict[str, Any]]) -> str:
    for m in messages:
        if m.get("role") == "user":
            return "\n".join(_texts(m.get("content")))
    return ""


def decide(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The content blocks of the next assistant message."""
    tools = {t.get("name") for t in body.get("tools") or []}
    if "Bash" not in tools:
        return [{"type": "text", "text": "Fake agent session"}]
    results = _tool_results(body.get("messages") or [])
    if not results:
        return [_bash("cat .mwb/TASK.md 2>/dev/null || echo NO_TASK", "Read the task")]
    if len(results) == 1:
        planned = PLANNED.findall(results[0])
        path = planned[0][1] if planned else "FAKE_AGENT.md"
        command = (
            f"mkdir -p \"$(dirname '{path}')\" .mwb"
            f" && printf '\\n# Retry transient failures (fake agent)\\n' >> '{path}'"
            f" && printf 'Changed {path} as the task asked.\\n' > .mwb/notes.md"
        )
        return [{"type": "text", "text": f"I'll change {path}."}, _bash(command, "Make the planned change")]
    return [{"type": "text", "text": "Done: the planned change is made and the notes are in .mwb/notes.md."}]


def _bash(command: str, description: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:20]}", "name": "Bash",
            "input": {"command": command, "description": description}}  # fmt: skip


def _usage() -> dict[str, int]:
    return {"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}


def _events(message_id: str, model: str, blocks: list[dict[str, Any]]):
    def sse(event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps({'type': event, **data})}\n\n"

    yield sse("message_start", {"message": {"id": message_id, "type": "message", "role": "assistant", "model": model,
                                            "content": [], "stop_reason": None, "stop_sequence": None,
                                            "usage": _usage()}})  # fmt: skip
    for i, block in enumerate(blocks):
        if block["type"] == "text":
            yield sse("content_block_start", {"index": i, "content_block": {"type": "text", "text": ""}})
            yield sse("content_block_delta", {"index": i, "delta": {"type": "text_delta", "text": block["text"]}})
        else:
            start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
            yield sse("content_block_start", {"index": i, "content_block": start})
            partial = json.dumps(block["input"])
            yield sse(
                "content_block_delta", {"index": i, "delta": {"type": "input_json_delta", "partial_json": partial}}
            )
        yield sse("content_block_stop", {"index": i})
    stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
    yield sse("message_delta", {"delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 20}})
    yield sse("message_stop", {})


@router.post("/v1/messages")
async def messages(request: Request):
    from tests.fakellm import server

    body = await request.json()
    rec, err = server.key_for(request, body.get("model"), anthropic=True)
    if err:
        return err
    first = first_user_text(body.get("messages") or [])
    if "[[fail]]" in first:
        return JSONResponse(
            {"type": "error", "error": {"type": "api_error", "message": "fake failure"}}, status_code=500
        )
    if slow := SLOW.search(first):
        await asyncio.sleep(float(slow.group(1)))
    blocks = decide(body)
    message_id = f"msg_{uuid.uuid4().hex[:20]}"
    model = body.get("model", "fake")
    call_id = server.charge(rec, server.CHAT_COST)
    headers = {"x-litellm-call-id": call_id}
    if body.get("stream"):
        return StreamingResponse(_events(message_id, model, blocks), media_type="text/event-stream", headers=headers)
    stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
    return JSONResponse(
        {"id": message_id, "type": "message", "role": "assistant", "model": model, "content": blocks,
         "stop_reason": stop, "stop_sequence": None, "usage": _usage()},
        headers={**headers, "x-litellm-response-cost": str(server.CHAT_COST)},
    )  # fmt: skip

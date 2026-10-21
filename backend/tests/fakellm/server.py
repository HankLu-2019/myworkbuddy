"""Deterministic stand-in for the LiteLLM gateway, used by CI and local end-to-end tests (design 01 §H2).

Implements the slice of the OpenAI API the app uses:
- POST /v1/chat/completions (streaming and not), including tool calls and structured output tools
- POST /v1/embeddings (bag-of-words vectors, same scheme as app.llm.embeddings.HashEmbedder)
- GET /health/liveliness

Chat behaviour:
- If the request offers `search_knowledge` and the last message is from the user, call it with the user's text
  (a `[[tool:<name> key=value ...]]` marker in the user text picks a different tool and arguments).
- If a tool offered is an output tool (name starts with `final_result`), call it with arguments generated from its
  JSON schema.
- Otherwise answer with a fixed sentence that cites [1] when the conversation contains a tool result.
- A `[[slow:<seconds>]]` marker delays the first token; `[[fail]]` returns HTTP 500.
"""

import asyncio
import hashlib
import json
import math
import re
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="fake-llm")

ANSWER = "Here is what I found in your knowledge base [1]."
PLAIN_ANSWER = "This is a test answer from the fake model."
_MARKER = re.compile(r"\[\[(\w+)(?::([^\]\s]+))?((?:\s+\w+=[^\]\s]+)*)\]\]")


@app.get("/health/liveliness")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------- embeddings ----------


def _embed(text: str, dims: int) -> list[float]:
    vec = [0.0] * dims
    for word in text.lower().split():
        vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % dims] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@app.post("/v1/embeddings")
async def embeddings(request: Request) -> dict[str, Any]:
    body = await request.json()
    inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
    dims = int(body.get("dimensions") or 1024)
    return {
        "object": "list",
        "model": "fake-embed",
        "data": [{"object": "embedding", "index": i, "embedding": _embed(t, dims)} for i, t in enumerate(inputs)],
        "usage": {"prompt_tokens": sum(len(t.split()) for t in inputs), "total_tokens": 0},
    }


# ---------- chat ----------


def _schema_value(schema: dict[str, Any], defs: dict[str, Any]) -> Any:
    if "$ref" in schema:
        return _schema_value(defs[schema["$ref"].split("/")[-1]], defs)
    if "default" in schema:
        return schema["default"]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [s for s in schema[key] if s.get("type") != "null"] or schema[key]
            return _schema_value(options[0], defs)
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    kind = schema.get("type", "object")
    if kind == "object":
        props = schema.get("properties", {})
        return {k: _schema_value(v, defs) for k, v in props.items() if k in schema.get("required", [])}
    if kind == "array":
        return [_schema_value(schema["items"], defs)] if schema.get("minItems") and "items" in schema else []
    return {"string": "fake", "integer": 0, "number": 0, "boolean": False}.get(kind, None)


def _text_of(message: dict[str, Any]) -> str:
    content = message.get("content") or ""
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content)


def _decide(body: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    """Returns (text, tool_calls)."""
    messages = body.get("messages", [])
    tools = {t["function"]["name"]: t["function"] for t in body.get("tools", []) if t.get("type") == "function"}
    last = messages[-1] if messages else {}
    user_text = _text_of(last) if last.get("role") == "user" else ""
    marker = _MARKER.search(user_text)

    if marker and marker.group(1) == "tool" and marker.group(2) in tools:
        args = dict(kv.split("=", 1) for kv in marker.group(3).split())
        return None, [_call(marker.group(2), args)]
    output_tool = next((n for n in tools if n.startswith("final_result")), None)
    if output_tool and last.get("role") != "tool":
        params = tools[output_tool].get("parameters", {})
        return None, [_call(output_tool, _schema_value(params, params.get("$defs", {})))]
    if last.get("role") == "user" and "search_knowledge" in tools:
        query = _MARKER.sub("", user_text).strip()[:200] or "test"
        return None, [_call("search_knowledge", {"query": query})]
    if output_tool:  # after other tool results, still finish through the output tool
        params = tools[output_tool].get("parameters", {})
        return None, [_call(output_tool, _schema_value(params, params.get("$defs", {})))]
    used_tool = any(m.get("role") == "tool" for m in messages)
    return (ANSWER if used_tool else PLAIN_ANSWER), []


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _usage(body: dict[str, Any], text: str | None) -> dict[str, int]:
    prompt = sum(len(_text_of(m).split()) for m in body.get("messages", []))
    completion = len((text or "").split()) + 5
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    last_text = _text_of(body["messages"][-1]) if body.get("messages") else ""
    for m in _MARKER.finditer(last_text):
        if m.group(1) == "fail":
            return JSONResponse({"error": {"message": "fake failure", "type": "server_error"}}, status_code=500)
        if m.group(1) == "slow":
            await asyncio.sleep(float(m.group(2) or 1))
    text, tool_calls = _decide(body)
    base = {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "created": int(time.time()), "model": body.get("model", "fake")}
    finish = "tool_calls" if tool_calls else "stop"
    usage = _usage(body, text)
    headers = {"x-litellm-response-cost": "0.001"}

    if not body.get("stream"):
        message: dict[str, Any] = {"role": "assistant", "content": text}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return JSONResponse(
            {
                **base,
                "object": "chat.completion",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": usage,
            },
            headers=headers,
        )

    async def events():
        def chunk(delta: dict[str, Any], finish_reason: str | None = None, **extra: Any) -> str:
            data = {
                **base,
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
                **extra,
            }
            return f"data: {json.dumps(data)}\n\n"

        yield chunk({"role": "assistant", "content": ""})
        if tool_calls:
            for i, call in enumerate(tool_calls):
                yield chunk({"tool_calls": [{"index": i, **call}]})
        else:
            for word in re.findall(r"\S+\s*", text or ""):
                yield chunk({"content": word})
                await asyncio.sleep(0)
        yield chunk({}, finish)
        final = {**base, "object": "chat.completion.chunk", "choices": [], "usage": usage}
        yield f"data: {json.dumps(final)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers=headers)

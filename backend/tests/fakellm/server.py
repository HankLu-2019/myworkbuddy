"""Deterministic stand-in for the LiteLLM gateway, used by CI and local end-to-end tests (design 01 §H2).

Implements the slice of the OpenAI API the app uses:
- POST /v1/chat/completions (streaming and not), including tool calls and structured output tools
- POST /v1/embeddings (bag-of-words vectors, same scheme as app.llm.embeddings.HashEmbedder)
- GET /health/liveliness
- LiteLLM key management (design 01 §H12): POST /key/generate, /key/update, /key/delete (keys or key_aliases),
  GET /key/list?key_alias=&return_full_object=true, GET /spend/logs?request_id=, plus POST /fake/reset for tests.

Keys: a request whose bearer token is a generated key is checked against that key's models and budget (HTTP 429
`budget_exceeded` when spend has reached max_budget, like LiteLLM) and its cost is added to the key's spend.
Any other bearer token is treated as the master key. Every chat call costs USD 0.001 and every embeddings call
USD 0.00001. Non-streamed responses carry `x-litellm-response-cost`; streamed ones only `x-litellm-call-id`,
and their cost appears in the spend log after the stream ends (as with LiteLLM).

Chat behaviour:
- If the request offers `search_knowledge` and the last message is from the user, call it with the user's text
  (a `[[tool:<name> key=value ...]]` marker in the user text picks a different tool and arguments).
- If a tool offered is an output tool (name starts with `final_result`), call it with arguments generated from its
  JSON schema.
- Otherwise answer with a fixed sentence that cites [1] when the conversation contains a tool result.
- A `[[slow:<seconds>]]` marker delays the first token; `[[fail]]` returns HTTP 500.
- Phase 2 (02 §6, 07 §5.6–5.7): when `propose_schedule` is offered and the user asks for something "every
  weekday", propose a K8s health check of the payments namespace. When `feed_latest` is offered and the prompt
  names a feed URL (a scheduled run's inputs), read it, then report one alert finding per item, with
  fingerprints built from the item links the tool returned.

- Phase 3 (07 §5.8): the planner (an output tool with a `commit_message` field) gets a fixed plan for the fake
  repository, revised with the user's feedback (tests/fakellm/forge.py).

- Phase 4 (04 §4): `POST /v1/messages`, the Anthropic API Claude Code uses in the sandbox (tests/fakellm/anthropic.py).

Also a stand-in feed at GET /fake/feed.xml with one item published a minute ago, and stand-ins for Jira and a Git
server (tests/fakellm/forge.py).
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

from tests.fakellm import anthropic, forge

app = FastAPI(title="fake-llm")
app.include_router(forge.router)
app.include_router(anthropic.router)

CHAT_COST = 0.001
EMBED_COST = 0.00001
KEYS: dict[str, dict[str, Any]] = {}
SPEND_LOGS: dict[str, dict[str, Any]] = {}


def _bearer(request: Request) -> str:
    return request.headers.get("authorization", "").removeprefix("Bearer ").strip()


def _key_for(request: Request, model: str | None) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    """(key record or None for the master key, error response)."""
    rec = KEYS.get(_bearer(request))
    if rec is None:
        return None, None
    if model and rec["models"] and model not in rec["models"]:
        err = {"message": f"key not allowed to access model {model}", "type": "key_model_access_denied"}
        return rec, JSONResponse({"error": {**err, "param": "model", "code": "403"}}, status_code=403)
    if rec["max_budget"] is not None and rec["spend"] >= rec["max_budget"]:
        msg = (
            f"Budget has been exceeded! Key={rec['key_alias']} "
            f"Current cost: {rec['spend']}, Max budget: {rec['max_budget']}"
        )
        body = {"error": {"message": msg, "type": "budget_exceeded", "param": None, "code": "429"}}
        return rec, JSONResponse(body, status_code=429)
    return rec, None


def key_for(request: Request, model: str | None, *, anthropic: bool = False):
    """`_key_for` for the Anthropic route: a budget error is HTTP 400, which Claude Code doesn't retry."""
    rec, err = _key_for(request, model)
    if anthropic and err is not None and err.status_code == 429:
        body = json.loads(err.body)
        body["error"]["code"] = "400"
        err = JSONResponse(body, status_code=400)
    return rec, err


def _charge(rec: dict[str, Any] | None, cost: float) -> str:
    call_id = f"call-{uuid.uuid4().hex}"
    if rec is not None:
        rec["spend"] = round(rec["spend"] + cost, 8)
    SPEND_LOGS[call_id] = {"request_id": call_id, "spend": cost, "api_key_alias": rec and rec["key_alias"]}
    return call_id


def _public(rec: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in rec.items() if k != "token"}


@app.post("/fake/reset")
async def fake_reset() -> dict[str, bool]:
    KEYS.clear()
    SPEND_LOGS.clear()
    return {"ok": True}


@app.post("/key/generate")
async def key_generate(request: Request) -> dict[str, Any]:
    body = await request.json()
    key = f"sk-{uuid.uuid4().hex}"
    KEYS[key] = {
        "token": key,
        "key_alias": body.get("key_alias"),
        "user_id": body.get("user_id"),
        "models": body.get("models") or [],
        "max_budget": body.get("max_budget"),
        "budget_duration": body.get("budget_duration"),
        "duration": body.get("duration"),
        "budget_reset_at": "2026-11-20T00:00:00+00:00" if body.get("budget_duration") else None,
        "metadata": body.get("metadata") or {},
        "spend": 0.0,
    }
    return {"key": key, **_public(KEYS[key])}


@app.post("/key/update")
async def key_update(request: Request):
    body = await request.json()
    rec = KEYS.get(body.get("key", ""))
    if rec is None:
        return JSONResponse({"error": {"message": "key not found", "type": "not_found"}}, status_code=404)
    for field in ("max_budget", "models", "metadata"):
        if field in body:
            rec[field] = body[field]
    return _public(rec)


@app.post("/key/delete")
async def key_delete(request: Request) -> dict[str, Any]:
    body = await request.json()
    keys = set(body.get("keys") or [])
    aliases = set(body.get("key_aliases") or [])
    gone = [k for k, rec in KEYS.items() if k in keys or rec["key_alias"] in aliases]
    for k in gone:
        del KEYS[k]
    return {"deleted_keys": len(gone)}


@app.get("/key/list")
async def key_list(key_alias: str | None = None, return_full_object: bool = False) -> dict[str, Any]:
    rows = [r for r in KEYS.values() if key_alias is None or r["key_alias"] == key_alias]
    keys = [_public(r) for r in rows] if return_full_object else [r["key_alias"] for r in rows]
    return {"keys": keys, "total_count": len(rows), "current_page": 1, "total_pages": 1}


@app.get("/spend/logs")
async def spend_logs(request_id: str | None = None) -> list[dict[str, Any]]:
    if request_id is None:
        return list(SPEND_LOGS.values())
    return [SPEND_LOGS[request_id]] if request_id in SPEND_LOGS else []


ANSWER = "Here is what I found in your knowledge base [1]."
PLAIN_ANSWER = "This is a test answer from the fake model."
_MARKER = re.compile(r"\[\[(\w+)(?::([^\]\s]+))?((?:\s+\w+=[^\]\s]+)*)\]\]")


@app.get("/api/v4/user")
async def fake_gitlab_user(request: Request) -> JSONResponse:
    """A stand-in GitLab for end-to-end runs (02 §1 verify call): tokens starting glpat- are accepted."""
    token = request.headers.get("PRIVATE-TOKEN", "")
    if not token.startswith("glpat-"):
        return JSONResponse({"message": "401 Unauthorized"}, status_code=401)
    return JSONResponse({"id": 1, "username": "e2e-user"})


@app.get("/fake/feed.xml")
async def fake_feed() -> StreamingResponse:
    """An RSS feed for end-to-end runs of the morning brief (07 §5.7)."""
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    when = format_datetime(datetime.now(UTC) - timedelta(minutes=1))
    xml = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Status</title>'
        "<item><title>payments: 1 pod crashlooping</title><link>https://status.example.com/incidents/42</link>"
        f"<description>payments-7d9f is in CrashLoopBackOff.</description><pubDate>{when}</pubDate></item>"
        "</channel></rss>"
    )
    return StreamingResponse(iter([xml]), media_type="application/rss+xml")


PROPOSAL = {
    "name": "Payments health",
    "agent": "k8s-health",
    "cron": "0 8 * * 1-5",
    "inputs": {"namespaces": ["payments"]},
    "notify_mode": "on_alert",
}
_FEED_URL = re.compile(r"https?://[^\s\"']+\.xml")


def _last_tool_name(messages: list[dict[str, Any]]) -> str | None:
    """The tool whose result is the last message (OpenAI tool messages only carry the call id)."""
    if not messages or messages[-1].get("role") != "tool":
        return None
    call_id = messages[-1].get("tool_call_id")
    for m in reversed(messages):
        for call in m.get("tool_calls") or []:
            if call.get("id") == call_id:
                return call["function"]["name"]
    return None


def _feed_report(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """A run report from the feed_latest result: one alert per item, fingerprinted by its link."""
    items: list[dict[str, Any]] = []
    try:
        envelope = json.loads(_text_of(messages[-1]))
        items = json.loads(envelope["content"]).get("items", [])
    except (ValueError, KeyError, TypeError):
        pass
    findings = [
        {
            "fingerprint": f"feed:{i['link']}",
            "severity": "alert",
            "title": i["title"],
            "detail": i.get("summary", ""),
            "links": [],
        }
        for i in items
    ]
    return {
        "status": "alert" if findings else "ok",
        "headline": f"{len(findings)} new item(s) in the status feed" if findings else "Nothing new",
        "findings": findings,
        "summary_md": "\n".join(f"- {f['title']}" for f in findings) or "All quiet.",
    }


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
async def embeddings(request: Request):
    body = await request.json()
    rec, err = _key_for(request, "embed")
    if err:
        return err
    inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
    dims = int(body.get("dimensions") or 1024)
    _charge(rec, EMBED_COST)
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
    last_tool = _last_tool_name(messages)
    if "propose_schedule" in tools and re.search(r"every weekday", user_text, re.IGNORECASE):
        return None, [_call("propose_schedule", PROPOSAL)]
    if last_tool == "propose_schedule":
        return "I've proposed a schedule for that. Check the card below and click Create to start it.", []
    output_tool = next((n for n in tools if n.startswith("final_result")), None)
    if output_tool and "feed_latest" in tools and last.get("role") == "user" and (feed := _FEED_URL.search(user_text)):
        return None, [_call("feed_latest", {"url": feed.group(0)})]
    if output_tool and last_tool == "feed_latest":
        return None, [_call(output_tool, _feed_report(messages))]
    if (
        output_tool
        and last.get("role") == "user"
        and "commit_message" in tools[output_tool].get("parameters", {}).get("properties", {})
    ):
        return None, [_call(output_tool, forge.plan(user_text))]
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
    rec, err = _key_for(request, body.get("model"))
    if err:
        return err
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

    if not body.get("stream"):
        call_id = _charge(rec, CHAT_COST)
        headers = {"x-litellm-response-cost": str(CHAT_COST), "x-litellm-call-id": call_id}
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

    call_id = f"call-{uuid.uuid4().hex}"
    headers = {"x-litellm-call-id": call_id}  # a streamed response's cost isn't known when headers are sent

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
        if rec is not None:
            rec["spend"] = round(rec["spend"] + CHAT_COST, 8)
        SPEND_LOGS[call_id] = {"request_id": call_id, "spend": CHAT_COST, "api_key_alias": rec and rec["key_alias"]}

    return StreamingResponse(events(), media_type="text/event-stream", headers=headers)


charge = _charge

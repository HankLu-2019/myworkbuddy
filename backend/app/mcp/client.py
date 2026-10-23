"""A small MCP client for streamable HTTP (design 08 §F12): `initialize`, `tools/list`, `tools/call`.

Each operation opens its own session (initialize, the `initialized` notification, the request, then DELETE), so
there is no long-lived connection to keep healthy. A response may be JSON or an event stream; either is read up to
`MAX_BYTES`. Errors never carry the auth header (rule R7): a refused call says only what status came back.
"""

import json
from typing import Any

import httpx2

PROTOCOL = "2025-06-18"
MAX_BYTES = 2 * 1024 * 1024
MAX_TOOLS = 200

_transport: httpx2.AsyncBaseTransport | None = None  # tests: the fake MCP server


def set_transport(transport: httpx2.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = transport


class McpError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _parse(content_type: str, body: bytes, want_id: int) -> dict[str, Any]:
    if "text/event-stream" in content_type:
        for block in body.decode(errors="replace").split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
            if not data:
                continue
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == want_id:
                return msg
        raise McpError("bad_response", "the server's event stream had no answer")
    try:
        msg = json.loads(body)
    except ValueError:
        raise McpError("bad_response", "the server didn't answer with JSON") from None
    if not isinstance(msg, dict):
        raise McpError("bad_response", "the server's answer isn't a JSON-RPC message")
    return msg


class Session:
    def __init__(self, url: str, headers: dict[str, str], timeout_s: float) -> None:
        self.url = url
        self.headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                        "User-Agent": "MyWorkBuddy", **headers}  # fmt: skip
        self.timeout = httpx2.Timeout(timeout_s)
        self.session_id: str | None = None
        self._next = 0
        self._c: httpx2.AsyncClient | None = None

    async def __aenter__(self) -> "Session":
        self._c = httpx2.AsyncClient(timeout=self.timeout, transport=_transport, follow_redirects=False,
                                     trust_env=True)  # fmt: skip
        await self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                          "clientInfo": {"name": "MyWorkBuddy", "version": "1"}})  # fmt: skip
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, notify=True)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._c is None:
            return
        if self.session_id:
            try:
                await self._c.delete(self.url, headers=self._headers())
            except httpx2.HTTPError:
                pass
        await self._c.aclose()

    def _headers(self) -> dict[str, str]:
        h = dict(self.headers)
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
            h["MCP-Protocol-Version"] = PROTOCOL
        return h

    async def _post(self, message: dict[str, Any], *, notify: bool = False) -> dict[str, Any] | None:
        assert self._c is not None
        try:
            async with self._c.stream("POST", self.url, json=message, headers=self._headers()) as resp:
                if resp.status_code in (401, 403):
                    raise McpError("auth_failed", f"the server refused the credential ({resp.status_code})")
                if resp.status_code >= 400:
                    raise McpError("unavailable", f"the server answered {resp.status_code}")
                if sid := resp.headers.get("mcp-session-id"):
                    self.session_id = sid[:200]
                if notify:
                    return None
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise McpError("too_large", f"the answer is larger than {MAX_BYTES // 1024} KB")
                    if "text/event-stream" in resp.headers.get("content-type", "") and b"\n\n" in chunk:
                        try:
                            return _parse("text/event-stream", bytes(body), message["id"])
                        except McpError:
                            continue  # not the answer yet: keep reading
                return _parse(resp.headers.get("content-type", ""), bytes(body), message["id"])
        except httpx2.TimeoutException:
            raise McpError("timeout", "the server didn't answer in time") from None
        except httpx2.TransportError as exc:
            raise McpError("unavailable", f"the server isn't reachable ({type(exc).__name__})") from None

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next += 1
        msg = await self._post({"jsonrpc": "2.0", "id": self._next, "method": method, "params": params})
        assert msg is not None
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            raise McpError("tool_error", str(err.get("message") or "the server returned an error")[:300])
        result = msg.get("result")
        if not isinstance(result, dict):
            raise McpError("bad_response", "the answer has no result")
        return result


async def list_tools(url: str, headers: dict[str, str], timeout_s: float = 20) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    async with Session(url, headers, timeout_s) as s:
        cursor: str | None = None
        while True:
            result = await s.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += [t for t in result.get("tools") or [] if isinstance(t, dict) and isinstance(t.get("name"), str)]
            cursor = result.get("nextCursor")
            if not cursor or len(tools) >= MAX_TOOLS:
                return tools[:MAX_TOOLS]


async def call_tool(url: str, headers: dict[str, str], name: str, arguments: dict[str, Any],
                    timeout_s: float) -> dict[str, Any]:  # fmt: skip
    async with Session(url, headers, timeout_s) as s:
        return await s.request("tools/call", {"name": name, "arguments": arguments})


def result_text(result: dict[str, Any]) -> str:
    """The text a tool returned: its text blocks, else its structured content as JSON."""
    parts = [
        str(b.get("text", "")) for b in result.get("content") or [] if isinstance(b, dict) and b.get("type") == "text"
    ]
    if not parts and result.get("structuredContent") is not None:
        parts = [json.dumps(result["structuredContent"], ensure_ascii=False)]
    return "\n".join(parts)

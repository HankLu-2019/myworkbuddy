"""The worker's client for the runner's internal API (design 04 §4). Network only: callers hold no DB transaction
(rule R1). Errors become RunnerError with the runner's `code` when it gave one."""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, BinaryIO

import httpx2

from app.config import get_settings

_transport: httpx2.AsyncBaseTransport | None = None


def set_transport(transport: httpx2.AsyncBaseTransport | None) -> None:
    """Tests replace the runner with an in-process fake."""
    global _transport
    _transport = transport


class RunnerError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _client(timeout: float = 120) -> httpx2.AsyncClient:
    s = get_settings()
    headers = {"Authorization": f"Bearer {s.runner_token.get_secret_value()}"}
    return httpx2.AsyncClient(base_url=s.runner_url, headers=headers, timeout=timeout, transport=_transport)


def _raise(resp: httpx2.Response) -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
        code, detail = body.get("code") or f"http_{resp.status_code}", body.get("detail") or ""
    except ValueError:
        code, detail = f"http_{resp.status_code}", resp.text[:300]
    raise RunnerError(code, f"runner: {detail}", resp.status_code)


async def _call(method: str, path: str, *, timeout: float = 120, **kw: Any) -> httpx2.Response:
    try:
        async with _client(timeout) as c:
            resp = await c.request(method, path, **kw)
    except httpx2.TransportError as exc:
        raise RunnerError("unavailable", f"The runner isn't reachable ({type(exc).__name__}).") from None
    _raise(resp)
    return resp


async def health() -> dict[str, Any]:
    """The runner's own checks, plus whether Sysbox is installed for verification with containers (08 §F10)."""
    resp = await _call("GET", "/v1/health", timeout=5)
    return resp.json()


async def create_workspace(job_id: uuid.UUID) -> None:
    await _call("POST", f"/v1/workspaces/{job_id}")


async def put_content(job_id: uuid.UUID, tar: BinaryIO) -> int:
    resp = await _call("PUT", f"/v1/workspaces/{job_id}/content", content=tar.read(), timeout=600)
    return int(resp.json()["entries"])


async def patch(job_id: uuid.UUID, base_sha: str) -> bytes:
    return (await _call("GET", f"/v1/workspaces/{job_id}/patch", params={"base_sha": base_sha}, timeout=600)).content


async def read_mwb(job_id: uuid.UUID, name: str) -> bytes | None:
    try:
        return (await _call("GET", f"/v1/workspaces/{job_id}/mwb/{name}")).content
    except RunnerError as exc:
        if exc.status == 404:
            return None
        raise


async def delete_mwb(job_id: uuid.UUID, name: str) -> None:
    await _call("DELETE", f"/v1/workspaces/{job_id}/mwb/{name}")


async def delete_workspace(job_id: uuid.UUID) -> None:
    await _call("DELETE", f"/v1/workspaces/{job_id}")


async def start_run(
    job_id: uuid.UUID, preset: str, *, timeout_s: int, cpus: float, memory_gb: int, env: dict[str, str] | None = None,
    extra_hosts: list[str] | None = None,
) -> str:  # fmt: skip
    body = {"job_id": str(job_id), "preset": preset, "timeout_s": timeout_s, "cpus": cpus, "memory_gb": memory_gb,
            "env": env or {}, "extra_hosts_acl": extra_hosts or []}  # fmt: skip
    return str((await _call("POST", "/v1/runs", json=body)).json()["run_id"])


async def run_status(run_id: str) -> dict[str, Any] | None:
    try:
        return (await _call("GET", f"/v1/runs/{run_id}")).json()
    except RunnerError as exc:
        if exc.status == 404:
            return None
        raise


async def kill_run(run_id: str, *, remove: bool = False) -> None:
    try:
        await _call("DELETE", f"/v1/runs/{run_id}", params={"remove": "true" if remove else "false"})
    except RunnerError as exc:
        if exc.status != 404:
            raise


async def logs(run_id: str, *, follow: bool, timeout: float) -> AsyncIterator[tuple[str, str]]:
    """(stream, line) pairs; with follow, until the run ends or `timeout` seconds pass (TimeoutError)."""
    try:
        async with _client(timeout) as c:
            async with c.stream(
                "GET", f"/v1/runs/{run_id}/logs", params={"follow": "true" if follow else "false"}
            ) as r:
                if r.status_code >= 400:
                    await r.aread()
                    _raise(r)
                async for line in r.aiter_lines():
                    if line:
                        item = json.loads(line)
                        yield item["stream"], item["line"]
    except httpx2.ReadTimeout:
        raise TimeoutError("run logs timed out") from None
    except httpx2.TransportError as exc:
        raise RunnerError("unavailable", f"The runner isn't reachable ({type(exc).__name__}).") from None

"""HTTP clients for integrations (design 02 §1 "TLS and proxies").

Each client verifies TLS against the integration's CA bundle when it has one (else the system store), honours
HTTPS_PROXY/NO_PROXY, uses connect 10 s / read 30 s / total 60 s timeouts and caps response bodies (2 MB).
Client certificates (kubeconfig) are loaded through a private temporary file that is deleted at once: Python's
ssl module can't load a key from memory.
"""

import asyncio
import json
import os
import ssl
import tempfile
from typing import Any

import httpx2

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TIMEOUT = httpx2.Timeout(60.0, connect=10.0, read=30.0)

_transport: httpx2.AsyncBaseTransport | None = None  # tests only


def set_transport(transport: httpx2.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = transport


class ResponseTooLarge(Exception):
    pass


def ssl_context(
    ca_pem: str | None = None, cert_pem: bytes | None = None, key_pem: bytes | None = None
) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cadata=ca_pem) if ca_pem else ssl.create_default_context()
    if cert_pem and key_pem:
        with tempfile.TemporaryDirectory(prefix="mwb-cert-") as tmp:
            os.chmod(tmp, 0o700)
            cert, key = os.path.join(tmp, "c.pem"), os.path.join(tmp, "k.pem")
            for path, data in ((cert, cert_pem), (key, key_pem)):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
            ctx.load_cert_chain(cert, key)
    return ctx


def client(
    base_url: str,
    *,
    ca_pem: str | None = None,
    headers: dict[str, str] | None = None,
    auth: tuple[str, str] | None = None,
    cert_pem: bytes | None = None,
    key_pem: bytes | None = None,
) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        base_url=base_url,
        verify=ssl_context(ca_pem, cert_pem, key_pem),
        trust_env=True,
        timeout=TIMEOUT,
        headers={"User-Agent": "MyWorkBuddy", **(headers or {})},
        auth=auth,
        follow_redirects=False,
        transport=_transport,
    )


async def request(
    c: httpx2.AsyncClient, method: str, url: str, *, limit: int = MAX_RESPONSE_BYTES, **kw: Any
) -> tuple[int, bytes]:
    """(status, body) with the body capped at `limit` bytes; the whole call is bounded by the total timeout."""
    async with asyncio.timeout(TIMEOUT.pool or 60.0):
        async with c.stream(method, url, **kw) as resp:
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > limit:
                    raise ResponseTooLarge(f"response larger than {limit // 1024} KB")
            return resp.status_code, bytes(body)


def parse_json(body: bytes) -> Any:
    """JSON, tolerating Gerrit's `)]}'` XSSI prefix."""
    text = body.decode(errors="replace")
    if text.startswith(")]}'"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
    return json.loads(text) if text.strip() else None


async def request_tail(
    c: httpx2.AsyncClient, method: str, url: str, *, keep: int = 256 * 1024, **kw: Any
) -> tuple[int, bytes]:
    """(status, last `keep` bytes): for logs, which can be far larger than the cap but only the end matters."""
    async with asyncio.timeout(TIMEOUT.pool or 60.0):
        async with c.stream(method, url, **kw) as resp:
            tail = bytearray()
            async for chunk in resp.aiter_bytes():
                tail += chunk
                if len(tail) > keep:
                    del tail[: len(tail) - keep]
            return resp.status_code, bytes(tail)

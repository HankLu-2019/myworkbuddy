"""What every connector shares (design 02 §2 "Error handling").

Connectors raise ConnectorError with one of the codes below; the tool registry turns it into
`{"error": code, "message": ...}` so nothing raises into the agent. Calls retry twice on 429/502/503/504 and
transport errors, honouring Retry-After up to 30 s. A 401 marks the user's credential invalid and, the first
time, adds an inbox item, inside a short transaction of its own (no transaction is open during the call, R1).
"""

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx2
from sqlalchemy import select, update

from app.db import get_sessionmaker
from app.inbox.service import add_item
from app.integrations import http
from app.integrations.kubeconfig import parse_stored
from app.integrations.service import credential_rows, get_credentials
from app.logs import log
from app.models import Integration, UserCredential

ErrorCode = Literal[
    "auth_failed", "not_found", "forbidden", "timeout", "unavailable", "too_large", "not_configured", "invalid_args"
]
RETRY_STATUSES = {429, 502, 503, 504}
RETRIES = 2
MAX_RETRY_AFTER = 30.0
BACKOFF = (1.0, 3.0)

# Tests replace this to avoid waiting.
sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


class ConnectorError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Connection:
    """One user's access to one integration, resolved before any network call."""

    user_id: int
    integration: Integration
    creds: dict[str, str]
    _client: httpx2.AsyncClient | None = field(default=None, repr=False)

    @property
    def name(self) -> str:
        return self.integration.name

    def client(self) -> httpx2.AsyncClient:
        if self._client is None:
            self._client = make_client(self.integration, self.creds)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def make_client(integration: Integration, creds: dict[str, str]) -> httpx2.AsyncClient:
    kind, base, ca = integration.kind, integration.base_url, integration.ca_bundle
    if kind == "gitlab":
        return http.client(base, ca_pem=ca, headers={"PRIVATE-TOKEN": creds["token"]})
    if kind == "jira":
        return http.client(base, ca_pem=ca, headers={"Authorization": f"Bearer {creds['token']}"})
    if kind == "gerrit":
        return http.client(base, ca_pem=ca, auth=(creds["username"], creds["password"]))
    if kind == "jenkins":
        return http.client(base, ca_pem=ca, auth=(creds["username"], creds["token"]))
    if kind == "kubernetes":
        cluster, user = parse_stored(creds["kubeconfig"])
        ca_data = cluster.get("certificate-authority-data")
        return http.client(
            base,
            ca_pem=base64.b64decode(ca_data).decode() if ca_data else ca,
            headers={"Authorization": f"Bearer {user['token']}"} if user.get("token") else {},
            cert_pem=base64.b64decode(user["client-certificate-data"]) if "client-certificate-data" in user else None,
            key_pem=base64.b64decode(user["client-key-data"]) if "client-key-data" in user else None,
        )
    return http.client(base, ca_pem=ca)  # feed


async def connect(user_id: int, kind: str, integration: str | None = None) -> Connection:
    """The caller's usable integration of `kind`: the named one, or the only one with saved credentials.
    Reads in its own short session and closes it before returning (R1)."""
    async with get_sessionmaker()() as db:
        candidates = list(
            await db.scalars(
                select(Integration).where(Integration.kind == kind, Integration.enabled).order_by(Integration.name)
            )
        )
        if integration is not None:
            candidates = [i for i in candidates if i.name.lower() == integration.lower()]
            if not candidates:
                raise ConnectorError("not_configured", f"No enabled {kind} integration called {integration!r}")
        usable = []
        for i in candidates:
            if kind == "feed" or await credential_rows(db, user_id, i.id):
                usable.append(i)
        if not usable:
            raise ConnectorError(
                "not_configured", f"You have no {kind} credentials; add them in Settings → Integrations"
            )
        if len(usable) > 1:
            names = ", ".join(i.name for i in usable)
            raise ConnectorError("invalid_args", f"Several {kind} integrations ({names}); pass integration=<name>")
        chosen = usable[0]
        try:
            creds = await get_credentials(db, user_id, chosen) if kind != "feed" else {}
        except Exception as exc:  # unreadable: credential_invalid AppError
            raise ConnectorError("auth_failed", str(getattr(exc, "detail", exc))) from None
        db.expunge(chosen)
    return Connection(user_id=user_id, integration=chosen, creds=creds)


async def mark_rejected(conn: Connection, status: int) -> None:
    """The credential was refused: mark it invalid and, when it wasn't already, tell the user once."""
    integration = conn.integration
    async with get_sessionmaker()() as db:
        rows = await db.scalars(
            select(UserCredential).where(
                UserCredential.user_id == conn.user_id, UserCredential.integration_id == integration.id
            )
        )
        newly = any(r.status != "invalid" for r in rows)
        message = f"{integration.name} rejected the credential ({status})"
        await db.execute(
            update(UserCredential)
            .where(UserCredential.user_id == conn.user_id, UserCredential.integration_id == integration.id)
            .values(status="invalid", last_error=message)
        )
        if newly:
            add_item(
                db, conn.user_id, kind="credential", severity="warning",
                title=f"Your {integration.name} credential was rejected",
                body_md=f"{message}. Anything that needs {integration.name} fails until you update it.",
                link=f"/settings/integrations?focus={integration.id}",
            )  # fmt: skip
        await db.commit()
    log.warning("connector.auth_failed", integration=integration.id, user_id=conn.user_id, status=status)


def _retry_after(resp_headers: httpx2.Headers | None, attempt: int) -> float:
    raw = resp_headers.get("Retry-After") if resp_headers else None
    try:
        return min(float(raw), MAX_RETRY_AFTER) if raw is not None else BACKOFF[min(attempt, len(BACKOFF) - 1)]
    except ValueError:
        return BACKOFF[min(attempt, len(BACKOFF) - 1)]


async def call(
    conn: Connection,
    method: str,
    path: str,
    *,
    tail: int | None = None,
    limit: int = http.MAX_RESPONSE_BYTES,
    **kw: Any,
) -> bytes:
    """The response body of a successful call, or ConnectorError. `tail` keeps only the last N bytes (logs)."""
    c = conn.client()
    for attempt in range(RETRIES + 1):
        try:
            async with asyncio.timeout(http.TIMEOUT.pool or 60.0):
                async with c.stream(method, path, **kw) as resp:
                    status, headers = resp.status_code, resp.headers
                    if status in RETRY_STATUSES and attempt < RETRIES:
                        wait = _retry_after(headers, attempt)
                        await resp.aclose()
                        await sleep(wait)
                        continue
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body += chunk
                        if tail is not None and len(body) > tail:
                            del body[: len(body) - tail]
                        elif tail is None and len(body) > limit:
                            raise ConnectorError(
                                "too_large", f"{conn.name} sent more than {limit // 1024} KB; narrow the request"
                            )
        except (httpx2.TransportError, TimeoutError) as exc:
            if attempt < RETRIES:
                await sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
                continue
            if isinstance(exc, (httpx2.TimeoutException, TimeoutError)):
                raise ConnectorError("timeout", f"{conn.name} didn't answer in time") from None
            raise ConnectorError("unavailable", f"Can't reach {conn.name}") from None
        if status == 401:
            await mark_rejected(conn, status)
            raise ConnectorError(
                "auth_failed", f"{conn.name} rejected your credential; update it in Settings → Integrations"
            )
        if status == 403:
            raise ConnectorError("forbidden", f"{conn.name} says you don't have access to this")
        if status == 404:
            raise ConnectorError("not_found", f"{conn.name} has no such item, or you can't see it")
        if status >= 400:
            raise ConnectorError("unavailable", f"{conn.name} answered {status}")
        return bytes(body)
    raise ConnectorError("unavailable", f"{conn.name} kept answering 'try again later'")


async def call_json(conn: Connection, method: str, path: str, **kw: Any) -> Any:
    body = await call(conn, method, path, **kw)
    try:
        return http.parse_json(body)
    except (ValueError, json.JSONDecodeError):
        raise ConnectorError("unavailable", f"{conn.name} answered with something that isn't JSON") from None


def tail_lines(body: bytes, lines: int) -> str:
    text = body.decode(errors="replace")
    return "\n".join(text.splitlines()[-lines:])

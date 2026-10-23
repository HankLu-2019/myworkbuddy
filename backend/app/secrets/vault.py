"""HashiCorp Vault Transit as the root of trust for stored secrets (envelope encryption).

Vault's Transit engine holds a key-encryption key (KEK) that never leaves Vault. MyWorkBuddy keeps data keys (DEKs):
Vault makes each one (`datakey/plaintext`), the database keeps only its wrapped form (`system_state` key
`vault_data_keys`), and each process asks Vault to unwrap them once at start-up and keeps them in memory. Every
encrypt and decrypt after that is local (Fernet with the DEK), so no network call happens inside a database
transaction (rule R1), and a copy of the database is useless without Vault.

Auth: a token (`MWB_VAULT_TOKEN`) or AppRole (`MWB_VAULT_ROLE_ID` + `MWB_VAULT_SECRET_ID`). The policy needs only
`transit/datakey/plaintext/<key>`, `transit/decrypt/<key>` and `transit/rewrap/<key>`.
"""

import base64
from datetime import UTC, datetime
from typing import Any

import httpx2

from app.config import get_settings

STATE_KEY = "vault_data_keys"
TIMEOUT = httpx2.Timeout(10.0)

_transport: httpx2.AsyncBaseTransport | None = None  # tests: the fake Vault


def set_transport(transport: httpx2.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = transport


class VaultError(RuntimeError):
    """Vault refused or couldn't be reached. Never carries a token or a key."""


def _client() -> httpx2.AsyncClient:
    s = get_settings()
    verify: Any = True
    if s.vault_ca_file:
        import ssl

        verify = ssl.create_default_context(cafile=s.vault_ca_file)
    headers = {"User-Agent": "MyWorkBuddy"}
    if s.vault_namespace:
        headers["X-Vault-Namespace"] = s.vault_namespace
    return httpx2.AsyncClient(base_url=s.vault_addr.rstrip("/"), timeout=TIMEOUT, transport=_transport, verify=verify,
                              headers=headers, trust_env=False, follow_redirects=False)  # fmt: skip


async def _call(c: httpx2.AsyncClient, method: str, path: str, token: str | None, body: dict | None = None) -> dict:
    try:
        resp = await c.request(method, path, json=body, headers={"X-Vault-Token": token} if token else None)
    except httpx2.TransportError as exc:
        raise VaultError(f"Vault isn't reachable at {get_settings().vault_addr} ({type(exc).__name__})") from None
    if resp.status_code == 503:
        raise VaultError("Vault is sealed: unseal it (deploy/vault/unseal.sh), then restart MyWorkBuddy")
    if resp.status_code in (401, 403):
        raise VaultError(f"Vault refused MyWorkBuddy's credentials ({resp.status_code} for {path})")
    if resp.status_code >= 400:
        raise VaultError(f"Vault answered {resp.status_code} for {path}")
    return resp.json() if resp.content else {}


async def _token(c: httpx2.AsyncClient) -> str:
    s = get_settings()
    if s.vault_token.get_secret_value():
        return s.vault_token.get_secret_value()
    data = await _call(c, "POST", "/v1/auth/approle/login", None,
                       {"role_id": s.vault_role_id, "secret_id": s.vault_secret_id.get_secret_value()})  # fmt: skip
    token = (data.get("auth") or {}).get("client_token")
    if not token:
        raise VaultError("Vault's AppRole login returned no token")
    return token


def _path(op: str) -> str:
    s = get_settings()
    return f"/v1/{s.vault_transit_mount.strip('/')}/{op}/{s.vault_transit_key}"


def fernet_key(raw_b64: str) -> str:
    """A Fernet key from Vault's 256-bit data key (standard base64)."""
    raw = base64.b64decode(raw_b64)
    if len(raw) != 32:
        raise VaultError("Vault's data key isn't 256 bits")
    return base64.urlsafe_b64encode(raw).decode()


async def new_data_key() -> tuple[str, str]:
    """(Fernet key, wrapped ciphertext) of a fresh data key."""
    async with _client() as c:
        data = await _call(c, "POST", _path("datakey/plaintext"), await _token(c), {"bits": 256})
    d = data.get("data") or {}
    if not d.get("plaintext") or not d.get("ciphertext"):
        raise VaultError("Vault's datakey answer is missing a field")
    return fernet_key(d["plaintext"]), d["ciphertext"]


async def unwrap(ciphertexts: list[str]) -> list[str]:
    """Fernet keys for wrapped data keys, in order."""
    out = []
    async with _client() as c:
        token = await _token(c)
        for ct in ciphertexts:
            data = await _call(c, "POST", _path("decrypt"), token, {"ciphertext": ct})
            out.append(fernet_key((data.get("data") or {}).get("plaintext") or ""))
    return out


async def rewrap(ciphertexts: list[str]) -> list[str]:
    """The same data keys wrapped with Vault's newest key version (after `transit/keys/<key>/rotate`)."""
    out = []
    async with _client() as c:
        token = await _token(c)
        for ct in ciphertexts:
            data = await _call(c, "POST", _path("rewrap"), token, {"ciphertext": ct})
            out.append((data.get("data") or {}).get("ciphertext") or ct)
    return out


async def health() -> dict[str, Any]:
    """For the System page: reachable, initialised, unsealed."""
    async with _client() as c:
        try:
            resp = await c.get("/v1/sys/health", params={"standbyok": "true", "sealedcode": "200",
                                                         "uninitcode": "200"})  # fmt: skip
        except httpx2.TransportError as exc:
            return {"ok": False, "error": f"not reachable ({type(exc).__name__})"}
    d = resp.json() if resp.content else {}
    problem = "not initialised" if not d.get("initialized", True) else "sealed" if d.get("sealed") else None
    return {"ok": problem is None, "error": problem, "version": d.get("version"),
            "checked_at": datetime.now(UTC).isoformat()}  # fmt: skip

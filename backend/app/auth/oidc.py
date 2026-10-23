"""Single sign-on with OpenID Connect (design 08 §F11): authorization code flow with PKCE.

`start()` makes a `state`, a `nonce` and a PKCE verifier, keeps them (with where to go next) in a short-lived
encrypted cookie, and returns the provider's authorization URL. `finish()` checks the returned `state` against the
cookie, exchanges the code with the verifier and the client secret, and checks the ID token: signature against the
issuer's published keys, `iss`, `aud` (and `azp` when there are several audiences), `exp`, `iat` and `nonce`. No
database transaction is open during any of it (rule R1); matching the user to an account is the route's job.
"""

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx2
import jwt
from cryptography.fernet import InvalidToken

from app.config import get_settings
from app.logs import log
from app.secrets.service import _fernet

COOKIE = "mwb_oidc"
COOKIE_PATH = "/api/auth/oidc"
FLOW_TTL_S = 600
DISCOVERY_TTL_S = 3600
LEEWAY_S = 60
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "ES256", "ES384"]
TIMEOUT = httpx2.Timeout(10.0)

_transport: httpx2.AsyncBaseTransport | None = None  # tests: the fake provider
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def set_transport(transport: httpx2.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = transport
    _cache.clear()


class OidcError(Exception):
    """A refused sign-in. `code` goes to the sign-in page (`?sso_error=`); the detail only to the log."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def enabled() -> bool:
    return bool(get_settings().oidc_issuer)


def redirect_uri() -> str:
    return f"{get_settings().hostname.rstrip('/')}{COOKIE_PATH}/callback"


def _client() -> httpx2.AsyncClient:
    return httpx2.AsyncClient(timeout=TIMEOUT, transport=_transport, follow_redirects=False, trust_env=True,
                              headers={"User-Agent": "MyWorkBuddy"})  # fmt: skip


async def _get_json(url: str) -> dict[str, Any]:
    now = time.monotonic()
    hit = _cache.get(url)
    if hit and now - hit[0] < DISCOVERY_TTL_S:
        return hit[1]
    try:
        async with _client() as c:
            resp = await c.get(url)
    except httpx2.TransportError as exc:
        raise OidcError("provider_unreachable", f"{url}: {type(exc).__name__}") from None
    if resp.status_code != 200:
        raise OidcError("provider_unreachable", f"{url} answered {resp.status_code}")
    data = resp.json()
    _cache[url] = (now, data)
    return data


async def discovery() -> dict[str, Any]:
    issuer = get_settings().oidc_issuer.rstrip("/")
    doc = await _get_json(f"{issuer}/.well-known/openid-configuration")
    if doc.get("issuer", "").rstrip("/") != issuer:
        raise OidcError("provider_misconfigured", f"discovery issuer {doc.get('issuer')!r} isn't {issuer!r}")
    return doc


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def safe_next(path: str | None) -> str:
    """Only an in-app path: never another site (an open redirect)."""
    if not path or not path.startswith("/") or path.startswith(("//", "/\\")) or path.startswith("/api/"):
        return "/"
    return path[:500]


@dataclass
class Flow:
    url: str
    cookie: str


async def start(next_path: str | None, link_user_id: int | None = None) -> Flow:
    """`link_user_id`: a signed-in user linking their own account (the only way an admin account gets a `sub`)."""
    s = get_settings()
    doc = await discovery()
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    params = {
        "response_type": "code", "client_id": s.oidc_client_id, "redirect_uri": redirect_uri(),
        "scope": s.oidc_scopes, "state": state, "nonce": nonce,
        "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
    }  # fmt: skip
    flow = {"state": state, "nonce": nonce, "verifier": verifier, "next": safe_next(next_path), "at": int(time.time()),
            "link": link_user_id}  # fmt: skip
    cookie = _fernet().encrypt(json.dumps(flow).encode()).decode()
    return Flow(url=f"{doc['authorization_endpoint']}?{urlencode(params)}", cookie=cookie)


def _read_flow(cookie: str | None) -> dict[str, Any]:
    if not cookie:
        raise OidcError("expired", "no sign-in in progress (cookie missing)")
    try:
        flow = json.loads(_fernet().decrypt(cookie.encode(), ttl=FLOW_TTL_S))
    except (InvalidToken, ValueError):
        raise OidcError("expired", "the sign-in cookie is invalid or older than 10 minutes") from None
    return flow


async def finish(code: str | None, state: str | None, cookie: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """The ID token's verified claims, and the flow (`next`, and `link` when a signed-in user is linking)."""
    flow = _read_flow(cookie)
    if not state or not secrets.compare_digest(state, flow["state"]):
        raise OidcError("state", "state doesn't match")
    if not code:
        raise OidcError("denied", "the provider returned no code")
    s = get_settings()
    doc = await discovery()
    form = {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
        "code_verifier": flow["verifier"], "client_id": s.oidc_client_id,
        "client_secret": s.oidc_client_secret.get_secret_value(),
    }  # fmt: skip
    try:
        async with _client() as c:
            resp = await c.post(doc["token_endpoint"], data=form, headers={"Accept": "application/json"})
    except httpx2.TransportError as exc:
        raise OidcError("provider_unreachable", f"token endpoint: {type(exc).__name__}") from None
    if resp.status_code != 200:
        raise OidcError("token", f"token endpoint answered {resp.status_code}")  # never the body: it may echo secrets
    try:
        body = resp.json()
    except ValueError:
        raise OidcError("token", "the token endpoint didn't answer with JSON") from None
    id_token = body.get("id_token") if isinstance(body, dict) else None
    if not id_token:
        raise OidcError("token", "no id_token in the token response")
    claims = await verify_id_token(id_token, doc, flow["nonce"])
    return claims, flow


async def _key_for(token: str, doc: dict[str, Any]) -> tuple[Any, str]:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise OidcError("token", "the ID token isn't a JWT") from None
    if header.get("alg") not in ALGORITHMS:
        raise OidcError("token", f"algorithm {header.get('alg')!r} isn't allowed")
    jwks = await _get_json(doc["jwks_uri"])
    keys = [k for k in jwks.get("keys", []) if k.get("use", "sig") == "sig"]
    match = [k for k in keys if k.get("kid") == header.get("kid")] if header.get("kid") else keys
    if not match:
        _cache.pop(doc["jwks_uri"], None)  # the provider may have rotated its keys: fetch once more
        jwks = await _get_json(doc["jwks_uri"])
        match = [k for k in jwks.get("keys", []) if k.get("kid") == header.get("kid")]
    if len(match) != 1:
        raise OidcError("signature", "no signing key matches the ID token")
    return jwt.PyJWK(match[0]).key, header["alg"]


async def verify_id_token(token: str, doc: dict[str, Any], nonce: str) -> dict[str, Any]:
    s = get_settings()
    key, alg = await _key_for(token, doc)
    try:
        claims = jwt.decode(
            token, key, algorithms=[alg], audience=s.oidc_client_id, issuer=doc["issuer"], leeway=LEEWAY_S,
            options={"require": ["iss", "aud", "exp", "iat", "sub"]},
        )  # fmt: skip
    except jwt.InvalidSignatureError:
        raise OidcError("signature", "the ID token's signature doesn't verify") from None
    except jwt.ExpiredSignatureError:
        raise OidcError("expired_token", "the ID token has expired") from None
    except jwt.InvalidAudienceError:
        raise OidcError("audience", "the ID token is for another client") from None
    except jwt.InvalidIssuerError:
        raise OidcError("issuer", "the ID token is from another issuer") from None
    except jwt.PyJWTError as exc:
        raise OidcError("token", f"the ID token is invalid ({type(exc).__name__})") from None
    aud = claims["aud"]
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != s.oidc_client_id:
        raise OidcError("audience", "azp isn't this client")
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OidcError("nonce", "nonce doesn't match")
    log.info("oidc.token_ok", sub_len=len(str(claims["sub"])))
    return claims

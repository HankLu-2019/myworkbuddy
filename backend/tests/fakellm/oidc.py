"""A fake OpenID Connect provider (design 08 §F11), mounted in the fake gateway at /oidc.

- GET  /oidc/.well-known/openid-configuration
- GET  /oidc/authorize: checks the client and PKCE challenge method, then redirects back with a code (no login page:
  the user is whoever `STATE["user"]` says).
- POST /oidc/token: checks the code, the client secret and the PKCE verifier, and answers with an RS256 ID token.
- GET  /oidc/jwks
- POST /fake/oidc: set the user's claims and what to break (`tamper`: wrong_nonce, wrong_aud, wrong_iss,
  expired, bad_signature, unverified_email).
"""

import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

router = APIRouter()
CLIENT_ID = "mwb"
CLIENT_SECRET = "fake-oidc-secret"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "fake-1"
DEFAULT_USER = {"sub": "oidc-alice", "email": "alice@example.com", "email_verified": True, "name": "Alice A",
                "preferred_username": "alice"}  # fmt: skip
STATE: dict[str, Any] = {"user": dict(DEFAULT_USER), "tamper": None, "codes": {}}


def reset() -> None:
    STATE.update(user=dict(DEFAULT_USER), tamper=None, codes={})


def _issuer(request: Request) -> str:
    return f"{str(request.base_url).rstrip('/')}/oidc"


@router.post("/fake/oidc")
async def configure(body: dict[str, Any]) -> dict[str, Any]:
    if body.get("reset"):
        reset()
    if "user" in body:
        STATE["user"] = {**DEFAULT_USER, **body["user"]}
    STATE["tamper"] = body.get("tamper")
    return {"ok": True}


@router.get("/oidc/.well-known/openid-configuration")
async def discovery(request: Request) -> dict[str, Any]:
    iss = _issuer(request)
    return {
        "issuer": iss, "authorization_endpoint": f"{iss}/authorize", "token_endpoint": f"{iss}/token",
        "jwks_uri": f"{iss}/jwks", "response_types_supported": ["code"], "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"], "code_challenge_methods_supported": ["S256"],
    }  # fmt: skip


@router.get("/oidc/jwks")
async def jwks() -> dict[str, Any]:
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True)
    return {"keys": [{**jwk, "kid": KID, "use": "sig", "alg": "RS256"}]}


@router.get("/oidc/authorize")
async def authorize(
    request: Request, client_id: str, redirect_uri: str, state: str, nonce: str, code_challenge: str,
    code_challenge_method: str, response_type: str = "code", scope: str = "",
) -> RedirectResponse:  # fmt: skip
    if client_id != CLIENT_ID or response_type != "code" or code_challenge_method != "S256" or "openid" not in scope:
        raise HTTPException(400, "bad authorization request")
    code = secrets.token_urlsafe(16)
    STATE["codes"][code] = {"challenge": code_challenge, "nonce": nonce, "redirect_uri": redirect_uri,
                            "iss": _issuer(request)}  # fmt: skip
    return RedirectResponse(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}", status_code=302)


@router.post("/oidc/token")
async def token(
    grant_type: str = Form(), code: str = Form(), redirect_uri: str = Form(), code_verifier: str = Form(),
    client_id: str = Form(), client_secret: str = Form(),
) -> dict[str, Any]:  # fmt: skip
    grant = STATE["codes"].pop(code, None)
    if grant is None or grant_type != "authorization_code" or redirect_uri != grant["redirect_uri"]:
        raise HTTPException(400, "invalid_grant")
    if client_id != CLIENT_ID or client_secret != CLIENT_SECRET:
        raise HTTPException(401, "invalid_client")
    digest = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
    if digest != grant["challenge"]:
        raise HTTPException(400, "invalid_grant: PKCE")
    tamper = STATE["tamper"]
    now = int(time.time())
    claims = {
        **STATE["user"], "iss": grant["iss"], "aud": CLIENT_ID, "iat": now, "exp": now + 300, "nonce": grant["nonce"],
    }  # fmt: skip
    if tamper == "wrong_nonce":
        claims["nonce"] = "not-the-nonce"
    elif tamper == "wrong_aud":
        claims["aud"] = "someone-else"
    elif tamper == "wrong_iss":
        claims["iss"] = "https://evil.example.com"
    elif tamper == "expired":
        claims.update(iat=now - 3600, exp=now - 1800)
    elif tamper == "unverified_email":
        claims["email_verified"] = False
    key = OTHER_KEY if tamper == "bad_signature" else KEY
    id_token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID})
    return {"access_token": secrets.token_urlsafe(16), "token_type": "Bearer", "expires_in": 300, "id_token": id_token}

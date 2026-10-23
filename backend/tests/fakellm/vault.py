"""A fake HashiCorp Vault with the Transit engine and AppRole (app/secrets/vault.py), for tests.

Not cryptography: wrapped keys are opaque `vault:v<version>:<id>` strings looked up in STATE. What it checks is
what matters to MyWorkBuddy: the token, the mount and key name, the sealed state, and key versions on rewrap.
"""

import base64
import os
import secrets
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="fake-vault")
ROLE_ID, SECRET_ID, ROOT = "role-mwb", "secret-mwb", "s.root-token"
STATE: dict[str, Any] = {}


def reset() -> None:
    STATE.update(sealed=False, version=1, keys={}, tokens={ROOT}, calls=[])


reset()


def _denied(request: Request) -> JSONResponse | None:
    if STATE["sealed"]:
        return JSONResponse({"errors": ["Vault is sealed"]}, status_code=503)
    if request.headers.get("x-vault-token") not in STATE["tokens"]:
        return JSONResponse({"errors": ["permission denied"]}, status_code=403)
    return None


@app.get("/v1/sys/health")
async def health() -> dict:
    return {"initialized": True, "sealed": STATE["sealed"], "version": "1.17.6"}


@app.post("/v1/auth/approle/login")
async def login(body: dict) -> JSONResponse:
    if STATE["sealed"]:
        return JSONResponse({"errors": ["Vault is sealed"]}, status_code=503)
    if body.get("role_id") != ROLE_ID or body.get("secret_id") != SECRET_ID:
        return JSONResponse({"errors": ["invalid role or secret ID"]}, status_code=400)
    token = "s." + secrets.token_hex(8)
    STATE["tokens"].add(token)
    return JSONResponse({"auth": {"client_token": token, "policies": ["mwb"]}})


@app.post("/v1/transit/datakey/plaintext/mwb")
async def datakey(request: Request) -> JSONResponse:
    if (no := _denied(request)) is not None:
        return no
    raw = base64.b64encode(os.urandom(32)).decode()
    ct = f"vault:v{STATE['version']}:{secrets.token_hex(8)}"
    STATE["keys"][ct] = raw
    STATE["calls"].append("datakey")
    return JSONResponse({"data": {"plaintext": raw, "ciphertext": ct}})


@app.post("/v1/transit/decrypt/mwb")
async def decrypt(request: Request, body: dict) -> JSONResponse:
    if (no := _denied(request)) is not None:
        return no
    raw = STATE["keys"].get(body.get("ciphertext"))
    STATE["calls"].append("decrypt")
    if raw is None:
        return JSONResponse({"errors": ["invalid ciphertext"]}, status_code=400)
    return JSONResponse({"data": {"plaintext": raw}})


@app.post("/v1/transit/rewrap/mwb")
async def rewrap(request: Request, body: dict) -> JSONResponse:
    if (no := _denied(request)) is not None:
        return no
    raw = STATE["keys"].get(body.get("ciphertext"))
    if raw is None:
        return JSONResponse({"errors": ["invalid ciphertext"]}, status_code=400)
    ct = f"vault:v{STATE['version']}:{body['ciphertext'].split(':')[-1]}"
    STATE["keys"][ct] = raw
    return JSONResponse({"data": {"ciphertext": ct}})

"""Every error response has the {"detail", "code"} shape (rule R12)."""

from app.errors import CODES


def _assert_error(resp, status: int, code: str) -> dict:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) >= {"detail", "code"} and isinstance(body["detail"], str)
    assert body["code"] == code and code in CODES
    return body


async def test_not_signed_in(anon) -> None:
    _assert_error(await anon.get("/api/auth/me"), 401, "not_signed_in")


async def test_invalid_credentials(anon, make_user) -> None:
    await make_user("erin")
    resp = await anon.post("/api/auth/login", json={"username": "erin", "password": "nope-nope-nope"})
    assert _assert_error(resp, 401, "invalid_credentials")["detail"] == "Wrong username or password"


async def test_csrf_invalid(client_for) -> None:
    c = await client_for("fred")
    del c.headers["X-CSRF-Token"]
    _assert_error(await c.post("/api/chat/sessions", json={}), 403, "csrf_invalid")


async def test_forbidden_and_not_found(client_for) -> None:
    c = await client_for("gina")
    _assert_error(await c.get("/api/admin/users"), 403, "forbidden")
    _assert_error(await c.get("/api/wiki/pages/999999"), 404, "not_found")


async def test_validation_error_lists_fields(anon) -> None:
    body = _assert_error(await anon.post("/api/auth/login", json={"username": "x"}), 422, "validation_error")
    assert body["errors"][0]["loc"][-1] == "password"


async def test_conflict_and_unsupported_file(client_for) -> None:
    admin = await client_for("hank", role="admin")
    user = {"username": "dup", "password": "long-enough-pw"}
    assert (await admin.post("/api/admin/users", json=user)).status_code == 201
    _assert_error(await admin.post("/api/admin/users", json=user), 409, "conflict")
    resp = await admin.post("/api/knowledge/documents", files={"file": ("x.exe", b"MZ", "application/octet-stream")})
    _assert_error(resp, 415, "unsupported_file")


async def test_rate_limited(anon, make_user) -> None:
    await make_user("ivan")
    for _ in range(5):
        await anon.post("/api/auth/login", json={"username": "ivan", "password": "wrong-password"})
    resp = await anon.post("/api/auth/login", json={"username": "ivan", "password": "wrong-password"})
    _assert_error(resp, 429, "rate_limited")

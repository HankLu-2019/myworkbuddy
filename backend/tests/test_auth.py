from sqlalchemy import select

from app.db import get_sessionmaker
from app.models import AuditLog
from tests.conftest import PASSWORD


async def test_login_me_logout(client_for):
    c = await client_for("alice")
    me = await c.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "alice"
    assert me.json()["csrf_token"] == c.csrf
    assert (await c.post("/api/auth/logout")).status_code == 204
    assert (await c.get("/api/auth/me")).status_code == 401


async def test_login_is_case_insensitive_and_audited(client_for, anon):
    await client_for("alice")
    resp = await anon.post("/api/auth/login", json={"username": " Alice ", "password": PASSWORD})
    assert resp.status_code == 200
    async with get_sessionmaker()() as db:
        actions = list(await db.scalars(select(AuditLog.action)))
    assert actions.count("login") == 2


async def test_wrong_password_then_rate_limited(make_user, anon):
    await make_user("bob")
    for _ in range(5):
        resp = await anon.post("/api/auth/login", json={"username": "bob", "password": "nope-nope-nope"})
        assert resp.status_code == 401
    resp = await anon.post("/api/auth/login", json={"username": "bob", "password": PASSWORD})
    assert resp.status_code == 429


async def test_unknown_user_gets_same_error(anon):
    resp = await anon.post("/api/auth/login", json={"username": "ghost", "password": "whatever-123"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Wrong username or password"


async def test_unsafe_methods_need_csrf(client_for):
    c = await client_for("alice")
    del c.headers["X-CSRF-Token"]
    assert (await c.post("/api/chat/sessions", json={})).status_code == 403
    c.headers["X-CSRF-Token"] = "wrong"
    assert (await c.post("/api/chat/sessions", json={})).status_code == 403
    c.headers["X-CSRF-Token"] = c.csrf
    assert (await c.post("/api/chat/sessions", json={})).status_code == 201


async def test_password_change_revokes_sessions(client_for):
    c = await client_for("alice")
    bad = await c.post("/api/auth/password", json={"current_password": "wrong", "new_password": "x" * 12})
    assert bad.status_code == 400
    short = await c.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "short"})
    assert short.status_code == 422
    ok = await c.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "brand-new-pass"})
    assert ok.status_code == 204
    assert (await c.get("/api/auth/me")).status_code == 401
    await c.login("alice", "brand-new-pass")


async def test_admin_user_management(client_for, anon):
    admin = await client_for("root", role="admin")
    member = await client_for("alice")
    assert (await member.get("/api/admin/users")).status_code == 403

    created = await admin.post("/api/admin/users", json={"username": "carol"})
    assert created.status_code == 201
    initial = created.json()["initial_password"]
    assert len(initial) == 16 and created.json()["must_change_password"] is True
    dup = await admin.post("/api/admin/users", json={"username": "Carol"})
    assert dup.status_code == 409
    bad_name = await admin.post("/api/admin/users", json={"username": "bad name!"})
    assert bad_name.status_code == 422
    assert "initial_password" not in (await admin.get("/api/admin/users")).json()[0]

    users = (await admin.get("/api/admin/users")).json()
    assert [u["username"] for u in users] == ["alice", "carol", "root"]

    alice_id = member.user_id
    resp = await admin.patch(f"/api/admin/users/{alice_id}", json={"is_active": False})
    assert resp.json()["is_active"] is False
    assert (await member.get("/api/auth/me")).status_code == 401
    login = await anon.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    assert login.status_code == 401
    # Admins no longer choose passwords.
    assert (await admin.patch(f"/api/admin/users/{alice_id}", json={"password": "x" * 12})).status_code == 422

    assert (await admin.patch(f"/api/admin/users/{alice_id}", json={"is_active": True})).status_code == 200
    reset = await admin.post(f"/api/admin/users/{alice_id}/reset-password")
    assert reset.status_code == 200
    one_time = reset.json()["initial_password"]
    assert (await anon.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})).status_code == 401
    assert (await anon.post("/api/auth/login", json={"username": "alice", "password": one_time})).is_success

    assert (await admin.patch(f"/api/admin/users/{admin.user_id}", json={"role": "member"})).status_code == 400
    assert (await admin.patch("/api/admin/users/9999", json={"role": "member"})).status_code == 404


async def test_health(anon):
    assert (await anon.get("/api/health")).json() == {"status": "ok"}

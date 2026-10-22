"""Single sign-on with OpenID Connect (design 08 §F11; plan F11), against the fake provider in the fake gateway."""

from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, update

from app.auth import oidc
from app.config import Settings, get_settings, oidc_problems
from app.db import get_sessionmaker
from app.main import app
from app.models import AuditLog, User
from tests.conftest import PASSWORD
from tests.fakellm import oidc as fake
from tests.fakellm import server as fake_gateway

ISSUER = "http://fake-llm/oidc"


@pytest.fixture
def sso(monkeypatch):
    s = get_settings()
    for key, value in {
        "oidc_issuer": ISSUER, "oidc_client_id": fake.CLIENT_ID, "oidc_name": "Company SSO",
        "oidc_client_secret": SecretStr(fake.CLIENT_SECRET), "hostname": "http://test",
        "oidc_auto_create": False, "local_login": "all",
    }.items():  # fmt: skip
        monkeypatch.setattr(s, key, value)
    fake.reset()
    oidc.set_transport(httpx2.ASGITransport(app=fake_gateway.app))
    yield fake
    oidc.set_transport(None)
    fake.reset()


async def browser() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def sign_in(c: AsyncClient, next_path: str = "/work") -> httpx2.Response | object:
    """The whole browser round trip: app → provider → app. Returns the app's last answer."""
    r = await c.get("/api/auth/oidc/start", params={"next": next_path})
    assert r.status_code == 302, r.text
    where = r.headers["location"]
    assert where.startswith(f"{ISSUER}/authorize?")
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=fake_gateway.app), base_url="http://fake-llm"
    ) as p:
        back = await p.get(where.removeprefix("http://fake-llm"))
    assert back.status_code == 302
    url = urlparse(back.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "http://test/api/auth/oidc/callback"
    return await c.get(f"{url.path}?{url.query}")


def refused(r, code: str) -> bool:
    return r.status_code == 302 and r.headers["location"] == f"/login?sso_error={code}"


async def test_sign_in_matches_by_email_once_then_by_sub(make_user, sso) -> None:
    alice = await make_user("alice", email="Alice@Example.com")
    async with await browser() as c:
        assert (await c.get("/api/auth/oidc/config")).json() == {"enabled": True, "name": "Company SSO",
                                                                  "local_login": "all"}  # fmt: skip
        r = await sign_in(c, "/work/123")
        assert r.status_code == 302 and r.headers["location"] == "/work/123"
        me = (await c.get("/api/auth/me")).json()
        assert me["user"]["id"] == alice.id and me["csrf_token"]
    async with get_sessionmaker()() as db:
        assert (await db.get(User, alice.id)).oidc_sub == "oidc-alice"
        actions = list(await db.scalars(select(AuditLog.action).order_by(AuditLog.id)))
    assert "user.oidc_linked" in actions and actions[-1] == "login"
    # After that, only `sub`: a new email still signs in alice; another sub with her email doesn't.
    sso.STATE["user"] = {**sso.DEFAULT_USER, "email": "alice.new@example.com"}
    async with await browser() as c:
        r = await sign_in(c)
        assert r.headers["location"] == "/work" and (await c.get("/api/auth/me")).json()["user"]["id"] == alice.id
    sso.STATE["user"] = {**sso.DEFAULT_USER, "sub": "someone-else"}
    async with await browser() as c:
        assert refused(await sign_in(c), "no_account")
        assert (await c.get("/api/auth/me")).status_code == 401


@pytest.mark.parametrize(
    ("tamper", "code"),
    [("wrong_nonce", "nonce"), ("bad_signature", "signature"), ("expired", "expired_token"), ("wrong_aud", "audience"),
     ("wrong_iss", "issuer")],
)  # fmt: skip
async def test_a_bad_id_token_is_refused(make_user, sso, tamper: str, code: str) -> None:
    await make_user("alice", email="alice@example.com")
    sso.STATE["tamper"] = tamper
    async with await browser() as c:
        assert refused(await sign_in(c), code)
        assert (await c.get("/api/auth/me")).status_code == 401
    async with get_sessionmaker()() as db:
        row = await db.scalar(select(AuditLog).where(AuditLog.action == "login.failed"))
    assert row.detail == {"method": "oidc", "reason": code}


async def test_a_wrong_state_or_a_missing_flow_is_refused(make_user, sso) -> None:
    await make_user("alice", email="alice@example.com")
    async with await browser() as c:
        r = await c.get("/api/auth/oidc/start")
        query = parse_qs(urlparse(r.headers["location"]).query)
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=fake_gateway.app)) as p:
            back = await p.get(r.headers["location"])
        code = parse_qs(urlparse(back.headers["location"]).query)["code"][0]
        assert refused(await c.get("/api/auth/oidc/callback", params={"code": code, "state": "forged"}), "state")
        assert query["code_challenge_method"] == ["S256"] and "code_verifier" not in r.headers["location"]
    async with await browser() as other:  # no flow cookie: someone else's callback link
        assert refused(await other.get("/api/auth/oidc/callback", params={"code": code, "state": query["state"][0]}),
                       "expired")  # fmt: skip
        assert refused(await other.get("/api/auth/oidc/callback", params={"error": "access_denied"}), "denied")
    async with await browser() as c:  # an outside `next` lands in the app, never elsewhere
        r = await sign_in(c, "https://evil.example.com/x")
        assert r.headers["location"] == "/"


async def test_unknown_users_are_created_only_by_setting(make_user, sso, monkeypatch) -> None:
    sso.STATE["user"] = {**sso.DEFAULT_USER, "sub": "oidc-bob", "email": "bob@example.com",
                         "preferred_username": "Bob.Builder", "name": "Bob B"}  # fmt: skip
    async with await browser() as c:
        assert refused(await sign_in(c), "no_account")
    monkeypatch.setattr(get_settings(), "oidc_auto_create", True)
    await make_user("bob.builder")  # the name is taken: the new account gets a suffix
    async with await browser() as c:
        r = await sign_in(c)
        me = (await c.get("/api/auth/me")).json()["user"]
    assert r.headers["location"] == "/work" and me["role"] == "member" and me["username"].startswith("bob.builder-")
    assert me["email"] == "bob@example.com" and me["display_name"] == "Bob B"
    # An unverified email never matches an existing account.
    await make_user("carol", email="carol@example.com")
    monkeypatch.setattr(get_settings(), "oidc_auto_create", False)
    sso.STATE["user"] = {**sso.DEFAULT_USER, "sub": "oidc-carol", "email": "carol@example.com"}
    sso.STATE["tamper"] = "unverified_email"
    async with await browser() as c:
        assert refused(await sign_in(c), "no_account")


async def test_deactivated_users_are_refused_and_admins_keep_the_password(make_user, sso, monkeypatch) -> None:
    alice = await make_user("alice", email="alice@example.com")
    await make_user("root", role="admin")
    async with get_sessionmaker()() as db:
        await db.execute(update(User).where(User.id == alice.id).values(is_active=False))
        await db.commit()
    async with await browser() as c:
        assert refused(await sign_in(c), "inactive")
    monkeypatch.setattr(get_settings(), "local_login", "admins")
    async with get_sessionmaker()() as db:
        await db.execute(update(User).where(User.id == alice.id).values(is_active=True))
        await db.commit()
    async with await browser() as c:
        r = await c.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
        assert r.status_code == 403 and r.json()["detail"] == "Sign in with Company SSO."
        r = await c.post("/api/auth/login", json={"username": "root", "password": PASSWORD})
        assert r.status_code == 200
    async with await browser() as c:
        assert (await sign_in(c)).headers["location"] == "/work"  # alice, with SSO


def test_settings_are_checked_at_startup() -> None:
    base = {
        "database_url": "postgresql://x/y",
        "secret_key": "k",
        "env": "prod",
        "hostname": "https://mwb.example.com",
        "cookie_secure": True,
    }
    assert oidc_problems(Settings(**base)) == []
    assert oidc_problems(Settings(**base, local_login="admins")) == [
        "MWB_LOCAL_LOGIN=admins needs single sign-on: set MWB_OIDC_ISSUER"]  # fmt: skip
    problems = oidc_problems(Settings(**base, oidc_issuer="http://idp.example.com", oidc_scopes="email"))
    assert problems == [
        "MWB_OIDC_ISSUER must be an https URL", "Set MWB_OIDC_CLIENT_ID with MWB_OIDC_ISSUER",
        "Set MWB_OIDC_CLIENT_SECRET with MWB_OIDC_ISSUER", "MWB_OIDC_SCOPES must include openid",
    ]  # fmt: skip
    good = Settings(**base, oidc_issuer="https://idp.example.com/realms/x", oidc_client_id="mwb",
                    oidc_client_secret="s")  # fmt: skip
    assert oidc_problems(good) == []

"""Auth, sessions, secrets and offboarding (design 01 §H11)."""

import asyncio
import os
import sys
import time
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from app.auth.service import login_keys, record_failed_login
from app.config import get_settings
from app.db import get_sessionmaker
from app.main import app
from app.maintenance import prune_database, prune_tmp
from app.models import (
    AuditLog,
    AuthSession,
    ChatSession,
    Chunk,
    Document,
    LoginAttempt,
    Project,
    User,
    UserSecret,
    WikiPage,
)
from app.secrets.service import get_secret, rotate_secrets, set_secret
from app.tasks import app as tasks_mod
from tests.conftest import PASSWORD, Client

# ---------- rate limits ----------


def _anon(ip: str) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app, client=(ip, 1234)), base_url="http://test")


async def _login(c: AsyncClient, username: str, password: str = "wrong-password") -> int:
    return (await c.post("/api/auth/login", json={"username": username, "password": password})).status_code


async def test_username_and_ip_limit(make_user) -> None:
    await make_user("bob")
    async with _anon("10.0.0.1") as c:
        for _ in range(5):
            assert await _login(c, "bob") == 401
        r = await c.post("/api/auth/login", json={"username": "bob", "password": PASSWORD})
        assert r.status_code == 429 and r.json()["code"] == "rate_limited"
    async with _anon("10.0.0.2") as other:  # the same user from elsewhere is not locked out by this key
        assert await _login(other, "bob", PASSWORD) == 200


async def test_ip_limit_across_usernames(make_user, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "login_max_attempts_ip", 4)
    await make_user("carol")
    async with _anon("10.0.0.3") as c:
        for i in range(4):
            assert await _login(c, f"guess{i}") == 401
        assert await _login(c, "carol", PASSWORD) == 429
    async with _anon("10.0.0.4") as other:
        assert await _login(other, "carol", PASSWORD) == 200


async def test_username_limit_across_ips(make_user, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "login_max_attempts_user", 3)
    await make_user("dave")
    for i in range(3):
        async with _anon(f"10.1.0.{i}") as c:
            assert await _login(c, "dave") == 401
    async with _anon("10.1.0.99") as c:
        r = await c.post("/api/auth/login", json={"username": "dave", "password": PASSWORD})
    assert r.status_code == 429
    assert r.json()["detail"] == "Too many attempts. Try again in 15 minutes."  # same message for every key


async def test_success_clears_only_the_username_ip_key(make_user) -> None:
    await make_user("erin")
    keys = login_keys("erin", "10.2.0.1")
    async with get_sessionmaker()() as db:
        await record_failed_login(db, keys)
    async with _anon("10.2.0.1") as c:
        assert await _login(c, "erin", PASSWORD) == 200
    async with get_sessionmaker()() as db:
        left = set(await db.scalars(select(LoginAttempt.key)))
    assert left == {keys["ip"], keys["user"]}


# ---------- sessions ----------


def _client(user_agent: str = "test-client") -> Client:
    return Client(transport=ASGITransport(app=app), base_url="http://test", headers={"user-agent": user_agent})


async def _session_of(c: Client) -> AuthSession:
    async with get_sessionmaker()() as db:
        return await db.scalar(
            select(AuthSession).where(AuthSession.user_id == c.user_id).order_by(AuthSession.id.desc())
        )


async def test_idle_timeout_signs_out(client_for) -> None:
    alice = await client_for("alice")
    assert (await alice.get("/api/auth/me")).status_code == 200
    async with get_sessionmaker()() as db:
        await db.execute(update(AuthSession).values(last_seen_at=datetime.now(UTC) - timedelta(hours=13)))
        await db.commit()
    assert (await alice.get("/api/auth/me")).status_code == 401


async def test_last_seen_is_refreshed_at_most_every_five_minutes(client_for) -> None:
    alice = await client_for("alice")
    recent = datetime.now(UTC) - timedelta(minutes=1)
    async with get_sessionmaker()() as db:
        await db.execute(update(AuthSession).values(last_seen_at=recent))
        await db.commit()
    await alice.get("/api/auth/me")
    assert abs(((await _session_of(alice)).last_seen_at - recent).total_seconds()) < 1  # untouched
    async with get_sessionmaker()() as db:
        await db.execute(update(AuthSession).values(last_seen_at=datetime.now(UTC) - timedelta(minutes=6)))
        await db.commit()
    await alice.get("/api/auth/me")
    assert datetime.now(UTC) - (await _session_of(alice)).last_seen_at < timedelta(seconds=30)


async def test_sessions_list_and_sign_out_others(client_for) -> None:
    laptop = await client_for("alice")
    phone = await _client(user_agent="phone-browser").login("alice")
    listed = (await laptop.get("/api/auth/sessions")).json()
    assert len(listed) == 2 and sum(s["current"] for s in listed) == 1
    assert {s["ip"] for s in listed} == {"127.0.0.1"} and "phone-browser" in {s["user_agent"] for s in listed}
    r = await laptop.post("/api/auth/sessions/sign-out-others")
    assert r.json() == {"signed_out": 1}
    assert (await phone.get("/api/auth/me")).status_code == 401
    assert (await laptop.get("/api/auth/me")).status_code == 200


# ---------- one-time passwords ----------


async def test_initial_password_must_be_changed_first(client_for) -> None:
    admin = await client_for("root", role="admin")
    created = (await admin.post("/api/admin/users", json={"username": "frank"})).json()
    frank = await _client().login("frank", created["initial_password"])
    me = (await frank.get("/api/auth/me")).json()
    assert me["user"]["must_change_password"] is True
    blocked = await frank.get("/api/knowledge/documents")
    assert blocked.status_code == 403 and blocked.json()["code"] == "password_change_required"
    same = await frank.post(
        "/api/auth/password",
        json={"current_password": created["initial_password"], "new_password": created["initial_password"]},
    )
    assert same.status_code == 422
    ok = await frank.post(
        "/api/auth/password", json={"current_password": created["initial_password"], "new_password": "franks-own-pw"}
    )
    assert ok.status_code == 204
    # The forced change keeps this sign-in, and everything is allowed again.
    assert (await frank.get("/api/knowledge/documents")).status_code == 200
    assert (await frank.get("/api/auth/me")).json()["user"]["must_change_password"] is False


# ---------- maintenance ----------


async def test_maintenance_prunes_old_rows_jobs_and_tmp(client_for, task_queue) -> None:
    alice = await client_for("alice")
    old = datetime.now(UTC) - timedelta(days=2)
    async with get_sessionmaker()() as db:
        db.add_all([LoginAttempt(key="k-old", at=old), LoginAttempt(key="k-new")])
        db.add(AuditLog(action="ancient", at=datetime.now(UTC) - timedelta(days=401)))
        db.add(
            AuthSession(token_hash="a" * 64, user_id=alice.user_id, csrf_token="c", expires_at=old, last_seen_at=old)
        )
        db.add(
            AuthSession(
                token_hash="b" * 64,
                user_id=alice.user_id,
                csrf_token="c",
                expires_at=datetime.now(UTC) + timedelta(days=1),
                last_seen_at=datetime.now(UTC) - timedelta(hours=13),
            )
        )
        await db.commit()
    await tasks_mod.defer_ingest(1)
    [job_id] = list(task_queue.jobs)
    task_queue.jobs[job_id]["status"] = "succeeded"
    for e in task_queue.events[job_id]:
        e["at"] = datetime.now(UTC) - timedelta(days=31)
    tmp = get_settings().data_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "stale").write_text("x")
    os.utime(tmp / "stale", (time.time() - 2 * 86400,) * 2)
    (tmp / "fresh").write_text("x")

    counts = await tasks_mod.maintenance_hourly(timestamp=0)
    assert counts == {"login_attempts": 1, "sessions": 2, "audit_rows": 1, "tmp_entries": 1}
    assert task_queue.jobs == {}
    assert not (tmp / "stale").exists() and (tmp / "fresh").exists()
    async with get_sessionmaker()() as db:
        assert set(await db.scalars(select(LoginAttempt.key))) == {"k-new"}
    assert (await alice.get("/api/auth/me")).status_code == 200  # the live session survives
    assert await prune_database() == {"login_attempts": 0, "sessions": 0, "audit_rows": 0}
    assert prune_tmp() == 0


# ---------- secrets ----------


async def test_old_key_still_decrypts_and_rotation_moves_to_the_new_key(make_user, monkeypatch) -> None:
    alice = await make_user("alice")
    old_key, new_key = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setattr(get_settings(), "secret_keys", old_key)
    async with get_sessionmaker()() as db:
        await set_secret(db, alice.id, "gitlab_token", "glpat-1")
        await db.commit()
    monkeypatch.setattr(get_settings(), "secret_keys", f"{new_key},{old_key}")
    async with get_sessionmaker()() as db:
        assert await get_secret(db, alice.id, "gitlab_token") == "glpat-1"
        assert await rotate_secrets(db) == {"rotated": 1, "unreadable": 0}
        await db.commit()
    monkeypatch.setattr(get_settings(), "secret_keys", new_key)
    async with get_sessionmaker()() as db:
        assert await get_secret(db, alice.id, "gitlab_token") == "glpat-1"


async def test_unreadable_secret_is_reported_not_500(client_for, monkeypatch) -> None:
    alice = await client_for("alice")
    assert (await alice.put("/api/secrets/gitlab_token", json={"value": "glpat-1"})).status_code == 204
    monkeypatch.setattr(get_settings(), "secret_keys", Fernet.generate_key().decode())  # the old key is gone
    listed = {s["kind"]: s for s in (await alice.get("/api/secrets")).json()}
    assert listed["gitlab_token"]["status"] == "unreadable" and listed["jira_token"]["status"] == "not_set"
    async with get_sessionmaker()() as db:
        with pytest.raises(Exception) as err:
            await get_secret(db, alice.user_id, "gitlab_token")
        assert err.value.code == "credential_invalid" and "Re-enter it in Settings" in err.value.detail
        assert await rotate_secrets(db) == {"rotated": 0, "unreadable": 1}
    assert (await alice.put("/api/secrets/gitlab_token", json={"value": "glpat-2"})).status_code == 204
    listed = {s["kind"]: s for s in (await alice.get("/api/secrets")).json()}
    assert listed["gitlab_token"]["status"] == "set"


# ---------- offboarding ----------


async def test_offboarding_preview_and_transfer(client_for, task_queue) -> None:
    admin = await client_for("root", role="admin")
    alice = await client_for("alice")
    # Public and private documents.
    for name, share in [("shared.md", True), ("mine.md", False)]:
        doc = (
            await alice.post("/api/knowledge/documents", files={"file": (name, f"# {name}".encode(), "text/markdown")})
        ).json()
        if share:
            await alice.post(f"/api/knowledge/documents/{doc['id']}/visibility", json={"visibility": "public"})
    # Wiki: one page shared with the team, one private.
    team = (await alice.post("/api/wiki/pages", json={"path": "team/runbook.md", "content": "# Runbook"})).json()
    await alice.post(f"/api/wiki/pages/{team['id']}/visibility", json={"visibility": "public"})
    await alice.post("/api/wiki/pages", json={"path": "me/notes.md", "content": "# Notes"})
    # Projects: shared, private, and a shared one whose repo the admin also added.
    for url, share in [("https://g/shared.git", True), ("https://g/private.git", False), ("https://g/both.git", True)]:
        p = (await alice.post("/api/projects", json={"repo_url": url, "host": "gitlab"})).json()
        if share:
            await alice.post(f"/api/projects/{p['id']}/visibility", json={"visibility": "public"})
    await admin.post("/api/projects", json={"repo_url": "https://g/both.git", "host": "gitlab"})
    # Chats: one shared, one private.
    async with get_sessionmaker()() as db:
        db.add_all(
            [
                ChatSession(owner_id=alice.user_id, title="shared chat", visibility="public"),
                ChatSession(owner_id=alice.user_id, title="private chat"),
            ]
        )
        await db.commit()
    await alice.put("/api/secrets/gitlab_token", json={"value": "glpat-1"})
    private_repo = get_settings().wiki_dir / "users" / str(alice.user_id)
    assert private_repo.exists()

    preview = await admin.get(
        f"/api/admin/users/{alice.user_id}/offboarding-preview", params={"transfer_to": admin.user_id}
    )
    assert preview.status_code == 200
    body = preview.json()
    assert body["transfer"] == {"documents": 2, "wiki_pages": 1, "projects": 1, "chat_sessions": 1, "episodes": 0}
    assert body["delete"]["wiki_pages"] == 1 and body["delete"]["projects"] == 2
    assert body["delete"]["chat_sessions"] == 1 and body["delete"]["secrets"] == 1
    assert body["delete"]["documents"] == 2  # the private upload and the private page's index entry
    assert len(body["duplicate_projects"]) == 1

    # Guard rails.
    assert (
        await admin.delete(f"/api/admin/users/{admin.user_id}", params={"transfer_to": admin.user_id})
    ).status_code == 400
    not_admin = await admin.delete(f"/api/admin/users/{alice.user_id}", params={"transfer_to": alice.user_id})
    assert not_admin.status_code == 422
    assert (
        await alice.delete(f"/api/admin/users/{alice.user_id}", params={"transfer_to": admin.user_id})
    ).status_code == 403

    done = await admin.delete(f"/api/admin/users/{alice.user_id}", params={"transfer_to": admin.user_id})
    assert done.status_code == 200
    assert done.json()["transfer"] == body["transfer"] and done.json()["delete"] == body["delete"]  # counts match

    async with get_sessionmaker()() as db:
        assert await db.get(User, alice.user_id) is None
        docs = list(await db.scalars(select(Document)))
        assert {d.owner_id for d in docs} == {admin.user_id}
        assert {d.visibility for d in docs} == {"public"}
        assert await db.scalar(select(func.count()).where(Chunk.owner_id == alice.user_id)) == 0
        pages = list(await db.scalars(select(WikiPage)))
        assert [(p.path, p.owner_id) for p in pages] == [("team/runbook.md", admin.user_id)]
        projects = {p.repo_url: p.owner_id for p in await db.scalars(select(Project))}
        assert projects == {"https://g/shared.git": admin.user_id, "https://g/both.git": admin.user_id}
        assert len(projects) == 2
        chats = list(await db.scalars(select(ChatSession)))
        assert [(c.title, c.owner_id) for c in chats] == [("shared chat", admin.user_id)]
        assert await db.scalar(select(func.count()).select_from(UserSecret)) == 0
        transfers = list(await db.scalars(select(AuditLog).where(AuditLog.action == "offboard.transfer")))
        assert len(transfers) == 5 and all(a.detail["from_user"] == alice.user_id for a in transfers)
        assert await db.scalar(select(func.count()).where(AuditLog.action == "user.delete")) == 1

    # The public page is still readable by the new owner; files go in the background, under the right locks.
    assert (await admin.get(f"/api/wiki/pages/{team['id']}")).status_code == 200
    jobs = [j for j in task_queue.jobs.values() if j["task_name"] in ("cleanup_user_files", "cleanup_project_cache")]
    assert sorted(j["task_name"] for j in jobs) == [
        "cleanup_project_cache",
        "cleanup_project_cache",
        "cleanup_user_files",
    ]
    assert next(j for j in jobs if j["task_name"] == "cleanup_user_files")["lock"] == f"librarian:{alice.user_id}"
    await tasks_mod.cleanup_user_files(user_id=alice.user_id)
    assert not private_repo.exists()
    assert (get_settings().wiki_dir / "public").exists()


async def test_offboarding_suffixes_colliding_documents(client_for) -> None:
    admin = await client_for("root", role="admin")
    alice = await client_for("alice")
    async with get_sessionmaker()() as db:
        for owner in (alice.user_id, admin.user_id):
            db.add(
                Document(
                    owner_id=owner,
                    source_type="upload",
                    source_ref="upload:notes.md",  # pre-H9 refs used the file name
                    title="notes.md",
                    content_hash="h",
                    visibility="public",
                )
            )
        await db.commit()
    r = await admin.delete(f"/api/admin/users/{alice.user_id}", params={"transfer_to": admin.user_id})
    assert r.status_code == 200
    async with get_sessionmaker()() as db:
        refs = sorted(await db.scalars(select(Document.source_ref)))
    assert refs == ["upload:notes.md", f"upload:notes.md#from-{alice.user_id}"]


# ---------- CLI ----------


async def _cli(*args: str) -> tuple[int, str]:
    """Run the real CLI in a subprocess (it has its own event loop and engine); env points at the test DB."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from app.cli import main; main()",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode()


async def test_cli_reset_password_and_list_users(make_user) -> None:
    await make_user("gina")
    code, out = await _cli("reset-password", "--username", "Gina")
    assert code == 0, out
    password = out.split("One-time password for gina: ", 1)[1].split()[0]
    assert len(password) == 16
    code, listing = await _cli("list-users")
    assert code == 0 and "gina\tmember\tactive, must change password\tlast sign-in never" in listing
    async with _anon("10.9.0.1") as c:
        assert await _login(c, "gina", PASSWORD) == 401
        assert await _login(c, "gina", password) == 200
    code, out = await _cli("reset-password", "--username", "nobody")
    assert code == 1 and "No user nobody" in out


async def test_cli_rotate_secrets(make_user) -> None:
    alice = await make_user("alice")
    async with get_sessionmaker()() as db:
        await set_secret(db, alice.id, "gitlab_token", "glpat-1")
        await db.commit()
    code, out = await _cli("rotate-secrets")
    assert code == 0 and "rotated: 1, unreadable (users must re-enter): 0" in out, out

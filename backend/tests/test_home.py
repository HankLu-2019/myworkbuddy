"""Home, activity, profile, repository detail, librarian revert (design 07-ux §4.1, §4.6, §5.1, §5.5, §5.10)."""

from datetime import UTC, datetime

import httpx2
from sqlalchemy import update

from app.db import get_sessionmaker
from app.knowledge.wiki import LIBRARIAN, find_page, read_page, repo_for, save_page
from app.llm import keys
from app.llm.routes import month_start
from app.models import ChatSession, Document, LibrarianRun, Message, Project, User, UserSecret
from app.ops import health
from app.secrets.service import set_secret
from tests.fakellm import server as fake


def _key_of(user_id: int) -> dict:
    [rec] = [r for r in fake.KEYS.values() if r["key_alias"] == f"mwb-user-{user_id}"]
    return rec


async def _project(owner_id: int, **kw) -> int:
    async with get_sessionmaker()() as db:
        p = Project(
            owner_id=owner_id, name=kw.pop("name", "etl"), repo_url=f"https://g/{kw.get('status', 'x')}.git",
            host="gitlab", **kw,
        )  # fmt: skip
        db.add(p)
        await db.commit()
        return p.id


# ---------- home ----------


async def test_home_for_a_new_member_is_a_getting_started_checklist(client_for) -> None:
    alice = await client_for("alice")
    body = (await alice.get("/api/home")).json()
    assert body["needs_you"] == [] and body["running"] == [] and body["recent_chats"] == []
    [checklist] = body["checklists"]
    assert checklist["kind"] == "getting_started"
    assert {s["key"]: s["done"] for s in checklist["steps"]} == {
        "timezone": False, "tokens": False, "repository": False, "import": False, "question": False,
    }  # fmt: skip


async def test_checklist_ticks_as_the_member_works(client_for) -> None:
    alice = await client_for("alice")
    assert (await alice.patch("/api/auth/me", json={"timezone": "Asia/Shanghai"})).status_code == 200
    await alice.put("/api/secrets/gitlab_token", json={"value": "glpat-x"})
    await _project(alice.user_id, status="ready")
    async with get_sessionmaker()() as db:
        s = ChatSession(owner_id=alice.user_id, title="first", source="chat")
        db.add(s)
        await db.flush()
        db.add(Message(session_id=s.id, role="user", content="hi"))
        await db.commit()
    steps = {s["key"]: s["done"] for s in (await alice.get("/api/home")).json()["checklists"][0]["steps"]}
    assert steps == {"timezone": True, "tokens": True, "repository": True, "import": False, "question": True}


async def test_admin_also_gets_the_setup_checklist(client_for) -> None:
    health.set_gateway_transport(httpx2.ASGITransport(app=fake.app))
    try:
        root = await client_for("root", role="admin")
        [admin, member] = (await root.get("/api/home")).json()["checklists"]
    finally:
        health.set_gateway_transport(None)
    assert admin["kind"] == "admin_setup" and member["kind"] == "getting_started"
    steps = {s["key"]: s["done"] for s in admin["steps"]}
    assert steps == {"system": False, "users": False, "budgets": False}  # the worker has no heartbeat in tests
    await client_for("alice")
    async with get_sessionmaker()() as db:
        await db.execute(update(User).where(User.username == "alice").values(llm_budget_usd=20))
        await db.commit()
    health.set_gateway_transport(httpx2.ASGITransport(app=fake.app))
    try:
        steps = {s["key"]: s["done"] for s in (await root.get("/api/home")).json()["checklists"][0]["steps"]}
    finally:
        health.set_gateway_transport(None)
    assert steps["users"] and steps["budgets"]


async def test_needs_you_lists_failed_ingests_unreadable_tokens_and_budget(client_for) -> None:
    alice = await client_for("alice")
    bob = await client_for("bob")
    pid = await _project(alice.user_id, name="payments-api", status="failed", error="Authentication failed for x\nmore")
    await _project(bob.user_id, name="bobs", status="failed", error="nope")  # not Alice's
    async with get_sessionmaker()() as db:
        db.add(UserSecret(user_id=alice.user_id, kind="jira_token", ciphertext="not-a-fernet-token"))
        await db.commit()
    await keys.ensure_key(alice.user_id)
    _key_of(alice.user_id)["spend"] = 42.5  # of the default 50
    cards = (await alice.get("/api/home")).json()["needs_you"]
    assert [c["kind"] for c in cards] == ["budget_warning", "ingest_failed", "credential_unreadable"]
    budget, ingest, secret = cards
    assert (
        budget["message"] == "You've used 85% of this month's AI budget ($50.00)."
        and budget["href"] == "/settings/usage"
    )
    assert ingest["message"] == "Indexing payments-api failed: Authentication failed for x"
    assert ingest["href"] == f"/sources/repos/{pid}" and ingest["action"] == "Open repository"
    assert secret["action"] == "Update token" and secret["href"] == "/settings/integrations?focus=jira_token"


async def test_budget_reached_card(client_for) -> None:
    alice = await client_for("alice")
    await keys.ensure_key(alice.user_id)
    _key_of(alice.user_id)["spend"] = 50
    [card] = (await alice.get("/api/home")).json()["needs_you"]
    assert card["kind"] == "budget_exceeded" and "An admin can raise it" in card["message"]


async def test_home_survives_the_gateway_being_down(client_for) -> None:
    alice = await client_for("alice")
    keys.set_transport(httpx2.MockTransport(lambda r: httpx2.Response(503)))
    r = await alice.get("/api/home")
    assert r.status_code == 200 and r.json()["needs_you"] == []


async def test_recent_shows_my_chats_visible_pages_and_last_nights_librarian(client_for) -> None:
    alice = await client_for("alice")
    bob = await client_for("bob")
    async with get_sessionmaker()() as db:
        owner = await db.get(User, alice.user_id)
        for i in range(7):
            db.add(ChatSession(owner_id=alice.user_id, title=f"chat {i}", source="chat"))
        db.add(ChatSession(owner_id=alice.user_id, title="imported", source="claude-code", external_ref="x"))
        await db.commit()
        await save_page(db, owner=owner, path="notes/a.md", content="# A\n", author=LIBRARIAN)
        bobs = await db.get(User, bob.user_id)
        await save_page(db, owner=bobs, path="notes/secret.md", content="# Secret\n", author=LIBRARIAN)
        db.add(
            LibrarianRun(
                user_id=alice.user_id, local_date=datetime.now(UTC).date(), status="partial",
                pages_updated=["notes/a.md", "me/how-i-work.md"], pages_rejected=[{"path": "x", "reason": "r"}],
            )
        )  # fmt: skip
        await db.commit()
    body = (await alice.get("/api/home")).json()
    assert len(body["recent_chats"]) == 5 and all(c["title"].startswith("chat") for c in body["recent_chats"])
    assert [p["path"] for p in body["recent_pages"]] == ["notes/a.md"]  # Bob's private page stays hidden
    assert body["librarian"]["pages_changed"] == 2 and body["librarian"]["rejected"] == 1


# ---------- activity ----------


async def test_activity_lists_my_queued_and_running_ingests(client_for) -> None:
    alice = await client_for("alice")
    bob = await client_for("bob")
    running = await _project(alice.user_id, name="api", status="running", progress="Indexing code 4 of 10")
    queued = await _project(alice.user_id, name="etl", status="queued")
    await _project(alice.user_id, name="done", status="ready")
    await _project(bob.user_id, name="bobs", status="running")
    items = (await alice.get("/api/activity")).json()["items"]
    assert {i["id"]: (i["status"], i["step"]) for i in items} == {
        running: ("running", "Indexing code 4 of 10"),
        queued: ("queued", "Waiting to start"),
    }
    assert items[0]["href"].startswith("/sources/repos/")
    assert (await alice.get("/api/home")).json()["running"] == items


# ---------- profile ----------


async def test_profile_update_validates_timezone_and_is_audited(client_for) -> None:
    alice = await client_for("alice")
    r = await alice.patch("/api/auth/me", json={"timezone": "Mars/Olympus"})
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
    r = await alice.patch("/api/auth/me", json={"display_name": "  Alice L ", "timezone": "Europe/Warsaw"})
    assert r.status_code == 200
    user = r.json()
    assert user["display_name"] == "Alice L" and user["timezone"] == "Europe/Warsaw" and user["timezone_confirmed"]
    assert (await alice.patch("/api/auth/me", json={"email": "not-an-email"})).status_code == 422
    assert (await alice.patch("/api/auth/me", json={"email": ""})).json()["email"] is None
    alice.headers.pop("X-CSRF-Token")
    assert (await alice.patch("/api/auth/me", json={"display_name": "x"})).json()["code"] == "csrf_invalid"
    async with get_sessionmaker()() as db:
        from sqlalchemy import select

        from app.models import AuditLog

        actions = list(await db.scalars(select(AuditLog.action).where(AuditLog.user_id == alice.user_id)))
    assert actions.count("profile.update") == 2


def test_month_start_uses_the_users_timezone() -> None:
    user = User(timezone="Asia/Shanghai")
    # 2026-10-31 20:00 UTC is already 1 November in Shanghai.
    assert month_start(user, datetime(2026, 10, 31, 20, tzinfo=UTC)) == datetime(2026, 10, 31, 16, tzinfo=UTC)
    assert month_start(User(timezone="Not/AZone"), datetime(2026, 10, 15, tzinfo=UTC)).day == 1


async def test_usage_includes_this_months_chat_spend(client_for) -> None:
    alice = await client_for("alice")
    async with get_sessionmaker()() as db:
        s = ChatSession(owner_id=alice.user_id, title="t", source="chat")
        db.add(s)
        await db.flush()
        db.add(Message(session_id=s.id, role="assistant", content="a", cost_usd=0.0125))
        db.add(
            Message(
                session_id=s.id,
                role="assistant",
                content="old",
                cost_usd=5,
                created_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        )
        await db.commit()
    assert (await alice.get("/api/usage")).json()["chat_usd_month"] == 0.0125


# ---------- repository detail ----------


async def test_repository_detail_counts_and_fix_hints(client_for) -> None:
    alice = await client_for("alice")
    bob = await client_for("bob")
    pid = await _project(alice.user_id, name="payments-api", status="failed", error="fatal: Authentication failed")
    async with get_sessionmaker()() as db:
        for i, kind in enumerate(["code", "code", "repo_doc"]):
            db.add(
                Document(
                    owner_id=alice.user_id, visibility="private", source_type=kind, source_ref=f"{pid}:f{i}",
                    title=f"f{i}", content_hash="h", project_id=pid,
                )
            )  # fmt: skip
        await db.commit()
    d = (await alice.get(f"/api/projects/{pid}")).json()
    assert (d["files"], d["docs"], d["code_chunks"]) == (2, 1, 0)
    assert d["hint"]["action"] == "Add token"  # no GitLab token at all
    async with get_sessionmaker()() as db:
        await set_secret(db, alice.user_id, "gitlab_token", "glpat-x")
        await db.commit()
    hint = (await alice.get(f"/api/projects/{pid}")).json()["hint"]
    assert hint == {
        "message": "Your GitLab token was rejected. Update it, then re-index.",
        "action": "Update token",
        "href": "/settings/integrations?focus=gitlab_token",
    }
    assert (await bob.get(f"/api/projects/{pid}")).json()["code"] == "not_found"  # private stays private


# ---------- librarian revert ----------


async def _librarian_edit(user_id: int, path: str, before: str | None, after: str) -> str:
    async with get_sessionmaker()() as db:
        owner = await db.get(User, user_id)
        if before is not None:
            await save_page(db, owner=owner, path=path, content=before, author=LIBRARIAN.__class__("Alice", "a@x"))
        page = await find_page(db, user_id, "private", path)
        await save_page(db, owner=owner, path=path, content=after, author=LIBRARIAN, page=page)
    return await repo_for("private", user_id).last_commit(path)


async def test_revert_restores_the_previous_version_with_a_new_commit(client_for) -> None:
    alice = await client_for("alice")
    sha = await _librarian_edit(alice.user_id, "me/how-i-work.md", "# How\n\nmine\n", "# How\n\nlibrarian\n")
    r = await alice.post(f"/api/wiki/librarian/changes/{sha}/revert")
    assert r.status_code == 200 and r.json() == {"reverted": ["me/how-i-work.md"]}
    async with get_sessionmaker()() as db:
        page = await find_page(db, alice.user_id, "private", "me/how-i-work.md")
        assert read_page(page) == "# How\n\nmine\n"
    log = await repo_for("private", alice.user_id).log(path="me/how-i-work.md")
    assert log[0].message.startswith(f"Revert librarian change {sha[:8]}") and log[0].email != LIBRARIAN.email


async def test_revert_of_a_created_page_deletes_it(client_for) -> None:
    alice = await client_for("alice")
    sha = await _librarian_edit(alice.user_id, "projects/new.md", None, "# New\n")
    assert (await alice.post(f"/api/wiki/librarian/changes/{sha}/revert")).status_code == 200
    async with get_sessionmaker()() as db:
        assert await find_page(db, alice.user_id, "private", "projects/new.md") is None


async def test_revert_refuses_when_the_page_changed_since(client_for) -> None:
    alice = await client_for("alice")
    sha = await _librarian_edit(alice.user_id, "me/a.md", "# A\n\none\n", "# A\n\ntwo\n")
    async with get_sessionmaker()() as db:
        owner = await db.get(User, alice.user_id)
        page = await find_page(db, alice.user_id, "private", "me/a.md")
        await save_page(
            db, owner=owner, path="me/a.md", content="# A\n\nthree\n", author=LIBRARIAN.__class__("A", "a@x"), page=page
        )
    r = await alice.post(f"/api/wiki/librarian/changes/{sha}/revert")
    assert r.status_code == 409 and "changed after" in r.json()["detail"]


async def test_revert_only_librarian_commits_of_my_own_wiki(client_for) -> None:
    alice = await client_for("alice")
    bob = await client_for("bob")
    sha = await _librarian_edit(alice.user_id, "me/a.md", "# A\n\none\n", "# A\n\ntwo\n")
    assert (await bob.post(f"/api/wiki/librarian/changes/{sha}/revert")).status_code == 404
    own = await repo_for("private", alice.user_id).log(path="me/a.md")
    user_commit = own[-1].sha  # Alice's own first version
    assert (await alice.post(f"/api/wiki/librarian/changes/{user_commit}/revert")).status_code == 404
    assert (await alice.post("/api/wiki/librarian/changes/zzz/revert")).status_code == 422


# ---------- system ----------


async def test_system_page_shows_recent_audit_entries(client_for) -> None:
    health.set_gateway_transport(httpx2.ASGITransport(app=fake.app))
    try:
        root = await client_for("root", role="admin")
        await root.post("/api/admin/users", json={"username": "carol"})
        audit = (await root.get("/api/admin/system")).json()["audit"]
    finally:
        health.set_gateway_transport(None)
    assert audit[0]["action"] == "user.create" and audit[0]["user"] == "root"

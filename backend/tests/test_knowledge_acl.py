"""Private by default; only explicitly published items are visible to the team."""


async def _titles(client, q, scope="all"):
    resp = await client.get("/api/knowledge/search", params={"q": q, "scope": scope})
    assert resp.status_code == 200
    return [h["title"] for h in resp.json()]


async def _upload(client, name, body):
    resp = await client.post("/api/knowledge/documents", files={"file": (name, body.encode(), "text/markdown")})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_uploads_are_private_until_shared(client_for):
    alice = await client_for("alice")
    bob = await client_for("bob")
    doc = await _upload(alice, "postmortem.md", "# Postmortem\n\nGerrit replication lag caused stale builds.")
    assert doc["visibility"] == "private"

    assert await _titles(alice, "gerrit replication") == ["postmortem.md"]
    assert await _titles(bob, "gerrit replication") == []
    assert (await bob.get("/api/knowledge/documents")).json() == []
    assert (await bob.delete(f"/api/knowledge/documents/{doc['id']}")).status_code == 404
    shared = await bob.post(f"/api/knowledge/documents/{doc['id']}/visibility", json={"visibility": "public"})
    assert shared.status_code == 404

    pub = await alice.post(f"/api/knowledge/documents/{doc['id']}/visibility", json={"visibility": "public"})
    assert pub.json()["visibility"] == "public"
    assert await _titles(bob, "gerrit replication") == ["postmortem.md"]
    listed = (await bob.get("/api/knowledge/documents")).json()
    assert listed[0]["mine"] is False
    assert (await bob.delete(f"/api/knowledge/documents/{doc['id']}")).status_code == 403

    await alice.post(f"/api/knowledge/documents/{doc['id']}/visibility", json={"visibility": "private"})
    assert await _titles(bob, "gerrit replication") == []
    assert (await alice.delete(f"/api/knowledge/documents/{doc['id']}")).status_code == 204
    assert await _titles(alice, "gerrit replication") == []


async def test_admin_can_unpublish_but_not_publish_others(client_for):
    alice = await client_for("alice")
    admin = await client_for("root", role="admin")
    secret = await _upload(alice, "secret.md", "private notes about salaries")
    assert (
        await admin.post(f"/api/knowledge/documents/{secret['id']}/visibility", json={"visibility": "public"})
    ).status_code == 404
    shared = await _upload(alice, "shared.md", "team runbook")
    await alice.post(f"/api/knowledge/documents/{shared['id']}/visibility", json={"visibility": "public"})
    down = await admin.post(f"/api/knowledge/documents/{shared['id']}/visibility", json={"visibility": "private"})
    assert down.status_code == 200
    assert down.json()["visibility"] == "private"


async def test_upload_validation(client_for):
    alice = await client_for("alice")
    exe = await alice.post("/api/knowledge/documents", files={"file": ("tool.exe", b"MZ", "application/octet-stream")})
    assert exe.status_code == 415
    empty = await alice.post("/api/knowledge/documents", files={"file": ("empty.txt", b"   ", "text/plain")})
    assert empty.status_code == 422


async def test_wiki_lifecycle(client_for):
    alice = await client_for("alice")
    bob = await client_for("bob")
    created = await alice.post(
        "/api/wiki/pages", json={"path": "runbooks/oom.md", "content": "# OOM runbook\n\nRaise executor memory."}
    )
    assert created.status_code == 201
    page = created.json()
    assert page["title"] == "OOM runbook"
    assert page["visibility"] == "private"
    assert page["can_edit"] is True

    dup = await alice.post("/api/wiki/pages", json={"path": "runbooks/oom.md", "content": "x"})
    assert dup.status_code == 409
    for bad in ["../etc/passwd.md", "/abs.md", "notes.txt", ".git/config.md", "a//b.md"]:
        assert (await alice.post("/api/wiki/pages", json={"path": bad, "content": "x"})).status_code == 422

    pid = page["id"]
    assert (await bob.get(f"/api/wiki/pages/{pid}")).status_code == 404
    assert await _titles(bob, "executor memory", "wiki") == []

    edited = await alice.put(
        f"/api/wiki/pages/{pid}", json={"content": "# OOM runbook v2\n\nUse 8g executors.", "message": "bump memory"}
    )
    assert edited.json()["title"] == "OOM runbook v2"
    history = (await alice.get(f"/api/wiki/pages/{pid}/history")).json()
    assert [c["message"] for c in history] == ["bump memory", "Create runbooks/oom.md"]
    diff = (await alice.get(f"/api/wiki/pages/{pid}/commits/{history[0]['sha']}")).json()["diff"]
    assert "+Use 8g executors." in diff
    assert (await alice.get(f"/api/wiki/pages/{pid}/commits/not-a-sha")).status_code == 422

    published = await alice.post(f"/api/wiki/pages/{pid}/visibility", json={"visibility": "public"})
    assert published.json()["visibility"] == "public"
    assert published.json()["content"].startswith("# OOM runbook v2")
    seen = await bob.get(f"/api/wiki/pages/{pid}")
    assert seen.status_code == 200
    assert seen.json()["can_edit"] is False
    assert await _titles(bob, "executor", "wiki") == ["OOM runbook v2"]
    assert (await bob.put(f"/api/wiki/pages/{pid}", json={"content": "vandalism"})).status_code == 403
    assert (await bob.delete(f"/api/wiki/pages/{pid}")).status_code == 403

    # Bob can't publish a page at a path the team wiki already uses.
    clash = (await bob.post("/api/wiki/pages", json={"path": "runbooks/oom.md", "content": "# Mine"})).json()
    assert (
        await bob.post(f"/api/wiki/pages/{clash['id']}/visibility", json={"visibility": "public"})
    ).status_code == 409

    pages = (await bob.get("/api/wiki/pages")).json()
    assert {(p["path"], p["visibility"], p["mine"]) for p in pages} == {
        ("runbooks/oom.md", "public", False),
        ("runbooks/oom.md", "private", True),
    }

    back = await alice.post(f"/api/wiki/pages/{pid}/visibility", json={"visibility": "private"})
    assert back.json()["visibility"] == "private"
    assert (await bob.get(f"/api/wiki/pages/{pid}")).status_code == 404
    assert (await alice.delete(f"/api/wiki/pages/{pid}")).status_code == 204
    assert (await alice.get(f"/api/wiki/pages/{pid}")).status_code == 404


async def test_admin_can_edit_public_pages(client_for):
    alice = await client_for("alice")
    admin = await client_for("root", role="admin")
    pid = (await alice.post("/api/wiki/pages", json={"path": "team.md", "content": "# Team"})).json()["id"]
    assert (await admin.get(f"/api/wiki/pages/{pid}")).status_code == 404
    await alice.post(f"/api/wiki/pages/{pid}/visibility", json={"visibility": "public"})
    fixed = await admin.put(f"/api/wiki/pages/{pid}", json={"content": "# Team (fixed)"})
    assert fixed.status_code == 200
    history = (await admin.get(f"/api/wiki/pages/{pid}/history")).json()
    assert history[0]["author"] == "root"


async def test_shared_sessions(client_for):
    alice = await client_for("alice")
    bob = await client_for("bob")
    sid = (await alice.post("/api/chat/sessions", json={"title": "Spark tuning"})).json()["id"]
    assert (await bob.get(f"/api/chat/sessions/{sid}")).status_code == 404
    assert (await bob.get(f"/api/chat/sessions/{sid}/messages")).status_code == 404
    assert (await bob.get("/api/chat/shared")).json() == []

    await alice.patch(f"/api/chat/sessions/{sid}", json={"visibility": "public"})
    assert [s["title"] for s in (await bob.get("/api/chat/shared")).json()] == ["Spark tuning"]
    assert (await bob.get(f"/api/chat/sessions/{sid}/messages")).status_code == 200
    # Viewing is allowed; changing or posting is not.
    assert (await bob.patch(f"/api/chat/sessions/{sid}", json={"title": "mine"})).status_code == 404
    assert (await bob.post(f"/api/chat/sessions/{sid}/messages", json={"content": "hi"})).status_code == 404
    assert (await bob.delete(f"/api/chat/sessions/{sid}")).status_code == 404

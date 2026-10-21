"""Design 01 §H7: optimistic concurrency, compensation, reconcile, history across publish."""

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import get_sessionmaker
from app.knowledge import wiki
from app.knowledge.gitrepo import Author, GitRepo
from app.knowledge.reconcile import reconcile_wiki
from app.models import Chunk, Document, SystemState, User, WikiPage

ME = Author("Out Of Band", "oob@example.com")


def _repo(*parts: str) -> GitRepo:
    return GitRepo(get_settings().wiki_dir.joinpath(*parts))


async def _create(client, path: str, content: str) -> dict:
    resp = await client.post("/api/wiki/pages", json={"path": path, "content": content})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _page_row(page_id: int) -> WikiPage:
    async with get_sessionmaker()() as db:
        return await db.get(WikiPage, page_id)


async def test_concurrent_edits_with_the_same_base_conflict(client_for) -> None:
    alice = await client_for("alice")
    page = await _create(alice, "notes.md", "# Notes\n\nv1")
    first = await alice.put(
        f"/api/wiki/pages/{page['id']}", json={"content": "# Notes\n\ntab A", "base_sha": page["head_sha"]}
    )
    assert first.status_code == 200
    second = await alice.put(
        f"/api/wiki/pages/{page['id']}", json={"content": "# Notes\n\ntab B", "base_sha": page["head_sha"]}
    )
    assert second.status_code == 409
    body = second.json()
    assert body["code"] == "wiki_conflict"
    assert body["current_sha"] == first.json()["head_sha"]
    assert "tab A" in body["current_content"]
    assert "tab A" in (await alice.get(f"/api/wiki/pages/{page['id']}")).json()["content"]


async def test_db_failure_after_git_commit_is_rolled_back_in_git(client_for, monkeypatch) -> None:
    alice = await client_for("alice")
    page = await _create(alice, "notes.md", "# Notes\n\nv1")
    repo = _repo("users", str(alice.user_id))
    head_before = await repo.head()

    async def boom(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(wiki, "write_document", boom)
    with pytest.raises(RuntimeError):
        await alice.put(
            f"/api/wiki/pages/{page['id']}", json={"content": "# Notes\n\nv2", "base_sha": page["head_sha"]}
        )
    assert await repo.head() == head_before
    assert repo.read("notes.md") == "# Notes\n\nv1"
    assert (await _page_row(page["id"])).head_sha == page["head_sha"]


async def test_failed_publish_leaves_the_file_in_the_private_repo(client_for, monkeypatch) -> None:
    alice = await client_for("alice")
    page = await _create(alice, "plan.md", "# Plan")

    async def boom(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(wiki, "set_visibility", boom)
    with pytest.raises(RuntimeError):
        await alice.post(f"/api/wiki/pages/{page['id']}/visibility", json={"visibility": "public"})
    assert _repo("users", str(alice.user_id)).read("plan.md") == "# Plan"
    assert _repo("public").read("plan.md") is None
    assert (await _page_row(page["id"])).visibility == "private"


async def test_reconcile_repairs_each_kind_of_drift(client_for, make_user) -> None:
    alice = await client_for("alice")
    bob = await client_for("bob")
    await make_user("root", role="admin")
    edited = await _create(alice, "edited.md", "# Edited\n\nold text")
    moved = await _create(alice, "moved.md", "# Moved\n\nquokka facts")
    gone = await _create(alice, "gone.md", "# Gone")
    private = _repo("users", str(alice.user_id))
    public = _repo("public")

    # Out-of-band changes: an edit, a half-done publish, a deleted file, and a file with no row.
    await private.write("edited.md", "# Edited\n\nnew zebra text", ME, "edit outside the app")
    await public.write("moved.md", "# Moved\n\nquokka facts", ME, "half publish")
    await private.remove("moved.md", ME, "half publish (moved)")
    await private.remove("gone.md", ME, "deleted outside the app")
    await private.write("orphan.md", "# Orphan\n\nadopt me", ME, "no row")

    dry = await reconcile_wiki(dry_run=True)
    assert dry.adopted and (await _page_row(edited["id"])).head_sha == edited["head_sha"]

    report = await reconcile_wiki()
    assert report.resynced == [edited["id"]]
    assert report.visibility_fixed == [moved["id"]]
    assert report.missing == [gone["id"]]
    assert report.adopted == [f"users/{alice.user_id}/orphan.md"]

    assert (await _page_row(edited["id"])).head_sha == await private.last_commit("edited.md")
    hits = (await alice.get("/api/knowledge/search", params={"q": "zebra", "scope": "wiki"})).json()
    assert hits and hits[0]["title"] == "Edited"
    assert (await bob.get(f"/api/wiki/pages/{moved['id']}")).json()["visibility"] == "public"
    async with get_sessionmaker()() as db:
        chunk_vis = set(
            await db.scalars(
                select(Chunk.visibility).join(Document).where(Document.source_ref == f"wiki:{moved['id']}")
            )
        )
        state = await db.get(SystemState, "wiki_reconcile")
        orphan = await db.scalar(select(WikiPage).where(WikiPage.path == "orphan.md"))
    assert chunk_vis == {"public"}
    assert state.value["missing"] == [gone["id"]]
    assert orphan.owner_id == alice.user_id and orphan.visibility == "private"
    paths = {p["path"] for p in (await alice.get("/api/wiki/pages")).json()}
    assert "gone.md" not in paths and "orphan.md" in paths

    # A second run finds nothing to do.
    again = await reconcile_wiki()
    assert not (again.resynced or again.visibility_fixed or again.missing or again.adopted)


async def test_history_follows_publish_for_the_owner_only(client_for) -> None:
    alice = await client_for("alice")
    bob = await client_for("bob")
    page = await _create(alice, "guide.md", "# Guide\n\nv1")
    await alice.put(f"/api/wiki/pages/{page['id']}", json={"content": "# Guide\n\nv2", "base_sha": page["head_sha"]})
    await alice.post(f"/api/wiki/pages/{page['id']}/visibility", json={"visibility": "public"})

    owner_view = [c["message"] for c in (await alice.get(f"/api/wiki/pages/{page['id']}/history")).json()]
    assert owner_view[0] == "Publish guide.md"
    assert owner_view[1:] == ["Update guide.md", "Create guide.md"]
    team_view = [c["message"] for c in (await bob.get(f"/api/wiki/pages/{page['id']}/history")).json()]
    assert team_view == ["Publish guide.md"]

    first_sha = (await alice.get(f"/api/wiki/pages/{page['id']}/history")).json()[-1]["sha"]
    version = await alice.get(f"/api/wiki/pages/{page['id']}/versions/{first_sha}")
    assert version.json()["content"] == "# Guide\n\nv1"
    assert "+v1" in (await alice.get(f"/api/wiki/pages/{page['id']}/commits/{first_sha}")).json()["diff"]
    assert (await bob.get(f"/api/wiki/pages/{page['id']}/versions/{first_sha}")).status_code == 404
    assert (await bob.get(f"/api/wiki/pages/{page['id']}/commits/{first_sha}")).status_code == 404


async def test_history_does_not_borrow_commits_from_similar_pages(client_for) -> None:
    alice = await client_for("alice")
    body = "# Runbook\n\n" + "Restart the executor and raise memory.\n" * 20
    old = await _create(alice, "old.md", body)
    assert (await alice.delete(f"/api/wiki/pages/{old['id']}")).status_code == 204
    new = await _create(alice, "new.md", body)
    history = [c["message"] for c in (await alice.get(f"/api/wiki/pages/{new['id']}/history")).json()]
    assert history == ["Create new.md"]


async def test_librarian_does_not_overwrite_an_edit_made_during_its_run(client_for) -> None:
    from app.knowledge.librarian import update_user_wiki
    from app.llm.models import set_model_override
    from app.models import ChatSession, Episode
    from tests.helpers import scripted_model
    from tests.test_memory import SUMMARY

    alice = await client_for("alice")
    page = await _create(alice, "me/how-i-work.md", "# How I work\n\nUses uv.")
    async with get_sessionmaker()() as db:
        s = ChatSession(owner_id=alice.user_id, title="x")
        db.add(s)
        await db.flush()
        db.add(Episode(session_id=s.id, owner_id=alice.user_id, summary={**SUMMARY, "decisions": []}, message_count=2))
        await db.commit()
    repo_dir = Path(get_settings().wiki_dir, "users", str(alice.user_id))

    calls: list[str] = []

    def rewrite_while_user_edits(prompt: str) -> str:
        # The user saves while the LLM is "thinking" -- on both attempts, so the librarian gives up (design 01 §H8).
        calls.append(prompt)
        (repo_dir / "me/how-i-work.md").write_text(f"# How I work\n\nUser's own edit {len(calls)}.")
        subprocess.run(
            ["git", "-c", "user.name=u", "-c", "user.email=u@x", "commit", "-qam", "user edit"],
            cwd=repo_dir,
            check=True,
        )
        return "# How I work\n\nLibrarian rewrite."

    set_model_override(scripted_model(rewrite_while_user_edits))
    async with get_sessionmaker()() as db:
        user = await db.get(User, alice.user_id)
        updated = await update_user_wiki(db, user)
        await db.commit()
    assert "me/how-i-work.md" not in updated
    assert len(calls) == 2  # one retry after the first conflict, then skipped
    assert (repo_dir / "me/how-i-work.md").read_text() == "# How I work\n\nUser's own edit 2."
    assert page["id"]

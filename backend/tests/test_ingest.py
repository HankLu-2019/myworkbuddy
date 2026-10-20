import base64
import json
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db import get_sessionmaker
from app.knowledge.ingest import repos
from app.knowledge.ingest.repos import _auth_header, ingest_project, run_git
from app.knowledge.wiki import find_page, read_page
from app.llm.models import set_model_override
from app.models import Document, Project, WikiPage
from app.secrets.service import set_secret
from tests.helpers import scripted_model


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "Alice",
            "GIT_AUTHOR_EMAIL": "alice@example.com",
            "GIT_COMMITTER_NAME": "Alice",
            "GIT_COMMITTER_EMAIL": "alice@example.com",
            "HOME": str(cwd),
            "PATH": "/usr/bin:/bin",
        },
    )


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "pipeline"
    (repo / "src").mkdir(parents=True)
    (repo / "vendor").mkdir()
    (repo / "README.md").write_text("# Pipeline\n\nCollects Jenkins build metrics into Postgres.\n")
    (repo / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (repo / "src" / "collector.py").write_text(
        "def fetch_builds(job):\n    return []\n\nclass MetricsWriter:\n    def write(self, rows):\n        pass\n"
    )
    (repo / "vendor" / "lib.py").write_text("def vendored():\n    pass\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "Add build collector")
    return repo


async def _project(owner_id: int, visibility: str = "private") -> int:
    async with get_sessionmaker()() as db:
        n = len(list(await db.scalars(select(Project.id))))
        p = Project(
            owner_id=owner_id,
            name="pipeline",
            repo_url=f"https://gitlab.example.com/t{n}/pipeline.git",
            host="gitlab",
            visibility=visibility,
        )
        db.add(p)
        await db.commit()
        return p.id


async def test_ingest_project_indexes_code_docs_and_writes_card(client_for, fixture_repo, monkeypatch):
    alice = await client_for("alice")
    prompts: list[str] = []

    async def fake_checkout(db, project):
        return fixture_repo

    def card(prompt: str) -> str:
        prompts.append(prompt)
        return "# pipeline\n\n## Purpose\nCollects Jenkins build metrics."

    monkeypatch.setattr(repos, "sync_checkout", fake_checkout)
    set_model_override(
        scripted_model(card, structured={"response": {"fetch_builds": "Fetches builds for a Jenkins job."}})
    )
    pid = await _project(alice.user_id)

    async with get_sessionmaker()() as db:
        await ingest_project(db, pid)
    async with get_sessionmaker()() as db:
        project = await db.get(Project, pid)
        assert project.status == "ready" and project.error is None and len(project.last_ingested_sha) == 40
        docs = {
            d.source_type + ":" + d.meta["path"]
            for d in await db.scalars(select(Document))
            if d.source_type in ("code", "repo_doc")
        }
        assert docs == {"code:src/collector.py", "repo_doc:README.md"}
        page = await find_page(db, alice.user_id, "private", "projects/pipeline.md")
        assert read_page(page).startswith("# pipeline")
    assert "Add build collector" in prompts[0]  # the user's own commits feed the card
    assert "=== Dockerfile ===" in prompts[0]

    hits = (await alice.get("/api/knowledge/search", params={"q": "fetch_builds", "scope": "code"})).json()
    assert hits[0]["meta"]["symbol"] == "fetch_builds"
    assert "Fetches builds for a Jenkins job." in hits[0]["excerpt"]

    # Unchanged HEAD: nothing is re-indexed and the card isn't regenerated.
    async with get_sessionmaker()() as db:
        await ingest_project(db, pid)
    assert len(prompts) == 1

    # Files deleted upstream drop out of the index.
    (fixture_repo / "src" / "collector.py").unlink()
    _git(fixture_repo, "commit", "-qam", "remove collector")
    async with get_sessionmaker()() as db:
        await ingest_project(db, pid)
        refs = [d.meta["path"] for d in await db.scalars(select(Document).where(Document.source_type == "code"))]
    assert refs == []


async def test_card_failure_keeps_index(client_for, fixture_repo, monkeypatch):
    alice = await client_for("alice")

    async def fake_checkout(db, project):
        return fixture_repo

    def boom(prompt: str) -> str:
        raise RuntimeError("model down")

    monkeypatch.setattr(repos, "sync_checkout", fake_checkout)
    set_model_override(scripted_model(boom, structured={}))
    pid = await _project(alice.user_id)
    async with get_sessionmaker()() as db:
        await ingest_project(db, pid)
    project = (await alice.get("/api/projects")).json()[0]
    assert project["status"] == "ready"
    assert "project card could not be written" in project["error"]


async def test_checkout_failure_marks_project_failed(client_for, monkeypatch):
    alice = await client_for("alice")

    async def broken(db, project):
        raise RuntimeError("git clone failed: auth required")

    monkeypatch.setattr(repos, "sync_checkout", broken)
    pid = await _project(alice.user_id)
    async with get_sessionmaker()() as db:
        with pytest.raises(RuntimeError):
            await ingest_project(db, pid)
    project = (await alice.get("/api/projects")).json()[0]
    assert project["status"] == "failed" and "auth required" in project["error"]


async def test_project_api_and_visibility(client_for, task_queue):
    alice = await client_for("alice")
    bob = await client_for("bob")
    bad = await alice.post("/api/projects", json={"repo_url": "file:///etc", "host": "gitlab"})
    assert bad.status_code == 422
    created = await alice.post(
        "/api/projects", json={"repo_url": "https://gitlab.example.com/t/etl.git", "host": "gitlab"}
    )
    assert created.status_code == 201
    pid = created.json()["id"]
    assert created.json()["name"] == "etl"
    assert [j["task_name"] for j in task_queue.jobs.values()] == ["ingest_project"]
    dup = await alice.post("/api/projects", json={"repo_url": "https://gitlab.example.com/t/etl.git", "host": "gitlab"})
    assert dup.status_code == 409

    async with get_sessionmaker()() as db:
        db.add(
            Document(
                owner_id=alice.user_id,
                source_type="code",
                source_ref=f"{pid}:etl.py",
                title="etl/etl.py",
                content_hash="x",
                project_id=pid,
            )
        )
        await db.commit()

    assert (await bob.get("/api/projects")).json() == []
    assert (await bob.post(f"/api/projects/{pid}/ingest")).status_code == 404
    shared = await alice.post(f"/api/projects/{pid}/visibility", json={"visibility": "public"})
    assert shared.json()["visibility"] == "public"
    async with get_sessionmaker()() as db:
        assert (await db.scalar(select(Document).where(Document.project_id == pid))).visibility == "public"
    seen = (await bob.get("/api/projects")).json()
    assert seen[0]["mine"] is False
    assert (await bob.post(f"/api/projects/{pid}/ingest")).status_code == 403
    assert (await bob.delete(f"/api/projects/{pid}")).status_code == 403

    assert (await alice.post(f"/api/projects/{pid}/ingest")).json() == {"queued": False}  # already queued
    assert (await alice.delete(f"/api/projects/{pid}")).status_code == 204
    async with get_sessionmaker()() as db:
        assert await db.scalar(select(Document).where(Document.project_id == pid)) is None


async def test_auth_header_and_secret_masking(make_user, tmp_path):
    alice = await make_user("alice")
    async with get_sessionmaker()() as db:
        gitlab = Project(owner_id=alice.id, name="a", repo_url="https://x/a.git", host="gitlab")
        gerrit = Project(owner_id=alice.id, name="b", repo_url="https://x/b", host="gerrit")
        assert await _auth_header(db, gitlab) is None
        await set_secret(db, alice.id, "gitlab_token", "glpat-1")
        await set_secret(db, alice.id, "gerrit_username", "alice")
        await set_secret(db, alice.id, "gerrit_password", "pw")
        assert await _auth_header(db, gitlab) == "Authorization: Basic " + base64.b64encode(b"oauth2:glpat-1").decode()
        assert base64.b64decode((await _auth_header(db, gerrit)).split()[-1]) == b"alice:pw"

    header = "Authorization: Basic c2VjcmV0"
    with pytest.raises(RuntimeError) as err:
        await run_git(
            "clone", "-q", "--", "https://127.0.0.1:9/none.git", str(tmp_path / "x"), cwd=tmp_path, auth_header=header
        )
    assert "c2VjcmV0" not in str(err.value)


async def test_claude_code_import_api(client_for, task_queue):
    alice = await client_for("alice")
    lines = [
        {
            "type": "user",
            "sessionId": "abc",
            "timestamp": "2026-10-01T10:00:00Z",
            "message": {"role": "user", "content": "Debug the gerrit trigger"},
        },
        {
            "type": "assistant",
            "sessionId": "abc",
            "timestamp": "2026-10-01T10:01:00Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Checking the stream events."}]},
        },
    ]
    body = "\n".join(json.dumps(x) for x in lines).encode()
    files = [("files", ("abc.jsonl", body, "application/json")), ("files", ("junk.jsonl", b"{}", "application/json"))]
    results = (await alice.post("/api/imports/claude-code", files=files)).json()
    assert [r["status"] for r in results] == ["imported", "failed"]
    assert results[0]["title"] == "Debug the gerrit trigger"
    assert [j["task_name"] for j in task_queue.jobs.values()] == ["build_episode"]

    again = (await alice.post("/api/imports/claude-code", files=files[:1])).json()
    assert again[0]["status"] == "unchanged"
    grown = body + b"\n" + json.dumps({**lines[0], "message": {"role": "user", "content": "thanks"}}).encode()
    regrown = (await alice.post("/api/imports/claude-code", files=[("files", ("abc.jsonl", grown, "x"))])).json()
    assert regrown[0]["status"] == "imported"
    sessions = (await alice.get("/api/chat/sessions", params={"source": "claude-code"})).json()
    assert len(sessions) == 1
    messages = (await alice.get(f"/api/chat/sessions/{sessions[0]['id']}/messages")).json()
    assert [m["content"] for m in messages][-1] == "thanks"


async def test_symlinks_are_never_followed(client_for, fixture_repo, tmp_path, monkeypatch):
    alice = await client_for("alice")
    secret = tmp_path / "secret.txt"
    secret.write_text("MWB_SECRET_KEY=do-not-leak")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.md").write_text("# Other user's notes\n\nprivate-marker")
    (fixture_repo / "LEAK.md").symlink_to(secret)
    (fixture_repo / "docs").symlink_to(outside)
    (fixture_repo / "broken.md").symlink_to(tmp_path / "missing")
    _git(fixture_repo, "add", ".")
    _git(fixture_repo, "commit", "-qm", "symlinks")
    prompts: list[str] = []

    async def fake_checkout(db, project):
        return fixture_repo

    monkeypatch.setattr(repos, "sync_checkout", fake_checkout)
    set_model_override(scripted_model(lambda p: prompts.append(p) or "# card", structured={"response": {}}))
    pid = await _project(alice.user_id)
    async with get_sessionmaker()() as db:
        await ingest_project(db, pid)
    assert (await alice.get("/api/projects")).json()[0]["status"] == "ready"
    for marker in ["do-not-leak", "private-marker"]:
        hits = (await alice.get("/api/knowledge/search", params={"q": marker, "limit": 50})).json()
        assert not any(marker in h["excerpt"] for h in hits)
    assert "do-not-leak" not in prompts[0]


async def test_visibility_change_during_ingest_wins(client_for, fixture_repo, monkeypatch):
    alice = await client_for("alice")
    bob = await client_for("bob")
    pid = await _project(alice.user_id, visibility="public")

    async def checkout_then_unpublish(db, project):
        # The owner makes the project private while the worker is mid-run.
        await alice.post(f"/api/projects/{pid}/visibility", json={"visibility": "private"})
        return fixture_repo

    monkeypatch.setattr(repos, "sync_checkout", checkout_then_unpublish)
    set_model_override(scripted_model("# card", structured={"response": {}}))
    async with get_sessionmaker()() as db:
        await ingest_project(db, pid)
    assert (await bob.get("/api/knowledge/search", params={"q": "fetch_builds"})).json() == []
    async with get_sessionmaker()() as db:
        assert {d.visibility for d in await db.scalars(select(Document).where(Document.project_id == pid))} == {
            "private"
        }


async def test_same_named_repos_get_separate_cards(client_for, fixture_repo, monkeypatch):
    alice = await client_for("alice")

    async def fake_checkout(db, project):
        return fixture_repo

    monkeypatch.setattr(repos, "sync_checkout", fake_checkout)
    first, second = await _project(alice.user_id), await _project(alice.user_id)
    for pid, text in [(first, "# first"), (second, "# second")]:
        set_model_override(scripted_model(text, structured={"response": {}}))
        async with get_sessionmaker()() as db:
            await ingest_project(db, pid, force=True)
    async with get_sessionmaker()() as db:
        cards = [await db.get(WikiPage, (await db.get(Project, pid)).card_page_id) for pid in (first, second)]
        assert [c.path for c in cards] == ["projects/pipeline.md", f"projects/pipeline-{second}.md"]
        assert [read_page(c).strip() for c in cards] == ["# first", "# second"]

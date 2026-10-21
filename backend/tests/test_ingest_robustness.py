"""Ingestion robustness (design 01 §H9)."""

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import get_sessionmaker
from app.errors import AppError
from app.knowledge.ingest import repos
from app.knowledge.ingest.repos import (
    Progress,
    check_disk_space,
    git_env,
    ingest_project,
    normalize_repo_url,
    run_git,
    sweep_orphan_caches,
    sync_checkout,
)
from app.llm.models import set_model_override
from app.models import Chunk, Document, Project, SystemState
from app.secrets.service import set_secret
from app.tasks import app as tasks_mod
from tests.helpers import scripted_model
from tests.test_ingest import _project, fixture_repo  # noqa: F401  (fixture)

HEADER = "Authorization: Basic Z2xwYXQtc2VjcmV0"  # oauth2:glpat-secret


class _Usage:
    def __init__(self, total: int, free: int) -> None:
        self.total, self.used, self.free = total, total - free, free


@pytest.fixture(autouse=True)
def healthy_disk(monkeypatch):
    """Don't depend on the CI runner's free space; tests that need a full disk patch this again."""
    monkeypatch.setattr(repos.shutil, "disk_usage", lambda _p: _Usage(100, 50))


# ---------- serialization ----------


async def test_ingest_jobs_for_one_project_never_overlap(task_queue) -> None:
    assert await tasks_mod.defer_ingest(7)
    assert not await tasks_mod.defer_ingest(7)  # already waiting: de-duplicated
    [first] = task_queue.jobs.values()
    assert first["lock"] == "project:7" and first["queueing_lock"] == "ingest:7"

    worker = (await task_queue.register_worker_one())["worker_id"]
    assert (await task_queue.fetch_job_one(None, worker))["id"] == first["id"]  # now running
    # A re-ingest and a cache cleanup can be queued while it runs, but neither starts until it finishes.
    assert await tasks_mod.defer_ingest(7, force=True)
    await tasks_mod.defer_once(tasks_mod.cleanup_project_cache, "cache-cleanup:7", lock="project:7", project_id=7)
    assert await tasks_mod.defer_ingest(8)
    other = await task_queue.fetch_job_one(None, worker)
    assert other["args"] == {"project_id": 8, "force": False}  # other projects are not blocked
    assert (await task_queue.fetch_job_one(None, worker))["id"] is None
    first["status"] = "succeeded"
    nxt = await task_queue.fetch_job_one(None, worker)
    assert nxt["task_name"] == "ingest_project" and nxt["args"]["force"] is True


# ---------- credentials ----------


async def test_git_auth_header_goes_through_env_not_argv(monkeypatch, tmp_path) -> None:
    seen: dict = {}

    class Proc:
        returncode = 1

        async def communicate(self):
            return b"", f"fatal: auth failed for {HEADER} (Z2xwYXQtc2VjcmV0)".encode()

    async def fake_exec(*argv, env, **kw):
        seen["argv"], seen["env"] = argv, env
        return Proc()

    monkeypatch.setattr(repos.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "5")  # an inherited value must not leak in
    with pytest.raises(RuntimeError) as err:
        await run_git("fetch", "origin", cwd=tmp_path, auth_header=HEADER)
    assert not any("Z2xwYXQtc2VjcmV0" in a for a in seen["argv"])
    assert seen["env"]["GIT_CONFIG_COUNT"] == "1"
    assert seen["env"]["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert seen["env"]["GIT_CONFIG_VALUE_0"] == HEADER
    assert "Z2xwYXQtc2VjcmV0" not in str(err.value) and "***" in str(err.value)


async def test_git_reads_the_header_from_env(tmp_path) -> None:
    # Real git: the header is visible to git through the environment, and absent without it.
    assert (await run_git("config", "--get", "http.extraHeader", cwd=tmp_path, auth_header=HEADER)).strip() == HEADER
    assert "GIT_CONFIG_VALUE_0" not in git_env(None)


async def test_clone_uses_no_tags_and_no_symlinks(make_user, monkeypatch) -> None:
    alice = await make_user("alice")
    calls: list[tuple[str, ...]] = []

    async def fake_git(*args, cwd, auth_header=None):
        calls.append(args)
        return ""

    monkeypatch.setattr(repos, "run_git", fake_git)
    async with get_sessionmaker()() as db:
        await set_secret(db, alice.id, "gitlab_token", "glpat-secret")
        project = Project(owner_id=alice.id, name="p", repo_url="https://gitlab.example.com/t/p.git", host="gitlab")
        db.add(project)
        await db.commit()
        await sync_checkout(db, project)
        clone = calls[0]
        assert clone[0] == "clone" and "--no-tags" in clone and "core.symlinks=false" in clone
        (repos.cache_dir(project.id) / ".git").mkdir(parents=True)
        calls.clear()
        await sync_checkout(db, project)
        assert calls[0] == ("config", "core.symlinks", "false")
        assert calls[1][0] == "fetch" and "--no-tags" in calls[1]


# ---------- Gerrit URLs ----------


def test_gerrit_urls_get_the_authenticated_prefix() -> None:
    assert normalize_repo_url("https://gerrit.example.com/a/team/app", "gerrit") == (
        "https://gerrit.example.com/a/team/app",
        None,
    )
    url, notice = normalize_repo_url("https://gerrit.example.com/team/app", "gerrit")
    assert url == "https://gerrit.example.com/a/team/app" and "/a/" in notice
    assert normalize_repo_url("https://gitlab.example.com/team/app.git", "gitlab") == (
        "https://gitlab.example.com/team/app.git",
        None,
    )


async def test_gerrit_rewrite_is_reported_by_the_api(client_for) -> None:
    alice = await client_for("alice")
    r = await alice.post("/api/projects", json={"repo_url": "https://gerrit.example.com/team/app", "host": "gerrit"})
    assert r.status_code == 201
    body = r.json()
    assert body["repo_url"] == "https://gerrit.example.com/a/team/app"
    assert "Gerrit needs the /a/ prefix" in body["notice"]
    again = await alice.post(
        "/api/projects", json={"repo_url": "https://gerrit.example.com/team/app", "host": "gerrit"}
    )
    assert again.status_code == 409  # the normalised URL is what de-duplicates


# ---------- cache lifecycle ----------


async def test_deleting_a_project_removes_its_cache(client_for, task_queue) -> None:
    alice = await client_for("alice")
    pid = (
        await alice.post("/api/projects", json={"repo_url": "https://gitlab.example.com/t/etl.git", "host": "gitlab"})
    ).json()["id"]
    cache = repos.cache_dir(pid)
    (cache / ".git").mkdir(parents=True)
    (cache / "file.py").write_text("x")
    assert (await alice.delete(f"/api/projects/{pid}")).status_code == 204
    [job] = [j for j in task_queue.jobs.values() if j["task_name"] == "cleanup_project_cache"]
    assert job["lock"] == f"project:{pid}" and job["args"] == {"project_id": pid}
    await tasks_mod.cleanup_project_cache(project_id=pid)
    assert not cache.exists()


async def test_weekly_sweep_removes_orphan_caches(make_user) -> None:
    alice = await make_user("alice")
    pid = await _project(alice.id)
    root = get_settings().repo_cache_dir
    for name in (str(pid), "999", "not-a-project"):
        (root / name).mkdir(parents=True, exist_ok=True)
    async with get_sessionmaker()() as db:
        assert await sweep_orphan_caches(db) == [999]
    assert (root / str(pid)).exists() and (root / "not-a-project").exists() and not (root / "999").exists()
    await tasks_mod.repo_cache_sweep(timestamp=0)


# ---------- disk guard ----------


async def test_ingest_refuses_to_start_when_disk_is_low(make_user, monkeypatch) -> None:
    alice = await make_user("alice")
    pid = await _project(alice.id)
    monkeypatch.setattr(repos.shutil, "disk_usage", lambda _p: _Usage(100, 5))
    cloned = []
    monkeypatch.setattr(repos, "run_git", lambda *a, **k: cloned.append(a))
    async with get_sessionmaker()() as db:
        with pytest.raises(AppError) as err:
            await ingest_project(db, pid)
    assert err.value.code == "disk_full" and err.value.status_code == 507
    assert cloned == []
    async with get_sessionmaker()() as db:
        project = await db.get(Project, pid)
        assert project.status == "failed" and project.progress is None
        assert project.error.startswith("Only 5% of the data disk is free")
        state = (await db.get(SystemState, "disk_guard")).value
        assert state["ok"] is False and "5%" in state["problem"]


async def test_disk_guard_cache_limit_and_ok_state(monkeypatch) -> None:
    root = get_settings().repo_cache_dir
    (root / "1").mkdir(parents=True, exist_ok=True)
    (root / "1" / "big").write_bytes(b"x" * 2048)
    os.symlink("/etc/passwd", root / "1" / "link")  # symlinks are counted by their own size, not followed
    monkeypatch.setattr(get_settings(), "repo_cache_max_gb", 1 / 1024**2)  # 1 KiB
    async with get_sessionmaker()() as db:
        with pytest.raises(AppError, match="repository cache uses"):
            await check_disk_space(db)
    monkeypatch.setattr(get_settings(), "repo_cache_max_gb", 50)
    async with get_sessionmaker()() as db:
        await check_disk_space(db)
        await db.commit()
        assert (await db.get(SystemState, "disk_guard")).value["ok"] is True


# ---------- progress ----------


async def test_ingest_reports_progress_and_clears_it(client_for, fixture_repo, monkeypatch) -> None:  # noqa: F811
    alice = await client_for("alice")
    written: list[str | None] = []
    orig = Progress._write

    async def record(self, text):
        written.append(text)
        await orig(self, text)

    async def fake_checkout(db, project):
        return fixture_repo

    monkeypatch.setattr(Progress, "_write", record)
    monkeypatch.setattr(repos, "sync_checkout", fake_checkout)
    set_model_override(scripted_model("# pipeline\n", structured={"response": {"fetch_builds": "Fetches builds."}}))
    pid = await _project(alice.user_id)
    async with get_sessionmaker()() as db:
        await ingest_project(db, pid)
    assert written[0] == "Reading files"
    assert "Summarising files 1 of 1" in written and "Indexing code 1 of 1" in written
    assert "Writing the project card" in written and written[-1] is None
    assert (await alice.get("/api/projects")).json()[0]["progress"] is None
    async with get_sessionmaker()() as db:
        project = await db.get(Project, pid)
        project.status, project.progress = "running", "Indexing code 3 of 9"
        await db.commit()
    assert (await alice.get("/api/projects")).json()[0]["progress"] == "Indexing code 3 of 9"


async def test_progress_is_throttled(make_user, monkeypatch) -> None:
    alice = await make_user("alice")
    pid = await _project(alice.id)
    progress = Progress(pid)
    for i in range(1, 50):
        await progress("Indexing code", i, 100)
    async with get_sessionmaker()() as db:
        assert (await db.get(Project, pid)).progress == "Indexing code 1 of 100"
    await progress("Indexing code", 100, 100)  # the final count is always written
    await progress("Writing the project card")  # so is a new step
    async with get_sessionmaker()() as db:
        assert (await db.get(Project, pid)).progress == "Writing the project card"


# ---------- uploads ----------


async def test_same_name_uploads_are_separate_and_replace_keeps_id(client_for) -> None:
    alice = await client_for("alice")
    bob = await client_for("bob")

    async def upload(body: str) -> dict:
        r = await alice.post("/api/knowledge/documents", files={"file": ("notes.md", body.encode(), "text/markdown")})
        assert r.status_code == 201
        return r.json()

    a, b = await upload("# A\n\nfirst"), await upload("# B\n\nsecond")
    assert a["id"] != b["id"] and a["title"] == b["title"] == "notes.md"
    await alice.post(f"/api/knowledge/documents/{a['id']}/visibility", json={"visibility": "public"})

    replaced = await alice.put(
        f"/api/knowledge/documents/{a['id']}/file",
        files={"file": ("notes-v2.md", b"# A v2\n\nreplaced body\n\n## More\n\nsection", "text/markdown")},
    )
    assert replaced.status_code == 200
    body = replaced.json()
    assert body["id"] == a["id"] and body["visibility"] == "public" and body["title"] == "notes-v2.md"
    assert body["chunks"] >= 1
    async with get_sessionmaker()() as db:
        assert len(list(await db.scalars(select(Document).where(Document.source_type == "upload")))) == 2
    async with get_sessionmaker()() as db:
        contents = list(await db.scalars(select(Chunk.content).where(Chunk.document_id == a["id"])))
    assert any("replaced body" in c for c in contents) and not any("first" in c for c in contents)

    # A teammate who can see the public document still can't replace it.
    r = await bob.put(f"/api/knowledge/documents/{a['id']}/file", files={"file": ("x.md", b"# X", "text/markdown")})
    assert r.status_code == 403
    r = await bob.put(f"/api/knowledge/documents/{b['id']}/file", files={"file": ("x.md", b"# X", "text/markdown")})
    assert r.status_code == 404


# ---------- transcripts ----------


async def test_oversize_transcript_fails_only_that_file(client_for, monkeypatch) -> None:
    alice = await client_for("alice")
    monkeypatch.setattr(get_settings(), "transcript_max_mb", 1)
    line = {
        "type": "user",
        "sessionId": "s1",
        "timestamp": "2026-10-01T10:00:00Z",
        "message": {"role": "user", "content": "hello"},
    }
    ok = json.dumps(line).encode()
    big = ok + b"\n" + b" " * (1024 * 1024)
    files = [("files", ("big.jsonl", big, "application/json")), ("files", ("ok.jsonl", ok, "application/json"))]
    results = (await alice.post("/api/imports/claude-code", files=files)).json()
    assert [(r["filename"], r["status"], r["code"]) for r in results] == [
        ("big.jsonl", "failed", "file_too_large"),
        ("ok.jsonl", "imported", None),
    ]
    assert "Larger than 1 MB" in results[0]["error"]


def test_settings_reject_nonpositive_limits(monkeypatch) -> None:
    from pydantic import ValidationError

    from app.config import Settings

    monkeypatch.setenv("MWB_REPO_CACHE_MAX_GB", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_cache_dir_is_under_the_repo_cache() -> None:
    assert repos.cache_dir(5) == Path(get_settings().repo_cache_dir, "5")

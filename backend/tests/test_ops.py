"""Operations foundations (design 01 §H14): readiness, heartbeat, backups, restore, System page."""

import asyncio
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import psycopg
import pytest
from sqlalchemy import update

from app.config import get_settings
from app.db import get_sessionmaker
from app.models import Chunk, SystemState
from app.ops import backup, health
from tests.fakellm import server as fake

TOOLS = all(shutil.which(t) for t in ("pg_dump", "pg_restore", "psql", "createdb", "dropdb", "restic", "tar"))
needs_tools = pytest.mark.skipif(
    not TOOLS and not os.environ.get("MWB_REQUIRE_BACKUP_TOOLS"), reason="pg client tools and restic not installed"
)


@pytest.fixture(autouse=True)
def gateway_is_the_fake():
    health.set_gateway_transport(httpx2.ASGITransport(app=fake.app))
    yield
    health.set_gateway_transport(None)


# ---------- readiness and heartbeat ----------


async def test_ready_names_what_is_down(anon) -> None:
    r = await anon.get("/api/ready")
    assert r.status_code == 503 and r.json()["checks"]["worker"] == "worker: no heartbeat yet"
    await health.beat()
    r = await anon.get("/api/ready")
    assert r.status_code == 200 and r.json() == {
        "ready": True,
        "checks": {"database": "ok", "llm_gateway": "ok", "worker": "ok"},
    }

    def down(request):
        return httpx2.Response(500)

    health.set_gateway_transport(httpx2.MockTransport(down))
    r = await anon.get("/api/ready")
    assert r.status_code == 503 and r.json()["checks"]["llm_gateway"] == "llm gateway: HTTP 500"


async def test_stale_heartbeat_fails_readiness_and_health(anon) -> None:
    old = (datetime.now(UTC) - timedelta(minutes=4)).isoformat()
    await health.put_state(health.HEARTBEAT_KEY, {"at": old})
    r = await anon.get("/api/ready")
    assert r.status_code == 503 and r.json()["checks"]["worker"].startswith("worker: last heartbeat 24")
    code, out = await _cli("worker-health")
    assert code == 1 and "unhealthy" in out
    await health.beat()
    code, out = await _cli("worker-health")
    assert code == 0 and "healthy" in out


async def test_heartbeat_loop_keeps_beating(monkeypatch) -> None:
    monkeypatch.setattr(health, "HEARTBEAT_INTERVAL", 0.05)
    task = asyncio.create_task(health.heartbeat_loop())
    await asyncio.sleep(0.2)
    task.cancel()
    state = (await health.get_states(health.HEARTBEAT_KEY))[health.HEARTBEAT_KEY]
    assert health.heartbeat_age(state) < timedelta(seconds=5) and state["pid"] == os.getpid()


async def _cli(*args: str) -> tuple[int, str]:
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


# ---------- scheduling ----------


def test_next_backup_time_in_local_time(monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "backup_time", "01:30")
    monkeypatch.setattr(s, "backup_timezone", "Europe/Stockholm")
    # 21 Oct 2026, 12:00 UTC = 14:00 CEST -> next is 22 Oct 01:30 CEST = 23:30 UTC on the 21st.
    assert backup.next_run(datetime(2026, 10, 21, 12, 0, tzinfo=UTC)) == datetime(2026, 10, 21, 23, 30, tzinfo=UTC)
    # After the clocks go back (25 Oct), 01:30 CET = 00:30 UTC.
    assert backup.next_run(datetime(2026, 10, 25, 12, 0, tzinfo=UTC)) == datetime(2026, 10, 26, 0, 30, tzinfo=UTC)


def test_backup_alerts() -> None:
    now = datetime(2026, 10, 21, 12, tzinfo=UTC)
    assert "not configured" in backup.status_problem(None)
    ok = {"status": "ok", "last_success_at": (now - timedelta(hours=10)).isoformat()}
    assert backup.status_problem(ok, now) is None
    assert "27 h ago" in backup.status_problem({**ok, "last_success_at": (now - timedelta(hours=27)).isoformat()}, now)
    assert "failed" in backup.status_problem({**ok, "status": "failed", "error": "disk"}, now)
    assert "No successful backup" in backup.status_problem({"status": "failed", "error": "x"}, now)


async def test_unconfigured_backup_is_recorded(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "backup_repo", "")
    with pytest.raises(backup.BackupError, match="MWB_BACKUP_REPO"):
        await backup.run_backup()
    state = (await health.get_states(backup.STATE_KEY))[backup.STATE_KEY]
    assert state["status"] == "not_configured"


# ---------- backup and restore with real tools ----------


def _url(dbname: str) -> str:
    return get_settings().database_url.rsplit("/", 1)[0] + f"/{dbname}"


def _psql(dbname: str, sql: str) -> list:
    with psycopg.connect(_url(dbname).replace("+asyncpg", "").replace("+psycopg", ""), autocommit=True) as conn:
        cur = conn.execute(sql)
        return cur.fetchall() if cur.description else []


@needs_tools
async def test_backup_then_restore_round_trip(client_for, git_integrations, tmp_path, monkeypatch) -> None:
    s = get_settings()
    alice = await client_for("alice")
    gitlab = git_integrations["gitlab"]
    await alice.put(f"/api/integrations/{gitlab}/credentials/token", json={"value": "glpat-keep-me"})
    page = (await alice.post("/api/wiki/pages", json={"path": "notes/restore.md", "content": "# Restore me"})).json()
    (s.data_dir / "repos" / "7").mkdir(parents=True, exist_ok=True)
    (s.data_dir / "repos" / "7" / "big.bin").write_bytes(b"x" * 1000)  # excluded: rebuilt by re-ingest
    (s.data_dir / "tmp").mkdir(exist_ok=True)
    (s.data_dir / "tmp" / "scratch").write_text("x")

    # A gateway database next to ours, like in production.
    admin_db = s.database_url.rsplit("/", 1)[0].replace("+asyncpg", "").replace("+psycopg", "") + "/postgres"
    with psycopg.connect(admin_db, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS litellm WITH (FORCE)")
        conn.execute("CREATE DATABASE litellm")
    _psql("litellm", "CREATE TABLE verification_token (token text); INSERT INTO verification_token VALUES ('k1')")

    monkeypatch.setattr(s, "backup_repo", str(tmp_path / "repo"))
    monkeypatch.setattr(s, "backup_staging_dir", tmp_path / "staging")
    monkeypatch.setenv("RESTIC_PASSWORD", "test-restic-password")
    result = await backup.run_backup()
    assert result.snapshot_id and set(result.sizes) == {"mwb.dump", "litellm.dump", "data.tar"}
    state = (await health.get_states(backup.STATE_KEY))[backup.STATE_KEY]
    assert state["status"] == "ok" and state["snapshot_id"] == result.snapshot_id

    paths = await backup.list_snapshot("latest")
    names = {Path(p).name for p in paths}
    assert {"mwb.dump", "litellm.dump", "data.tar"} <= names
    listing = subprocess.run(["tar", "-tf", str(tmp_path / "staging" / "data.tar")], capture_output=True, text=True)
    assert f"./wiki/users/{alice.user_id}/notes/restore.md" in listing.stdout
    assert "./repos/" not in listing.stdout and "./tmp/" not in listing.stdout

    # Restore into another database name and data dir, as a drill does with a separate compose project.
    target_db = "mwb_restore_drill"
    monkeypatch.setattr(s, "database_url", _url(target_db))
    target_data = tmp_path / "restored-data"
    target_data.mkdir()
    monkeypatch.setattr(s, "data_dir", target_data)
    try:
        report = await backup.restore("latest", force=True, workdir=tmp_path / "restore")
    finally:
        monkeypatch.undo()
    assert report["databases"] == ["mwb", "litellm"]
    assert _psql(target_db, "SELECT username FROM users") == [("alice",)]
    assert _psql(target_db, "SELECT path FROM wiki_pages") == [(page["path"],)]
    assert _psql(target_db, "SELECT count(*) FROM user_credentials WHERE kind = 'token'") == [(1,)]
    assert _psql("litellm", "SELECT token FROM verification_token") == [("k1",)]
    assert (
        (target_data / "wiki" / "users" / str(alice.user_id) / "notes" / "restore.md")
        .read_text()
        .startswith("# Restore me")
    )
    assert not (target_data / "repos").exists()
    with psycopg.connect(admin_db, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {target_db} WITH (FORCE)")
        conn.execute("DROP DATABASE IF EXISTS litellm WITH (FORCE)")


@needs_tools
async def test_restore_refuses_a_database_with_users(make_user, tmp_path, monkeypatch) -> None:
    await make_user("alice")
    monkeypatch.setattr(get_settings(), "backup_repo", str(tmp_path / "repo"))
    monkeypatch.setenv("RESTIC_PASSWORD", "x")
    with pytest.raises(backup.BackupError, match="already has 1 users"):
        await backup.restore("latest")


@needs_tools
async def test_failed_backup_keeps_the_last_success(monkeypatch, tmp_path) -> None:
    s = get_settings()
    await health.put_state(backup.STATE_KEY, {"status": "ok", "last_success_at": "2026-10-20T23:30:00+00:00"})
    monkeypatch.setattr(s, "backup_repo", str(tmp_path / "repo"))
    monkeypatch.setattr(s, "backup_staging_dir", tmp_path / "staging")
    monkeypatch.setenv("RESTIC_PASSWORD", "x")
    monkeypatch.setattr(s, "database_url", _url("no_such_database"))
    with pytest.raises(backup.BackupError):
        await backup.run_backup()
    state = (await health.get_states(backup.STATE_KEY))[backup.STATE_KEY]
    assert state["status"] == "failed" and state["last_success_at"] == "2026-10-20T23:30:00+00:00"
    assert "x" not in state["error"].split()  # the restic password isn't echoed


# ---------- System page ----------


async def test_system_page_shows_each_item(client_for, task_queue) -> None:
    admin = await client_for("root", role="admin")
    member = await client_for("alice")
    assert (await member.get("/api/admin/system")).status_code == 403
    await health.beat()
    await health.put_state("disk_guard", {"ok": False, "problem": "Only 5% of the data disk is free"})
    await health.put_state("wiki_reconcile", {"missing": ["a.md"]})
    await backup.record_drill(True, "restored into mwb-drill")
    doc = (
        await member.post("/api/knowledge/documents", files={"file": ("x.md", b"# X\n\ntext", "text/markdown")})
    ).json()
    async with get_sessionmaker()() as db:
        await db.execute(
            update(Chunk)
            .where(Chunk.document_id == doc["id"])
            .values(embedding=None, embed_attempts=5, embed_error="400")
        )
        await db.commit()
    body = (await admin.get("/api/admin/system")).json()
    assert body["checks"] == {"database": "ok", "llm_gateway": "ok", "worker": "ok"}
    assert body["worker"]["age_s"] is not None and body["worker"]["age_s"] < 60
    assert set(body["jobs"]) == {"queues", "failed_24h", "periodic"}
    assert body["backup"]["status"] == "not_configured" and body["backup_drill"]["ok"] is True
    assert body["disk"]["total_bytes"] > 0 and body["disk_guard"]["ok"] is False
    assert body["wiki_reconcile"] == {"missing": ["a.md"]}
    assert body["embeddings"]["failed"] == 1 and body["embeddings"]["sample"][0]["error"] == "400"
    assert set(body["llm_spend"]) == {"month_to_date_usd", "users_counted", "users_unknown"}
    alerts = " | ".join(body["alerts"])
    for expected in ("Backups are not configured", "Only 5% of the data disk", "missing from git", "failed embedding"):
        assert expected in alerts


async def test_failed_jobs_are_listed_and_can_be_retried(client_for) -> None:
    admin = await client_for("root", role="admin")
    # Real procrastinate tables (the in-memory connector is only for deferring in tests).
    rows = _psql(
        get_settings().database_url.rsplit("/", 1)[1],
        "INSERT INTO procrastinate_jobs (queue_name, task_name, args, status, attempts) "
        "VALUES ('default', 'build_episode', '{}', 'failed', 3) RETURNING id",
    )
    job_id = rows[0][0]
    _psql(
        get_settings().database_url.rsplit("/", 1)[1],
        f"INSERT INTO procrastinate_events (job_id, type, at) VALUES ({job_id}, 'failed', now())",
    )
    body = (await admin.get("/api/admin/system")).json()
    assert [j["id"] for j in body["jobs"]["failed_24h"]] == [job_id]
    assert (await admin.post("/api/admin/system/jobs/999999/retry")).status_code == 404
    from app.tasks import app as tasks_mod

    called = {}

    async def fake_retry(jid, retry_at):
        called["id"] = jid

    tasks_mod.app.job_manager.retry_job_by_id_async = fake_retry  # type: ignore[method-assign]
    try:
        r = await admin.post(f"/api/admin/system/jobs/{job_id}/retry")
    finally:
        del tasks_mod.app.job_manager.retry_job_by_id_async
    assert r.json() == {"retried": job_id} and called["id"] == job_id
    _psql(get_settings().database_url.rsplit("/", 1)[1], f"DELETE FROM procrastinate_jobs WHERE id = {job_id}")


async def test_record_drill_cli() -> None:
    code, out = await _cli("backup", "record-drill", "--ok", "--note", "monthly drill")
    assert code == 0, out
    state = (await health.get_states(backup.DRILL_KEY))[backup.DRILL_KEY]
    assert state["ok"] is True and state["note"] == "monthly drill"
    async with get_sessionmaker()() as db:
        assert await db.get(SystemState, backup.DRILL_KEY) is not None

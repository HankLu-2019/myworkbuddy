"""Backups and restores with restic (design 06 §5, §6; design 01 §H14). Runs in the `backup` service.

A snapshot holds three files from the staging directory:
- `mwb.dump` and `litellm.dump` (`pg_dump -Fc`; the gateway's keys, budgets and spend are in `litellm`);
- `data.tar`: `/data` without `repos/` (rebuilt by re-ingest) and `tmp/`.

Secrets reach pg_dump and restic only through the environment (PGPASSWORD, RESTIC_PASSWORD and the target's
credentials), never argv (rule R7). Results go to `system_state['backup']` for the System page.
"""

import asyncio
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.config import get_settings
from app.db import get_engine, get_sessionmaker
from app.logs import log
from app.models import User
from app.ops.health import get_states, put_state

STATE_KEY = "backup"
DRILL_KEY = "backup_drill"
DUMPS = ("mwb", "litellm")
STALE_AFTER = timedelta(hours=26)  # the System page alerts after this long without a successful backup


class BackupError(RuntimeError):
    pass


@dataclass
class Pg:
    host: str
    port: int
    user: str
    password: str
    database: str

    @classmethod
    def from_settings(cls) -> "Pg":
        u = urlparse(get_settings().database_url.replace("+asyncpg", "").replace("+psycopg", ""))
        return cls(
            host=u.hostname or "localhost",
            port=u.port or 5432,
            user=unquote(u.username or "postgres"),
            password=unquote(u.password or ""),
            database=(u.path or "/mwb").lstrip("/"),
        )

    def env(self) -> dict[str, str]:
        return {
            **os.environ,
            "PGHOST": self.host,
            "PGPORT": str(self.port),
            "PGUSER": self.user,
            "PGPASSWORD": self.password,
        }


def _restic_env() -> dict[str, str]:
    repo = get_settings().backup_repo
    if not repo:
        raise BackupError("MWB_BACKUP_REPO is not set")
    if not os.environ.get("RESTIC_PASSWORD") and not os.environ.get("RESTIC_PASSWORD_FILE"):
        raise BackupError("RESTIC_PASSWORD is not set")
    return {**os.environ, "RESTIC_REPOSITORY": repo}


async def _run(*argv: str, env: dict[str, str], cwd: Path | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        *argv, env=env, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        secret_values = [v for k, v in env.items() if k in ("PGPASSWORD", "RESTIC_PASSWORD") and v]
        msg = err.decode(errors="replace").strip()
        for v in secret_values:
            msg = msg.replace(v, "***")
        raise BackupError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''} failed: {msg[-500:]}")
    return out.decode(errors="replace")


async def _database_exists(pg: Pg, name: str) -> bool:
    out = await _run(
        "psql", "-d", pg.database, "-Atc", f"SELECT 1 FROM pg_database WHERE datname = '{name}'", env=pg.env()
    )
    return out.strip() == "1"


async def _role_exists(pg: Pg, name: str) -> bool:
    out = await _run(
        "psql", "-d", pg.database, "-Atc", f"SELECT 1 FROM pg_roles WHERE rolname = '{name}'", env=pg.env()
    )
    return out.strip() == "1"


async def _ensure_repo(env: dict[str, str]) -> None:
    try:
        await _run("restic", "cat", "config", env=env)
    except BackupError:
        await _run("restic", "init", env=env)
        log.info("backup.repo_initialised")


@dataclass
class Result:
    snapshot_id: str = ""
    sizes: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


async def run_backup() -> Result:
    """Dump, archive, snapshot, prune; records the outcome in system_state either way."""
    s = get_settings()
    started = time.monotonic()
    previous = (await get_states(STATE_KEY)).get(STATE_KEY, {})
    try:
        env = _restic_env()
        pg = Pg.from_settings()
        staging = s.backup_staging_dir
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        result = Result()
        for name in DUMPS:
            if name != pg.database and not await _database_exists(pg, name):
                result.skipped.append(name)
                continue
            target = staging / f"{name}.dump"
            await _run("pg_dump", "-Fc", "-d", pg.database if name == "mwb" else name, "-f", str(target), env=pg.env())
            result.sizes[target.name] = target.stat().st_size
        tar = staging / "data.tar"
        await _run(
            "tar",
            "-C",
            str(s.data_dir),
            "--exclude=./repos",
            "--exclude=./tmp",
            "-cf",
            str(tar),
            ".",
            env=dict(os.environ),
        )
        result.sizes[tar.name] = tar.stat().st_size
        await _ensure_repo(env)
        out = await _run("restic", "backup", "--json", "--tag", "mwb", ".", env=env, cwd=staging)
        result.snapshot_id = _snapshot_id(out)
        await _run("restic", "forget", "--tag", "mwb", *s.backup_keep.split(), "--prune", env=env)
        state = {
            "status": "ok",
            "at": datetime.now(UTC).isoformat(),
            "last_success_at": datetime.now(UTC).isoformat(),
            "snapshot_id": result.snapshot_id,
            "sizes": result.sizes,
            "skipped": result.skipped,
            "duration_s": round(time.monotonic() - started, 1),
        }
        await put_state(STATE_KEY, state)
        log.info("backup.done", snapshot=result.snapshot_id, sizes=result.sizes, skipped=result.skipped)
        return result
    except Exception as exc:
        await put_state(
            STATE_KEY,
            {
                "status": "not_configured" if "is not set" in str(exc) else "failed",
                "at": datetime.now(UTC).isoformat(),
                "last_success_at": previous.get("last_success_at"),
                "snapshot_id": previous.get("snapshot_id"),
                "error": str(exc)[:1000],
                "duration_s": round(time.monotonic() - started, 1),
            },
        )
        log.error("backup.failed", error=str(exc)[:500])
        raise


def _snapshot_id(restic_json: str) -> str:
    import json

    for line in restic_json.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("message_type") == "summary":
            return msg.get("snapshot_id", "")
    raise BackupError("restic backup printed no summary")


async def list_snapshot(snapshot: str = "latest") -> list[str]:
    """Paths in a snapshot (for checks and drills)."""
    out = await _run("restic", "ls", "--json", snapshot, env=_restic_env())
    import json

    paths = []
    for line in out.splitlines():
        try:
            node = json.loads(line)
        except ValueError:
            continue
        if node.get("struct_type") == "node":
            paths.append(node["path"])
    return paths


def next_run(now: datetime | None = None) -> datetime:
    """The next MWB_BACKUP_TIME in MWB_BACKUP_TIMEZONE (default MWB_DEFAULT_TIMEZONE), as a UTC instant."""
    s = get_settings()
    tz = ZoneInfo(s.backup_timezone or s.default_timezone)
    local = (now or datetime.now(UTC)).astimezone(tz)
    hour, minute = map(int, s.backup_time.split(":"))
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate = (local + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate.astimezone(UTC)


async def daemon() -> None:
    """The backup service's main loop: sleep until the next backup time (checked every minute, so suspends and
    clock changes don't skip a night), run, repeat. A failed run is retried the next night; the System page
    shows the failure."""
    if not get_settings().backup_repo:
        await put_state(STATE_KEY, {"status": "not_configured", "at": datetime.now(UTC).isoformat()})
        log.warning("backup.not_configured")
    while True:
        due = next_run()
        log.info("backup.next", at=due.isoformat())
        while datetime.now(UTC) < due:
            await asyncio.sleep(min(60.0, max(1.0, (due - datetime.now(UTC)).total_seconds())))
        try:
            await run_backup()
        except Exception:
            pass  # recorded in system_state


# ---------- restore ----------


async def restore(snapshot: str = "latest", *, force: bool = False, workdir: Path | None = None) -> dict[str, Any]:
    """Restore databases and /data from a snapshot into this stack (design 06 §6).

    Refuses when the app database already has users, unless `force`. The caller (deploy/restore.sh) runs the
    migrations, starts the stack, reconciles the wiki and queues re-ingest afterwards.
    """
    s = get_settings()
    pg = Pg.from_settings()
    if not force:
        async with get_sessionmaker()() as db:
            try:
                users = await db.scalar(select(func.count()).select_from(User))
            except Exception:
                users = 0  # no schema yet: a fresh stack
        await get_engine().dispose()  # dropdb --force would cut our pooled connections anyway
        if users:
            raise BackupError(f"The database already has {users} users; pass --force to overwrite it")
    env = _restic_env()
    work = workdir or s.backup_staging_dir.parent / "restore"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    await _run("restic", "restore", snapshot, "--target", str(work), env=env)
    files = {p.name: p for p in work.rglob("*") if p.is_file()}
    missing = [f for f in ("mwb.dump", "data.tar") if f not in files]
    if missing:
        raise BackupError(f"snapshot {snapshot} has no {', '.join(missing)}")
    restored = []
    for name in DUMPS:
        dump = files.get(f"{name}.dump")
        if dump is None:
            continue
        database = pg.database if name == "mwb" else name
        owner = pg.user if name == "mwb" else ("litellm" if await _role_exists(pg, "litellm") else pg.user)
        # A fresh database from template0 holds exactly what the dump has: no clashes with extensions that the
        # server image preinstalls in template1, and no leftovers from the stack being replaced.
        admin = {**pg.env(), "PGDATABASE": "postgres"}
        await _run("dropdb", "--if-exists", "--force", database, env=admin)
        await _run("createdb", "-T", "template0", "-O", owner, database, env=admin)
        await _run("pg_restore", "--exit-on-error", "-d", database, str(dump), env=pg.env())
        restored.append(name)
    for child in ("wiki", "artifacts"):
        shutil.rmtree(s.data_dir / child, ignore_errors=True)
    await _run("tar", "-C", str(s.data_dir), "-xf", str(files["data.tar"]), env=dict(os.environ))
    shutil.rmtree(work, ignore_errors=True)
    log.info("backup.restored", snapshot=snapshot, databases=restored)
    return {"snapshot": snapshot, "databases": restored}


async def record_drill(ok: bool, note: str = "") -> None:
    await put_state(DRILL_KEY, {"ok": ok, "at": datetime.now(UTC).isoformat(), "note": note[:500]})


def status_problem(state: dict[str, Any] | None, now: datetime | None = None) -> str | None:
    """What the System page should alert about, or None."""
    if not state or state.get("status") == "not_configured":
        return "Backups are not configured (MWB_BACKUP_REPO)"
    last = state.get("last_success_at")
    if not last:
        return f"No successful backup yet: {state.get('error', 'not run')}"
    age = (now or datetime.now(UTC)) - datetime.fromisoformat(last)
    if age > STALE_AFTER:
        return f"Last successful backup was {int(age.total_seconds() // 3600)} h ago"
    if state.get("status") == "failed":
        return f"The latest backup failed: {state.get('error', '')[:200]}"
    return None

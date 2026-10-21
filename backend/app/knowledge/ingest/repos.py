import asyncio
import base64
import fnmatch
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from fastapi import HTTPException, status
from pydantic_ai import Agent
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_sessionmaker
from app.errors import AppError
from app.knowledge import prompts
from app.knowledge.acl import visible_filter
from app.knowledge.chunking import ChunkIn, chunk_markdown
from app.knowledge.indexer import commit_before_network, set_project_visibility, upsert_document
from app.knowledge.ingest.code import extract_symbols, supported
from app.knowledge.wiki import LIBRARIAN, find_page, save_page
from app.llm.models import get_chat_model
from app.logs import log
from app.models import Document, Project, SystemState, User, WikiPage
from app.secrets.service import get_secret

KEY_FILES = [
    "README*",
    "readme*",
    "pyproject.toml",
    "requirements*.txt",
    "go.mod",
    "package.json",
    "Makefile",
    "Dockerfile*",
    "*/Dockerfile*",
    "docker-compose*.y*ml",
    "compose*.y*ml",
    "Jenkinsfile*",
    ".gitlab-ci.yml",
    "*.env.example",
    "Chart.yaml",
    "*/Chart.yaml",
    "*/values*.yaml",
    "k8s/*",
    "*/k8s/*",
    "deploy/*",
    "*/spinnaker/*.json",
    "spinnaker/*",
    ".github/workflows/*",
]
SKIP_DIRS = ("vendor/", "node_modules/", "third_party/", ".venv/", "dist/", "build/")
CARD_BUDGET = 60_000
CLONE_DEPTH = 300


def validate_repo_url(url: str) -> str:
    parsed = urlparse(url.strip())
    allowed = {"https", "http"} if get_settings().allow_http_repos else {"https"}
    if parsed.scheme not in allowed or not parsed.hostname:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Repository URL must use {'/'.join(allowed)}")
    if parsed.username or parsed.password:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Don't put credentials in the URL; save your token in Settings instead",
        )
    return url.strip()


def normalize_repo_url(url: str, host: str) -> tuple[str, str | None]:
    """Validate, and for Gerrit make sure the URL uses the authenticated `/a/` prefix.

    Gerrit serves anonymous git at `/<project>` and authenticated git at `/a/<project>`; HTTP credentials sent to
    the anonymous path are ignored, so private projects fail with a confusing "not found". Returns (url, notice).
    """
    url = validate_repo_url(url)
    if host != "gerrit":
        return url, None
    parsed = urlparse(url)
    if parsed.path.startswith("/a/"):
        return url, None
    fixed = urlunparse(parsed._replace(path="/a/" + parsed.path.lstrip("/")))
    return fixed, f"Changed the URL to {fixed}: Gerrit needs the /a/ prefix for authenticated access."


def project_name(url: str) -> str:
    return Path(urlparse(url).path.rstrip("/")).name.removesuffix(".git") or "repo"


async def _auth_header(db: AsyncSession, project: Project) -> str | None:
    if project.host == "gitlab":
        token = await get_secret(db, project.owner_id, "gitlab_token")
        creds = f"oauth2:{token}" if token else None
    else:
        user = await get_secret(db, project.owner_id, "gerrit_username")
        password = await get_secret(db, project.owner_id, "gerrit_password")
        creds = f"{user}:{password}" if user and password else None
    return f"Authorization: Basic {base64.b64encode(creds.encode()).decode()}" if creds else None


def git_env(auth_header: str | None) -> dict[str, str]:
    """Environment for git subprocesses.

    The auth header goes in GIT_CONFIG_* (rule R7): never in argv (visible in `ps`) and never in .git/config.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("GIT_CONFIG_") and k not in ("GIT_ASKPASS", "SSH_ASKPASS")
    }
    env |= {"GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "https:http", "GIT_CONFIG_NOSYSTEM": "1"}
    if auth_header:
        env |= {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader", "GIT_CONFIG_VALUE_0": auth_header}
    return env


def _redact(text: str, auth_header: str | None) -> str:
    if auth_header:
        text = text.replace(auth_header, "***").replace(auth_header.rsplit(" ", 1)[-1], "***")
    return text


async def run_git(*args: str, cwd: Path, auth_header: str | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        env=git_env(auth_header),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        msg = _redact(err.decode(errors="replace").strip(), auth_header)
        raise RuntimeError(f"git {args[0]} failed: {msg[-400:]}")
    return out.decode(errors="replace")


# ---------- repo cache: disk guard and cleanup ----------

MIN_FREE_RATIO = 0.10


def _dir_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


async def check_disk_space(db: AsyncSession) -> None:
    """Refuse to clone or fetch when /data is nearly full or the repo cache is over its limit (design 01 §H9).

    Records `system_state['disk_guard']` so the System page can alert admins.
    """
    s = get_settings()
    root = s.repo_cache_dir
    root.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(s.data_dir)
    free_ratio = usage.free / usage.total if usage.total else 1.0
    cache_bytes = await asyncio.to_thread(_dir_bytes, root)
    limit = int(s.repo_cache_max_gb * 1024**3)
    problem = None
    if free_ratio < MIN_FREE_RATIO:
        problem = f"Only {free_ratio:.0%} of the data disk is free (minimum {MIN_FREE_RATIO:.0%})"
    elif cache_bytes > limit:
        problem = f"The repository cache uses {cache_bytes / 1024**3:.1f} GB (limit {s.repo_cache_max_gb:g} GB)"
    state = {
        "ok": problem is None,
        "problem": problem,
        "free_ratio": round(free_ratio, 3),
        "cache_bytes": cache_bytes,
        "at": datetime.now(UTC).isoformat(),
    }
    await db.execute(
        insert(SystemState)
        .values(key="disk_guard", value=state)
        .on_conflict_do_update(index_elements=["key"], set_={"value": state, "updated_at": datetime.now(UTC)})
    )
    if problem:
        await db.commit()
        log.error("ops.disk_guard", **state)
        raise AppError(507, "disk_full", f"{problem}. An admin needs to free space before projects can be indexed.")


def cache_dir(project_id: int) -> Path:
    return get_settings().repo_cache_dir / str(project_id)


def remove_cache(project_id: int) -> bool:
    path = cache_dir(project_id)
    if not path.exists():
        return False
    shutil.rmtree(path, ignore_errors=True)
    return True


async def sweep_orphan_caches(db: AsyncSession) -> list[int]:
    """Remove cache dirs whose project row no longer exists (e.g. the cleanup task never ran)."""
    root = get_settings().repo_cache_dir
    if not root.exists():
        return []
    ids = {int(p.name) for p in root.iterdir() if p.is_dir() and p.name.isdigit()}
    if not ids:
        return []
    existing = set(await db.scalars(select(Project.id).where(Project.id.in_(ids))))
    removed = sorted(ids - existing)
    for pid in removed:
        remove_cache(pid)
    return removed


# ---------- progress ----------


class Progress:
    """Writes `projects.progress` in its own short transaction, at most every 2 s (plus on every step change)."""

    INTERVAL = 2.0

    def __init__(self, project_id: int) -> None:
        self.project_id = project_id
        self._step: str | None = None
        self._last = 0.0

    async def __call__(self, step: str, done: int | None = None, total: int | None = None) -> None:
        now = time.monotonic()
        if step == self._step and now - self._last < self.INTERVAL and done != total:
            return
        self._step, self._last = step, now
        await self._write(f"{step} {done} of {total}" if total else step)

    async def clear(self) -> None:
        await self._write(None)

    async def _write(self, text: str | None) -> None:
        async with get_sessionmaker()() as db:
            await db.execute(update(Project).where(Project.id == self.project_id).values(progress=text))
            await db.commit()


async def sync_checkout(db: AsyncSession, project: Project) -> Path:
    root = get_settings().repo_cache_dir
    root.mkdir(parents=True, exist_ok=True)
    dest = cache_dir(project.id)
    header = await _auth_header(db, project)
    await check_disk_space(db)
    await commit_before_network(db)  # rule R1: git fetch/clone below talks to the remote
    if (dest / ".git").exists():
        ref = project.branch or "HEAD"
        await run_git("config", "core.symlinks", "false", cwd=dest)  # caches cloned before H9
        await run_git("fetch", "-q", "--no-tags", f"--depth={CLONE_DEPTH}", "origin", ref, cwd=dest, auth_header=header)
        await run_git("reset", "-q", "--hard", "FETCH_HEAD", cwd=dest)
    else:
        # Symlinks are checked out as plain files holding the target path; _read() also refuses them.
        args = ["clone", "-q", "-c", "core.symlinks=false", "--no-tags", f"--depth={CLONE_DEPTH}", "--single-branch"]
        if project.branch:
            args += ["--branch", project.branch]
        await run_git(*args, "--", project.repo_url, str(dest), cwd=root, auth_header=header)
    return dest


def _tracked_files(listing: str) -> list[str]:
    return [p for p in listing.splitlines() if p and not any(p.startswith(d) or f"/{d}" in p for d in SKIP_DIRS)]


def _read(checkout: Path, path: str) -> bytes | None:
    """Read a tracked file, refusing symlinks and anything that resolves outside the checkout.

    A repo can commit `README.md -> /proc/self/environ` or a symlinked directory; following it would index the
    worker's secrets or other users' data into this user's knowledge.
    """
    f = checkout / path
    try:
        if f.is_symlink() or not f.resolve().is_relative_to(checkout.resolve()) or not f.is_file():
            return None
        if f.stat().st_size > get_settings().repo_max_file_bytes:
            return None
        return f.read_bytes()
    except OSError:
        return None


def _key_file_bundle(checkout: Path, files: list[str]) -> str:
    parts, used = [], 0
    for path in files:
        if not any(fnmatch.fnmatch(path, pat) for pat in KEY_FILES):
            continue
        data = _read(checkout, path)
        if data is None:
            continue
        body = data.decode(errors="replace")[:8000]
        if used + len(body) > CARD_BUDGET:
            break
        parts.append(f"=== {path} ===\n{body}")
        used += len(body)
    return "\n\n".join(parts)


async def _write_project_card(
    db: AsyncSession, owner: User, project: Project, checkout: Path, files: list[str]
) -> None:
    commits = ""
    if owner.email:
        commits = await run_git(
            "log", f"--author={owner.email}", "-n200", "--date=short", "--format=%ad %h %s", cwd=checkout
        )
    tree = "\n".join(files[:400])
    prompt = (
        f"Repository: {project.repo_url}\n\nFile list (truncated):\n{tree}\n\n"
        f"Key files:\n{_key_file_bundle(checkout, files)}\n\n"
        f"Commits by this person ({owner.email or 'unknown email'}):\n{commits or '(none found)'}"
    )
    agent = Agent(get_chat_model(), instructions=prompts.PROJECT_CARD.format(name=project.name))
    await commit_before_network(db)
    card = (await agent.run(prompt)).output
    page = await db.get(WikiPage, project.card_page_id) if project.card_page_id else None
    path = page.path if page else f"projects/{project.name}.md"
    if page is None and await find_page(db, owner.id, "private", path):
        path = f"projects/{project.name}-{project.id}.md"  # another repo with the same name already has a card
    page = await save_page(
        db,
        owner=owner,
        path=path,
        content=card,
        author=LIBRARIAN,
        message=f"Project card for {project.name} @ {project.last_ingested_sha or 'new'}",
        page=page,
    )
    project.card_page_id = page.id


async def _summaries(path: str, symbols_text: str) -> dict[str, str]:
    agent = Agent(
        get_chat_model(get_settings().fast_model), output_type=dict[str, str], instructions=prompts.CODE_SUMMARY
    )
    try:
        return (await agent.run(f"File: {path}\n\n{symbols_text[:20000]}")).output
    except Exception as exc:  # summaries are optional; indexing proceeds without them
        log.warning("ingest.summary_failed", path=path, error=str(exc))
        return {}


@dataclass
class FileResults:
    seen: set[str] = field(default_factory=set)
    failed: list[str] = field(default_factory=list)

    def merge(self, other: "FileResults") -> "FileResults":
        return FileResults(self.seen | other.seen, self.failed + other.failed)


async def _noop_progress(step: str, done: int | None = None, total: int | None = None) -> None:
    return None


async def _index_code(
    db: AsyncSession, project: Project, checkout: Path, files: list[str], progress=_noop_progress
) -> FileResults:
    """Parse and summarise every file with no transaction open, then write each file in its own commit."""
    s = get_settings()
    code_files = [p for p in files if supported(p)][: s.repo_max_files]
    summary_budget = s.ingest_max_summary_files
    sem = asyncio.Semaphore(4)
    to_summarise = min(len(code_files), summary_budget)
    summarised = 0

    async def prepare(index: int, path: str) -> tuple[str, list, dict[str, str]]:
        data = _read(checkout, path)
        symbols = extract_symbols(path, data) if data else []
        if not symbols or index >= summary_budget:
            return path, symbols, {}
        nonlocal summarised
        async with sem:
            summaries = await _summaries(path, "\n\n".join(f"### {sym.name}\n{sym.code}" for sym in symbols))
        summarised += 1
        await progress("Summarising files", summarised, to_summarise)
        return path, symbols, summaries

    await commit_before_network(db)
    prepared = await asyncio.gather(*(prepare(i, p) for i, p in enumerate(code_files)))
    results = FileResults()
    for n, (path, symbols, summaries) in enumerate(prepared, start=1):
        await progress("Indexing code", n, len(prepared))
        if not symbols:
            continue
        chunks = [
            ChunkIn(
                f"{path}:{sym.start_line}-{sym.end_line} {sym.kind} {sym.name}\n"
                f"{summaries.get(sym.name, '')}\n\n{sym.code[:3000]}",
                {"path": path, "symbol": sym.name, "lines": [sym.start_line, sym.end_line]},
            )
            for sym in symbols
        ]
        await _write_file(
            db, results, project, "code", f"{project.id}:{path}", f"{project.name}/{path}", chunks, {"path": path}
        )
    return results


async def _write_file(
    db: AsyncSession,
    results: FileResults,
    project: Project,
    source_type: str,
    ref: str,
    title: str,
    chunks: list[ChunkIn],
    meta: dict,
) -> None:
    """Index one file and commit it, so a later failure doesn't undo (or re-bill) the files before it."""
    results.seen.add(ref)  # a failed file keeps its previous index rather than being deleted as unseen
    try:
        await upsert_document(
            db,
            owner_id=project.owner_id,
            source_type=source_type,
            source_ref=ref,
            title=title,
            chunks=chunks,
            project_id=project.id,
            meta=meta,
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        results.failed.append(ref)
        log.warning("ingest.file_failed", project_id=project.id, ref=ref, error=str(exc)[:300])


async def _index_docs(db: AsyncSession, project: Project, checkout: Path, files: list[str]) -> FileResults:
    results = FileResults()
    for path in [p for p in files if p.lower().endswith(".md")][:100]:
        data = _read(checkout, path)
        if data is None:
            continue
        await _write_file(
            db,
            results,
            project,
            "repo_doc",
            f"{project.id}:{path}",
            f"{project.name}/{path}",
            chunk_markdown(data.decode(errors="replace")),
            {"path": path},
        )
    return results


FAILED_FILES_LIMIT = 0.2  # more than this share of files failing marks the ingest failed


async def ingest_project(db: AsyncSession, project_id: int, force: bool = False) -> None:
    """Clone or fetch, index code and docs (one commit per file), then write the project card.

    No transaction is open during git network calls, LLM summaries or embeddings (rule R1).
    """
    project = await db.get(Project, project_id)
    if project is None:
        return
    owner = await db.get(User, project.owner_id)
    project.status, project.error, project.progress = "running", None, "Fetching the repository"
    await db.commit()
    progress = Progress(project_id)
    try:
        checkout = await sync_checkout(db, project)
        head = (await run_git("rev-parse", "HEAD", cwd=checkout)).strip()
        if head == project.last_ingested_sha and not force:
            log.info("ingest.unchanged", project_id=project.id, sha=head)
            project.status, project.progress = "ready", None
            await db.commit()
            return
        files = _tracked_files(await run_git("ls-files", cwd=checkout))
        await progress("Reading files")
        results = (await _index_code(db, project, checkout, files, progress)).merge(
            await _index_docs(db, project, checkout, files)
        )

        project = await db.get(Project, project_id)
        if project is None:  # deleted while we worked; its documents went with it
            log.info("ingest.project_deleted", project_id=project_id)
            return
        await db.execute(
            delete(Document).where(
                Document.project_id == project.id,
                Document.source_type.in_(["code", "repo_doc"]),
                Document.source_ref.not_in(results.seen) if results.seen else Document.id.is_not(None),
            )
        )
        # The owner may have changed the project's visibility while this ran; apply the current value.
        await db.refresh(project)
        await set_project_visibility(db, project, project.visibility, actor_id=project.owner_id)
        total = len(results.seen)
        project.progress = None
        if results.failed and len(results.failed) > FAILED_FILES_LIMIT * max(total, 1):
            project.status = "failed"
            project.error = f"{len(results.failed)} of {total} files could not be indexed; first: {results.failed[0]}"
        else:
            project.status = "ready"
            project.last_ingested_sha = head
            if results.failed:
                project.error = f"Indexed with {len(results.failed)} file(s) skipped: {', '.join(results.failed[:5])}"
        await db.commit()
        if project.status == "failed":
            return
        try:
            await progress("Writing the project card")
            await _write_project_card(db, owner, project, checkout, files)
            await db.commit()
        except Exception as exc:  # the code index is still useful without the card
            await db.rollback()
            project = await db.get(Project, project_id)
            if project is not None:
                project.error = f"Indexed, but the project card could not be written: {exc}"[:2000]
                await db.commit()
            log.warning("ingest.card_failed", project_id=project_id, error=str(exc))
        log.info("ingest.done", project_id=project_id, sha=head, documents=total, failed=len(results.failed))
    except Exception as exc:
        await db.rollback()
        project = await db.get(Project, project_id)
        if project is not None:
            message = exc.detail if isinstance(exc, HTTPException) else str(exc)
            project.status, project.error, project.progress = "failed", str(message)[:2000], None
            await db.commit()
        log.exception("ingest.failed", project_id=project_id)
        raise
    finally:
        await progress.clear()


async def list_projects(db: AsyncSession, user: User) -> list[Project]:
    return list(await db.scalars(select(Project).where(visible_filter(Project, user)).order_by(Project.name)))

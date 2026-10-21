import asyncio
import base64
import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from fastapi import HTTPException, status
from pydantic_ai import Agent
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.knowledge import prompts
from app.knowledge.acl import visible_filter
from app.knowledge.chunking import ChunkIn, chunk_markdown
from app.knowledge.indexer import commit_before_network, set_project_visibility, upsert_document
from app.knowledge.ingest.code import extract_symbols, supported
from app.knowledge.wiki import LIBRARIAN, find_page, save_page
from app.llm.models import get_chat_model
from app.logs import log
from app.models import Document, Project, User, WikiPage
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


async def run_git(*args: str, cwd: Path, auth_header: str | None = None) -> str:
    # The token goes in a per-command header so it is never written to .git/config.
    pre = ["-c", f"http.extraHeader={auth_header}"] if auth_header else []
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "https:http"}
    proc = await asyncio.create_subprocess_exec(
        "git", *pre, *args, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        msg = err.decode().strip().replace(auth_header or "\0", "***")
        raise RuntimeError(f"git {args[0]} failed: {msg[-400:]}")
    return out.decode(errors="replace")


async def sync_checkout(db: AsyncSession, project: Project) -> Path:
    root = get_settings().repo_cache_dir
    root.mkdir(parents=True, exist_ok=True)
    dest = root / str(project.id)
    header = await _auth_header(db, project)
    await commit_before_network(db)  # rule R1: git fetch/clone below talks to the remote
    if (dest / ".git").exists():
        ref = project.branch or "HEAD"
        await run_git("fetch", "-q", f"--depth={CLONE_DEPTH}", "origin", ref, cwd=dest, auth_header=header)
        await run_git("reset", "-q", "--hard", "FETCH_HEAD", cwd=dest)
    else:
        args = ["clone", "-q", f"--depth={CLONE_DEPTH}", "--single-branch"]
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


async def _index_code(db: AsyncSession, project: Project, checkout: Path, files: list[str]) -> FileResults:
    """Parse and summarise every file with no transaction open, then write each file in its own commit."""
    s = get_settings()
    code_files = [p for p in files if supported(p)][: s.repo_max_files]
    summary_budget = s.ingest_max_summary_files
    sem = asyncio.Semaphore(4)

    async def prepare(index: int, path: str) -> tuple[str, list, dict[str, str]]:
        data = _read(checkout, path)
        symbols = extract_symbols(path, data) if data else []
        if not symbols or index >= summary_budget:
            return path, symbols, {}
        async with sem:
            summaries = await _summaries(path, "\n\n".join(f"### {sym.name}\n{sym.code}" for sym in symbols))
        return path, symbols, summaries

    await commit_before_network(db)
    prepared = await asyncio.gather(*(prepare(i, p) for i, p in enumerate(code_files)))
    results = FileResults()
    for path, symbols, summaries in prepared:
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
    project.status, project.error = "running", None
    await db.commit()
    try:
        checkout = await sync_checkout(db, project)
        head = (await run_git("rev-parse", "HEAD", cwd=checkout)).strip()
        if head == project.last_ingested_sha and not force:
            log.info("ingest.unchanged", project_id=project.id, sha=head)
            project.status = "ready"
            await db.commit()
            return
        files = _tracked_files(await run_git("ls-files", cwd=checkout))
        results = (await _index_code(db, project, checkout, files)).merge(
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
            project.status, project.error = "failed", str(exc)[:2000]
            await db.commit()
        log.exception("ingest.failed", project_id=project_id)
        raise


async def list_projects(db: AsyncSession, user: User) -> list[Project]:
    return list(await db.scalars(select(Project).where(visible_filter(Project, user)).order_by(Project.name)))

"""Nightly `projects_refresh` (design 02 §9): re-ingest ready repositories whose branch moved.

For each ready repository: read the owner's credentials in a short transaction, close it, ask the remote for the
branch's head with `git ls-remote` (rule R1), and queue an ingest when it differs from `last_ingested_sha`.
A remote that fails to answer is logged and skipped; the repository's status doesn't change, because last
night's index is still good.
"""

from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy import select

from app.config import get_settings
from app.db import get_sessionmaker
from app.knowledge.ingest.repos import auth_header, ca_file_for, project_integration, run_git
from app.logs import log
from app.models import Project

LS_REMOTE_TIMEOUT = 60
MAX_PROJECTS = 500


@dataclass
class RefreshResult:
    checked: int = 0
    moved: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)


def head_from_ls_remote(output: str, branch: str | None) -> str | None:
    """The sha for the branch (or HEAD) in `git ls-remote` output."""
    want = f"refs/heads/{branch}" if branch else "HEAD"
    for line in output.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == want and sha:
            return sha.strip()
    return None


async def remote_head(project_id: int) -> tuple[Project, str | None]:
    async with get_sessionmaker()() as db:
        project = await db.get(Project, project_id)
        assert project is not None
        header = await auth_header(db, project)
        ca = ca_file_for(await project_integration(db, project))
        db.expunge(project)
        await db.commit()
    ref = f"refs/heads/{project.branch}" if project.branch else "HEAD"
    root = get_settings().repo_cache_dir
    root.mkdir(parents=True, exist_ok=True)
    out = await run_git(
        "ls-remote", "--", project.repo_url, ref, cwd=root, auth_header=header, ca_file=ca, timeout=LS_REMOTE_TIMEOUT
    )
    return project, head_from_ls_remote(out, project.branch)


async def refresh_projects() -> RefreshResult:
    """Returns which repositories moved; the caller queues their ingests."""
    async with get_sessionmaker()() as db:
        ids = list(
            await db.scalars(
                select(Project.id).where(Project.status == "ready").order_by(Project.id).limit(MAX_PROJECTS)
            )
        )
    result = RefreshResult()
    for project_id in ids:
        result.checked += 1
        try:
            project, head = await remote_head(project_id)
        except (RuntimeError, HTTPException, OSError) as exc:
            result.failed.append(project_id)
            log.warning("projects_refresh.failed", project_id=project_id, error=str(getattr(exc, "detail", exc))[:300])
            continue
        if head and head != project.last_ingested_sha:
            result.moved.append(project_id)
    return result

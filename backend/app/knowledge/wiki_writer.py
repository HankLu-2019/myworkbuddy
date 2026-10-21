"""Keeps the wiki's git repos and the database consistent (design 01 §H7).

    async with WikiWriter() as w:
        await w.lock(repo_a, repo_b)          # session-level advisory locks, sorted, on a dedicated connection
        await w.expect(repo, path, base_sha)  # optional optimistic check -> 409 wiki_conflict
        await w.write(repo, path, ...)        # git commits; the writer remembers each repo's previous HEAD
        ... DB changes in the request session ...
        await db.commit()                     # inside the block

If anything in the block raises (including the DB commit), every touched repo is reset to its previous HEAD
before the locks are released. Locks are session-level, so they cover the compensation even though the request
transaction has ended. If the process dies between the git commit and the DB commit, wiki_reconcile repairs it.
"""

from types import TracebackType

from fastapi import status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db import get_engine
from app.errors import AppError
from app.knowledge.gitrepo import Author, GitRepo
from app.logs import log


class WikiWriter:
    def __init__(self) -> None:
        self._conn: AsyncConnection | None = None
        self._locked: set[str] = set()
        self._prev: dict[str, tuple[GitRepo, str]] = {}

    async def __aenter__(self) -> "WikiWriter":
        return self

    async def lock(self, *repos: GitRepo) -> None:
        if self._conn is None:
            conn = await get_engine().connect()
            self._conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        for key in sorted({str(r.root) for r in repos} - self._locked):  # fixed order: no deadlocks
            await self._conn.execute(text("SELECT pg_advisory_lock(hashtext(:k))"), {"k": f"wiki:{key}"})
            self._locked.add(key)

    def _require_lock(self, repo: GitRepo) -> None:
        if str(repo.root) not in self._locked:
            raise RuntimeError(f"lock {repo.root} before writing to it")

    async def _remember(self, repo: GitRepo) -> None:
        key = str(repo.root)
        if key not in self._prev:
            await repo.ensure()
            self._prev[key] = (repo, await repo.head())

    async def expect(self, repo: GitRepo, path: str, base_sha: str | None, current_content: str | None) -> None:
        """Raise 409 wiki_conflict unless `path`'s last commit is `base_sha`."""
        self._require_lock(repo)
        current = await repo.last_commit(path)
        if base_sha is not None and not (current or "").startswith(base_sha):
            raise AppError(
                status.HTTP_409_CONFLICT,
                "wiki_conflict",
                "This page changed since you opened it",
                current_sha=current,
                current_content=current_content,
            )

    async def write(self, repo: GitRepo, path: str, content: str, author: Author, message: str) -> str:
        self._require_lock(repo)
        await self._remember(repo)
        return await repo.write(path, content, author, message)

    async def remove(self, repo: GitRepo, path: str, author: Author, message: str) -> str:
        self._require_lock(repo)
        await self._remember(repo)
        return await repo.remove(path, author, message)

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        try:
            if exc_type is not None:
                for repo, sha in self._prev.values():
                    try:
                        await repo.reset_hard(sha)
                        log.warning("wiki.compensated", repo=str(repo.root), reset_to=sha, error=str(exc)[:200])
                    except Exception:  # the reconcile job is the last line of defence
                        log.exception("wiki.compensation_failed", repo=str(repo.root))
        finally:
            if self._conn is not None:
                try:
                    await self._conn.execute(text("SELECT pg_advisory_unlock_all()"))
                    await self._conn.commit()
                finally:
                    await self._conn.close()
                    self._conn = None

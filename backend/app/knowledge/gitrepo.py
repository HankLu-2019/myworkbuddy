import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, status

from app.logs import log

_PAGE_PATH = re.compile(r"^(?!/)(?!.*(^|/)\.)[A-Za-z0-9_\-./]{1,250}\.md$")


class GitError(RuntimeError):
    pass


@dataclass
class Author:
    name: str
    email: str


@dataclass
class Commit:
    sha: str
    author: str
    email: str
    date: str
    message: str
    moved_from: str | None = None  # "Moved-From: <repo dir>@<sha>" trailer on publish/unpublish commits


_SHA = re.compile(r"[0-9a-f]{7,40}")


def validate_sha(sha: str) -> str:
    if not _SHA.fullmatch(sha):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid commit id")
    return sha


def validate_page_path(path: str) -> str:
    path = path.strip()
    if not _PAGE_PATH.match(path) or ".." in path.split("/") or "//" in path:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Page path must be relative, end in .md, and use letters, digits, '-', '_', '.' and '/'",
        )
    return path


class GitRepo:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def _git(self, *args: str, author: Author | None = None, check: bool = True) -> str:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if author:
            env.update(
                GIT_AUTHOR_NAME=author.name,
                GIT_AUTHOR_EMAIL=author.email,
                GIT_COMMITTER_NAME=author.name,
                GIT_COMMITTER_EMAIL=author.email,
            )
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.root),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        out, err = await proc.communicate()
        if check and proc.returncode != 0:
            log.error("git.failed", repo=str(self.root), args=args[:2], stderr=err.decode()[-500:])
            raise GitError(err.decode().strip())
        return out.decode()

    async def ensure(self) -> None:
        if (self.root / ".git").exists():
            return
        self.root.mkdir(parents=True, exist_ok=True)
        await self._git("init", "-q", "-b", "main")
        await self._git(
            "commit", "-q", "--allow-empty", "-m", "Initialize wiki", author=Author("MyWorkBuddy", "mwb@localhost")
        )

    def read(self, path: str) -> str | None:
        f = self.root / path
        return f.read_text() if f.is_file() else None

    async def write(self, path: str, content: str, author: Author, message: str) -> str:
        """Write and commit one file. Returns the last commit touching `path` (unchanged content: no new commit)."""
        await self.ensure()
        f = self.root / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
        await self._git("add", "--", path)
        if (await self._git("status", "--porcelain", "--", path)).strip():
            await self._git("commit", "-q", "-m", message, "--", path, author=author)
        return await self.last_commit(path) or await self.head()

    async def remove(self, path: str, author: Author, message: str) -> str:
        await self._git("rm", "-q", "--", path)
        await self._git("commit", "-q", "-m", message, author=author)
        return await self.head()

    async def last_commit(self, path: str) -> str | None:
        """The newest commit that touched `path`, or None. Used for optimistic concurrency (design 01 §H7)."""
        await self.ensure()
        return (await self._git("log", "-1", "--format=%H", "--", path)).strip() or None

    async def reset_hard(self, sha: str) -> None:
        if not _SHA.fullmatch(sha):
            raise GitError(f"invalid commit id {sha!r}")
        await self._git("reset", "-q", "--hard", sha)

    async def list_markdown(self) -> list[str]:
        if not (self.root / ".git").exists():
            return []
        return [p for p in (await self._git("ls-files", "--", "*.md")).splitlines() if p]

    async def has_commit(self, sha: str) -> bool:
        if not _SHA.fullmatch(sha) or not (self.root / ".git").exists():
            return False
        return (await self._git("cat-file", "-t", sha, check=False)).strip() == "commit"

    async def file_at(self, sha: str, path: str) -> str | None:
        """Content of `path` at commit `sha`, or None if it didn't exist there."""
        if not _SHA.fullmatch(sha):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid commit id")
        await self.ensure()
        if not (await self._git("cat-file", "-t", sha, check=False)).strip():
            return None  # unknown commit
        if not (await self._git("ls-tree", "--name-only", sha, "--", path, check=False)).strip():
            return None
        return await self._git("show", f"{sha}:{path}")

    async def changed_files(self, sha: str) -> list[str]:
        """Paths the commit added, changed or removed (no rename detection, like log)."""
        if not _SHA.fullmatch(sha):
            raise GitError(f"invalid commit id {sha!r}")
        out = await self._git("diff-tree", "--no-commit-id", "--name-only", "-r", "--no-renames", "--root", sha)
        return [p for p in out.splitlines() if p]

    async def parent(self, sha: str) -> str | None:
        """The first parent of `sha`, or None for the root commit."""
        if not _SHA.fullmatch(sha):
            raise GitError(f"invalid commit id {sha!r}")
        out = await self._git("rev-parse", "--verify", "-q", f"{sha}^", check=False)
        return out.strip() or None

    async def head(self) -> str:
        return (await self._git("rev-parse", "HEAD")).strip()

    async def log(
        self, path: str | None = None, limit: int = 50, author: str | None = None, until: str | None = None
    ) -> list[Commit]:
        """Commits newest first. No --follow: rename detection would attribute other similar pages' commits
        to this one (review M16). Moves between repos are linked by the Moved-From trailer instead."""
        await self.ensure()
        args = [
            "log",
            f"-n{limit}",
            "--format=%H%x1f%an%x1f%ae%x1f%aI%x1f%s%x1f%(trailers:key=Moved-From,valueonly)%x1e",
        ]
        if author:
            args.append(f"--author={author}")
        if until:
            if not _SHA.fullmatch(until):
                raise GitError(f"invalid commit id {until!r}")
            args.append(until)
        if path:
            args += ["--", path]
        out = await self._git(*args)
        commits = []
        for rec in out.split("\x1e"):
            if rec.strip():
                sha, name, email, date, msg, moved = rec.strip("\n").split("\x1f")
                commits.append(Commit(sha, name, email, date, msg, moved.strip() or None))
        return commits

    async def show(self, sha: str, path: str | None = None) -> str:
        if not _SHA.fullmatch(sha):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid commit id")
        args = ["show", "--format=%H%n%an <%ae>%n%aI%n%s%n", sha]
        if path:
            args += ["--", path]
        return await self._git(*args)

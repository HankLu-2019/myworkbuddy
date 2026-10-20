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
        await self.ensure()
        f = self.root / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
        await self._git("add", "--", path)
        if not (await self._git("status", "--porcelain", "--", path)).strip():
            return await self.head()
        await self._git("commit", "-q", "-m", message, "--", path, author=author)
        return await self.head()

    async def remove(self, path: str, author: Author, message: str) -> str:
        await self._git("rm", "-q", "--", path)
        await self._git("commit", "-q", "-m", message, author=author)
        return await self.head()

    async def head(self) -> str:
        return (await self._git("rev-parse", "HEAD")).strip()

    async def log(self, path: str | None = None, limit: int = 50, author: str | None = None) -> list[Commit]:
        await self.ensure()
        args = ["log", f"-n{limit}", "--format=%H%x1f%an%x1f%ae%x1f%aI%x1f%s%x1e"]
        if author:
            args.append(f"--author={author}")
        if path:
            args += ["--follow", "--", path]
        out = await self._git(*args)
        commits = []
        for rec in out.split("\x1e"):
            if rec.strip():
                sha, name, email, date, msg = rec.strip().split("\x1f")
                commits.append(Commit(sha, name, email, date, msg))
        return commits

    async def show(self, sha: str, path: str | None = None) -> str:
        if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid commit id")
        args = ["show", "--format=%H%n%an <%ae>%n%aI%n%s%n", sha]
        if path:
            args += ["--", path]
        return await self._git(*args)

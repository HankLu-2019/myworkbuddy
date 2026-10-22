"""The planner's read-only repository tools (design 03 §5): `repo_tree`, `repo_read`, `repo_grep`.

They read the owner's ingest cache for the work item's repository, at the plan's `base_sha`, with `git ls-tree`,
`git cat-file`/`git show` and `git grep`: local commands only, never the network (the plan task fetched first).
Paths get the same checks as plan validation; symlinks and submodules are refused (a committed
`README.md -> /proc/self/environ` must not be read), as are binary files and files over 1 MB. File contents and
grep output are untrusted text (rule R6).
"""

import asyncio
import re
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext

from app.connectors.base import ConnectorError
from app.plans.validation import normalise_path

MAX_TREE_ENTRIES = 500
MAX_TREE_DEPTH = 3
MAX_READ_LINES = 400
MAX_FILE_BYTES = 1_000_000
MAX_GREP_MATCHES = 50
MAX_GREP_LINES = 200
MAX_PATTERN = 200
GIT_TIMEOUT = 30
SHA = re.compile(r"[0-9a-f]{7,64}")


async def git(checkout: Path, *args: str) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "git", "-c", "core.quotepath=off", *args, cwd=checkout,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(checkout), "PATH": "/usr/bin:/bin"},
    )  # fmt: skip
    try:
        out, err = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise ConnectorError("timeout", "The repository took too long to answer.") from None
    if proc.returncode not in (0, 1):  # git grep exits 1 for "no matches"
        raise ConnectorError("unavailable", f"git {args[0]} failed: {err.decode(errors='replace').strip()[-200:]}")
    return out


def _context(ctx: RunContext[Any]) -> tuple[Path, str]:
    checkout, sha = getattr(ctx.deps, "checkout", None), getattr(ctx.deps, "base_sha", None)
    if checkout is None or not sha or not SHA.fullmatch(sha):
        raise ConnectorError("not_configured", "No repository checkout for this plan.")
    return checkout, sha


def _clean(path: str, *, allow_root: bool = False) -> str:
    if allow_root and path.strip() in ("", ".", "/"):
        return ""
    norm, problem = normalise_path(path)
    if problem:
        raise ConnectorError("invalid_args", f"path {problem}")
    return norm


async def _entry(checkout: Path, sha: str, path: str) -> tuple[str, str, int]:
    """(mode, type, size) of a path at sha; refuses anything that isn't a regular file."""
    out = (await git(checkout, "ls-tree", "-l", sha, "--", path)).decode(errors="replace").strip()
    if not out:
        raise ConnectorError("not_found", f"{path} doesn't exist at {sha[:12]}")
    meta, _, _name = out.partition("\t")
    mode, kind, _obj, size = meta.split()
    if mode == "120000":
        raise ConnectorError("forbidden", f"{path} is a symbolic link; read its target instead")
    if kind != "blob":
        raise ConnectorError("invalid_args", f"{path} is a {'submodule' if kind == 'commit' else 'directory'}")
    return mode, kind, int(size) if size.isdigit() else 0


async def tracked_paths(checkout: Path, sha: str) -> set[str]:
    """Every tracked file at sha, for plan validation."""
    out = await git(checkout, "ls-tree", "-r", "--name-only", "-z", sha)
    return {p for p in out.decode(errors="replace").split("\0") if p}


async def repo_tree(ctx: RunContext[Any], dir: str = "", depth: int = 2) -> dict[str, Any]:
    """Files tracked in the repository under a directory, at the plan's base commit.

    Args:
        dir: directory relative to the repository root; empty for the root.
        depth: how many directory levels below `dir` to list, 1 to 3.
    """
    checkout, sha = _context(ctx)
    base = _clean(dir, allow_root=True)
    levels = max(1, min(depth, MAX_TREE_DEPTH))
    args = ["ls-tree", "-r", "-t", "--name-only", "-z", sha]
    if base:
        args += ["--", f"{base}/"]
    names = [p for p in (await git(checkout, *args)).decode(errors="replace").split("\0") if p]
    prefix = f"{base}/" if base else ""
    entries = [p for p in names if p.startswith(prefix) and p[len(prefix) :].count("/") < levels]
    return {
        "dir": base or ".",
        "base_sha": sha,
        "entries": entries[:MAX_TREE_ENTRIES],
        "truncated": len(entries) > MAX_TREE_ENTRIES,
    }


async def repo_read(ctx: RunContext[Any], path: str, start_line: int = 1, end_line: int = 200) -> dict[str, Any]:
    """Lines of a file at the plan's base commit (at most 400 at a time).

    Args:
        path: file path relative to the repository root.
        start_line: first line, from 1.
        end_line: last line, inclusive.
    """
    checkout, sha = _context(ctx)
    clean = _clean(path)
    _mode, _kind, size = await _entry(checkout, sha, clean)
    if size > MAX_FILE_BYTES:
        raise ConnectorError("too_large", f"{clean} is {size // 1000} kB; read a smaller file or grep it")
    data = await git(checkout, "show", f"{sha}:{clean}")
    if b"\0" in data[:8000]:
        raise ConnectorError("invalid_args", f"{clean} is a binary file")
    lines = data.decode(errors="replace").splitlines()
    start = max(1, start_line)
    end = min(len(lines), max(start, end_line), start + MAX_READ_LINES - 1)
    return {
        "path": clean,
        "start_line": start,
        "end_line": end,
        "total_lines": len(lines),
        "text": "\n".join(f"{n}: {lines[n - 1]}" for n in range(start, end + 1)),
    }


async def repo_grep(ctx: RunContext[Any], pattern: str, path_glob: str = "", regex: bool = False) -> dict[str, Any]:
    """Search tracked files at the plan's base commit (at most 50 matches per file, 200 lines in all).

    Args:
        pattern: text to find; a fixed string unless `regex` is true. At most 200 characters.
        path_glob: limit to paths matching this glob, e.g. "src/**/*.py".
        regex: treat the pattern as a basic regular expression.
    """
    checkout, sha = _context(ctx)
    if not pattern or len(pattern) > MAX_PATTERN:
        raise ConnectorError("invalid_args", f"pattern must be 1–{MAX_PATTERN} characters")
    args = ["grep", "-n", "-I", f"--max-count={MAX_GREP_MATCHES}", "-F" if not regex else "-G", "-e", pattern, sha]
    if path_glob:
        if ".." in path_glob.split("/") or path_glob.startswith("/"):
            raise ConnectorError("invalid_args", "path_glob must be relative to the repository root")
        args += ["--", f":(glob){path_glob}"]
    lines = (await git(checkout, *args)).decode(errors="replace").splitlines()
    matches = [line.removeprefix(f"{sha}:") for line in lines]
    return {"pattern": pattern, "matches": matches[:MAX_GREP_LINES], "truncated": len(matches) > MAX_GREP_LINES}

"""Repository conventions (design 08 §F4): the rules a team writes down for agents.

The first of `.mwb/rules.md`, `AGENTS.md` and `CLAUDE.md` at the plan's base commit, else the Conventions text the
repository's owner saved, at most 20 KB. The text is repository content, so the planner gets it wrapped as untrusted
(rule R6). An optional front-matter block makes part of it enforceable:

    ---
    mwb:
      protected: ["migrations/**", "vendor/**"]
      test: "pytest -q"
    ---

`protected` paths fail the job check `protected_paths` unless the plan's approval acknowledged them
(`protected_path:<path>`), and warn in plan validation; `test` is suggested when a plan has no verification.
"""

import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

FILES = (".mwb/rules.md", "AGENTS.md", "CLAUDE.md")
MAX_BYTES = 20_000
MAX_PATTERNS = 50
SETTINGS_SOURCE = "the repository's Conventions setting"
FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)


@dataclass
class Conventions:
    source: str  # a file name, or SETTINGS_SOURCE
    text: str
    protected: list[str] = field(default_factory=list)
    test: str | None = None
    truncated: bool = False


def parse(source: str, raw: str) -> Conventions:
    truncated = len(raw.encode()) > MAX_BYTES
    text = raw.encode()[:MAX_BYTES].decode(errors="ignore")
    protected: list[str] = []
    test = None
    if m := FRONT.match(text):
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        mwb = meta.get("mwb") if isinstance(meta, dict) else None
        if isinstance(mwb, dict):
            raw_protected = mwb.get("protected") or []
            if isinstance(raw_protected, list):
                protected = [str(p).strip().lstrip("/") for p in raw_protected if str(p).strip()][:MAX_PATTERNS]
                protected = [p[:200] for p in protected]
            if isinstance(mwb.get("test"), str) and mwb["test"].strip():
                test = mwb["test"].strip()[:500]
    return Conventions(source=source, text=text, protected=protected, test=test, truncated=truncated)


async def find(read: Callable[[str], Awaitable[str | None]], settings_text: str | None) -> Conventions | None:
    """The first convention file `read` returns (None when missing), else the saved setting."""
    for name in FILES:
        raw = await read(name)
        if raw is not None and raw.strip():
            return parse(name, raw)
    if settings_text and settings_text.strip():
        return parse(SETTINGS_SOURCE, settings_text)
    return None


def from_checkout(checkout: Path, sha: str, settings_text: str | None):
    """`find()` over `git show <sha>:<file>` in a local clone (the planner's checkout)."""
    from app.connectors.base import ConnectorError
    from app.plans import repo

    async def read(name: str) -> str | None:
        try:
            out = await repo.git(checkout, "show", f"{sha}:{name}")
        except ConnectorError:
            return None
        return out.decode(errors="replace") if out else None

    return find(read, settings_text)


def from_tree(root: Path, settings_text: str | None):
    """`find()` over a working tree (the job's clone at base_sha); symlinks aren't followed."""

    async def read(name: str) -> str | None:
        path = root / name
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as f:
            return f.read(MAX_BYTES + 1).decode(errors="replace")

    return find(read, settings_text)


def _regex(pattern: str) -> re.Pattern[str]:
    """Glob → regex: `**` crosses directories, `*` and `?` don't. `dir/` means everything under it."""
    if pattern.endswith("/"):
        pattern += "**"
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(f"^{out}$")


def protected_matches(paths: Iterable[str], patterns: Iterable[str]) -> list[str]:
    compiled = [_regex(p) for p in patterns]
    return [p for p in paths if any(c.match(p) for c in compiled)]

"""Deterministic checks on a job's patch (design 04 §5.4), run in the worker, never in the sandbox.

Each check gives `{"name", "status": pass|warn|fail, "detail"}`. A `fail` makes the attempt fail (the agent gets
another try with feedback, up to max_attempts); a `warn` is shown to the reviewer.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["pass", "warn", "fail"]
DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")


@dataclass
class FileStat:
    path: str
    status: str = "modified"  # added | modified | deleted | renamed
    added: int = 0
    removed: int = 0
    binary: bool = False
    old_mode: str | None = None
    new_mode: str | None = None
    symlink: bool = False
    submodule: bool = False
    added_lines: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {"path": self.path, "status": self.status, "added": self.added, "removed": self.removed,
                "binary": self.binary}  # fmt: skip


def parse_patch(patch: str) -> list[FileStat]:
    files: list[FileStat] = []
    cur: FileStat | None = None
    for line in patch.splitlines():
        if m := DIFF_HEADER.match(line):
            cur = FileStat(path=m.group(2))
            files.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("new file mode"):
            cur.status, cur.new_mode = "added", line.split()[-1]
        elif line.startswith("deleted file mode"):
            cur.status, cur.old_mode = "deleted", line.split()[-1]
        elif line.startswith("old mode"):
            cur.old_mode = line.split()[-1]
        elif line.startswith("new mode"):
            cur.new_mode = line.split()[-1]
        elif line.startswith("rename to "):
            cur.status = "renamed"
        elif line.startswith(("Binary files", "GIT binary patch")):
            cur.binary = True
        elif line.startswith("+") and not line.startswith("+++"):
            cur.added += 1
            cur.added_lines.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            cur.removed += 1
        for mode in (cur.new_mode, cur.old_mode):
            if mode == "120000":
                cur.symlink = True
            if mode == "160000":
                cur.submodule = True
    return files


def check(name: str, status: Status, detail: str) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail}


def run_checks(patch: str, files: list[FileStat], policy: dict[str, Any], verify: list[dict[str, Any]]) -> list[dict]:
    out = []
    if not patch.strip():
        out.append(check("no_changes", "fail", "The agent made no changes."))
    else:
        out.append(check("no_changes", "pass", f"{len(files)} file(s) changed."))
    failed = [r for r in verify if r.get("exit_code") != 0]
    if not verify:
        out.append(check("verification", "warn", "The plan has no verification commands."))
    elif failed:
        names = ", ".join(f"`{r['command']}`" for r in failed)
        out.append(check("verification", "fail", f"{len(failed)} of {len(verify)} command(s) failed: {names}."))
    else:
        out.append(check("verification", "pass", f"All {len(verify)} command(s) passed."))
    return out

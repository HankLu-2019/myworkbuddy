"""Deterministic checks on a job's patch (design 04 §5.4), run in the worker, never in the sandbox.

Each check gives `{"name", "status": pass|warn|fail, "detail"}`. A `fail` makes the attempt fail (the agent gets
another try with feedback, up to max_attempts); a `warn` is shown to the reviewer.
"""

import asyncio
import json
import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Literal

from app.plans.conventions import protected_matches
from app.plans.validation import CI_PATTERNS

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


GITLEAKS_TIMEOUT_S = 120
EXEC_DIRS = ("scripts/", "bin/")


async def secret_scan(files: list[FileStat]) -> dict[str, Any]:
    """gitleaks over the added lines only (04 §5.4). Findings name the rule and file:line, never the secret (R7).
    Without gitleaks the check fails: a scan that didn't run is not a pass."""
    exe = shutil.which("gitleaks")
    if exe is None:
        return check("secret_scan", "fail", "The secret scanner (gitleaks) isn't installed on the worker.")
    lines: list[str] = []
    origin: list[tuple[str, int]] = []
    for f in files:
        for n, line in enumerate(f.added_lines, start=1):
            lines.append(line)
            origin.append((f.path, n))
    if not lines:
        return check("secret_scan", "pass", "No added lines to scan.")
    proc = await asyncio.create_subprocess_exec(
        exe, "stdin", "--no-banner", "--log-level", "error", "--report-format", "json", "--report-path", "-",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    try:
        out, _err = await asyncio.wait_for(proc.communicate("\n".join(lines).encode()), GITLEAKS_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        return check("secret_scan", "fail", "The secret scan took too long.")
    try:
        findings = json.loads(out or b"[]")
    except ValueError:
        return check("secret_scan", "fail", "The secret scanner's output couldn't be read.")
    if not findings:
        return check("secret_scan", "pass", f"No secrets in {len(lines)} added line(s).")
    where = []
    for f in findings[:10]:
        idx = int(f.get("StartLine", 1)) - 1
        path, n = origin[idx] if 0 <= idx < len(origin) else ("?", 0)
        where.append(f"{f.get('RuleID', 'secret')} in {path} (added line {n})")
    return check("secret_scan", "fail", f"{len(findings)} possible secret(s): {'; '.join(where)}.")


async def run_checks(
    patch: str, files: list[FileStat], policy: dict[str, Any], verify: list[dict[str, Any]]
) -> list[dict]:
    """The checks table of 04 §5.4, in a stable order."""
    out = []
    if not patch.strip():
        out.append(check("no_changes", "fail", "The agent made no changes."))
    else:
        out.append(check("no_changes", "pass", f"{len(files)} file(s) changed."))

    planned = set(policy.get("changes") or [])
    outside = [f.path for f in files if f.path not in planned]
    out.append(
        check("outside_plan", "warn", f"Files not in the plan: {', '.join(outside[:20])}.")
        if outside
        else check("outside_plan", "pass", "Only planned files changed.")
    )

    limit = int(policy.get("max_changed_lines", 3000))
    total = sum(f.added + f.removed for f in files)
    out.append(
        check("size", "fail", f"{total} changed lines; the limit is {limit}.")
        if total > limit
        else check("size", "pass", f"{total} changed line(s).")
    )

    binary = [f.path for f in files if f.binary]
    out.append(
        check("binary", "warn", f"Binary files: {', '.join(binary[:20])}.")
        if binary
        else check("binary", "pass", "No binary files.")
    )

    odd = [f"{f.path} (symlink)" for f in files if f.symlink]
    odd += [f"{f.path} (submodule)" for f in files if f.submodule]
    odd += [f"{f.path} (made executable)" for f in files
            if f.new_mode == "100755" and f.old_mode != "100755" and not f.path.startswith(EXEC_DIRS)]  # fmt: skip
    out.append(
        check("file_modes", "warn", f"Unusual changes: {', '.join(odd[:20])}.")
        if odd
        else check("file_modes", "pass", "No symlinks, submodules or new executables.")
    )

    acked = set(policy.get("acknowledged") or [])
    ci = [f.path for f in files if any(p.search(f.path) for p in CI_PATTERNS)]
    unacked = [p for p in ci if f"ci_config:{p}" not in acked]
    if unacked:
        out.append(
            check(
                "ci_config",
                "fail",
                f"CI/CD files changed without being acknowledged in the plan: {', '.join(unacked)}.",
            )
        )
    elif ci:
        out.append(check("ci_config", "pass", f"CI/CD changes acknowledged in the plan: {', '.join(ci)}."))
    else:
        out.append(check("ci_config", "pass", "No CI/CD files changed."))

    patterns = policy.get("protected") or []
    hit = protected_matches([f.path for f in files], patterns)
    blocked = [p for p in hit if f"protected_path:{p}" not in acked]
    source = policy.get("conventions_source") or "the repository's conventions"
    if blocked:
        out.append(check("protected_paths", "fail", f"Paths protected by {source} changed: {', '.join(blocked)}."))
    elif hit:
        out.append(check("protected_paths", "pass", f"Protected paths acknowledged in the plan: {', '.join(hit)}."))
    else:
        out.append(
            check("protected_paths", "pass", "No protected paths changed." if patterns else "No protected paths.")
        )

    out.append(await secret_scan(files))

    failed = [r for r in verify if r.get("exit_code") != 0]
    if not verify:
        out.append(check("verification", "warn", "The plan has no verification commands."))
    elif failed:
        names = ", ".join(f"`{r['command']}`" for r in failed)
        out.append(check("verification", "fail", f"{len(failed)} of {len(verify)} command(s) failed: {names}."))
    else:
        out.append(check("verification", "pass", f"All {len(verify)} command(s) passed."))
    return out

"""The checks on a job's patch (design 04 §5.4; plan X5): one test per row of the table, and gitleaks."""

import shutil

import pytest

from app.jobs import checks
from app.jobs.checks import parse_patch, run_checks

POLICY = {"changes": ["app/webhooks.py"], "max_changed_lines": 3000, "acknowledged": []}
OK_VERIFY = [{"command": "pytest -q", "exit_code": 0}]


def diff(path: str, added: list[str], *, header: str = "", removed: int = 0) -> str:
    lines = [
        f"diff --git a/{path} b/{path}",
        *([header] if header else []),
        f"--- a/{path}",
        f"+++ b/{path}",
        "@@ -1 +1 @@",
    ]
    lines += ["-old"] * removed + [f"+{a}" for a in added]
    return "\n".join(lines) + "\n"


async def statuses(patch: str, policy: dict | None = None, verify: list | None = None) -> dict[str, tuple[str, str]]:
    files = parse_patch(patch)
    out = await run_checks(patch, files, policy or POLICY, OK_VERIFY if verify is None else verify)
    return {c["name"]: (c["status"], c["detail"]) for c in out}


async def test_a_clean_patch_passes_everything() -> None:
    result = await statuses(diff("app/webhooks.py", ["retry = 3"]))
    assert {k: v[0] for k, v in result.items()} == {
        "no_changes": "pass", "outside_plan": "pass", "size": "pass", "binary": "pass", "file_modes": "pass",
        "ci_config": "pass", "secret_scan": "pass", "verification": "pass",
    }  # fmt: skip


async def test_empty_patch_fails() -> None:
    assert (await statuses(""))["no_changes"][0] == "fail"


async def test_files_outside_the_plan_warn() -> None:
    status, detail = (await statuses(diff("app/webhooks.py", ["x"]) + diff("app/other.py", ["y"])))["outside_plan"]
    assert status == "warn" and detail == "Files not in the plan: app/other.py."


async def test_too_many_changed_lines_fail() -> None:
    status, detail = (
        await statuses(diff("app/webhooks.py", ["x"] * 8, removed=3), {**POLICY, "max_changed_lines": 10})
    )["size"]
    assert status == "fail" and detail == "11 changed lines; the limit is 10."


async def test_binary_files_warn() -> None:
    patch = "diff --git a/logo.png b/logo.png\nnew file mode 100644\nGIT binary patch\nliteral 3\nKcmZ?\n"
    assert (await statuses(patch))["binary"][0] == "warn"


@pytest.mark.parametrize(
    ("header", "path", "expected"),
    [
        ("new file mode 120000", "link", "warn"),
        ("new file mode 160000", "vendor/lib", "warn"),
        ("old mode 100644\nnew mode 100755", "app/run.py", "warn"),
        ("old mode 100644\nnew mode 100755", "scripts/run.sh", "pass"),
        ("new file mode 100755", "bin/tool", "pass"),
    ],
)
async def test_symlinks_submodules_and_new_executables_warn(header: str, path: str, expected: str) -> None:
    assert (await statuses(diff(path, ["x"], header=header)))["file_modes"][0] == expected


async def test_unacknowledged_ci_changes_fail() -> None:
    patch = diff(".gitlab-ci.yml", ["  - pytest"])
    status, detail = (await statuses(patch))["ci_config"]
    assert status == "fail" and ".gitlab-ci.yml" in detail
    acked = {**POLICY, "acknowledged": ["ci_config:.gitlab-ci.yml"]}
    assert (await statuses(patch, acked))["ci_config"][0] == "pass"


async def test_failing_or_missing_verification() -> None:
    status, detail = (
        await statuses(diff("app/webhooks.py", ["x"]), verify=[{"command": "pytest -q", "exit_code": 1}])
    )["verification"]
    assert status == "fail" and "`pytest -q`" in detail
    assert (await statuses(diff("app/webhooks.py", ["x"]), verify=[]))["verification"][0] == "warn"


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks isn't installed (CI installs it)")
async def test_gitleaks_blocks_a_planted_key() -> None:
    patch = diff("app/webhooks.py", ["retry = 3", 'AWS_KEY = "AKIAZ7TJ3QKXN5W2PL4M"'])
    status, detail = (await statuses(patch))["secret_scan"]
    assert status == "fail" and "aws-access-token in app/webhooks.py (added line 2)" in detail
    assert "AKIAZ7TJ3QKXN5W2PL4M" not in detail  # never the secret itself (R7)


async def test_without_gitleaks_the_scan_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(checks.shutil, "which", lambda name: None)
    status, detail = (await statuses(diff("app/webhooks.py", ["x"])))["secret_scan"]
    assert status == "fail" and "isn't installed" in detail


def test_parse_patch_counts_and_statuses() -> None:
    patch = (
        diff("a.py", ["1", "2"], removed=1)
        + "diff --git a/old.py b/new.py\nsimilarity index 90%\nrename from old.py\nrename to new.py\n"
        + diff("gone.py", [], header="deleted file mode 100644", removed=2)
    )
    files = {f.path: f.public() for f in parse_patch(patch)}
    assert files["a.py"] == {"path": "a.py", "status": "modified", "added": 2, "removed": 1, "binary": False}
    assert files["new.py"]["status"] == "renamed" and files["gone.py"]["status"] == "deleted"

"""sandbox/mwb-verify: the verification commands the verify preset runs (design 04 §4, §5.4)."""

import importlib.machinery
import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "sandbox" / "mwb-verify"
loader = importlib.machinery.SourceFileLoader("mwb_verify", str(SCRIPT))
spec = importlib.util.spec_from_loader("mwb_verify", loader)
verify = importlib.util.module_from_spec(spec)
loader.exec_module(verify)


def write(ws: Path, commands: list[dict], total: float = 60) -> str:
    (ws / ".mwb").mkdir(parents=True, exist_ok=True)
    path = ws / ".mwb" / "verify.json"
    path.write_text(json.dumps({"commands": commands, "total_timeout_s": total}))
    return str(path)


def results(ws: Path) -> list[dict]:
    return json.loads((ws / ".mwb" / "verify-result.json").read_text())


def test_runs_each_command_and_records_the_result(tmp_path: Path) -> None:
    spec_path = write(tmp_path, [{"command": "echo hello && pwd"}, {"command": "echo boom >&2; exit 3"}])
    assert verify.run(spec_path, str(tmp_path)) == 1
    ok, bad = results(tmp_path)
    assert ok["exit_code"] == 0 and not ok["timed_out"] and ok["log"] == ".mwb/logs/verify-1.log"
    assert (tmp_path / ok["log"]).read_text().splitlines() == ["hello", str(tmp_path)]
    assert bad["exit_code"] == 3 and "boom" in (tmp_path / bad["log"]).read_text()


def test_a_command_over_its_timeout_is_killed_with_its_children(tmp_path: Path) -> None:
    spec_path = write(tmp_path, [{"command": "sleep 30 & sleep 30", "timeout_s": 1}, {"command": "true"}])
    assert verify.run(spec_path, str(tmp_path)) == 1
    slow, quick = results(tmp_path)
    assert slow["timed_out"] and slow["exit_code"] == 124 and slow["duration_s"] < 10
    assert quick["exit_code"] == 0


def test_commands_after_the_total_timeout_are_skipped(tmp_path: Path) -> None:
    spec_path = write(tmp_path, [{"command": "sleep 2", "timeout_s": 5}, {"command": "true"}], total=1)
    assert verify.run(spec_path, str(tmp_path)) == 1
    first, second = results(tmp_path)
    assert first["timed_out"] and second["skipped"] and second["exit_code"] is None


def test_long_logs_keep_the_end(tmp_path: Path) -> None:
    spec_path = write(tmp_path, [{"command": "yes x | head -c 1500000; echo END"}])
    assert verify.run(spec_path, str(tmp_path)) == 0
    log = (tmp_path / results(tmp_path)[0]["log"]).read_text()
    assert log.startswith("[...truncated...]") and log.rstrip().endswith("END") and len(log) < 1_100_000


def test_all_passing_exits_zero(tmp_path: Path) -> None:
    assert verify.run(write(tmp_path, [{"command": "true"}]), str(tmp_path)) == 0

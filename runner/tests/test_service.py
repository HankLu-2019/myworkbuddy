"""The runner's service logic against an in-memory Docker (design 04 §4, §9 tests 4, 5 and 9)."""

import io
import tarfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mwb_runner.config import RunnerSettings
from mwb_runner.presets import RunRejected, RunRequest, volume_name
from mwb_runner.service import NotFound, Runner
from mwb_runner.tarsafe import UnsafeArchive
from tests.fake_docker import FakeOps

JOB = uuid.UUID("00000000-0000-4000-8000-0000000000aa")


@pytest.fixture
def setup(tmp_path: Path):
    ops = FakeOps()
    s = RunnerSettings(env="dev", sandbox_runtime="runc", proxy_jobs_dir=str(tmp_path), reconfigure_wait_s=0,
                       docker_root=str(tmp_path / "missing"))  # fmt: skip
    return ops, Runner(ops, s), tmp_path


def tar_bytes(files: dict[str, str]) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, text in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(text.encode())
            t.addfile(info, io.BytesIO(text.encode()))
    buf.seek(0)
    return buf


def test_workspace_create_upload_patch_and_delete(setup) -> None:
    ops, runner, _ = setup
    with pytest.raises(NotFound):
        runner.put_content(JOB, tar_bytes({"a.txt": "a"}))
    runner.create_workspace(JOB)
    runner.create_workspace(JOB)  # idempotent
    assert ops.volumes_ == {volume_name(JOB): {"mwb.job": str(JOB)}}
    assert runner.put_content(JOB, tar_bytes({"app/main.py": "print(1)"})) == 1
    [(volume, data)] = ops.archives
    names = tarfile.open(fileobj=io.BytesIO(data)).getnames()
    assert volume == volume_name(JOB) and names == ["app/main.py"]
    with pytest.raises(UnsafeArchive):
        runner.put_content(JOB, tar_bytes({"../escape": "x"}))
    assert len(ops.archives) == 1  # nothing of a refused upload is extracted

    assert runner.patch(JOB, "abc1234").startswith(b"diff --git")
    vol, command, env = ops.helpers[-1]
    assert env["BASE_SHA"] == "abc1234" and "git -C /workspace diff --cached --binary" in command[2]
    with pytest.raises(RunRejected) as e:
        runner.patch(JOB, "HEAD; rm -rf /")
    assert e.value.code == "bad_sha"
    ops.helper_result = (0, b"x" * (runner.s.patch_max_bytes + 1), b"")
    with pytest.raises(RunRejected, match="over 20 MB"):
        runner.patch(JOB, "abc1234")
    ops.helper_result = (128, b"", b"fatal: bad revision")
    with pytest.raises(RunRejected, match="bad revision"):
        runner.patch(JOB, "abc1234")

    runner.delete_workspace(JOB)
    assert ops.volumes_ == {}


def test_a_run_gets_its_own_address_and_proxy_acl(setup) -> None:
    ops, runner, jobs_dir = setup
    runner.create_workspace(JOB)
    req = RunRequest(job_id=JOB, preset="agent", timeout_s=600, env={"ANTHROPIC_AUTH_TOKEN": "sk-x"},
                     extra_hosts_acl=["github.com"])  # fmt: skip
    first = runner.start_run(req)
    second = runner.start_run(req.model_copy(update={"extra_hosts_acl": []}))
    ips = [c["cfg"]["endpoint"]["mwb-sandbox"]["ipv4_address"] for c in ops.containers_.values()]
    assert ips == ["172.31.0.10", "172.31.0.11"]
    acl = (jobs_dir / f"run-{first}.conf").read_text()
    assert f"acl mwbrun_{first} src 172.31.0.10/32" in acl and "dstdomain github.com" in acl
    assert f"http_access allow agent_port mwbrun_{first} mwbrun_{first}_hosts" in acl
    assert not (jobs_dir / f"run-{second}.conf").exists()
    assert ops.execs == [("proxy1", ["squid", "-k", "parse", "-f", "/etc/squid/squid.conf"])]
    assert ops.signals == [("proxy1", "HUP")]

    assert runner.run_status(first)["state"] == "running"
    runner.kill_run(first)
    status = runner.run_status(first)
    assert status["state"] == "killed" and status["exit_code"] == 137
    assert not (jobs_dir / f"run-{first}.conf").exists() and len(ops.execs) == 2 and len(ops.signals) == 2
    with pytest.raises(NotFound):
        runner.run_status("not-a-run")
    with pytest.raises(NotFound):
        runner.run_status("0" * 16)


def test_runs_are_refused_without_a_workspace_or_disk(setup, tmp_path) -> None:
    ops, runner, _ = setup
    req = RunRequest(job_id=JOB, preset="verify", timeout_s=60)
    with pytest.raises(NotFound):
        runner.start_run(req)
    runner.create_workspace(JOB)
    runner.s.docker_root = str(tmp_path)
    runner.s.disk_min_free_ratio = 0.9999
    with pytest.raises(RunRejected) as e:
        runner.start_run(req)
    assert e.value.code == "disk_full"


def test_logs_are_tagged_ndjson(setup) -> None:
    import json

    ops, runner, _ = setup
    runner.create_workspace(JOB)
    run_id = runner.start_run(RunRequest(job_id=JOB, preset="verify", timeout_s=60))
    cid = next(iter(ops.containers_))
    ops.logs_[cid] = [("stdout", '{"type":"system"}'), ("stderr", "warning")]
    lines = [json.loads(x) for x in runner.run_logs(run_id, follow=False)]
    assert lines == [{"stream": "stdout", "line": '{"type":"system"}'}, {"stream": "stderr", "line": "warning"}]


def test_sweep_kills_overdue_runs_and_removes_orphans(setup) -> None:
    ops, runner, jobs_dir = setup
    other = uuid.uuid4()
    for job in (JOB, other):
        runner.create_workspace(job)
    slow = runner.start_run(RunRequest(job_id=JOB, preset="agent", timeout_s=60, extra_hosts_acl=["github.com"]))
    orphan = runner.start_run(RunRequest(job_id=other, preset="verify", timeout_s=3600))
    (jobs_dir / "run-ffffffffffffffff.conf").write_text("# left over")
    started = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)

    # The API can't be asked: only the timeout is enforced (60 s + 60 s grace).
    report = runner.sweep(None, now=started + timedelta(seconds=119))
    assert report == {"timed_out": 0, "containers": 0, "volumes": 0, "acls": 1}
    report = runner.sweep(None, now=started + timedelta(seconds=121))
    assert report["timed_out"] == 1 and runner.run_status(slow)["state"] == "killed"
    assert not (jobs_dir / f"run-{slow}.conf").exists()
    assert len(ops.containers_) == 2 and len(ops.volumes_) == 2

    # The API says only JOB is active: the other job's container and volume go (04 §9 test 9).
    report = runner.sweep({str(JOB)}, now=started + timedelta(seconds=130))
    assert report["containers"] == 1 and report["volumes"] == 1
    assert [c["Labels"]["mwb.job"] for c in ops.containers_.values()] == [str(JOB)]
    assert list(ops.volumes_) == [volume_name(JOB)]
    with pytest.raises(NotFound):
        runner.run_status(orphan)


def test_health(setup) -> None:
    ops, runner, _ = setup
    checks = {"docker": "ok", "runtime": "ok", "proxy_config": "ok", "proxy": "ok", "disk": "ok"}
    assert runner.health() == {"ok": True, "runtime": "runc", "checks": checks}
    runner.s.sandbox_runtime = "runsc"
    ops.proxies = []
    report = runner.health()
    assert not report["ok"] and report["checks"]["runtime"] == "Runtime runsc isn't installed"
    assert report["checks"]["proxy"] == "The egress proxy isn't running"
    ops.alive = False
    assert runner.health() == {"ok": False, "checks": {"docker": "Docker isn't reachable"}}

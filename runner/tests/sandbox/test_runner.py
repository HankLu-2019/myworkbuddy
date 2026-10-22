"""The runner against a real Docker daemon (design 04 §4, §9 tests 4, 5 and 9).

In CI the runner runs as its compose service and these tests call it over HTTP (MWB_RUNNER_URL, MWB_RUNNER_TOKEN).
Without MWB_RUNNER_URL they run the same app in-process with the real Docker adapter (MWB_PROXY_CONTAINER and
MWB_PROXY_JOBS_DIR must then point at the local proxy). Timeouts and the orphan sweep are driven in-process either
way, with an explicit clock.
"""

import io
import json
import os
import subprocess
import tarfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from mwb_runner.config import RunnerSettings
from mwb_runner.presets import volume_name

docker = pytest.importorskip("docker")
pytestmark = pytest.mark.docker

TOKEN = os.environ.get("MWB_RUNNER_TOKEN", "t" * 32)
AGENT_ENV = {"ANTHROPIC_AUTH_TOKEN": "sk-job-runner-test", "ANTHROPIC_MODEL": "coder",
             "ANTHROPIC_DEFAULT_HAIKU_MODEL": "fast", "AGENT_MAX_TURNS": "10"}  # fmt: skip


@pytest.fixture(scope="module")
def api():
    url = os.environ.get("MWB_RUNNER_URL")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if url:
        with httpx.Client(base_url=url, headers=headers, timeout=120) as c:
            yield c
        return
    from fastapi.testclient import TestClient

    from mwb_runner import app as app_module
    from mwb_runner.config import get_settings
    from mwb_runner.docker_ops import DockerOps
    from mwb_runner.service import Runner

    os.environ.setdefault("MWB_RUNNER_TOKEN", TOKEN)
    os.environ.setdefault("MWB_ENV", "dev")
    get_settings.cache_clear()
    app_module.set_runner(Runner(DockerOps(), get_settings()))
    app_module.app.state.no_sweeper = True
    with TestClient(app_module.app, headers=headers) as c:
        yield c
    app_module.set_runner(None)


@pytest.fixture(scope="module")
def client():
    return docker.from_env()


def git_repo_tar(tmp: Path, files: dict[str, str], mwb: dict[str, str]) -> tuple[bytes, str]:
    """A committed repository plus .mwb/ (excluded from git), as a tar; returns (tar, base sha)."""
    repo = tmp / "repo"
    repo.mkdir(parents=True)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=repo, check=True)
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    subprocess.run([*git, "add", "-A"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-qm", "base"], cwd=repo, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text(".mwb/\n")
    for name, text in mwb.items():
        (repo / ".mwb" / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / ".mwb" / name).write_text(text)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for path in sorted(repo.rglob("*")):
            t.add(path, arcname=str(path.relative_to(repo)), recursive=False)
    return buf.getvalue(), sha


def wait_exit(api, run_id: str, limit: float = 240) -> dict:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        status = api.get(f"/v1/runs/{run_id}").json()
        if status["state"] != "running":
            return status
        time.sleep(1)
    raise AssertionError(f"run {run_id} still running after {limit}s")


def test_health(api) -> None:
    r = api.get("/v1/health")
    assert r.status_code == 200, r.text
    assert r.json()["checks"] == {"docker": "ok", "runtime": "ok", "proxy_config": "ok", "proxy": "ok", "disk": "ok"}


def test_4_runner_rejects_bad_requests(api) -> None:
    job = str(uuid.uuid4())
    assert api.post(f"/v1/workspaces/{job}").status_code == 201
    for name, kind, link in [("../evil", tarfile.REGTYPE, ""), ("abs", tarfile.SYMTYPE, "/etc/shadow"),
                             ("up", tarfile.SYMTYPE, "../../x")]:  # fmt: skip
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            info = tarfile.TarInfo(name)
            info.type, info.linkname = kind, link
            t.addfile(info, io.BytesIO(b""))
        r = api.put(f"/v1/workspaces/{job}/content", content=buf.getvalue())
        assert r.status_code == 422 and r.json()["code"] == "unsafe_archive", (name, r.text)
    for body, code in [
        ({"preset": "shell"}, "unknown_preset"),
        ({"preset": "agent", "env": {"MWB_SECRET_KEY": "x"}}, "env_not_allowed"),
        ({"preset": "agent", "cpus": 32}, "limit_exceeded"),
        ({"preset": "agent", "memory_gb": 64}, "limit_exceeded"),
    ]:
        r = api.post("/v1/runs", json={"job_id": job, "timeout_s": 60, **body})
        assert r.status_code == 422 and r.json()["code"] == code, (body, r.text)
    assert api.delete(f"/v1/workspaces/{job}").status_code == 204


def test_agent_run_through_the_runner(api, client, tmp_path) -> None:
    job = str(uuid.uuid4())
    data, sha = git_repo_tar(tmp_path, {"app/webhooks.py": "def deliver():\n    pass\n", "README.md": "# x\n"}, {
        "TASK.md": "# Task\n\n## File changes\n- modify `app/webhooks.py`: retry\n",
        "prompt.md": "Read .mwb/TASK.md and carry it out.\n",
    })  # fmt: skip
    assert api.post(f"/v1/workspaces/{job}").status_code == 201
    r = api.put(f"/v1/workspaces/{job}/content", content=data)
    assert r.status_code == 200, r.text
    run = api.post("/v1/runs", json={"job_id": job, "preset": "agent", "timeout_s": 300, "env": AGENT_ENV}).json()
    status = wait_exit(api, run["run_id"])
    logs = [json.loads(x) for x in api.get(f"/v1/runs/{run['run_id']}/logs").text.splitlines()]
    assert status["state"] == "exited" and status["exit_code"] == 0, logs[-10:]
    events = [json.loads(x["line"]) for x in logs if x["stream"] == "stdout" and x["line"].startswith("{")]
    result = [e for e in events if e.get("type") == "result"]
    assert result and result[-1]["is_error"] is False
    patch = api.get(f"/v1/workspaces/{job}/patch", params={"base_sha": sha})
    assert patch.status_code == 200, patch.text
    assert "+# Retry transient failures (fake agent)" in patch.text and "app/webhooks.py" in patch.text
    assert ".mwb" not in patch.text  # excluded through .git/info/exclude
    assert api.delete(f"/v1/runs/{run['run_id']}?remove=true").status_code == 204
    assert api.get(f"/v1/runs/{run['run_id']}").status_code == 404
    assert api.delete(f"/v1/workspaces/{job}").status_code == 204
    with pytest.raises(docker.errors.NotFound):
        client.volumes.get(volume_name(uuid.UUID(job)))


def test_extra_hosts_are_allowed_for_that_run_only(api, tmp_path) -> None:
    """A per-run Squid ACL (04 §1): the verify preset reaches example.org only when the run asks for it."""
    codes = {}
    for extra in ([], ["example.org"]):
        job = str(uuid.uuid4())
        probe = "curl -s -o /dev/null -w '%{http_code}' --max-time 20 http://example.org/ > probe.txt"
        verify = {"commands": [{"command": probe}], "total_timeout_s": 60}
        data, sha = git_repo_tar(tmp_path / job, {"README.md": "x\n"}, {"verify.json": json.dumps(verify)})
        api.post(f"/v1/workspaces/{job}")
        assert api.put(f"/v1/workspaces/{job}/content", content=data).status_code == 200
        run = api.post("/v1/runs", json={"job_id": job, "preset": "verify", "timeout_s": 120, "extra_hosts_acl": extra})
        assert run.status_code == 201, run.text
        wait_exit(api, run.json()["run_id"], 120)
        patch = api.get(f"/v1/workspaces/{job}/patch", params={"base_sha": sha}).text
        codes[bool(extra)] = next(
            (ln[1:] for ln in patch.splitlines() if ln.startswith("+") and ln[1:].isdigit()), None
        )
        api.delete(f"/v1/workspaces/{job}")
    assert codes[False] == "403", codes
    assert codes[True] != "403", codes  # 200 with internet access; the proxy let it through either way


def test_5_and_9_timeouts_are_killed_and_orphans_removed(api, client, tmp_path) -> None:
    from mwb_runner.docker_ops import DockerOps
    from mwb_runner.service import Runner

    job = str(uuid.uuid4())
    data, _ = git_repo_tar(tmp_path, {"README.md": "x\n"}, {"prompt.md": "[[slow:600]] take your time\n"})
    api.post(f"/v1/workspaces/{job}")
    api.put(f"/v1/workspaces/{job}/content", content=data)
    run_id = api.post("/v1/runs", json={"job_id": job, "preset": "agent", "timeout_s": 10, "env": AGENT_ENV}).json()[
        "run_id"
    ]
    time.sleep(3)
    assert api.get(f"/v1/runs/{run_id}").json()["state"] == "running"

    settings = RunnerSettings(env="dev", sandbox_runtime=os.environ.get("MWB_SANDBOX_RUNTIME", "runc"),
                              proxy_jobs_dir=str(tmp_path), reconfigure_wait_s=0)  # fmt: skip
    sweeper = Runner(DockerOps(client), settings)
    later = datetime.now(UTC) + timedelta(seconds=75)  # past timeout_s + 60 s
    report = sweeper.sweep(None, now=later)
    assert report["timed_out"] >= 1
    assert sweeper.run_status(run_id)["state"] == "killed"
    assert client.volumes.get(volume_name(uuid.UUID(job)))  # unknown job state: nothing removed

    others = {c.labels["mwb.job"] for c in client.containers.list(all=True, filters={"label": "mwb.job"})} - {job}
    others |= {v.attrs["Labels"]["mwb.job"] for v in client.volumes.list(filters={"label": "mwb.job"})} - {job}
    report = sweeper.sweep(others, now=later)  # the API says this job isn't active (04 §9 test 9)
    assert report["containers"] >= 1 and report["volumes"] >= 1
    assert not client.containers.list(all=True, filters={"label": f"mwb.job={job}"})
    with pytest.raises(docker.errors.NotFound):
        client.volumes.get(volume_name(uuid.UUID(job)))
    assert api.get(f"/v1/runs/{run_id}").status_code == 404

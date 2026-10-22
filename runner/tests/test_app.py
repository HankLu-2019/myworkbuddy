"""The runner's HTTP API: bearer token on every route, errors as {detail, code} (design 04 §4)."""

import io
import tarfile
import uuid

import pytest
from fastapi.testclient import TestClient

from mwb_runner import app as app_module
from mwb_runner.config import RunnerSettings, get_settings
from mwb_runner.service import Runner
from tests.fake_docker import FakeOps

TOKEN = "t" * 32
JOB = str(uuid.uuid4())


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MWB_RUNNER_TOKEN", TOKEN)
    monkeypatch.setenv("MWB_ENV", "dev")
    get_settings.cache_clear()
    s = RunnerSettings(env="dev", runner_token=TOKEN, sandbox_runtime="runc", proxy_jobs_dir=str(tmp_path),
                       reconfigure_wait_s=0, docker_root=str(tmp_path / "none"))  # fmt: skip
    ops = FakeOps()
    app_module.set_runner(Runner(ops, s))
    app_module.app.state.no_sweeper = True
    with TestClient(app_module.app) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        c.ops = ops  # type: ignore[attr-defined]
        yield c
    app_module.set_runner(None)
    get_settings.cache_clear()


def test_every_route_needs_the_token(client) -> None:
    routes = [("get", "/v1/health"), ("post", f"/v1/workspaces/{JOB}"), ("put", f"/v1/workspaces/{JOB}/content"),
              ("get", f"/v1/workspaces/{JOB}/patch?base_sha=abc1234"), ("delete", f"/v1/workspaces/{JOB}"),
              ("post", "/v1/runs"), ("get", "/v1/runs/0123456789abcdef"), ("get", "/v1/runs/0123456789abcdef/logs"),
              ("delete", "/v1/runs/0123456789abcdef")]  # fmt: skip
    for method, path in routes:
        for headers in ({"Authorization": ""}, {"Authorization": "Bearer wrong"}):
            assert getattr(client, method)(path, headers=headers).status_code == 401, path
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404


def test_workspace_and_run_round_trip(client) -> None:
    assert client.post(f"/v1/workspaces/{JOB}").status_code == 201
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        info = tarfile.TarInfo("README.md")
        info.size = 2
        t.addfile(info, io.BytesIO(b"hi"))
    assert client.put(f"/v1/workspaces/{JOB}/content", content=buf.getvalue()).json() == {"entries": 1}
    bad = io.BytesIO()
    with tarfile.open(fileobj=bad, mode="w") as t:
        link = tarfile.TarInfo("x")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        t.addfile(link)
    r = client.put(f"/v1/workspaces/{JOB}/content", content=bad.getvalue())
    assert r.status_code == 422 and r.json()["code"] == "unsafe_archive"

    r = client.post("/v1/runs", json={"job_id": JOB, "preset": "agent", "timeout_s": 600, "env": {"SHELL": "/bin/sh"}})
    assert r.status_code == 422 and r.json()["code"] == "env_not_allowed"
    r = client.post("/v1/runs", json={"job_id": JOB, "preset": "agent", "timeout_s": 600, "cpus": 64})
    assert r.status_code == 422 and r.json()["code"] == "limit_exceeded"
    run_id = client.post("/v1/runs", json={"job_id": JOB, "preset": "verify", "timeout_s": 60}).json()["run_id"]
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "running"
    assert client.get(f"/v1/runs/{run_id}/logs").text == ""
    assert client.delete(f"/v1/runs/{run_id}").status_code == 204
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "killed"
    assert client.delete(f"/v1/runs/{run_id}?remove=true").status_code == 204
    assert client.get(f"/v1/runs/{run_id}").status_code == 404
    assert client.get(f"/v1/workspaces/{JOB}/patch?base_sha=abc1234").text.startswith("diff --git")
    assert client.delete(f"/v1/workspaces/{JOB}").status_code == 204
    r = client.get(f"/v1/workspaces/{JOB}/patch?base_sha=abc1234")
    assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_health_reports_problems_with_503(client) -> None:
    assert client.get("/v1/health").json()["ok"] is True
    client.ops.alive = False
    assert client.get("/v1/health").status_code == 503


def test_active_jobs_answer_or_none(monkeypatch) -> None:
    import httpx

    s = RunnerSettings(env="dev", runner_token=TOKEN)

    def fake_get(url, **kw):
        assert url == "http://api:8000/internal/jobs/active" and kw["headers"]["Authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(200, json={"jobs": [JOB]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    assert app_module.active_jobs(s) == {JOB}

    def down(url, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", down)
    assert app_module.active_jobs(s) is None

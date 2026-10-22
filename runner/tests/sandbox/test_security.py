"""Sandbox security tests 1–3 and the agent smoke run (design 04 §9), against a real Docker daemon.

Needs the `mwb-sandbox` image and the compose stack's egress proxy, fake gateway, api and postgres (CI job
`sandbox`; locally: `docker build -t mwb-sandbox sandbox` and `docker compose -f docker-compose.yml -f
compose.ci.yml up -d --wait egress-proxy llm-gateway api`). Containers are created with the runner's own
`container_config`; the probes only replace the command, never a flag.
"""

import json
import os
import time
import uuid

import pytest

from mwb_runner.config import RunnerSettings
from mwb_runner.presets import RunRequest, container_config, volume_name

docker = pytest.importorskip("docker")
pytestmark = pytest.mark.docker

SETTINGS = RunnerSettings(env="dev", sandbox_runtime="runc")
TOKEN = "sk-job-0123456789abcdef"
_ips = iter(range(200, 250))

PROBE = r"""
import json, os, socket, subprocess
out = {}
def curl(*args):
    r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "20", *args],
                       capture_output=True, text=True)
    return [r.returncode, r.stdout]
out["example_via_proxy"] = curl("https://example.com/")
out["example_direct"] = curl("--noproxy", "*", "https://example.com/")
out["gateway_via_proxy"] = curl("http://llm-gateway:4000/health/liveliness")
out["gateway_direct"] = curl("--noproxy", "*", "http://llm-gateway:4000/health/liveliness")
def connect(host, port):
    try:
        socket.create_connection((host, int(port)), timeout=5).close()
        return "connected"
    except OSError as e:
        return type(e).__name__
out["tcp"] = {f"{h}:{p}": connect(h, p) for h, p in json.loads(os.environ.get("PROBE_TARGETS", "[]"))}
try:
    socket.getaddrinfo("example.com", 443)
    out["dns_example"] = "resolved"
except OSError:
    out["dns_example"] = "failed"
if os.environ.get("PROBE_PIP"):
    r = subprocess.run(["python3", "-m", "pip", "download", "--no-deps", "-q", "-d", "/tmp/pd", "six==1.16.0"],
                       capture_output=True, text=True)
    out["pip"] = [r.returncode, r.stderr[-300:]]
out["uid"] = os.getuid()
try:
    open("/usr/local/bin/x", "w")
    out["rootfs"] = "writable"
except OSError:
    out["rootfs"] = "read-only"
out["cap_eff"] = next(l.split()[1] for l in open("/proc/self/status") if l.startswith("CapEff"))
out["env"] = dict(os.environ)
print("PROBE " + json.dumps(out))
"""


_volumes: list[str] = []


@pytest.fixture(scope="module")
def client():
    c = docker.from_env()
    c.images.get(SETTINGS.sandbox_image)  # build it first: docker build -t mwb-sandbox sandbox
    c.networks.get(SETTINGS.sandbox_network)
    yield c
    for name in _volumes:
        try:
            c.volumes.get(name).remove(force=True)
        except docker.errors.NotFound:
            pass


def _volume(client, job: uuid.UUID) -> None:
    name = volume_name(job)
    if name not in _volumes:
        client.volumes.create(name, labels={"mwb.job": str(job)})
        _volumes.append(name)


def _ip_of(client, service: str) -> str | None:
    for c in client.containers.list(filters={"label": f"com.docker.compose.service={service}"}):
        for net in c.attrs["NetworkSettings"]["Networks"].values():
            if net.get("IPAddress"):
                return net["IPAddress"]
    return None


def run(
    client, preset: str, *, env: dict | None = None, command: list[str] | None = None,
    extra_env: dict | None = None, job: uuid.UUID | None = None, timeout_s: int = 180,
) -> tuple[int, str]:  # fmt: skip
    """Create and run one sandbox container exactly as the runner configures it; returns (exit code, output)."""
    job = job or uuid.uuid4()
    _volume(client, job)
    req = RunRequest(job_id=job, preset=preset, env=env or {}, timeout_s=timeout_s, cpus=1, memory_gb=2)
    cfg = container_config(
        req, run_id=uuid.uuid4().hex[:12], ip=f"{SETTINGS.sandbox_prefix}.{next(_ips)}", settings=SETTINGS
    )
    api = client.api
    host = api.create_host_config(**cfg["host_config"])
    endpoint = cfg["endpoint"][SETTINGS.sandbox_network]
    networking = api.create_networking_config({SETTINGS.sandbox_network: api.create_endpoint_config(**endpoint)})
    environment = {**cfg["environment"], **(extra_env or {})}
    created = api.create_container(
        cfg["image"], command=command or cfg["command"], user=cfg["user"], environment=environment,
        working_dir=cfg["working_dir"], labels=cfg["labels"], host_config=host, networking_config=networking,
        stop_timeout=cfg["stop_timeout"],
    )  # fmt: skip
    cid = created["Id"]
    try:
        api.start(cid)
        code = api.wait(cid, timeout=timeout_s + 60)["StatusCode"]
        return code, api.logs(cid).decode(errors="replace")
    finally:
        api.remove_container(cid, force=True)


def probe(client, preset: str, **kw) -> dict:
    code, logs = run(client, preset, command=["python3", "-c", PROBE], **kw)
    line = next((x for x in logs.splitlines() if x.startswith("PROBE ")), None)
    assert line, f"probe printed nothing (exit {code}):\n{logs[-2000:]}"
    return json.loads(line[6:])


def _blocked(result: list) -> bool:
    code, http = result
    return code != 0 or http not in ("200", "301", "302")


def test_1_agent_reaches_only_the_gateway_and_mirrors_through_the_proxy(client) -> None:
    targets = [["api", 8000], ["postgres", 5432], ["llm-gateway", 4000]]
    for service, port in [("api", 8000), ("postgres", 5432), ("llm-gateway", 4000)]:
        if ip := _ip_of(client, service):
            targets.append([ip, port])
    out = probe(client, "agent", env={"ANTHROPIC_AUTH_TOKEN": TOKEN},
                extra_env={"PROBE_TARGETS": json.dumps(targets), "PROBE_PIP": "1"})  # fmt: skip
    assert _blocked(out["example_via_proxy"]), out["example_via_proxy"]
    assert _blocked(out["example_direct"]), out["example_direct"]
    assert out["gateway_via_proxy"] == [0, "200"], out["gateway_via_proxy"]
    assert _blocked(out["gateway_direct"]), out["gateway_direct"]
    assert all(v != "connected" for v in out["tcp"].values()), out["tcp"]
    assert out["dns_example"] == "failed"
    if not os.environ.get("MWB_TEST_NO_INTERNET"):  # local runs behind a TLS-intercepting proxy can't reach PyPI
        assert out["pip"][0] == 0, out["pip"]
    assert out["uid"] == 1000 and out["rootfs"] == "read-only" and int(out["cap_eff"], 16) == 0


def test_2_verify_cannot_reach_the_gateway(client) -> None:
    out = probe(client, "verify")
    assert _blocked(out["gateway_via_proxy"]), out["gateway_via_proxy"]
    assert out["gateway_via_proxy"][1] == "403"
    assert _blocked(out["example_via_proxy"])


def test_3_the_environment_holds_only_the_job_key(client) -> None:
    out = probe(client, "agent", env={"ANTHROPIC_AUTH_TOKEN": TOKEN, "ANTHROPIC_MODEL": "coder"})
    env = {k: v for k, v in out["env"].items() if k != "PROBE_TARGETS"}
    bad = [k for k in env if k.startswith(("MWB_", "AWS_", "GIT", "GITLAB", "JIRA", "LITELLM", "POSTGRES"))]
    assert bad == [], bad
    secrets = [k for k, v in env.items() if "sk-" in v or "token" in k.lower() or "key" in k.lower()]
    assert secrets == ["ANTHROPIC_AUTH_TOKEN"], secrets
    assert env["ANTHROPIC_AUTH_TOKEN"] == TOKEN


def _seed(client, job: uuid.UUID, files: dict[str, str]) -> None:
    """Write files into a job's volume with a throwaway container (no network)."""
    _volume(client, job)
    code = (
        "import json, os, pathlib\n"
        "for p, t in json.loads(os.environ['FILES']).items():\n"
        "    f = pathlib.Path(p); f.parent.mkdir(parents=True, exist_ok=True); f.write_text(t)\n"
    )
    client.containers.run(SETTINGS.sandbox_image, ["python3", "-c", code], user="1000:1000", network_mode="none",
                          environment={"FILES": json.dumps(files)}, working_dir="/workspace", remove=True,
                          volumes={volume_name(job): {"bind": "/workspace", "mode": "rw"}})  # fmt: skip


def _read(client, job: uuid.UUID, path: str) -> str:
    return client.containers.run(SETTINGS.sandbox_image, ["cat", path], user="1000:1000", network_mode="none",
                                 volumes={volume_name(job): {"bind": "/workspace", "mode": "ro"}},
                                 working_dir="/workspace", remove=True).decode()  # fmt: skip


def test_agent_preset_smoke_run_makes_a_change(client) -> None:
    job = uuid.uuid4()
    _seed(client, job, {
        "app/webhooks.py": "def deliver():\n    pass\n",
        ".mwb/TASK.md": "# Task\n\n## File changes\n- modify `app/webhooks.py`: retry transient failures\n",
        ".mwb/prompt.md": "Read .mwb/TASK.md and carry it out.\n",
    })  # fmt: skip
    started = time.monotonic()
    env = {"ANTHROPIC_AUTH_TOKEN": TOKEN, "ANTHROPIC_MODEL": "coder", "ANTHROPIC_DEFAULT_HAIKU_MODEL": "fast",
           "AGENT_MAX_TURNS": "10"}  # fmt: skip
    code, logs = run(client, "agent", job=job, env=env)
    assert code == 0, logs[-3000:]
    lines = [json.loads(x) for x in logs.splitlines() if x.startswith("{")]
    result = [x for x in lines if x.get("type") == "result"]
    assert result and result[-1]["is_error"] is False, logs[-3000:]
    assert "Retry transient failures (fake agent)" in _read(client, job, "app/webhooks.py")
    assert _read(client, job, ".mwb/notes.md").startswith("Changed app/webhooks.py")
    assert time.monotonic() - started < 300

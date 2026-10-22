"""The only containers the runner starts (design 04 §4, 08 §F10): three presets with fixed flags.

A caller chooses a preset, a few allowlisted environment variables, limits at or below the maxima, and optional
extra hosts for the proxy. Image, mounts, user, capabilities, network, DNS and everything else are fixed here.
`container_config` is what the runner gives Docker; `cli_args` is the same thing as `docker run` arguments, for
the docs and for a test that keeps the two in step.
"""

import re
import uuid
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from mwb_runner.config import RunnerSettings

WORKSPACE = "/workspace"
GATEWAY_URL = "http://llm-gateway:4000"
HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
SAFE_VALUE = re.compile(r"^[\x21-\x7e]{1,512}$")  # printable, no spaces or newlines
MODEL = re.compile(r"^[A-Za-z0-9._:/@-]{1,128}$")


class RunRejected(Exception):
    """A run request the runner refuses (HTTP 422); `code` is stable for callers and tests."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Preset:
    name: str
    command: tuple[str, ...]
    env_allowed: frozenset[str]
    proxy_port: int
    fixed_env: dict[str, str] = field(default_factory=dict)
    services: bool = False  # 08 §F10: the sandbox gets its own Docker (Sysbox), for tests that need containers

    def proxy_env(self) -> dict[str, str]:
        url = f"http://egress-proxy:{self.proxy_port}"
        return {
            "HTTPS_PROXY": url,
            "HTTP_PROXY": url,
            "https_proxy": url,
            "http_proxy": url,
            "NO_PROXY": "",
            "no_proxy": "",
        }


# Claude Code headless, checked against the docs for the pinned CLI (sandbox/Dockerfile, 2.1.293): print mode with
# stream-json (which needs --verbose), a turn cap, and no permission prompts (uid 1000, never root).
AGENT_COMMAND = (
    "sh",
    "-c",
    'exec claude -p "$(cat /workspace/.mwb/prompt.md)" --output-format stream-json --verbose'
    ' --max-turns "${AGENT_MAX_TURNS:-60}" --permission-mode bypassPermissions',
)

PRESETS: dict[str, Preset] = {
    "agent": Preset(
        name="agent",
        command=AGENT_COMMAND,
        env_allowed=frozenset(
            {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "AGENT_MAX_TURNS"}
        ),
        proxy_port=3128,
        fixed_env={
            "ANTHROPIC_BASE_URL": GATEWAY_URL,  # reached through the proxy: the sandbox has no direct route
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",  # no telemetry, error reports or auto-updates
            "DISABLE_AUTOUPDATER": "1",
            "HOME": "/home/agent",
        },
    ),
    "verify": Preset(
        name="verify",
        command=("/usr/local/bin/mwb-verify", "/workspace/.mwb/verify.json"),
        env_allowed=frozenset(),
        proxy_port=3129,
        fixed_env={"HOME": "/home/agent"},
    ),
    # 08 §F10. The entrypoint starts dockerd as the container's root, which Sysbox maps to an unprivileged host
    # user, then runs mwb-verify as uid 1000 (setpriv). Pulls go through the proxy to MWB_PROXY_REGISTRY_HOSTS.
    "verify-services": Preset(
        name="verify-services",
        command=("/usr/local/bin/mwb-dind-verify", "/workspace/.mwb/verify.json"),
        env_allowed=frozenset(),
        proxy_port=3129,
        fixed_env={"HOME": "/home/agent", "DOCKER_HOST": "unix:///run/mwb-docker/docker.sock"},
        services=True,
    ),
}
SERVICES_FACTOR = 2  # a services run gets twice the plan's CPU, memory, processes and disk (08 §F10)
DIND_ROOT = "/var/lib/docker"


class RunRequest(BaseModel):
    job_id: uuid.UUID
    preset: str
    env: dict[str, str] = {}
    timeout_s: int = Field(ge=10)
    cpus: float = Field(2, gt=0)
    memory_gb: int = Field(4, ge=1)
    extra_hosts_acl: list[str] = Field(default=[], max_length=10)


def check(req: RunRequest, settings: RunnerSettings) -> Preset:
    """The preset for a request the runner accepts; RunRejected otherwise (04 §9 test 4)."""
    preset = PRESETS.get(req.preset)
    if preset is None:
        raise RunRejected("unknown_preset", f"Unknown preset {req.preset!r}.")
    extra = sorted(set(req.env) - preset.env_allowed)
    if extra:
        raise RunRejected(
            "env_not_allowed", f"Environment variables not allowed for {preset.name}: {', '.join(extra)}."
        )
    for key, value in req.env.items():
        if not SAFE_VALUE.fullmatch(value):
            raise RunRejected("env_invalid", f"{key} has an invalid value.")
    for key in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
        if key in req.env and not MODEL.fullmatch(req.env[key]):
            raise RunRejected("env_invalid", f"{key} isn't a model name.")
    if "AGENT_MAX_TURNS" in req.env and not (
        req.env["AGENT_MAX_TURNS"].isdigit() and 1 <= int(req.env["AGENT_MAX_TURNS"]) <= 500
    ):
        raise RunRejected("env_invalid", "AGENT_MAX_TURNS must be 1–500.")
    if req.cpus > settings.max_cpus:
        raise RunRejected("limit_exceeded", f"cpus {req.cpus} is above the maximum {settings.max_cpus}.")
    if req.memory_gb > settings.max_memory_gb:
        raise RunRejected(
            "limit_exceeded", f"memory {req.memory_gb} GB is above the maximum {settings.max_memory_gb} GB."
        )
    if req.timeout_s > settings.max_timeout_s:
        raise RunRejected("limit_exceeded", f"timeout {req.timeout_s}s is above the maximum {settings.max_timeout_s}s.")
    for host in req.extra_hosts_acl:
        if not HOST.fullmatch(host):
            raise RunRejected("host_invalid", f"{host!r} isn't a host name.")
    if preset.name != "agent" and "llm-gateway" in req.extra_hosts_acl:
        raise RunRejected("host_invalid", "The verify preset never reaches the gateway.")
    return preset


def volume_name(job_id: uuid.UUID) -> str:
    return f"mwb-job-{job_id}"


def dind_volume_name(job_id: uuid.UUID) -> str:
    """The inner Docker's images and containers (08 §F10), removed with the workspace."""
    return f"mwb-dind-{job_id}"


def acl_hosts(preset: Preset, req: RunRequest, settings: RunnerSettings) -> list[str]:
    """The hosts this run may reach through the proxy besides the fixed ones: a services run adds the registry."""
    extra = settings.registry_hosts if preset.services else []
    return sorted(set(req.extra_hosts_acl) | set(extra))


def environment(preset: Preset, req: RunRequest) -> dict[str, str]:
    """Allowlisted values, then the preset's fixed ones (which always win)."""
    return {**req.env, **preset.fixed_env, **preset.proxy_env()}


def labels(job_id: uuid.UUID, run_id: str, preset: Preset, timeout_s: int) -> dict[str, str]:
    return {"mwb.job": str(job_id), "mwb.run": run_id, "mwb.preset": preset.name, "mwb.timeout_s": str(timeout_s)}


def container_config(req: RunRequest, *, run_id: str, ip: str, settings: RunnerSettings) -> dict:
    """Arguments for docker-py's low-level `create_container` (plus `host_config` and the endpoint), all fixed."""
    preset = check(req, settings)
    factor = SERVICES_FACTOR if preset.services else 1
    memory = f"{req.memory_gb * factor}g"
    binds = {volume_name(req.job_id): {"bind": WORKSPACE, "mode": "rw"}}
    tmpfs = {"/tmp": "size=2g,exec", "/home/agent": "size=1g,exec,uid=1000,gid=1000"}
    if preset.services:
        binds[dind_volume_name(req.job_id)] = {"bind": DIND_ROOT, "mode": "rw"}
        tmpfs["/run"] = "size=64m"
    return {
        "image": settings.services_image if preset.services else settings.sandbox_image,
        "command": list(preset.command),
        # A services run starts as the container's root (an unprivileged host user under Sysbox's user namespace)
        # so dockerd can run; the entrypoint drops to 1000:1000 for the verify commands.
        "user": "0:0" if preset.services else "1000:1000",
        "environment": environment(preset, req),
        "working_dir": WORKSPACE,
        "labels": labels(req.job_id, run_id, preset, req.timeout_s),
        "stop_timeout": 10,
        "host_config": {
            "runtime": settings.services_runtime if preset.services else settings.sandbox_runtime,
            "read_only": True,
            # Sysbox confines the capabilities to the container's user namespace; dockerd needs them there.
            "cap_drop": [] if preset.services else ["ALL"],
            "security_opt": ["no-new-privileges"],
            "pids_limit": settings.pids_limit * factor,
            "nano_cpus": int(req.cpus * factor * 1e9),
            "mem_limit": memory,
            "memswap_limit": memory,
            "network_mode": settings.sandbox_network,
            "dns": ["127.0.0.1"],  # no resolver: names resolve only through the proxy
            "extra_hosts": {"egress-proxy": settings.proxy_ip},
            "tmpfs": tmpfs,
            "binds": binds,
            "log_config": {"type": "json-file", "config": {"max-size": "20m"}},
        },
        "endpoint": {settings.sandbox_network: {"ipv4_address": ip}},
    }


def cli_args(req: RunRequest, *, run_id: str, ip: str, settings: RunnerSettings) -> list[str]:
    """The same container as `docker run` arguments (04 §4 lists these flags)."""
    c = container_config(req, run_id=run_id, ip=ip, settings=settings)
    h = c["host_config"]
    args = [
        "docker", "run", "-d", f"--runtime={h['runtime']}", "--user", c["user"], "--read-only",
        *[a for cap in h["cap_drop"] for a in ("--cap-drop", cap)],
        "--security-opt", "no-new-privileges", "--pids-limit", str(h["pids_limit"]),
        "--cpus", f"{h['nano_cpus'] / 1e9:g}", "--memory", h["mem_limit"], "--memory-swap", h["memswap_limit"],
        "--network", h["network_mode"], "--ip", ip, "--dns", "127.0.0.1",
        "--add-host", f"egress-proxy:{settings.proxy_ip}",
        *[a for path, opts in h["tmpfs"].items() for a in ("--tmpfs", f"{path}:{opts}")],
        *[a for vol, b in h["binds"].items() for a in ("-v", f"{vol}:{b['bind']}")], "-w", WORKSPACE,
        "--stop-timeout", "10", "--log-driver", "json-file", "--log-opt", "max-size=20m",
    ]  # fmt: skip
    for key, value in c["labels"].items():
        args += ["--label", f"{key}={value}"]
    for key in sorted(c["environment"]):
        args += ["-e", f"{key}={c['environment'][key]}"]
    return [*args, c["image"], *c["command"]]

"""Fixed presets and request validation (design 04 §4, §9 test 4)."""

import uuid

import pytest

from mwb_runner.config import RunnerSettings
from mwb_runner.presets import PRESETS, RunRejected, RunRequest, acl_hosts, check, cli_args, container_config

JOB = uuid.UUID("00000000-0000-4000-8000-000000000001")
S = RunnerSettings(env="dev", sandbox_runtime="runc")


def req(**over) -> RunRequest:
    return RunRequest(**{"job_id": JOB, "preset": "agent", "timeout_s": 600, **over})


def test_agent_container_is_locked_down() -> None:
    c = container_config(
        req(env={"ANTHROPIC_AUTH_TOKEN": "sk-job", "ANTHROPIC_MODEL": "coder"}),
        run_id="r1",
        ip="172.31.0.10",
        settings=S,
    )
    h = c["host_config"]
    assert c["user"] == "1000:1000" and c["image"] == "mwb-sandbox:latest" and c["working_dir"] == "/workspace"
    assert h["read_only"] and h["cap_drop"] == ["ALL"] and h["security_opt"] == ["no-new-privileges"]
    assert h["pids_limit"] == 512 and h["mem_limit"] == h["memswap_limit"] == "4g" and h["nano_cpus"] == 2_000_000_000
    assert h["network_mode"] == "mwb-sandbox" and h["dns"] == ["127.0.0.1"] and h["runtime"] == "runc"
    assert h["extra_hosts"] == {"egress-proxy": "172.31.0.2"}
    assert h["binds"] == {f"mwb-job-{JOB}": {"bind": "/workspace", "mode": "rw"}}
    assert c["endpoint"] == {"mwb-sandbox": {"ipv4_address": "172.31.0.10"}}
    env = c["environment"]
    assert env["ANTHROPIC_BASE_URL"] == "http://llm-gateway:4000" and env["HTTPS_PROXY"] == "http://egress-proxy:3128"
    assert env["NO_PROXY"] == "" and env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert not [k for k in env if k.startswith(("MWB_", "AWS_", "GIT", "JIRA"))]
    assert c["labels"] == {"mwb.job": str(JOB), "mwb.run": "r1", "mwb.preset": "agent", "mwb.timeout_s": "600"}
    assert (
        "--permission-mode bypassPermissions" in c["command"][2]
        and "--output-format stream-json --verbose" in c["command"][2]
    )


def test_verify_preset_uses_the_mirrors_only_port_and_no_key() -> None:
    c = container_config(req(preset="verify"), run_id="r2", ip="172.31.0.11", settings=S)
    assert c["command"] == ["/usr/local/bin/mwb-verify", "/workspace/.mwb/verify.json"]
    assert (
        c["environment"]["HTTPS_PROXY"] == "http://egress-proxy:3129" and "ANTHROPIC_BASE_URL" not in c["environment"]
    )
    with pytest.raises(RunRejected) as e:
        check(req(preset="verify", env={"ANTHROPIC_AUTH_TOKEN": "x"}), S)
    assert e.value.code == "env_not_allowed"


def test_verify_services_preset_is_fixed() -> None:
    """08 §F10: Sysbox, the dind image, its own Docker volume, the verify port, and twice the plan's limits."""
    s = RunnerSettings(env="dev", sandbox_runtime="runc", proxy_registry_hosts="registry.example.com, mirror.gcr.io")
    c = container_config(req(preset="verify-services", cpus=2, memory_gb=4), run_id="r4", ip="172.31.0.13", settings=s)
    h = c["host_config"]
    assert c["image"] == "mwb-sandbox-dind:latest" and h["runtime"] == "sysbox-runc"
    assert c["command"] == ["/usr/local/bin/mwb-dind-verify", "/workspace/.mwb/verify.json"]
    assert c["user"] == "0:0" and h["cap_drop"] == [] and h["security_opt"] == ["no-new-privileges"]
    assert h["read_only"] and h["network_mode"] == "mwb-sandbox" and h["dns"] == ["127.0.0.1"]
    assert h["nano_cpus"] == 4_000_000_000 and h["mem_limit"] == h["memswap_limit"] == "8g" and h["pids_limit"] == 1024
    assert h["binds"] == {f"mwb-job-{JOB}": {"bind": "/workspace", "mode": "rw"},
                          f"mwb-dind-{JOB}": {"bind": "/var/lib/docker", "mode": "rw"}}  # fmt: skip
    assert h["tmpfs"]["/run"] == "size=64m"
    assert "/var/run/docker.sock" not in str(c) and "docker.sock" not in str(h["binds"])  # never the host's socket
    env = c["environment"]
    assert (
        env["HTTPS_PROXY"] == "http://egress-proxy:3129" and env["DOCKER_HOST"] == "unix:///run/mwb-docker/docker.sock"
    )
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["MWB_INNER_PROXY"] == "http://172.31.0.2:3129"  # inner containers have no hosts entry or resolver
    plain = container_config(req(preset="verify"), run_id="r6", ip="172.31.0.15", settings=s)["environment"]
    assert "MWB_INNER_PROXY" not in plain
    assert acl_hosts(PRESETS["verify-services"], req(preset="verify-services", extra_hosts_acl=["pypi.org"]), s) == [
        "mirror.gcr.io", "pypi.org", "registry.example.com"]  # fmt: skip
    assert acl_hosts(PRESETS["verify"], req(preset="verify"), s) == []  # only services runs reach the registry
    line = " ".join(cli_args(req(preset="verify-services"), run_id="r5", ip="172.31.0.14", settings=s))
    for flag in ["--runtime=sysbox-runc", "--user 0:0", "--cpus 4", "--memory 8g", f"-v mwb-dind-{JOB}:/var/lib/docker",
                 "--tmpfs /run:size=64m", "mwb-sandbox-dind:latest"]:  # fmt: skip
        assert flag in line, flag
    assert "--cap-drop" not in line
    with pytest.raises(RunRejected) as e:
        check(req(preset="verify-services", extra_hosts_acl=["llm-gateway"]), s)
    assert e.value.code == "host_invalid"
    with pytest.raises(ValueError):
        RunnerSettings(env="dev", proxy_registry_hosts="evil.example/x")


def test_fixed_values_win_over_the_caller() -> None:
    with pytest.raises(RunRejected):
        check(req(env={"ANTHROPIC_BASE_URL": "http://evil.example"}), S)
    with pytest.raises(RunRejected):
        check(req(env={"HTTPS_PROXY": "http://elsewhere:3128"}), S)


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"preset": "shell"}, "unknown_preset"),
        ({"env": {"MWB_SECRET_KEY": "x"}}, "env_not_allowed"),
        ({"env": {"ANTHROPIC_AUTH_TOKEN": "a b"}}, "env_invalid"),
        ({"env": {"ANTHROPIC_MODEL": "x;rm"}}, "env_invalid"),
        ({"env": {"AGENT_MAX_TURNS": "9999"}}, "env_invalid"),
        ({"cpus": 4.5}, "limit_exceeded"),
        ({"memory_gb": 16}, "limit_exceeded"),
        ({"timeout_s": 3 * 60 * 60}, "limit_exceeded"),
        ({"extra_hosts_acl": ["evil.example/x"]}, "host_invalid"),
        ({"preset": "verify", "extra_hosts_acl": ["llm-gateway"]}, "host_invalid"),
    ],
)
def test_runner_rejects(over: dict, code: str) -> None:
    with pytest.raises(RunRejected) as e:
        check(req(**over), S)
    assert e.value.code == code


def test_cli_args_match_the_design() -> None:
    args = cli_args(req(cpus=1.5, memory_gb=2), run_id="r3", ip="172.31.0.12", settings=S)
    line = " ".join(args)
    for flag in [
        "--runtime=runc", "--user 1000:1000", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
        "--pids-limit 512", "--cpus 1.5", "--memory 2g", "--memory-swap 2g", "--network mwb-sandbox",
        "--ip 172.31.0.12", "--dns 127.0.0.1", "--add-host egress-proxy:172.31.0.2", "--tmpfs /tmp:size=2g,exec",
        f"-v mwb-job-{JOB}:/workspace", "-w /workspace", "--stop-timeout 10", "--log-opt max-size=20m",
    ]:  # fmt: skip
        assert flag in line, flag


def test_settings_are_validated() -> None:
    with pytest.raises(ValueError):
        RunnerSettings(env="prod", runner_token="short")
    with pytest.raises(ValueError):
        RunnerSettings(env="dev", sandbox_prefix="not-an-ip")
    assert RunnerSettings(env="prod", runner_token="x" * 32).sandbox_runtime == "runsc"

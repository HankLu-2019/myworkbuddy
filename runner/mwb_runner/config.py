"""Runner configuration, validated at startup (rule R14). Everything that shapes a sandbox comes from here, never
from a request (design 04 invariant 3)."""

import ipaddress
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RunnerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MWB_", extra="ignore")

    # Containers
    sandbox_image: str = "mwb-sandbox:latest"  # pin by digest in production (mwb-sandbox@sha256:...)
    sandbox_runtime: Literal["runsc", "runc"] = "runsc"
    sandbox_network: str = "mwb-sandbox"
    sandbox_prefix: str = "172.31.0"  # the sandbox network's /24; .2 is the proxy, .10–.250 are sandboxes
    proxy_container: str = "egress-proxy"  # name or id, for `squid -k reconfigure`
    proxy_jobs_dir: str = "/etc/squid/jobs"  # shared with the proxy

    # Limits: a run may ask for less, never more (04 §4).
    max_cpus: float = Field(4, gt=0, le=16)
    max_memory_gb: int = Field(8, ge=1, le=64)
    max_timeout_s: int = Field(2 * 60 * 60, ge=60)
    pids_limit: int = 512
    patch_max_bytes: int = 20 * 1024 * 1024
    disk_min_free_ratio: float = Field(0.15, ge=0, lt=1)

    # Service
    runner_token: str = Field("", min_length=0)
    env: Literal["prod", "dev"] = "prod"

    @model_validator(mode="after")
    def _check(self) -> "RunnerSettings":
        ipaddress.ip_network(f"{self.sandbox_prefix}.0/24")
        if self.env == "prod" and len(self.runner_token) < 32:
            raise ValueError("MWB_RUNNER_TOKEN must be at least 32 characters")
        return self

    @property
    def proxy_ip(self) -> str:
        return f"{self.sandbox_prefix}.2"


@lru_cache
def get_settings() -> RunnerSettings:
    return RunnerSettings()

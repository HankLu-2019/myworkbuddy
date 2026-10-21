from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MWB_")

    env: Literal["dev", "prod"] = "prod"
    hostname: str = ""  # public URL, e.g. https://mwb.example.com (MWB_HOSTNAME)
    tls_mode: Literal["files", "internal", "none"] = "files"
    database_url: str
    secret_key: str
    gateway_url: str = "http://llm-gateway:4000"
    gateway_key: str = ""
    chat_model: str = "chat"
    fast_model: str = "fast"
    embed_model: str = "embed"
    embed_dims: int = 1024
    data_dir: Path = Path("/data")
    cookie_secure: bool = True
    session_ttl_hours: int = 72
    login_max_attempts: int = 5
    login_window_minutes: int = 15
    log_level: str = "INFO"
    repo_max_files: int = 300
    repo_max_file_bytes: int = 200_000
    allow_http_repos: bool = False

    @model_validator(mode="after")
    def _production_safety(self) -> "Settings":
        """Refuse to start a production instance that would send cookies or passwords over plain HTTP."""
        if self.env != "prod":
            return self
        problems = []
        if not self.cookie_secure:
            problems.append("MWB_COOKIE_SECURE must be true")
        if urlparse(self.hostname).scheme != "https":
            problems.append("MWB_HOSTNAME must be an https:// URL")
        if self.tls_mode == "none":
            problems.append("MWB_TLS_MODE=none is only allowed with MWB_ENV=dev")
        if problems:
            raise ValueError("Unsafe production configuration: " + "; ".join(problems) + " (or set MWB_ENV=dev)")
        return self

    @property
    def session_cookie(self) -> str:
        # __Host- forces Secure, Path=/ and no Domain, so the cookie can't be set by a sibling subdomain.
        return "__Host-mwb_session" if self.cookie_secure else "mwb_session"

    @property
    def async_db_url(self) -> str:
        return self.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    @property
    def sync_db_url(self) -> str:
        return self.database_url.replace("postgresql://", "postgresql+psycopg://", 1)

    @property
    def wiki_dir(self) -> Path:
        return self.data_dir / "wiki"

    @property
    def repo_cache_dir(self) -> Path:
        return self.data_dir / "repos"


@lru_cache
def get_settings() -> Settings:
    return Settings()

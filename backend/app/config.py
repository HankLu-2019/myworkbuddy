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
    secret_keys: str = ""  # MWB_SECRET_KEYS: comma-separated Fernet keys, newest first
    secret_key: str = ""  # MWB_SECRET_KEY: legacy single key
    default_timezone: str = "Europe/Stockholm"
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
    ingest_max_summary_files: int = 150
    db_pool_size: int = 10
    db_statement_timeout_ms: int = 30_000
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
    def fernet_keys(self) -> list[str]:
        keys = [k.strip() for k in self.secret_keys.split(",") if k.strip()]
        return keys or ([self.secret_key] if self.secret_key else [])

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


def runtime_problems(s: Settings) -> list[str]:
    """Checks that need more than field validation (design 01 §H5). Empty list = OK."""
    import shutil
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from cryptography.fernet import Fernet

    from app.models import EMBED_DIMS

    problems = []
    if not s.fernet_keys:
        problems.append("Set MWB_SECRET_KEYS (or MWB_SECRET_KEY); generate one with `mwb generate-key`")
    for i, key in enumerate(s.fernet_keys, start=1):
        try:
            Fernet(key.encode())
        except ValueError:
            problems.append(f"MWB_SECRET_KEYS: entry {i} is not a valid Fernet key")
    if s.embed_dims != EMBED_DIMS:
        problems.append(
            f"MWB_EMBED_DIMS={s.embed_dims}, but the embedding dimension is fixed at {EMBED_DIMS} by the schema; "
            "changing it needs a migration"
        )
    try:
        ZoneInfo(s.default_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        problems.append(f"MWB_DEFAULT_TIMEZONE={s.default_timezone!r} is not a valid IANA time zone")
    try:
        s.data_dir.mkdir(parents=True, exist_ok=True)
        probe = s.data_dir / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        usage = shutil.disk_usage(s.data_dir)
        if usage.free / usage.total < 0.05:
            problems.append(f"{s.data_dir} has less than 5 % free space")
    except OSError as exc:
        problems.append(f"{s.data_dir} is not writable: {exc}")
    return problems


def ensure_runtime_ok() -> None:
    """Refuse to start with a clear message instead of failing later (rule R14)."""
    problems = runtime_problems(get_settings())
    if problems:
        raise SystemExit("MyWorkBuddy configuration problems:\n  - " + "\n  - ".join(problems))

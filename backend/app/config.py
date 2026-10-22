from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, model_validator
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
    planner_model: str = "planner"
    default_user_budget_usd: float = Field(50, ge=0)  # monthly, per user (design 01 §H12)
    embed_model: str = "embed"
    embed_dims: int = 1024
    data_dir: Path = Path("/data")
    cookie_secure: bool = True
    session_ttl_hours: int = Field(72, gt=0)
    session_idle_hours: int = Field(12, gt=0)
    login_max_attempts: int = 5  # per username and IP
    login_max_attempts_ip: int = 30
    login_max_attempts_user: int = 20
    login_window_minutes: int = 15
    log_level: str = "INFO"
    repo_max_files: int = 300
    ingest_max_summary_files: int = 150
    librarian_local_hour: int = Field(2, ge=0, le=23)  # local hour of the nightly librarian run (01 §H8)
    # Backups (design 06 §5). The repository is any restic repository URL or a path; RESTIC_PASSWORD and the
    # target's credentials come from the environment and are passed to restic only through it (rule R7).
    backup_repo: str = ""
    backup_time: str = Field("01:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    backup_timezone: str = ""  # default: MWB_DEFAULT_TIMEZONE
    backup_staging_dir: Path = Path("/backup/staging")
    backup_keep: str = "--keep-daily 14 --keep-weekly 8 --keep-monthly 6"
    db_pool_size: int = 10
    db_statement_timeout_ms: int = 30_000
    repo_max_file_bytes: int = 200_000
    repo_cache_max_gb: float = Field(50, gt=0)  # ingest refuses to clone above this (01 §H9)
    transcript_max_mb: int = Field(50, gt=0)
    allow_http_repos: bool = False
    # Integrations (design 02 §1): http:// base URLs only in dev and tests.
    allow_http_integrations: bool = False
    # Schedules (design 02 §4).
    schedule_min_interval_min: int = Field(15, ge=1)  # smallest gap between runs for members
    schedule_min_interval_admin_min: int = Field(5, ge=1)
    max_schedules_per_user: int = Field(20, ge=1)  # enabled schedules
    runs_retention_days: int = Field(180, ge=7)  # runs and their transcripts (02 §9)
    # Username whose AI budget the nightly agent evals use; empty: no nightly evals.
    agent_evals_user: str = ""
    # Hosts a plan may ask the Phase 4 sandbox to reach (ADR-12), comma-separated, e.g. "github.com,pypi.org".
    sandbox_optional_hosts: str = ""
    autoplan_per_day: int = Field(5, ge=0)  # plans the intake may auto-draft per user per day (03 §7)
    # Coding jobs (design 04). gVisor (`runsc`) unless the host can't run it; `runc` is flagged on the System page.
    sandbox_runtime: Literal["runsc", "runc"] = "runsc"
    runner_url: str = "http://runner:8000"
    runner_token: SecretStr = SecretStr("")  # shared with the runner; it also calls /internal/jobs/active
    job_budget_usd: float = Field(10, gt=0)  # per job's LLM key, at most the owner's remaining budget
    job_max_turns: int = Field(60, ge=1, le=500)
    job_max_changed_lines: int = Field(3000, ge=1)
    job_review_days: int = Field(7, ge=1)  # workspaces of jobs left in review are removed after this
    # Email (design 02 §5). No MWB_SMTP_HOST: emails stay queued and the System page says why.
    smtp_host: str = ""
    smtp_port: int = Field(587, ge=1, le=65535)
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""  # e.g. "MyWorkBuddy <mwb@example.com>"
    smtp_starttls: bool = True
    smtp_timeout_s: float = Field(30, gt=0)
    max_emails_per_day: int = Field(30, ge=1)  # per user, in their timezone
    # Read once by migration 0006 to move Phase 1 tokens into integrations; not used at runtime.
    gitlab_url: str = ""
    gerrit_url: str = ""
    jira_url: str = ""

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
        if self.smtp_user and not self.smtp_starttls:
            problems.append(
                "MWB_SMTP_STARTTLS must be true when MWB_SMTP_USER is set (the password would be sent in clear)"
            )
        if self.allow_http_integrations:
            problems.append("MWB_ALLOW_HTTP_INTEGRATIONS would send users' credentials in clear text")
        if problems:
            raise ValueError("Unsafe production configuration: " + "; ".join(problems) + " (or set MWB_ENV=dev)")
        return self

    @property
    def optional_hosts(self) -> set[str]:
        return {h.strip().lower() for h in self.sandbox_optional_hosts.split(",") if h.strip()}

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
    if s.smtp_host and "@" not in s.smtp_from:
        problems.append(
            "MWB_SMTP_FROM must be an address (e.g. MyWorkBuddy <mwb@example.com>) when MWB_SMTP_HOST is set"
        )
    if bool(s.smtp_user) != bool(s.smtp_password.get_secret_value()):
        problems.append("Set both MWB_SMTP_USER and MWB_SMTP_PASSWORD, or neither")
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

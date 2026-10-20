from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MWB_")

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

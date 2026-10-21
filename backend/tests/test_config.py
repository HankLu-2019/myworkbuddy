import pytest
from pydantic import ValidationError

from app.config import Settings

BASE = {"database_url": "postgresql://x@db/x", "secret_key": "k"}


def test_prod_requires_secure_cookie_https_and_tls() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(**BASE, env="prod", cookie_secure=False, hostname="http://mwb.local", tls_mode="none")
    msg = str(exc.value)
    assert "MWB_COOKIE_SECURE must be true" in msg
    assert "MWB_HOSTNAME must be an https:// URL" in msg
    assert "MWB_TLS_MODE=none" in msg


def test_valid_prod_and_relaxed_dev() -> None:
    prod = Settings(**BASE, env="prod", cookie_secure=True, hostname="https://mwb.example.com", tls_mode="internal")
    assert prod.session_cookie == "__Host-mwb_session"
    dev = Settings(**BASE, env="dev", cookie_secure=False, hostname="http://localhost:8080", tls_mode="none")
    assert dev.session_cookie == "mwb_session"

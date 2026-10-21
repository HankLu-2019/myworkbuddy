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


def test_runtime_problems_are_listed(tmp_path) -> None:
    from app.config import runtime_problems

    s = Settings(
        **{**BASE, "secret_key": ""},
        env="dev",
        secret_keys="not-a-key",
        embed_dims=1536,
        default_timezone="Mars/Olympus",
        data_dir=tmp_path,
    )
    problems = runtime_problems(s)
    assert any("entry 1 is not a valid Fernet key" in p for p in problems)
    assert any("MWB_EMBED_DIMS=1536" in p for p in problems)
    assert any("Mars/Olympus" in p for p in problems)


def test_runtime_ok_and_key_rotation_order(tmp_path) -> None:
    from cryptography.fernet import Fernet

    from app.config import runtime_problems

    new, old = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    s = Settings(**BASE, env="dev", secret_keys=f"{new}, {old}", data_dir=tmp_path)
    assert runtime_problems(s) == []
    assert s.fernet_keys == [new, old]
    assert Settings(**{**BASE, "secret_key": old}, env="dev").fernet_keys == [old]

import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from cryptography.fernet import Fernet

_TMP = Path(tempfile.mkdtemp(prefix="mwb-test-"))
_BASE_URL = os.environ.get("MWB_TEST_DATABASE_URL") or os.environ["MWB_DATABASE_URL"].rsplit("/", 1)[0] + "/mwb_test"
os.environ.update(
    MWB_DATABASE_URL=_BASE_URL,
    MWB_SECRET_KEY=Fernet.generate_key().decode(),
    MWB_DATA_DIR=str(_TMP),
    MWB_ENV="dev",
    MWB_COOKIE_SECURE="false",
    MWB_GATEWAY_URL="http://gateway.invalid",
    MWB_GATEWAY_KEY="sk-test-master",
)

import httpx2  # noqa: E402
import psycopg  # noqa: E402
import pytest  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from procrastinate.testing import InMemoryConnector  # noqa: E402
from pydantic_ai.models.test import TestModel  # noqa: E402
from sqlalchemy import text  # noqa: E402

from alembic import command  # noqa: E402
from app.auth.service import hash_password  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_engine, get_sessionmaker  # noqa: E402
from app.llm import client as llm_client  # noqa: E402
from app.llm import embeddings as llm_embeddings  # noqa: E402
from app.llm import keys as llm_keys  # noqa: E402
from app.llm.embeddings import HashEmbedder, set_embedder  # noqa: E402
from app.llm.models import set_model_override, set_override_wrapper  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402
from app.tasks.app import app as tasks_app  # noqa: E402
from tests import txguard  # noqa: E402
from tests.fakellm import server as fake_gateway  # noqa: E402

PASSWORD = "test-password-123"


def _recreate_database() -> None:
    admin_url, name = _BASE_URL.rsplit("/", 1)
    with psycopg.connect(f"{admin_url}/postgres", autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).parent.parent / "alembic"))
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session", autouse=True)
def database() -> None:
    _recreate_database()
    txguard.install(get_engine())
    set_override_wrapper(txguard.guard_model)
    yield
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def task_queue() -> InMemoryConnector:
    connector = InMemoryConnector()
    with tasks_app.replace_connector(connector):
        yield connector


@pytest.fixture(autouse=True)
async def clean(task_queue: InMemoryConnector) -> AsyncIterator[None]:
    set_embedder(txguard.GuardedEmbedder(HashEmbedder()))
    set_model_override(TestModel(call_tools=[], custom_output_text="ok"))
    # Key management and real-model tests talk to the in-process fake gateway, never the network.
    fake_gateway.KEYS.clear()
    fake_gateway.SPEND_LOGS.clear()
    llm_keys.set_transport(httpx2.ASGITransport(app=fake_gateway.app))
    llm_client.set_transport(httpx2.ASGITransport(app=fake_gateway.app))
    llm_embeddings.set_transport(httpx2.ASGITransport(app=fake_gateway.app))
    llm_embeddings._resolved = None  # the resolved embedding model is per process; tests start fresh
    llm_keys._info_cache.clear()
    txguard.reset()
    yield
    violations = list(txguard.violations)
    set_model_override(None)
    set_embedder(None)
    task_queue.reset()
    async with get_engine().begin() as conn:
        tables = (
            await conn.execute(
                text(
                    "SELECT string_agg(format('%I', tablename), ', ') FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename NOT LIKE 'procrastinate%' "
                    "AND tablename NOT IN ('alembic_version', 'spatial_ref_sys')"
                )
            )
        ).scalar()
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    shutil.rmtree(get_settings().data_dir / "wiki", ignore_errors=True)
    shutil.rmtree(get_settings().data_dir / "repos", ignore_errors=True)
    # Rule R1: no LLM or embedding call may run while a DB transaction is open (design 01 §H6).
    assert violations == [], violations


@pytest.fixture
def make_user() -> Callable:
    async def make(username: str, role: str = "member", email: str | None = None) -> User:
        async with get_sessionmaker()() as db:
            user = User(
                username=username,
                role=role,
                email=email or f"{username}@example.com",
                password_hash=hash_password(PASSWORD),
            )
            db.add(user)
            await db.commit()
            return user

    return make


class Client(AsyncClient):
    csrf: str = ""
    user_id: int = 0

    async def login(self, username: str, password: str = PASSWORD) -> "Client":
        resp = await self.post("/api/auth/login", json={"username": username, "password": password})
        resp.raise_for_status()
        self.csrf = resp.json()["csrf_token"]
        self.user_id = resp.json()["user"]["id"]
        self.headers["X-CSRF-Token"] = self.csrf
        return self


@pytest.fixture
def client_for(make_user: Callable) -> Callable:
    clients: list[Client] = []

    async def make(username: str, role: str = "member", create: bool = True) -> Client:
        if create:
            await make_user(username, role)
        c = Client(transport=ASGITransport(app=app), base_url="http://test")
        clients.append(c)
        return await c.login(username)

    return make


@pytest.fixture
async def anon() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c

"""Recorded connector responses replayed through httpx2.MockTransport (respx doesn't patch httpx2).

`api` (a fixture in conftest) installs a Recorder: `api.on(path, response, ...)` queues responses for a
percent-encoded path; `api.requests` and `api.waits` show what was asked and how long retries would have slept.
"""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx2
import yaml
from sqlalchemy import update

from app.db import get_sessionmaker
from app.integrations.service import set_credential
from app.models import Integration, UserCredential

K8S = "https://k8s.example.com:6443"


FIXTURES = Path(__file__).parent / "fixtures" / "connectors"


def fixture(kind: str, name: str) -> bytes:
    now = datetime.now(UTC)
    text = (FIXTURES / kind / name).read_text()
    text = text.replace("__RECENT__", (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"))
    text = text.replace("__RFC822_RECENT__", format_datetime(now - timedelta(hours=2)))
    text = text.replace("__ISO_RECENT__", (now - timedelta(hours=1)).isoformat())
    return text.encode()


class Recorder:
    """Routes requests by path to queued responses; records what was asked."""

    def __init__(self) -> None:
        self.routes: dict[str, list[httpx2.Response]] = {}
        self.requests: list[httpx2.Request] = []

    def on(self, path: str, *responses: httpx2.Response) -> "Recorder":
        self.routes.setdefault(path, []).extend(responses)
        return self

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.raw_path.split(b"?")[0].decode()  # still percent-encoded: team%2Fetl
        queue = self.routes.get(path)
        if not queue:
            return httpx2.Response(599, text=f"unrecorded {path}")
        return queue.pop(0) if len(queue) > 1 else queue[0]


def ok(body: bytes | dict | list, status: int = 200, **headers: str) -> httpx2.Response:
    if isinstance(body, bytes):
        return httpx2.Response(status, content=body, headers=headers)
    return httpx2.Response(status, json=body, headers=headers)


async def setup_integration(
    make_user, kind: str, base_url: str, creds: dict[str, str], settings: dict | None = None, status: str = "ok"
):
    user = await make_user("alice")
    async with get_sessionmaker()() as db:
        from app.integrations.kinds import validate_settings

        integration = Integration(
            kind=kind, name=kind.capitalize(), base_url=base_url, settings=validate_settings(kind, settings)
        )
        db.add(integration)
        await db.flush()
        for k, v in creds.items():
            await set_credential(db, user.id, integration, k, v)
        await db.flush()
        await db.execute(
            update(UserCredential).where(UserCredential.integration_id == integration.id).values(status=status)
        )
        await db.commit()
        return user, integration.id


def kubeconfig() -> str:
    return yaml.safe_dump(
        {
            "current-context": "ro",
            "clusters": [{"name": "c", "cluster": {"server": K8S}}],
            "users": [{"name": "u", "user": {"token": "sa-token"}}],
            "contexts": [{"name": "ro", "context": {"cluster": "c", "user": "u"}}],
        }
    )

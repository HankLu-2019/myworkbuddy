"""The fake Jira, Git server and planner reply the end-to-end flow 07 §5.8 relies on (tests/fakellm/forge.py)."""

import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from pydantic_ai import Agent

from app.plans.schema import PlanDoc
from app.plans.validation import validate_plan
from tests.fakellm import forge
from tests.fakellm.server import app
from tests.test_fakellm import _model


async def test_the_planner_reply_is_a_valid_plan_for_the_fake_repository() -> None:
    prompt = "Plan ticket PAY-101 for repository payments, branch main at 0123456789ab."
    doc = (await Agent(_model(), output_type=PlanDoc).run(prompt)).output
    clean, result = validate_plan(
        doc, jira_key="PAY-101", existing_paths=set(forge.FILES), visible_refs=[], optional_hosts=[]
    )
    assert result.ok and result.needs_ack == ["ci_config:.gitlab-ci.yml"]
    assert clean.commit_message == "PAY-101 Retry webhook delivery on 5xx" and len(clean.tests) == 1

    revised = PlanDoc.model_validate(forge.plan(f"{prompt}\n\n## The user's feedback on it\nGive up after three."))
    assert len(revised.tests) == 2 and revised.approach.endswith("Revised after your feedback: Give up after three.")


async def test_fake_jira() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fake") as c:
        assert (await c.get("/rest/api/2/myself")).status_code == 401
        auth = {"Authorization": "Bearer jira-e2e"}
        assert (await c.get("/rest/api/2/myself", headers=auth)).json()["displayName"] == "E2E User"
        issue = (await c.get("/rest/api/2/issue/PROJ-123", headers=auth)).json()
        assert issue["key"] == "PROJ-123" and "503" in issue["fields"]["description"]
        assert (await c.get("/rest/api/2/issue/nope", headers=auth)).status_code == 404
        assert (await c.get("/rest/api/2/search", headers=auth)).json()["total"] == 1


def test_fake_git_server_serves_shallow_clones_and_refuses_pushes(tmp_path: Path) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        url = f"http://127.0.0.1:{port}/fake/git/team/payments.git"
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run([*git, "clone", "-q", "--depth=1", "--single-branch", url, "c"], cwd=tmp_path, check=True)
        assert (tmp_path / "c" / ".gitlab-ci.yml").exists() and (tmp_path / "c" / "app" / "webhooks.py").exists()
        branch = subprocess.run([*git, "branch", "--show-current"], cwd=tmp_path / "c", capture_output=True, text=True)
        assert branch.stdout.strip() == "main"
        subprocess.run([*git, "fetch", "-q", "--depth=1", "origin", "HEAD"], cwd=tmp_path / "c", check=True)
        (tmp_path / "c" / "x").write_text("x")
        subprocess.run([*git, "add", "x"], cwd=tmp_path / "c", check=True)
        subprocess.run([*git, "commit", "-qm", "x"], cwd=tmp_path / "c", check=True)
        push = subprocess.run([*git, "push", "-q", "origin", "HEAD:main"], cwd=tmp_path / "c", capture_output=True)
        assert push.returncode != 0
    finally:
        server.should_exit = True
        thread.join(5)

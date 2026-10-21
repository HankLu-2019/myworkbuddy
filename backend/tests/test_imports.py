"""Every module imports on its own, in a fresh interpreter (the worker imports task modules lazily, so an import
cycle that the API's import order hides breaks scheduled runs; this caught one in S6)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
# Entry points: what the API, the worker's tasks and the CLI import first.
MODULES = [
    "app.main",
    "app.tasks.app",
    "app.cli",
    "app.schedules.service",
    "app.schedules.proposals",
    "app.schedules.validation",
    "app.agents.executor",
    "app.agents.seeds",
    "app.tools.registry",
    "app.tools.catalog",
    "app.chat.agent",
    "app.inbox.sender",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_first(module: str) -> None:
    env = {**os.environ, "MWB_ENV": "dev"}
    r = subprocess.run([sys.executable, "-c", f"import {module}"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]

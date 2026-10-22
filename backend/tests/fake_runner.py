"""An in-process stand-in for the runner (design 04 §4) for pipeline tests: workspaces are temp directories, the
verify preset runs the real sandbox/mwb-verify on them, patches come from real git, and the agent preset runs a
scripted behaviour (by default the same as the fake LLM's agent: change the first planned file, write notes).

Agent behaviours are callables `(workspace: Path, env: dict) -> (exit_code, stdout_lines)`, or the strings
"hang" (runs until killed), "killed" (the runner's timeout) and "disk_limit" (killed for its workspace size).
"""

import asyncio
import importlib.machinery
import importlib.util
import io
import json
import re
import shutil
import subprocess
import tarfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

ROOT = Path(__file__).resolve().parents[2]
_loader = importlib.machinery.SourceFileLoader("mwb_verify", str(ROOT / "sandbox" / "mwb-verify"))
_spec = importlib.util.spec_from_loader("mwb_verify", _loader)
mwb_verify = importlib.util.module_from_spec(_spec)
_loader.exec_module(mwb_verify)
PLANNED = re.compile(r"^- (create|modify) `([^`]+)`", re.M)


def stream(*events: dict) -> list[str]:
    return [json.dumps(e) for e in events]


def tool(command: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": command}}]},
    }


def text(t: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}}


def result(ok: bool = True, message: str = "Done.") -> dict:
    return {"type": "result", "subtype": "success", "is_error": not ok, "num_turns": 3, "total_cost_usd": 0.01,
            "result": message}  # fmt: skip


def planned_change(workspace: Path, env: dict) -> tuple[int, list[str]]:
    task = (workspace / ".mwb" / "TASK.md").read_text()
    path = PLANNED.findall(task)[0][1]
    target = workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a") as f:
        f.write("\n# Retry transient failures (fake agent)\n")
    (workspace / ".mwb" / "notes.md").write_text(f"Changed {path} as the task asked.\n")
    return 0, stream(tool("cat .mwb/TASK.md"), text(f"I'll change {path}."), tool(f"printf >> {path}"), result())


class FakeRunner:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.workspaces: dict[str, Path] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.agent: list[Any] = []  # behaviours, used in order; planned_change when empty
        self.calls: list[tuple[str, str]] = []
        self.app = self._build()

    def ws(self, job_id: str) -> Path:
        return self.workspaces[job_id]

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.middleware("http")
        async def record(request: Request, call_next):
            self.calls.append((request.method, request.url.path))
            assert request.headers.get("authorization", "").startswith("Bearer "), "runner token missing"
            return await call_next(request)

        @app.post("/v1/workspaces/{job_id}", status_code=201)
        async def create(job_id: str):
            path = self.root / f"ws-{job_id}"
            path.mkdir(parents=True, exist_ok=True)
            self.workspaces[job_id] = path
            return {"job_id": job_id}

        @app.put("/v1/workspaces/{job_id}/content")
        async def content(job_id: str, request: Request):
            if job_id not in self.workspaces:
                return JSONResponse({"detail": "no workspace", "code": "not_found"}, status_code=404)
            with tarfile.open(fileobj=io.BytesIO(await request.body())) as t:
                members = t.getmembers()
                t.extractall(self.workspaces[job_id], filter="data")
            return {"entries": len(members)}

        @app.get("/v1/workspaces/{job_id}/patch")
        async def patch(job_id: str, base_sha: str):
            ws = self.ws(job_id)
            subprocess.run(["git", "add", "-A"], cwd=ws, check=True, capture_output=True)
            out = subprocess.run(
                ["git", "diff", "--cached", "--binary", base_sha], cwd=ws, capture_output=True, check=True
            )
            return Response(out.stdout, media_type="text/x-diff")

        @app.get("/v1/workspaces/{job_id}/mwb/{name:path}")
        async def read(job_id: str, name: str):
            f = self.ws(job_id) / ".mwb" / name
            if not f.is_file():
                return JSONResponse({"detail": "missing", "code": "not_found"}, status_code=404)
            return Response(f.read_bytes())

        @app.delete("/v1/workspaces/{job_id}", status_code=204)
        async def delete(job_id: str):
            path = self.workspaces.pop(job_id, None)
            if path:
                shutil.rmtree(path, ignore_errors=True)
            return Response(status_code=204)

        @app.post("/v1/runs", status_code=201)
        async def start(request: Request):
            body = await request.json()
            run_id = uuid.uuid4().hex[:16]
            ws = self.ws(body["job_id"])
            run: dict[str, Any] = {"body": body, "state": "running", "exit_code": None, "lines": []}
            self.runs[run_id] = run
            if body["preset"] == "verify":
                code = mwb_verify.run(str(ws / ".mwb" / "verify.json"), str(ws))
                run.update(state="exited", exit_code=code)
            else:
                behaviour = self.agent.pop(0) if self.agent else planned_change
                if behaviour == "hang":
                    run["hang"] = asyncio.Event()
                elif behaviour == "killed":
                    run.update(state="killed", exit_code=137, reason="timeout")
                elif behaviour == "disk_limit":
                    run.update(state="killed", exit_code=137, reason="disk_limit")
                else:
                    code, lines = behaviour(ws, body["env"])
                    run.update(state="exited", exit_code=code, lines=lines)
            return {"run_id": run_id}

        @app.get("/v1/runs/{run_id}")
        async def status(run_id: str):
            run = self.runs.get(run_id)
            if run is None:
                return JSONResponse({"detail": "unknown", "code": "not_found"}, status_code=404)
            return {"run_id": run_id, "state": run["state"], "exit_code": run["exit_code"], "oom_killed": False,
                    "reason": run.get("reason")}  # fmt: skip

        @app.get("/v1/runs/{run_id}/logs")
        async def logs(run_id: str, follow: bool = False):
            run = self.runs[run_id]

            async def gen():
                for line in run["lines"]:
                    yield json.dumps({"stream": "stdout", "line": line}) + "\n"
                yield json.dumps({"stream": "stderr", "line": "[claude-code:unrecognized_model]"}) + "\n"
                if follow and "hang" in run:
                    await run["hang"].wait()

            return StreamingResponse(gen(), media_type="application/x-ndjson")

        @app.delete("/v1/runs/{run_id}", status_code=204)
        async def kill(run_id: str, remove: bool = False):
            run = self.runs.get(run_id)
            if run is None:
                return JSONResponse({"detail": "unknown", "code": "not_found"}, status_code=404)
            if run["state"] == "running":
                run.update(state="killed", exit_code=137, reason="requested")
                if "hang" in run:
                    run["hang"].set()
            if remove:
                self.runs.pop(run_id)
            return Response(status_code=204)

        return app

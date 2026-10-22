"""The runner's internal HTTP API (design 04 §4). Bearer `MWB_RUNNER_TOKEN` on every route but none other; it
listens only on the compose network, never through Caddy."""

import logging
import secrets
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from mwb_runner.config import RunnerSettings, get_settings
from mwb_runner.presets import RunRejected, RunRequest
from mwb_runner.service import NotFound, Runner
from mwb_runner.tarsafe import UnsafeArchive

log = logging.getLogger("mwb_runner")
bearer = HTTPBearer(auto_error=False)
_runner: Runner | None = None


def set_runner(runner: Runner | None) -> None:
    global _runner
    _runner = runner


def runner() -> Runner:
    global _runner
    if _runner is None:
        from mwb_runner.docker_ops import DockerOps

        _runner = Runner(DockerOps(), get_settings())
    return _runner


def authorized(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
    token = get_settings().runner_token
    if not token or creds is None or not secrets.compare_digest(creds.credentials.encode(), token.encode()):
        raise HTTPException(401, "Missing or wrong runner token.")


def active_jobs(settings: RunnerSettings) -> set[str] | None:
    """The API's active jobs, or None when it can't be asked (then nothing is removed)."""
    try:
        r = httpx.get(f"{settings.api_url}/internal/jobs/active", timeout=10,
                      headers={"Authorization": f"Bearer {settings.runner_token}"})  # fmt: skip
        r.raise_for_status()
        return {str(j) for j in r.json()["jobs"]}
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        log.warning("orphan sweep: can't ask the API for active jobs: %s", exc)
        return None


def _sweeper(stop: threading.Event) -> None:
    settings = get_settings()
    while True:
        try:
            report = runner().sweep(active_jobs(settings))
            if any(report.values()):
                log.info("sweep: %s", report)
        except Exception:  # noqa: BLE001 - the sweeper must keep going
            log.exception("sweep failed")
        if stop.wait(settings.sweep_interval_s):
            return


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = threading.Event()
    if get_settings().sweep_interval_s and not getattr(app.state, "no_sweeper", False):
        threading.Thread(target=_sweeper, args=(stop,), daemon=True, name="sweeper").start()
    yield
    stop.set()


app = FastAPI(title="mwb-runner", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(RunRejected)
async def _rejected(_: Request, exc: RunRejected) -> JSONResponse:
    return JSONResponse({"detail": exc.message, "code": exc.code}, status_code=422)


@app.exception_handler(UnsafeArchive)
async def _unsafe(_: Request, exc: UnsafeArchive) -> JSONResponse:
    return JSONResponse({"detail": str(exc), "code": "unsafe_archive"}, status_code=422)


@app.exception_handler(NotFound)
async def _missing(_: Request, exc: NotFound) -> JSONResponse:
    return JSONResponse({"detail": str(exc), "code": "not_found"}, status_code=404)


auth = [Depends(authorized)]


@app.get("/v1/health", dependencies=auth)
async def health() -> JSONResponse:
    report = await run_in_threadpool(runner().health)
    return JSONResponse(report, status_code=200 if report["ok"] else 503)


@app.post("/v1/workspaces/{job_id}", status_code=201, dependencies=auth)
async def create_workspace(job_id: uuid.UUID) -> dict[str, str]:
    await run_in_threadpool(runner().create_workspace, job_id)
    return {"job_id": str(job_id)}


@app.put("/v1/workspaces/{job_id}/content", dependencies=auth)
async def put_content(job_id: uuid.UUID, request: Request) -> dict[str, int]:
    limit = get_settings().upload_max_bytes
    with tempfile.TemporaryFile() as body:
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise RunRejected("too_large", "The upload is too large.")
            body.write(chunk)
        body.seek(0)
        count = await run_in_threadpool(runner().put_content, job_id, body)
    return {"entries": count}


@app.get("/v1/workspaces/{job_id}/patch", dependencies=auth)
async def patch(job_id: uuid.UUID, base_sha: str = Query(...)) -> Response:
    data = await run_in_threadpool(runner().patch, job_id, base_sha)
    return Response(data, media_type="text/x-diff")


@app.delete("/v1/workspaces/{job_id}", status_code=204, dependencies=auth)
async def delete_workspace(job_id: uuid.UUID) -> Response:
    await run_in_threadpool(runner().delete_workspace, job_id)
    return Response(status_code=204)


@app.post("/v1/runs", status_code=201, dependencies=auth)
async def start_run(req: RunRequest) -> dict[str, str]:
    return {"run_id": await run_in_threadpool(runner().start_run, req)}


@app.get("/v1/runs/{run_id}", dependencies=auth)
async def run_status(run_id: str) -> dict:
    return await run_in_threadpool(runner().run_status, run_id)


@app.get("/v1/runs/{run_id}/logs", dependencies=auth)
async def run_logs(run_id: str, follow: bool = False) -> StreamingResponse:
    r = runner()
    r._container(run_id)  # 404 before the stream starts
    return StreamingResponse(r.run_logs(run_id, follow=follow), media_type="application/x-ndjson")


@app.delete("/v1/runs/{run_id}", status_code=204, dependencies=auth)
async def kill_run(run_id: str, remove: bool = False) -> Response:
    r = runner()
    await run_in_threadpool(r.remove_run if remove else r.kill_run, run_id)
    return Response(status_code=204)

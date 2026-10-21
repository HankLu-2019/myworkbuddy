import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from sqlalchemy import text

from app import errors
from app.auth.routes import router as auth_router
from app.chat.routes import router as chat_router
from app.config import ensure_runtime_ok, get_settings
from app.db import get_engine
from app.knowledge.routes import router as knowledge_router
from app.llm.routes import router as usage_router
from app.logs import log, setup_logging
from app.ops.routes import router as ops_router
from app.secrets.routes import router as secrets_router
from app.tasks.app import app as tasks_app
from app.users.routes import router as users_router


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    setup_logging(get_settings().log_level)
    ensure_runtime_ok()
    async with tasks_app.open_async():
        log.info("api.started")
        yield
    await get_engine().dispose()


app = FastAPI(title="MyWorkBuddy", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
errors.install(app)


@app.middleware("http")
async def request_context(request: Request, call_next) -> Response:
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    if request.url.path != "/api/health":
        log.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            ms=round((time.perf_counter() - start) * 1000, 1),
        )
    return response


@app.get("/api/health")
async def health() -> dict[str, str]:
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok"}


for r in (auth_router, users_router, secrets_router, chat_router, knowledge_router, usage_router, ops_router):
    app.include_router(r)

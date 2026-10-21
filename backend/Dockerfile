# syntax=docker/dockerfile:1
# Stages: base -> deps -> prod (api, worker, migrate)  |  prod -> dev (dev override, CI tests)

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS base
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1
WORKDIR /app

FROM base AS deps
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM deps AS prod
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
RUN uv sync --frozen --no-dev && mkdir -p /data && chown -R app:app /data
USER app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM prod AS dev
USER root
RUN uv sync --frozen --group dev
COPY tests ./tests
RUN chown -R app:app /app
USER app

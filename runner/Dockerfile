# The runner (design 04 §1, §4): the only service with the Docker socket. Internal API on the compose network.
FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH=/opt/venv/bin:$PATH
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY mwb_runner ./mwb_runner
RUN uv sync --frozen --no-dev
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --retries=3 CMD python -c "import os,urllib.request as u; u.urlopen(u.Request('http://localhost:8000/v1/health', headers={'Authorization': 'Bearer ' + os.environ['MWB_RUNNER_TOKEN']}), timeout=8)"
CMD ["uvicorn", "mwb_runner.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

# The coding sandbox (design 04 §1): Claude Code and the verifier run here, as uid 1000, with no credentials.
# Built in CI; the runner starts it only with its fixed presets (runner/mwb_runner/presets.py).
FROM node:22.20.0-bookworm-slim

ARG CLAUDE_CODE_VERSION=2.1.293
ARG GO_VERSION=1.23.12
ARG UV_VERSION=0.8.22
ARG PYTHON_VERSION=3.12

# Build tools and git. No SSH client, no credential helpers.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git build-essential pkg-config procps less \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system init.defaultBranch main \
 && git config --system advice.detachedHead false

# Python 3.12 (managed by uv, shared in /opt) and uv itself.
RUN curl -fsSL "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz" \
    | tar -xz -C /usr/local/bin --strip-components=1 \
 && UV_PYTHON_INSTALL_DIR=/opt/python uv python install "${PYTHON_VERSION}" \
 && ln -s "$(UV_PYTHON_INSTALL_DIR=/opt/python uv python find ${PYTHON_VERSION})" /usr/local/bin/python3 \
 && ln -s /usr/local/bin/python3 /usr/local/bin/python \
 && python3 -m ensurepip --upgrade >/dev/null && python3 -m pip --version

# Go, pinned.
RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" | tar -xz -C /usr/local
ENV PATH=/usr/local/go/bin:$PATH

# Claude Code, pinned to an exact version (04 §4: flags are checked against this version).
RUN npm install -g --no-audit --no-fund "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && claude --version

# The node image's uid 1000 becomes `agent`, home on a tmpfs at run time.
RUN usermod -l agent -d /home/agent -m node && groupmod -n agent node \
 && mkdir -p /workspace && chown agent:agent /workspace

COPY mwb-verify /usr/local/bin/mwb-verify
RUN chmod 0755 /usr/local/bin/mwb-verify

ENV HOME=/home/agent \
    GOPATH=/home/agent/go \
    GOCACHE=/home/agent/.cache/go-build \
    npm_config_cache=/home/agent/.npm \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_CACHE_DIR=/home/agent/.cache/uv
USER 1000:1000
WORKDIR /workspace
CMD ["sh"]

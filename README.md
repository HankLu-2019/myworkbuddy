# MyWorkBuddy

A self-hosted web workbench for a small engineering team, inspired by Tencent WorkBuddy. It remembers each person's work (repositories, stack, past sessions), answers from that knowledge with citations, watches CI, Kubernetes and Jira on a schedule, and takes a Jira ticket through an approved plan and a sandboxed coding agent to a merge request, with a human approval before anything leaves the system.

- **Try it:** [docs/getting-started.html](docs/getting-started.html), a step-by-step tour on your own VM (fake mode first: no credentials needed)
- Implementation plan with acceptance criteria: [docs/implementation-plan.md](docs/implementation-plan.md)
- Detailed design (architecture, rules, every phase, security, operations): [docs/design/](docs/design/00-architecture.md); running it: [06-operations](docs/design/06-operations.md)
- Design review of 2026-10-21: [docs/review/](docs/review/2026-10-21-design-review.md)
- Rules for Claude Code when implementing: [CLAUDE.md](CLAUDE.md)

## Status

**All planned phases are built:** 0–1 (knowledge and chat), H (hardening), 2 (schedules and connectors), 3 (Jira → plan), 4 (sandboxed execution → merge request) and 5 (follow-through and team rollout), plus Vault for stored secrets. CI runs the backend, frontend, runner, sandbox security and end-to-end tests against deterministic fakes.

Still to do by hand, on the VM: each phase's end-to-end checklist with real Bedrock, GitLab/Gerrit and Jira (listed in the plan), gVisor (`runsc`), Sysbox for verification with containers, SSO against the company's identity provider, and Vault init/unseal.

## What it does

**Knowledge and chat**
- Streaming chat (Pydantic AI through a LiteLLM gateway to Bedrock or any OpenAI-compatible API), with citations
- Knowledge is **private by default**; only items explicitly shared are visible to the team
- Hybrid search: pgvector (HNSW) + ParadeDB BM25, fused with reciprocal rank fusion
- Repository ingestion for GitLab and Gerrit (tree-sitter code chunks for Python and Go, docs, an LLM-written project card), documents (PDF, Markdown, text), Claude Code transcripts
- A git-backed wiki (private and team), session summaries, and a nightly librarian that keeps `me/how-i-work.md`
- Merged changes become knowledge the planner reuses for the same repository

**Schedules**
- Kubernetes health, CI failures, Jira digest and a morning brief, on cron in your time zone; findings with verified fingerprints; notify only on change (inbox, email, Slack or Teams)
- Propose a schedule from chat; investigate a run in chat; turn a CI failure into a Jira ticket

**Ticket to merge request**
- A Board of your Jira tickets; a planner drafts a plan (files, tests, verification, risks) that's validated against the repository and its conventions; revise with feedback; approve the exact revision
- A coding agent (Claude Code) works in a gVisor sandbox with no credentials, a read-only root and an egress proxy; the plan's verification commands run; checks for secrets, size, protected paths
- Review the diff, request changes, answer the agent's questions, then push a branch and open a merge request (GitLab) or change (Gerrit) with a Jira comment, each bound to an approval of the exact payload
- After the push: pipeline watch and **Fix CI failure**, **Address review comments** with approved replies, retry a failed push, tracking to merge
- Optional: verification with containers (Sysbox) for tests that need a database

**Team rollout**
- Username/password with argon2id, server-side sessions, CSRF and rate limits; single sign-on with OpenID Connect
- Admin-managed integrations (GitLab, Gerrit, Jira, Jenkins, Kubernetes, feeds) on any self-hosted URL, with per-user, write-only, tested credentials
- Stored secrets encrypted with a key in `.env`, or under HashiCorp Vault Transit
- Insights: outcomes, times and AI cost per merged change; per-user AI budgets
- Admin-registered MCP servers, read-only tools only
- Audit log, backups with restic and a restore drill, a System page

## Architecture

| Service | Role |
|---|---|
| `web` | React + TypeScript (Vite) SPA; nginx in prod, Vite dev server with hot reload in dev |
| `api` | FastAPI: auth, REST, SSE streaming |
| `migrate` | One-shot: `mwb db-bootstrap` and Alembic migrations, before `api` and the workers start |
| `worker`, `worker-jobs` | Procrastinate (Postgres-backed queue and cron): ingestion, schedules, librarian, email and chat notifications; `worker-jobs` runs the job pipeline |
| `runner` | Starts sandboxes from fixed presets; the only service with the Docker socket |
| `egress-proxy` | Squid: the sandboxes' only way out, per-run allowlists |
| `postgres` | ParadeDB (Postgres 17 + pgvector + pg_search) |
| `llm-gateway` | LiteLLM proxy: model aliases, per-user keys and budgets |
| `caddy` | The only published service: TLS, security headers |
| `backup` | restic backups |
| `vault` | Optional (profile `vault`): HashiCorp Vault for the secrets key |
| `mailpit` | Dev only: captures email |

## Quick start

**Fake mode** (no credentials; the AI model, GitLab, Jira, a feed and an MCP server are deterministic stand-ins):

```bash
cp .env.example .env && chmod 600 .env    # then fill it in: docs/getting-started.html step 1.2 has a one-liner
docker build -t mwb-sandbox:latest sandbox
docker compose -f docker-compose.yml -f compose.ci.yml up -d --build --wait
docker compose -f docker-compose.yml -f compose.ci.yml exec api mwb create-admin --username admin --email you@example.com
```

Open http://localhost:8080 (over an SSH tunnel from your laptop: `ssh -L 8080:localhost:8080 you@vm`).

**Real mode:** set the Bedrock (or OpenAI-compatible) settings, `MWB_HOSTNAME` with TLS, and `MWB_SANDBOX_RUNTIME=runsc` with gVisor installed, then `docker compose -f docker-compose.yml up -d --build --wait`. Full steps: [06-operations](docs/design/06-operations.md). For development with hot reload: `docker compose up -d --build` (adds `compose.override.yml` and mailpit).

Logs are structured JSON: `docker compose logs -f api worker worker-jobs`.

## Tests

```bash
# Backend (dev stack: uses a separate mwb_test database; no real LLM calls)
docker compose exec api sh -c "cd /app && pytest --cov=app"
docker compose exec api ruff check . && docker compose exec api ruff format --check .

# Frontend
cd frontend && npm ci && npx tsc -b && npx vitest run && npm run build

# Runner
cd runner && uv run pytest
```

Currently about 630 backend tests (94 % coverage), 255 frontend tests and 47 runner tests, plus the Docker-backed sandbox security tests and Playwright end-to-end flows in CI.

## Notes

- `tree-sitter` is pinned below 0.26: 0.26.0 returns corrupted nodes and segfaults on some class bodies.
- Repository ingestion never follows symlinks or reads outside the checkout.
- Caddy overwrites `X-Forwarded-For` and uvicorn trusts it only from Caddy's fixed address, so the login rate limit can't be bypassed with a forged header.
- `pg_search` (ParadeDB) is AGPL-licensed, which is fine for internal use.

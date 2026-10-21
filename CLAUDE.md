# MyWorkBuddy — instructions for Claude Code

Self-hosted engineering workbench: FastAPI + SQLAlchemy async + Procrastinate (backend), React + Vite + TanStack Query (frontend), ParadeDB Postgres, LiteLLM gateway. Docker Compose on one VM.

## Before you change anything

1. Read `docs/design/00-architecture.md` (requirements, topology, rules, decisions).
2. Find your task in `docs/implementation-plan.md` and read its design section.
3. If the design is wrong or missing something, update the design doc in the same PR and say why. Don't silently diverge.

## Rules (from design/00 §3 — mandatory)

- **R1** No network call (LLM, embeddings, git remote, HTTP) inside an open DB transaction. Read → close → compute → short write transaction that re-checks what it read.
- **R2** Knowledge access only through `app/knowledge/acl.py`. New owned tables get `owner_id` + `visibility` + leakage tests.
- **R3** State-changing endpoints: auth + CSRF + `audit()` in the same transaction.
- **R4** Tasks are idempotent; `lock=` per entity, `queueing_lock=` only to de-duplicate queued work.
- **R5** External writes (push, MR, Jira comment, K8s change) only with an `approvals` row matching the exact payload hash.
- **R6** Untrusted text goes through `app/llm/untrusted.wrap()`; never into system prompts. Runs that read untrusted text get no external-write tools.
- **R7** Secrets never in logs, prompts, argv, URLs, exceptions or the sandbox. Git credentials via `GIT_CONFIG_*` env.
- **R8** UTC `timestamptz` in storage; user timezone (`users.timezone`) for anything user-facing.
- **R9** Enums: `StrEnum` + DB `CHECK` + API `Literal`.
- **R10** Migrations forward-only, expand/contract, tested from the previous revision with data.
- **R11** All LLM calls through `app/llm/client.py` with the user's gateway key, `UsageLimits`, timeout, cost recorded.
- **R12** API errors: `{"detail", "code"}` via `app/errors.py`.
- **R13** Unbounded lists use cursor pagination.
- **R14** Config validated at startup.
- **R15** Tests ship with the task; CI green; backend coverage ≥ 80 %.

LLM calls: build models with `app.llm.client.model_for(user_id, alias)`, run agents with `app.llm.client.run(..., purpose=)`, and embed with `embedder_for(user_id)`; `tests/test_llm_gateway.py` fails on any other `OpenAIProvider(`, `OpenAIChatModel(` or `agent.run(`. Wrap untrusted text with `app.llm.untrusted.wrap` and keep `UNTRUSTED_NOTICE` in every system prompt.

Frontend: follow `docs/design/07-ux.md` (navigation, page specs, flows, shared states, wording glossary, accessibility). Use the shared components; don't style one-off buttons, tables or dialogs. Render Markdown only with `components/ui.tsx` `Markdown` (no remote images, no raw HTML). Never `dangerouslySetInnerHTML`.

## Commands

```bash
# dev stack (hot reload, mailpit)
docker compose up -d --build

# backend tests (separate mwb_test DB in the same Postgres; no real LLM calls)
docker compose exec api sh -c "cd /app && pytest --cov=app"
docker compose exec api ruff check . && docker compose exec api ruff format --check .

# frontend
cd frontend && npm ci && npx tsc -b && npx vitest run && npm run build   # `npm run typecheck` after H1
```

## Testing conventions

- LLM: `app.llm.models.set_model_override(TestModel/FunctionModel)`; embeddings: `HashEmbedder`. Never call Bedrock in tests.
- External HTTP: `respx` with recorded fixtures under `backend/tests/fixtures/`.
- Every acceptance criterion in the plan becomes at least one test.
- Playwright: wait for the saved state with roles or `exact: true` text. `getByText` matches case-insensitive substrings, so a button label can satisfy a wait before the save has landed.

## Workflow

Implement one task → `/code-review` → fix → full tests → `/code-review` again → PR with the task ticked in `docs/implementation-plan.md`.

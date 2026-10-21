# 02 — Phase 2: connectors, agents, schedules, inbox and email

Status: approved baseline, 2026-10-21. Depends on Phase H being complete.

Goal: users get recurring, read-only checks (morning brief, Kubernetes health, failed pipelines, Jira digest) delivered to an in-app inbox and email, and can create them from chat with one confirmation.

Phase 2 adds **no external-write capability**. Every tool in this phase is tier `read`.

## 1. Integrations and credentials

An **integration** is one external system instance, configured by an admin. A **credential** is one user's secret for one integration.

```sql
create table integrations (
  id          int generated always as identity primary key,
  kind        text not null check (kind in ('jira','gitlab','gerrit','jenkins','kubernetes','feed')),
  name        text not null unique,                 -- "Jira", "GitLab", "Prod cluster"
  base_url    text not null,                        -- https only (http allowed when MWB_ALLOW_HTTP_INTEGRATIONS)
  settings    jsonb not null default '{}',          -- per kind, see below; validated by a Pydantic model
  ca_bundle   text,                                 -- optional PEM for company-internal CAs
  enabled     bool not null default true,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create table user_credentials (
  id             int generated always as identity primary key,
  user_id        int not null references users on delete cascade,
  integration_id int not null references integrations on delete cascade,
  kind           text not null,                     -- per integration kind, see table
  ciphertext     text not null,                     -- MultiFernet
  status         text not null default 'unverified' check (status in ('unverified','ok','invalid','unreadable')),
  verified_at    timestamptz,
  last_error     text,
  updated_at     timestamptz not null default now(),
  unique (user_id, integration_id, kind)
);
```

| Integration kind | `settings` | Credential kinds | Verify call |
|---|---|---|---|
| `jira` | `{"api_version": "2"}` | `token` (PAT) | `GET /rest/api/2/myself` |
| `gitlab` | `{}` | `token` (PAT, scopes `read_api read_repository`; Phase 4 adds `write_repository api`) | `GET /api/v4/user` |
| `gerrit` | `{}` | `username`, `password` (HTTP password) | `GET /a/accounts/self` |
| `jenkins` | `{}` | `username`, `token` | `GET /me/api/json` |
| `kubernetes` | `{"allowed_kinds": [...], "allowed_namespaces": [...] or null}` | `kubeconfig` | `GET /version`, then `SelfSubjectRulesReview` for each allowed namespace |
| `feed` | `{"urls": ["https://..."]}` (admin allowlist of RSS/Atom feeds) | none | fetch first URL |

**Migration 0006** (0005 went to H16's `users.timezone_confirmed`) creates both tables and migrates Phase 1 secrets: if `MWB_GITLAB_URL` / `MWB_GERRIT_URL` / `MWB_JIRA_URL` are set, it creates those integrations and moves each user's `gitlab_token`, `gerrit_username`/`gerrit_password`, `jira_token` rows into `user_credentials` (ciphertext unchanged; status `unverified`). `projects` gains `integration_id` (`ON DELETE SET NULL`; matched by URL host; set for every new project). `user_secrets` keeps only system kinds (`gateway_key`).

*Changed while building S1:*

- **No re-entry when the URLs aren't set.** Tokens the migration can't move stay in `user_secrets` (expand step, rule R10). When an admin creates the first integration of a kind, it **adopts** them: moves them to credentials and links projects on its host (`app/integrations/service.py`). A later contract migration drops whatever is left. Projects without an integration keep cloning with their Phase 1 token until then.
- **The Phase 1 `/api/secrets` API is removed**; Settings → Integrations uses `/api/integrations`. That and Admin → Integrations were brought forward from S7, so the app keeps working between S1 and S7.
- **Verification:** a 401/403 marks every part of the integration's credential `invalid`; a timeout, refused connection or 5xx keeps the status and only records `last_error`, because an outage says nothing about the credential. Saving one part of a multi-part credential (Gerrit username and password) makes the others `unverified`.
- **Admin edits:** changing `base_url` marks credentials `unverified` and deletes kubeconfigs, which name the old server. An integration with repositories can't be deleted (409: disable it instead). `MWB_ALLOW_HTTP_INTEGRATIONS` is refused when `MWB_ENV=prod`.
- **Repositories:** `POST /api/projects` takes `integration_id` (or `host`, matched by URL host). The URL's host must be the integration's host. git gets the integration's CA bundle through `GIT_CONFIG_*` `http.sslCAInfo`, a certificate file under `/data/ca/` (not secret).
- **Kubeconfig:** besides the refusals above, `insecure-skip-tls-verify`, `proxy-url`, impersonation (`as*`) and unknown fields are refused. Every user and cluster in the file is checked, not only the selected one.

**Kubeconfig validation** (on upload, before storing):

- Parse YAML; size ≤ 64 KB.
- Each referenced cluster `server` URL must equal an enabled `kubernetes` integration's `base_url`. The integration's `ca_bundle` overrides `certificate-authority-data` only if the kubeconfig has none.
- Allowed user auth: `token`, or `client-certificate-data` + `client-key-data`. **Rejected:** `exec`, `auth-provider`, `tokenFile`, file paths of any kind, `username/password`. (Exec plugins would run arbitrary commands inside our worker.)
- Store only the selected context's cluster/user (minimal kubeconfig).

**TLS and proxies.** Each connector's `httpx.AsyncClient` uses `verify=<ca_bundle or system>`, honours `HTTPS_PROXY`/`NO_PROXY` from env (`trust_env=True`), timeouts `connect=10, read=30, total=60`, and a response size cap (default 2 MB; log tails stream and keep only the tail).

## 2. Connectors and the tool registry

`app/connectors/<kind>.py` — one module per kind with a typed client. `app/tools/registry.py` — the only place agent tools are defined.

```python
class Tier(StrEnum):
    read = "read"
    sandbox_write = "sandbox_write"     # Phase 4: only inside job sandboxes
    external_write = "external_write"   # Phase 4+: only via approvals (rule R5)

@dataclass(frozen=True)
class ToolSpec:
    name: str
    tier: Tier
    integration_kind: str | None        # None = internal (knowledge, inbox)
    untrusted_output: bool              # True → result wrapped by app.llm.untrusted.wrap
    max_output_chars: int
    fn: Callable[..., Awaitable[Any]]   # signature: (ctx: RunContext[AgentDeps], **typed_args)
```

`toolset_for(agent, user) -> FunctionToolset` returns only tools whose tier ≤ the agent's `max_tier` **and** for which the user has a verified credential (or that need none). Tool calls are recorded in `tool_calls` exactly as in chat.

### Phase 2 read tools

| Tool | Args | Returns (stable ids for change detection) | Notes |
|---|---|---|---|
| `search_knowledge` | query, scope | existing | internal |
| `get_session` | session_id | existing | internal, ACL |
| `jira_search` | jql, max_results ≤ 50 | key, summary, status, assignee, updated | untrusted |
| `jira_get_issue` | key, include_comments (last 20) | fields + description + comments | untrusted |
| `jira_my_open_issues` | — | as `jira_search` | JQL `assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC` |
| `gitlab_list_mrs` | project path, state, author=me? | iid, title, state, pipeline status, updated | untrusted |
| `gitlab_pipeline_status` | project path, ref | pipeline id, status, failed job ids/names | |
| `gitlab_job_log_tail` | project path, job id, lines ≤ 400 | tail text | untrusted |
| `gerrit_query` | query (e.g. `owner:self status:open`) | change number, subject, status, verified/code-review votes | untrusted |
| `jenkins_job_status` | job path | last build number, result, timestamp, culprits | |
| `jenkins_failed_builds` | folder path, since_hours ≤ 72 | job, build, result, url | |
| `jenkins_build_log_tail` | job path, build, lines ≤ 400 | tail text | untrusted |
| `k8s_get` | integration, kind ∈ allowed_kinds, namespace, label_selector | name, status summary (phase, restarts, reasons, ready) | never `secrets`; `configmaps` only if admin allows |
| `k8s_events` | integration, namespace, since_minutes ≤ 180, types (Warning) | reason, object, count, last_seen, message | untrusted |
| `k8s_pod_logs` | integration, namespace, pod, container, lines ≤ 400, previous | tail text | untrusted |
| `feed_latest` | feed url ∈ allowlist, since_hours | title, link, published, summary (≤ 500 chars) | untrusted |

Default `allowed_kinds`: `pods, deployments, statefulsets, daemonsets, jobs, cronjobs, nodes, events, services, ingresses`.

**Error handling.** Connectors never raise into the agent. They return `{"error": code, "message": ...}` with codes `auth_failed` (also sets the credential `status='invalid'` and adds an inbox item "Your Jira token was rejected"), `not_found`, `forbidden`, `timeout`, `unavailable`, `too_large`. Retries: 2 retries with backoff on 429/502/503/504 and transport errors, honouring `Retry-After` up to 30 s.

**Testing.** Every connector has recorded-fixture tests using `respx` (fixtures in `tests/fixtures/connectors/<kind>/`), covering success, 401, 404, 429-then-success, pagination cap, oversize response. A manual `compose.testenv.yml` (Jira DC trial, GitLab CE, Gerrit, Jenkins, kind cluster) exists for exploratory testing but is not a CI gate.

## 3. Agents

```sql
create table agents (
  id            int generated always as identity primary key,
  slug          text not null unique,               -- 'chat', 'morning-brief', 'k8s-health', 'ci-failures', 'jira-digest'
  name          text not null,
  description   text not null,
  owner_id      int references users on delete cascade,  -- null = system agent
  model_alias   text not null check (model_alias in ('chat','fast','planner')),
  system_prompt text not null,
  tools         text[] not null,                    -- tool names; validated against the registry
  max_tier      text not null default 'read' check (max_tier in ('read','sandbox_write','external_write')),
  inputs_schema jsonb not null default '{}',        -- JSON Schema for schedule inputs (rendered as a form)
  report        bool not null default true,         -- output_type RunReport (scheduled agents)
  enabled       bool not null default true,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
```

Phase 2 ships system agents only, seeded and updated by migration from `app/agents/seed/*.md` (frontmatter + prompt). User-defined agents arrive with the editor in Phase 5. Every system prompt ends with `UNTRUSTED_NOTICE` (00 §5).

| Agent | Tools | Inputs |
|---|---|---|
| `morning-brief` | jira_my_open_issues, gitlab_list_mrs, jenkins_failed_builds, gerrit_query, feed_latest, search_knowledge | gitlab projects[], jenkins folders[], feeds[] |
| `k8s-health` | k8s_get, k8s_events, k8s_pod_logs, search_knowledge | integration, namespaces[] |
| `ci-failures` | gitlab_pipeline_status, gitlab_job_log_tail, jenkins_failed_builds, jenkins_build_log_tail, search_knowledge, get_session | gitlab projects[], jenkins folders[] |
| `jira-digest` | jira_search, jira_get_issue, search_knowledge | jql |

### Run report

Scheduled agents use a structured output so "alert only when something changed" does not depend on the model's wording:

```python
class Finding(BaseModel):
    fingerprint: str = Field(max_length=200)   # stable id built from tool ids, e.g. "k8s:prod/payments-7f9:CrashLoopBackOff"
    severity: Literal["info", "warning", "alert"]
    title: str = Field(max_length=200)
    detail: str = Field(max_length=4000)       # Markdown
    links: list[HttpUrl] = Field(default=[], max_length=5)

class RunReport(BaseModel):
    schema_version: Literal[1] = 1
    status: Literal["ok", "warning", "alert"]  # max severity of findings, or ok
    headline: str = Field(max_length=200)
    findings: list[Finding] = Field(max_length=50)
    summary_md: str = Field(max_length=20000)
```

The agent prompt tells the model how to build fingerprints for each tool (documented per agent). After the run, the server **recomputes** `status` from findings and **validates** that every fingerprint appears in, or is derived from, ids returned by tools in this run (substring match of the id part). Fingerprints that fail validation are kept but marked `unverified` and excluded from change detection, so a model that invents ids can't trigger or suppress alerts.

## 4. Schedules and runs

```sql
create table schedules (
  id                   uuid primary key default gen_random_uuid(),
  owner_id             int not null references users on delete cascade,
  agent_id             int not null references agents,
  name                 text not null check (length(name) between 1 and 120),
  prompt               text not null default '' check (length(prompt) <= 4000),
  inputs               jsonb not null default '{}',     -- validated against agents.inputs_schema
  cron                 text not null,
  timezone             text not null,
  enabled              bool not null default true,
  notify_mode          text not null default 'on_change' check (notify_mode in ('always','on_change','on_alert')),
  notify_email         bool not null default true,
  max_runtime_s        int not null default 300 check (max_runtime_s between 30 and 1800),
  next_run_at          timestamptz,                      -- null when disabled
  last_run_at          timestamptz,
  last_status          text,
  consecutive_failures int not null default 0,
  created_from_session uuid references chat_sessions on delete set null,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);
create index schedules_due on schedules (next_run_at) where enabled;

create table runs (
  id             uuid primary key default gen_random_uuid(),
  schedule_id    uuid references schedules on delete set null,
  owner_id       int not null references users on delete cascade,
  agent_id       int not null references agents,
  trigger        text not null check (trigger in ('schedule','manual')),
  config         jsonb not null,                                     -- snapshot: agent_id, prompt, inputs, notify settings
  scheduled_for  timestamptz not null,
  status         text not null check (status in ('queued','running','succeeded','failed','timed_out','skipped','cancelled')),
  session_id     uuid references chat_sessions on delete set null,  -- source='run'; full transcript
  report         jsonb,                                              -- RunReport
  fingerprints   text[] not null default '{}',                       -- verified only
  changed        bool,
  notified       bool not null default false,
  tokens_in      int, tokens_out int, cost_usd numeric(12,6),
  error_code     text, error text,
  started_at     timestamptz, finished_at timestamptz,
  created_at     timestamptz not null default now(),
  unique (schedule_id, scheduled_for)
);
create index runs_owner_created on runs (owner_id, created_at desc);
```

Runs are private to their owner (no `visibility`). Their sessions have `source='run'` and are excluded from episode building.

### Cron rules

- Parsed with `croniter`, standard 5 fields. Evaluated in the schedule's timezone with `zoneinfo`; DST gaps skip the non-existent time, DST overlaps fire once (first occurrence).
- Minimum interval: the smallest gap among the next 200 occurrences must be ≥ `MWB_SCHEDULE_MIN_INTERVAL_MIN` (default 15) for members, 5 for admins.
- Per-user limit: `MWB_MAX_SCHEDULES_PER_USER` (default 20) enabled schedules.
- `POST /api/schedules/preview` returns `{"valid", "error", "description", "next": [5 local ISO times]}`. The description comes from `cron-descriptor` on the server so UI and chat cards show identical text.

### The tick

Periodic `schedule_tick`, every minute, queue `default`, `queueing_lock="schedule_tick"`:

```text
BEGIN
  SELECT * FROM schedules
   WHERE enabled AND next_run_at <= now()
   ORDER BY next_run_at
   FOR UPDATE SKIP LOCKED LIMIT 100;
  for each s:
     fire_time := s.next_run_at
     INSERT INTO runs (schedule_id, scheduled_for = fire_time, status='queued', config = snapshot(s), ...)
         ON CONFLICT (schedule_id, scheduled_for) DO NOTHING
     s.next_run_at := next occurrence after max(now(), fire_time)   -- at most one catch-up run
COMMIT
for each inserted run: defer run_schedule(run_id) with lock="schedule:<id>", queueing_lock="run:<run_id>"
```

If deferring fails after commit, the run stays `queued`; the tick also re-defers runs `queued` for more than 5 minutes (same `queueing_lock`, so no duplicates). Users whose account is inactive are skipped and their schedules disabled.

### Executing a run

`run_schedule(run_id)`, queue `llm`:

1. Short tx: load run, agent, owner and (if `schedule_id` is set) the schedule; if the schedule is disabled or deleted, or the owner is inactive → `skipped`. Execution always uses the run's `config` snapshot, so editing a schedule never changes a run already queued, and test runs (`schedule_id` null) work the same way. Set `running`, `started_at`. Create the run session (`source='run'`). Commit.
2. No tx: build the agent with `toolset_for(agent, owner)`, `output_type=RunReport`, `UsageLimits` (01-hardening §H12 table), the owner's gateway key. Prompt = agent system prompt + schedule prompt + inputs (JSON). Run with `asyncio.timeout(max_runtime_s)`. Persist messages and tool calls to the run session as chat does.
3. No tx: validate the report (§3), compute verified fingerprints.
4. Short tx: load the previous `succeeded` run for this schedule; `changed = set(fingerprints) != set(previous.fingerprints)` (first run: `changed = true`). Store report, usage, cost, status. Decide notification:

| notify_mode | Notify when |
|---|---|
| `always` | every successful run |
| `on_change` | `changed` and status ≠ ok, or the previous run had findings and this one has none (a "recovered" notice) |
| `on_alert` | status = alert and `changed` |

   Create the inbox item and, if `notify_email` and `users.email_notifications`, an outbox email, in the same transaction. Update schedule `last_run_at`, `last_status`, reset `consecutive_failures`. Commit.
5. On failure or timeout: status `failed`/`timed_out` with `error_code`, increment `consecutive_failures`. At 3 consecutive failures, disable the schedule, create an inbox item and email "Schedule X was paused after 3 failures: <last error>". `budget_exceeded` disables immediately with that reason.

Manual run: `POST /api/schedules/{id}/run` creates a run with `trigger='manual'`, `scheduled_for=now()`, and always notifies in-app (never email).

## 5. Inbox and email

```sql
create table inbox_items (
  id         bigint generated always as identity primary key,
  owner_id   int not null references users on delete cascade,
  kind       text not null check (kind in ('run','alert','recovered','system','credential','proposal')),
  severity   text not null check (severity in ('info','warning','alert')),
  title      text not null,
  body_md    text not null default '',
  link       text,                       -- in-app path only, e.g. /runs/<id>
  run_id     uuid references runs on delete cascade,
  read_at    timestamptz,
  created_at timestamptz not null default now()
);
create index inbox_owner_unread on inbox_items (owner_id, created_at desc) where read_at is null;

create table outbound_emails (
  id              bigint generated always as identity primary key,
  owner_id        int references users on delete cascade,
  to_addr         text not null,
  subject         text not null,
  body_text       text not null,
  dedupe_key      text not null unique,     -- e.g. "run:<run_id>"
  status          text not null default 'pending' check (status in ('pending','sent','failed','suppressed')),
  attempts        int not null default 0,
  next_attempt_at timestamptz not null default now(),
  last_error      text,
  created_at      timestamptz not null default now(),
  sent_at         timestamptz
);
```

- **Email body is plain text**: headline, finding titles with severity, and a link to the run in MyWorkBuddy (`https://{MWB_HOSTNAME}/runs/<id>`). No LLM-generated HTML and no external links copied from findings. This avoids HTML injection and phishing-looking mail.
- Sender: `send_emails` periodic every minute, queue `default`, `lock="send_emails"`, picks up to 50 `pending` rows with `next_attempt_at <= now()`, sends with `aiosmtplib` (STARTTLS, auth from `MWB_SMTP_*`). Backoff 1, 5, 15, 60, 240 min; after 6 attempts → `failed` and an inbox `system` item.
- Daily cap per user `MWB_MAX_EMAILS_PER_DAY` (default 30); beyond it rows are `suppressed` and one inbox notice is created.
- Headers: `Auto-Submitted: auto-generated`, `X-MWB-Run: <id>`, `List-Unsubscribe: <https://{host}/settings#notifications>`.

## 6. Creating schedules from chat

The chat agent gets one more tool, tier `read` (it creates nothing by itself):

```python
async def propose_schedule(ctx, name: str, agent: AgentSlug, cron: str, prompt: str = "",
                           inputs: dict = {}, timezone: str | None = None,
                           notify_mode: NotifyMode = "on_change") -> dict:
    """Propose a recurring check for the user to confirm. Nothing runs until they accept."""
```

- Validates exactly like `POST /api/schedules` (agent visible to user, inputs schema, cron rules). On validation error, returns the error to the model so it can fix the arguments.
- Stores a `schedule_proposals` row: `id uuid, owner_id, session_id, message_id, payload jsonb, status ('pending','accepted','dismissed','expired'), expires_at (24 h), created_at`.
- Returns `{"proposal_id", "description", "next_runs"}` to the model, and the SSE stream emits `event: proposal` with the same data. The UI renders a card under the assistant message: name, agent, human cron, next 3 runs in the user's timezone, notify mode, and **Create** / **Edit** / **Dismiss**.
- **Create** → `POST /api/schedules/proposals/{id}/accept` (owner, CSRF, pending, not expired) creates the schedule exactly from the stored payload and audits `schedule.create` with `proposal_id`. **Edit** opens the schedule form pre-filled; saving creates the schedule and marks the proposal accepted.
- The model can never pass a proposal id to anything that creates a schedule. Only the user's click does.

### Attaching context to a chat

"Investigate in chat" on a run result (07-ux §5.7) opens `/chat?context=run:<id>`. The frontend creates a session with `POST /api/chat/sessions {"context": [{"type": "run", "id": "<id>"}]}`.

- `chat_sessions` gains `context jsonb not null default '[]'` (list of `{type, id}`; types `run` in Phase 2, `plan_revision` in Phase 3). Each ref is ACL-checked (owner only) on create and on every turn.
- `_instructions` adds one block per ref: the run's report rendered as Markdown, wrapped with `untrusted.wrap("run:<id>", …)` because findings were built from untrusted tool output (rule R6). Budget 8 000 chars per ref.
- The context panel lists attached refs; removing one is `PATCH /api/chat/sessions/{id} {"context": [...]}`.

### Test runs before saving

The schedule form's "Run once now to test" calls `POST /api/schedules/test` with the unsaved form payload (same validation as create). It creates a run with `schedule_id = null`, `trigger = 'manual'`, executes it like any run, never notifies, and returns the run id; the form shows the result in a drawer by polling `GET /api/runs/{id}`.

## 7. API

All endpoints require auth; unsafe methods require CSRF; owned resources return 404 to non-owners.

| Method and path | Purpose |
|---|---|
| `GET /api/admin/integrations` · `POST` · `PATCH /{id}` · `DELETE /{id}` | Admin manages integrations (audit each) |
| `GET /api/integrations` | Enabled integrations + the caller's credential status per kind |
| `PUT /api/integrations/{id}/credentials/{kind}` · `DELETE` | Write-only credential (kubeconfig validated, §1); 204, nothing echoed back |
| `POST /api/integrations/{id}/test` | Verify the caller's credential; updates status |
| `GET /api/agents` | Agents usable by the caller, with inputs schema |
| `GET /api/schedules` · `POST` | List (cursor) / create |
| `GET /api/schedules/{id}` · `PATCH` · `DELETE` | Read / update (recomputes `next_run_at`) / delete |
| `POST /api/schedules/preview` | Cron validation and description |
| `POST /api/schedules/{id}/run` | Manual run |
| `POST /api/schedules/test` | Test run of an unsaved schedule (§6) |
| `GET /api/schedules/{id}/runs` | Runs (cursor) |
| `GET /api/runs/{id}` | Run detail with report, usage, transcript link |
| `GET /api/schedules/proposals/{id}` · `POST /accept` · `POST /dismiss` | Proposal flow |
| `GET /api/inbox` · `GET /api/inbox/unread-count` · `POST /api/inbox/{id}/read` · `POST /api/inbox/read-all` | Inbox |
| `PATCH /api/me` | `display_name`, `timezone`, `email_notifications` |

## 8. Frontend

Layouts, navigation, flows and states are specified in [07-ux.md](07-ux.md) (§4.2, §4.7, §4.8, §5.6, §5.7, §5.10). The table below is the summary.

| Page | Content |
|---|---|
| Sidebar | Inbox with unread badge (polls `unread-count` every 30 s while the tab is visible) |
| Inbox | List grouped by day; severity colour; click opens the run; "Mark all read" |
| Schedules | Table: name, agent, human cron, next run (local), last status, enabled toggle, Run now |
| Schedule editor | Agent picker; inputs form generated from `inputs_schema`; prompt; cron with live preview; timezone (defaults to user's); notify mode; email toggle |
| Run detail | Headline, status, findings (severity, title, detail rendered with the safe Markdown component, links shown as text + host), cost and duration, "Open transcript" |
| Settings → Integrations | Per integration: credential fields, status, Test button, last error |
| Settings → Profile | Timezone, email notifications |
| Admin → Integrations | CRUD, CA bundle upload, Kubernetes allowed kinds/namespaces, feed allowlist |
| Chat | Proposal card under the assistant message |

## 9. Seeded system tasks added in Phase 2

| Task | Schedule | Purpose |
|---|---|---|
| `schedule_tick` | every minute | §4 |
| `send_emails` | every minute | §5 |
| `projects_refresh` | nightly 01:00 UTC | `git ls-remote` each ready project; re-ingest if HEAD moved |
| `runs_retention` | daily | Delete runs (and their sessions) older than 180 days |

## 10. Out of scope

Slack/Teams delivery, user-defined agents, Spinnaker connector, generic MCP servers, write tools of any kind. All Phase 5 except write tools, which arrive with approvals in Phases 3–4.

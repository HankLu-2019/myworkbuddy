# 06 — Operations runbook

Status: target state after Phase H. Commands assume the repo is checked out at `/opt/myworkbuddy` on the VM and you run them from there.

## 1. Host preparation

1. Linux VM (Ubuntu 24.04 LTS or Debian 12), sizing per 00 §1.2. Data disk mounted at `/var/lib/docker` (XFS recommended).
2. Docker Engine ≥ 27 with the Compose plugin. Enable `live-restore` in `/etc/docker/daemon.json`.
3. Phase 4: install gVisor and register `runsc` as a Docker runtime; check with `docker run --rm --runtime=runsc hello-world`.
4. Firewall: inbound 443 (and 80 for redirect) from the office network only; SSH from admin hosts.
5. Outbound: AWS Bedrock endpoints for the chosen region, company Jira/GitLab/Gerrit/Jenkins/K8s, SMTP relay, package mirrors (Phase 4), offsite backup target.
6. Time sync (chrony) enabled; session expiry, schedules and certificate checks depend on a correct clock.

## 2. First install

```bash
git clone <repo> /opt/myworkbuddy && cd /opt/myworkbuddy
cp .env.example .env && chmod 600 .env
docker compose run --rm api mwb generate-key        # paste into MWB_SECRET_KEYS
# edit .env: see §3; put the TLS cert and key in deploy/tls/ (or set MWB_TLS_MODE=internal)
docker compose -f docker-compose.yml up -d --wait
docker compose exec api mwb create-admin --username admin --email you@company.example
docker compose exec api mwb llm check                # gateway key management works (creates and deletes a test key)
```

Then sign in at `https://$MWB_HOSTNAME`, create integrations (Phase 2+), create users, and check **Admin → System** shows all checks green.

**Back up `.env` separately** (password manager or sealed vault). Without `MWB_SECRET_KEYS`, stored tokens cannot be decrypted; without the LiteLLM master key, user keys cannot be managed.

## 3. Configuration reference

| Variable | Default | Notes |
|---|---|---|
| `MWB_ENV` | `prod` | `dev` relaxes cookie/TLS checks |
| `MWB_HOSTNAME` | — | e.g. `mwb.company.example` |
| `MWB_ALLOW_HTTP_INTEGRATIONS` | `false` | `http://` integration URLs; dev and CI only, refused when `MWB_ENV=prod` |
| `MWB_GITLAB_URL`, `MWB_GERRIT_URL`, `MWB_JIRA_URL` | — | Read once by migration 0006 when upgrading from Phase 1 (§7) |
| `MWB_TLS_MODE` | `files` | `files` (uses `deploy/tls/cert.pem`, `key.pem`, mounted at `/tls`), `internal` (Caddy CA), or `none` (dev and CI only; refused when `MWB_ENV=prod`) |
| `POSTGRES_PASSWORD`, `LITELLM_DB_PASSWORD` | — | |
| `MWB_SECRET_KEYS` | — | Comma-separated Fernet keys, newest first |
| `LITELLM_MASTER_KEY` | — | Only api/worker use it, for key management |
| `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | — | Or an instance role; region per O1 |
| `MWB_CHAT_MODEL_ID`, `MWB_FAST_MODEL_ID`, `MWB_PLANNER_MODEL_ID`, `MWB_CODER_MODEL_ID`, `MWB_EMBED_MODEL_ID` | — | LiteLLM model strings |
| `MWB_DEFAULT_TIMEZONE` | `Europe/Stockholm` | For new users |
| `MWB_DEFAULT_USER_BUDGET_USD` | `50` | Monthly, per user; admins override per user |
| `LITELLM_DB_PASSWORD` | — | Required: the gateway's own database (virtual keys, budgets, spend logs) |
| `MWB_JOB_BUDGET_USD` | `10` | Per coding job |
| `MWB_JOB_MAX_QUESTIONS` | `3` | Questions the agent may ask per job before it must decide itself (08 §F3) |
| `MWB_JOB_REVIEW_DAYS` | `7` | A job waiting for review this long is discarded (notice a day before; `jobs_cleanup`, hourly) |
| `MWB_ARTIFACT_RETENTION_DAYS` | `90` | Transcripts, patches and logs of finished jobs (`jobs_cleanup`) |
| `MWB_JOB_DISK_GB` | `10` | Runner: a run whose workspace grows past this is killed (sweep) and the job fails `workspace_too_large` |
| `MWB_SESSION_TTL_HOURS` / `MWB_SESSION_IDLE_HOURS` | `72` / `12` | |
| `MWB_OIDC_ISSUER`, `MWB_OIDC_CLIENT_ID`, `MWB_OIDC_CLIENT_SECRET` | — | 08 §F11: single sign-on (authorization code + PKCE). Empty issuer: off. Register `<MWB_HOSTNAME>/api/auth/oidc/callback` as the redirect URI. Checked at startup: https issuer, client id and secret, `MWB_HOSTNAME` |
| `MWB_OIDC_SCOPES` / `MWB_OIDC_NAME` | `openid email profile` / `SSO` | Must include `openid`; the name is on the sign-in button |
| `MWB_OIDC_AUTO_CREATE` | `false` | Unknown users become members; otherwise "Ask an admin to add you" |
| `MWB_MCP_CALL_TIMEOUT_S` / `MWB_MCP_MAX_OUTPUT_CHARS` | `30` / `20000` | 08 §F12: one MCP tool call's time limit, and how much of its answer reaches the model |
| `MWB_LOCAL_LOGIN` | `all` | `admins`: members sign in with SSO only (needs `MWB_OIDC_ISSUER`); admins keep the password |
| `MWB_LOGIN_MAX_ATTEMPTS` / `_IP` / `_USER`, `MWB_LOGIN_WINDOW_MINUTES` | `5` / `30` / `20`, `15` | Failed sign-ins per username+IP, per IP, per username |
| `MWB_SMTP_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_FROM`, `_STARTTLS` | — / 587 / — / — / — / true | Phase 2 |
| `MWB_SMTP_TIMEOUT_S` | 30 | Phase 2 |
| `MWB_MAX_EMAILS_PER_DAY` | `30` | Per user, per local day |
| `MWB_NOTIFY_WEBHOOK_HOSTS` | `hooks.slack.com,*.webhook.office.com,*.logic.azure.com` | Hosts a Slack or Teams webhook may point at (fnmatch patterns, comma-separated; 08 §F8). The worker needs outbound HTTPS to them |
| `MWB_MAX_MESSAGES_PER_DAY` | `50` | Slack and Teams messages per user, per local day (08 §F8) |
| `MWB_SCHEDULE_MIN_INTERVAL_MIN` | `15` | Members |
| `MWB_MAX_SCHEDULES_PER_USER` | `20` | |
| `MWB_SCHEDULE_MIN_INTERVAL_ADMIN_MIN` | `5` | Admins |
| `MWB_RUNS_RETENTION_DAYS` | `180` | Runs and their transcripts (`runs_retention`, daily 02:40 UTC) |
| `MWB_AGENT_EVALS_USER` | — | Username whose AI budget the nightly agent evals use (03:10 UTC); unset: no nightly evals. Run by hand with `mwb eval-agents --user <name> [--agent <slug>]` |
| `MWB_INGEST_MAX_SUMMARY_FILES` | `150` | Per ingest |
| `MWB_REPO_CACHE_MAX_GB` | `50` | Ingest refuses to clone or fetch above this, or when `/data` has < 10 % free |
| `MWB_TRANSCRIPT_MAX_MB` | `50` | Per imported Claude Code transcript |
| `MWB_LIBRARIAN_LOCAL_HOUR` | `2` | User's local time |
| `MWB_MAX_CONCURRENT_JOBS` | `2` | Phase 4 |
| `MWB_SANDBOX_RUNTIME` | `runsc` | Phase 4. `runc` only where gVisor can't run; the System page then warns |
| `MWB_SERVICES_IMAGE` | `mwb-sandbox-dind:latest` | Runner, 08 §F10: the sandbox with its own Docker (`sandbox/Dockerfile.dind`), run with `sysbox-runc` (install Sysbox on the host; the System page says whether it's there) |
| `MWB_PROXY_REGISTRY_HOSTS` | — | Runner, 08 §F10: comma-separated registry (mirror) hosts a verification with containers may pull images from, through the proxy's verify port |
| `MWB_CODER_MODEL_ID` | the chat model | Phase 4. A Claude model: Claude Code uses LiteLLM's Anthropic route |
| `MWB_SANDBOX_NETWORK`, `MWB_SANDBOX_PREFIX` | `mwb-sandbox`, `172.31.0` | Phase 4. The internal sandbox network; change only to run a second stack on the same host |
| `MWB_RUNNER_TOKEN` | — | Phase 4, at least 32 characters; api, worker and runner share it |
| `MWB_DOCKER_ROOT` | `/var/lib/docker` | Phase 4. Docker's data root, mounted read-only into the runner for the free-space check |
| `MWB_PROXY_AGENT_HOSTS`, `MWB_PROXY_VERIFY_HOSTS`, `MWB_SANDBOX_OPTIONAL_HOSTS` | see 04 §1 | Phase 4 |
| `MWB_JOB_MAX_CHANGED_LINES`, `MWB_JOB_DISK_GB` | `3000`, `10` | Phase 4 |
| `MWB_JOB_MAX_TURNS` | `60` | Phase 4. Claude Code's turn cap per agent run |
| `MWB_BACKUP_REPO`, `RESTIC_PASSWORD` | — | §5. Any restic repository (`s3:…`, `sftp:…`, `rest:…`) or a path under `/backup-local` |
| `MWB_BACKUP_AWS_ACCESS_KEY_ID`, `MWB_BACKUP_AWS_SECRET_ACCESS_KEY` | — | Only for an S3 repository; kept apart from the Bedrock `AWS_*` |
| `MWB_BACKUP_LOCAL_DIR` | `./backups` | Host directory mounted at `/backup-local` (owned by uid 1000) |
| `MWB_BACKUP_TIME`, `MWB_BACKUP_TIMEZONE` | `01:30`, `MWB_DEFAULT_TIMEZONE` | Nightly backup time |
| `MWB_EDGE_PREFIX`, `MWB_HTTPS_PORT`, `MWB_HTTP_PORT` | `172.30.0`, `443`, `80` | Only to run a second stack (restore drill) on the same host |

## 4. Routine checks

Daily (2 minutes): Admin → System is green; last backup < 26 h old; no failed jobs in the last 24 h you didn't expect.

Weekly: review LLM spend per user; review `audit_log` for `login.failed` bursts; apply OS security updates (`unattended-upgrades`).

Monthly: restore drill (§6); review Dependabot/Renovate PRs; rotate the runner token.

## 5. Backups

The `backup` service runs nightly at `MWB_BACKUP_TIME` (01:30) local time:

1. `pg_dump -Fc mwb` and `pg_dump -Fc litellm` to `/backup/staging/`.
2. `tar` of `/data` excluding `repos/` and `tmp/`.
3. `restic backup` of the staging dir to `MWB_BACKUP_REPO` (initialised on first use), then `restic forget --keep-daily 14 --keep-weekly 8 --keep-monthly 6 --prune`.
4. Write `system_state['backup']` with snapshot id, sizes, duration, status. Admin → System alerts after 26 h without a success.

Setup: set `MWB_BACKUP_REPO` and `RESTIC_PASSWORD` in `.env`, and store the restic password with `.env` (§2), because without it the snapshots can't be read. For a repository on the host or a mounted NAS: `mkdir -p backups && sudo chown 1000:1000 backups` and `MWB_BACKUP_REPO=/backup-local/repo`. Off-host is the point; an S3 or sftp repository is preferred.

On-demand: `docker compose run --rm backup now`. List a snapshot: `docker compose run --rm backup list latest`.

Not in the backup on purpose: repo caches (rebuilt by re-ingest), job workspaces (ephemeral), `.env` (backed up separately, §2).

## 6. Restore

```bash
# On a fresh host (or after stopping the stack: docker compose down)
deploy/restore.sh --snapshot latest        # or a snapshot id from `restic snapshots`
```

`restore.sh`:

1. Refuses to run if the `mwb` database has data, unless `--force`.
2. Starts only `postgres`; restores `mwb` and `litellm` with `pg_restore --clean --if-exists`.
3. Restores `/data` (wiki, artifacts).
4. Runs `migrate`, then starts the stack, then runs `mwb wiki-reconcile`, queues re-ingest of all projects, and prints `mwb backup verify-restore` (users, wiki pages with their files, tokens that still decrypt; it exits non-zero if any is wrong).

Each database is dropped and recreated from `template0` before `pg_restore`, so the restore holds exactly what was backed up.

**Drill** (monthly, ~30 min), next to the live stack:

```bash
COMPOSE_PROJECT_NAME=mwb-drill MWB_EDGE_PREFIX=172.31.0 MWB_HTTPS_PORT=8443 MWB_HTTP_PORT=8081 \
  deploy/restore.sh --snapshot latest
```

Sign in at port 8443, open a wiki page, run a search, check a stored token shows "Saved". Then record the result, `docker compose exec api mwb backup record-drill --ok --note "…"`, and remove the drill: `COMPOSE_PROJECT_NAME=mwb-drill docker compose down -v`. CI runs the same restore on every change (job `ops`).

## 7. Upgrades

```bash
docker compose run --rm backup now
git fetch && git checkout <release tag>
docker compose -f docker-compose.yml pull     # images pinned by digest in the compose file
docker compose -f docker-compose.yml up -d --wait   # migrate runs first; api/workers wait for it
```

Rollback: `git checkout <previous tag>` and `up -d` works only if the release notes say the migrations are backward compatible (expand phase). Otherwise restore the pre-upgrade backup (§6).

**Upgrading Procrastinate.** Read its release notes for new migration SQL files. Add an Alembic migration that executes those files in order (vendored into `backend/alembic/procrastinate/<version>/`), bump the exact pin, and run the CI `migration-upgrade` job.

**Release with H10 (search quality).** After the upgrade, run `docker compose exec api mwb reembed --dry-run`, then `mwb reembed`. Vectors written before H10 are labelled with the configured alias instead of the resolved model, so search ignores them (keyword search still works) until the worker has re-embedded them. Migration 0004 rebuilds the BM25 index, which takes minutes for a team-sized index.

**Release with S1 (integrations, Phase 2).** Before upgrading, set `MWB_GITLAB_URL`, `MWB_GERRIT_URL` and `MWB_JIRA_URL` (base URLs, e.g. `https://gitlab.company.example`) in `.env` for the systems your users saved tokens for. Migration 0006 then creates those integrations, moves each user's saved tokens to them and links repositories by host. Without them nothing is lost: the tokens move when you add the integration in Admin → Integrations. Afterwards, add any CA bundle the systems need in Admin → Integrations, and ask users to press Test in Settings → Integrations (moved tokens start as "Not tested").

**Upgrading LiteLLM.** Bump the image tag, `up -d llm-gateway`, then run `mwb llm check`. It exercises `/key/generate`, `/key/list?key_alias=` and `/key/delete` with `key_aliases`, which `app/llm/keys.py` relies on. Check the release notes for changes to the budget error (`budget_exceeded`) and the `x-litellm-call-id` / `x-litellm-response-cost` headers.

**Upgrading ParadeDB / pg_search.** Major pg_search versions may require `REINDEX` of `chunks_bm25`. Follow ParadeDB's upgrade notes; run `ALTER EXTENSION pg_search UPDATE` in a migration; reindex during a maintenance window.

**Upgrading Claude Code in the sandbox image (Phase 4).** Bump the pinned version, rebuild the image in CI, run the agent-preset smoke test against the fake LLM, then update the digest in runner config.

## 8. Key and secret rotation

| Secret | Procedure |
|---|---|
| `MWB_SECRET_KEYS` | Generate a key (`mwb generate-key`), prepend it to the list, restart api/worker, run `docker compose exec api mwb rotate-secrets`, then remove the old key from the list and restart. If the command reports unreadable tokens, those users see "Can't be read any more" in Settings and must enter them again. |
| `LITELLM_MASTER_KEY` | Change in `.env`, restart `llm-gateway`, `api`, `worker`. Virtual keys are unaffected. |
| User virtual keys | `mwb llm rotate-key --user <name>` (creates a new key, deletes the old). |
| `MWB_RUNNER_TOKEN` | Change in `.env`, restart `runner` and `worker-jobs`. |
| TLS certificate | Replace files in `deploy/tls/`, `docker compose restart caddy`. |
| AWS credentials | Change in `.env`, restart `llm-gateway`. |

## 9. User lifecycle

- **Join**: admin creates the user under Users → Add user (a LiteLLM key is created automatically, H12) and passes on the one-time password shown once. The user signs in, must choose their own password before anything else, sets their timezone, and adds tokens under Settings.
- **Forgotten password**: admin → Users → Reset password (or `mwb reset-password --username <name>` if no admin can sign in). This signs the user out everywhere and shows a new one-time password.
- **Lost device**: the user signs in elsewhere and uses Settings → Sessions → "Sign out other sessions"; an admin can also Disable the account, which ends all its sessions.
- **Leave**: admin → Users → Delete, choosing the admin who receives the user's team items (H11). The dialog shows what moves and what is deleted; typing the username confirms. Ask the user's manager whether anything private should be kept first; if so, copy `/data/wiki/users/<id>` (a plain git repo) somewhere safe before deleting, because a background job removes it right after.

## 10. Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| Chat says "AI service unavailable" | System page gateway check; `docker compose logs llm-gateway` | AWS credentials, region, model access in Bedrock console |
| Chat says "monthly AI budget reached" | Admin → Users spend | Raise the user's budget |
| Search finds nothing new | System page: chunks waiting for embeddings | Check gateway; `mwb reembed --dry-run` |
| Project stuck in `running` | System page failed jobs; worker logs with `project_id` | Retry from System page; check disk |
| Wiki page shows "missing" | System page reconcile status | `mwb wiki-reconcile`; restore the file from backup if needed |
| Schedules not firing | Worker heartbeat; `schedule_tick` last run | Restart `worker`; check `next_run_at` and that the schedule is enabled |
| Emails not arriving | Admin → System outbox failures | SMTP settings; relay logs; per-user daily cap |
| Jobs fail at `preparing` | Job events | User's git token scope; base branch exists |
| Jobs fail at `running` immediately | Runner health; `docker info` runtimes | gVisor installed; sandbox image digest present |
| Disk filling | System page `/data` usage | Repo cache limit; artifacts retention; `docker system df` |

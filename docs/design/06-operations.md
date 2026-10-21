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
```

Then sign in at `https://$MWB_HOSTNAME`, create integrations (Phase 2+), create users, and check **Admin → System** shows all checks green.

**Back up `.env` separately** (password manager or sealed vault). Without `MWB_SECRET_KEYS`, stored tokens cannot be decrypted; without the LiteLLM master key, user keys cannot be managed.

## 3. Configuration reference

| Variable | Default | Notes |
|---|---|---|
| `MWB_ENV` | `prod` | `dev` relaxes cookie/TLS checks |
| `MWB_HOSTNAME` | — | e.g. `mwb.company.example` |
| `MWB_TLS_MODE` | `files` | `files` (uses `deploy/tls/cert.pem`, `key.pem`, mounted at `/tls`), `internal` (Caddy CA), or `none` (dev and CI only; refused when `MWB_ENV=prod`) |
| `POSTGRES_PASSWORD`, `LITELLM_DB_PASSWORD` | — | |
| `MWB_SECRET_KEYS` | — | Comma-separated Fernet keys, newest first |
| `LITELLM_MASTER_KEY` | — | Only api/worker use it, for key management |
| `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | — | Or an instance role; region per O1 |
| `MWB_CHAT_MODEL_ID`, `MWB_FAST_MODEL_ID`, `MWB_PLANNER_MODEL_ID`, `MWB_CODER_MODEL_ID`, `MWB_EMBED_MODEL_ID` | — | LiteLLM model strings |
| `MWB_DEFAULT_TIMEZONE` | `Europe/Stockholm` | For new users |
| `MWB_DEFAULT_USER_BUDGET_USD` | `50` | Monthly, per user |
| `MWB_JOB_BUDGET_USD` | `10` | Per coding job |
| `MWB_SESSION_TTL_HOURS` / `MWB_SESSION_IDLE_HOURS` | `72` / `12` | |
| `MWB_LOGIN_MAX_ATTEMPTS` / `_IP` / `_USER`, `MWB_LOGIN_WINDOW_MINUTES` | `5` / `30` / `20`, `15` | Failed sign-ins per username+IP, per IP, per username |
| `MWB_SMTP_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_FROM`, `_STARTTLS` | — / 587 / — / — / — / true | Phase 2 |
| `MWB_MAX_EMAILS_PER_DAY` | `30` | Per user |
| `MWB_SCHEDULE_MIN_INTERVAL_MIN` | `15` | Members |
| `MWB_MAX_SCHEDULES_PER_USER` | `20` | |
| `MWB_INGEST_MAX_SUMMARY_FILES` | `150` | Per ingest |
| `MWB_REPO_CACHE_MAX_GB` | `50` | Ingest refuses to clone or fetch above this, or when `/data` has < 10 % free |
| `MWB_TRANSCRIPT_MAX_MB` | `50` | Per imported Claude Code transcript |
| `MWB_LIBRARIAN_LOCAL_HOUR` | `2` | User's local time |
| `MWB_MAX_CONCURRENT_JOBS` | `2` | Phase 4 |
| `MWB_SANDBOX_RUNTIME` | `runsc` | Phase 4 |
| `MWB_RUNNER_TOKEN` | — | Phase 4, random 32 bytes |
| `MWB_PROXY_AGENT_HOSTS`, `MWB_PROXY_VERIFY_HOSTS`, `MWB_SANDBOX_OPTIONAL_HOSTS` | see 04 §1 | Phase 4 |
| `MWB_JOB_MAX_CHANGED_LINES`, `MWB_JOB_DISK_GB` | `3000`, `10` | Phase 4 |
| `MWB_BACKUP_REPO`, `RESTIC_PASSWORD`, plus target credentials | — | §5 |

## 4. Routine checks

Daily (2 minutes): Admin → System is green; last backup < 26 h old; no failed jobs in the last 24 h you didn't expect.

Weekly: review LLM spend per user; review `audit_log` for `login.failed` bursts; apply OS security updates (`unattended-upgrades`).

Monthly: restore drill (§6); review Dependabot/Renovate PRs; rotate the runner token.

## 5. Backups

The `backup` service runs nightly at 01:30 local time:

1. `pg_dump -Fc mwb` and `pg_dump -Fc litellm` to `/backup/staging/`.
2. `tar` of `/data` excluding `repos/` and `tmp/`.
3. `restic backup /backup/staging` to `MWB_BACKUP_REPO`, then `restic forget --keep-daily 14 --keep-weekly 8 --keep-monthly 6 --prune`.
4. Write `system_state['backup']` with snapshot id, sizes, duration, status.

On-demand: `docker compose run --rm backup now`.

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
4. Runs `migrate`, then starts the stack, then runs `mwb wiki-reconcile` and queues re-ingest of all projects.

**Drill** (monthly, ~30 min): restore the latest snapshot into a throwaway VM or a separate compose project name (`-p mwb-drill`) with a different port, sign in, open a wiki page, run a search, check a stored token shows "set". Record the result in the System page (`mwb backup record-drill --ok`).

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

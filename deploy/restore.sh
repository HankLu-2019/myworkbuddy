#!/usr/bin/env bash
# Restore MyWorkBuddy from a restic snapshot (design 06 §6).
#
#   deploy/restore.sh [--snapshot latest|<id>] [--force]
#
# Run from the repository root with the production .env (same MWB_SECRET_KEYS, or stored tokens can't be
# decrypted; same MWB_BACKUP_REPO and RESTIC_PASSWORD). For a drill next to the live stack, use another compose
# project and network, and start only what you need:
#
#   COMPOSE_PROJECT_NAME=mwb-drill MWB_EDGE_PREFIX=172.31.0 MWB_HTTPS_PORT=8443 MWB_HTTP_PORT=8081 \
#     deploy/restore.sh --snapshot latest
#
# Environment: MWB_COMPOSE_FILES (extra "-f file" arguments), MWB_RESTORE_SERVICES (services to start afterwards;
# default: the whole stack).
set -euo pipefail

snapshot=latest
force=()
while [ $# -gt 0 ]; do
  case "$1" in
    --snapshot) snapshot="$2"; shift 2 ;;
    --force) force=(--force); shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# shellcheck disable=SC2086
compose() { docker compose -f docker-compose.yml ${MWB_COMPOSE_FILES:-} "$@"; }

echo "== stopping app services (if running)"
compose stop api worker caddy >/dev/null 2>&1 || true

echo "== starting postgres"
compose up -d --wait postgres

echo "== restoring snapshot $snapshot (databases and /data)"
compose run --rm backup restore --snapshot "$snapshot" "${force[@]}"

echo "== migrating and starting the stack"
# shellcheck disable=SC2086
compose up -d --wait ${MWB_RESTORE_SERVICES:-}

echo "== reconciling the wiki and queueing re-ingest (repo caches aren't in backups)"
compose exec -T api mwb wiki-reconcile
compose exec -T api mwb reingest-all

echo "== checking"
compose exec -T api mwb backup verify-restore

echo "Restore finished. For a drill, record it: docker compose exec api mwb backup record-drill --ok --note '...'"

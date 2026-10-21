#!/bin/sh
# Entry point of the backup service (design 06 §5, §6).
#   daemon (default)          back up every night at MWB_BACKUP_TIME
#   now                       back up once, now
#   restore [--snapshot ID] [--force]   used by deploy/restore.sh
#   list [SNAPSHOT]           files in a snapshot
#   anything else             run it as a command (e.g. mwb backup record-drill --ok)
set -e
case "${1:-daemon}" in
  daemon) exec mwb backup daemon ;;
  now) exec mwb backup run ;;
  restore) shift; exec mwb backup restore "$@" ;;
  list) shift; exec mwb backup list "$@" ;;
  *) exec "$@" ;;
esac

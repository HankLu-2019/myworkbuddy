#!/usr/bin/env bash
# Vault starts sealed after every restart of its container or the VM; MyWorkBuddy then refuses to start with
# "Vault is sealed". Unseal it with the key from the password manager (asked for, never put in argv or history):
#   deploy/vault/unseal.sh && docker compose restart api worker worker-jobs
set -euo pipefail
cd "$(dirname "$0")/../.."
read -rsp "Unseal key: " key; echo
docker compose --profile vault exec -T -e VAULT_ADDR=http://127.0.0.1:8200 -e KEY="$key" vault sh -c 'vault operator unseal "$KEY"' >/dev/null
echo "unsealed"

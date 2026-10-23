#!/usr/bin/env bash
# First-time setup of the compose Vault (design 08, "Secrets in Vault"). Run once on the VM from the repo root:
#   docker compose --profile vault up -d vault && deploy/vault/init.sh
# Prints the unseal key and root token ONCE: store both in the password manager, not on the VM.
set -euo pipefail
cd "$(dirname "$0")/../.."
v() { docker compose --profile vault exec -T -e VAULT_ADDR=http://127.0.0.1:8200 ${VAULT_TOKEN:+-e VAULT_TOKEN} vault vault "$@"; }

if v status -format=json | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["initialized"] else 1)'; then
  echo "Vault is already initialised. To unseal it: deploy/vault/unseal.sh" >&2
  exit 1
fi
init=$(v operator init -key-shares=1 -key-threshold=1 -format=json)
unseal=$(echo "$init" | python3 -c 'import json,sys; print(json.load(sys.stdin)["unseal_keys_b64"][0])')
export VAULT_TOKEN=$(echo "$init" | python3 -c 'import json,sys; print(json.load(sys.stdin)["root_token"])')
v operator unseal "$unseal" >/dev/null

v secrets enable transit
v write -f transit/keys/mwb type=aes256-gcm96 exportable=false allow_plaintext_backup=false
v write transit/keys/mwb/config deletion_allowed=false
docker compose --profile vault exec -T vault sh -c 'cat > /tmp/mwb-policy.hcl' < deploy/vault/policy.hcl
v policy write mwb /tmp/mwb-policy.hcl
v auth enable approle
v write auth/approle/role/mwb token_policies=mwb token_ttl=1h token_max_ttl=4h secret_id_ttl=0
role_id=$(v read -field=role_id auth/approle/role/mwb/role-id)
secret_id=$(v write -f -field=secret_id auth/approle/role/mwb/secret-id)

cat <<OUT

Vault is initialised and unsealed. Store these two in the password manager now; they are not saved anywhere:
  unseal key: $unseal
  root token: $VAULT_TOKEN

Add to .env, then restart api, worker and worker-jobs:
  MWB_SECRETS_BACKEND=vault
  MWB_VAULT_ADDR=http://vault:8200
  MWB_VAULT_ROLE_ID=$role_id
  MWB_VAULT_SECRET_ID=$secret_id

Then move existing secrets under Vault's key:  docker compose exec api mwb rotate-secrets
OUT

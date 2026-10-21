#!/usr/bin/env bash
# Pin every third-party image in the compose files to its current digest (design 01 §H1, ADR-10).
# Run on a machine with registry access, review the diff, commit. Re-run deliberately to upgrade.
#   deploy/pin-images.sh            # pin tags that have no digest yet
#   deploy/pin-images.sh --refresh  # re-resolve all digests for the current tags
set -euo pipefail
cd "$(dirname "$0")/.."
refresh=${1:-}
files=(docker-compose.yml compose.override.yml)
[[ -f compose.ci.yml ]] && files+=(compose.ci.yml)

for f in "${files[@]}"; do
  grep -oE '^\s*image:\s*\S+' "$f" | awk '{print $2}' | sort -u | while read -r ref; do
    [[ "$ref" == myworkbuddy-* ]] && continue            # our own images
    tag="${ref%@sha256:*}"
    if [[ "$ref" == *@sha256:* && "$refresh" != "--refresh" ]]; then continue; fi
    digest=$(docker buildx imagetools inspect "$tag" --format '{{json .Manifest}}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["digest"])')
    echo "$f: $tag -> $digest"
    python3 - "$f" "$ref" "$tag@$digest" <<'PY'
import sys, pathlib
path, old, new = sys.argv[1:]
p = pathlib.Path(path)
p.write_text(p.read_text().replace(f"image: {old}\n", f"image: {new}\n"))
PY
  done
done
# Dockerfile base images
for df in backend/Dockerfile frontend/Dockerfile; do
  grep -oE '^FROM \S+' "$df" | awk '{print $2}' | sort -u | while read -r ref; do
    [[ "$ref" != */* && "$ref" != *:* ]] && continue      # stage names
    tag="${ref%@sha256:*}"
    if [[ "$ref" == *@sha256:* && "$refresh" != "--refresh" ]]; then continue; fi
    digest=$(docker buildx imagetools inspect "$tag" --format '{{json .Manifest}}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["digest"])')
    echo "$df: $tag -> $digest"
    sed -i "s|^FROM ${ref}\b|FROM ${tag}@${digest}|" "$df"
  done
done
echo "Done. Review with: git diff"

#!/usr/bin/env bash
# Turn the last lines of a log into one GitHub error annotation, so failures are readable from the checks API.
#   annotate-tail.sh <title> <file> [lines]
set -uo pipefail
title=$1; file=$2; lines=${3:-60}
[[ -f "$file" ]] || { echo "::error title=$title::(no log file $file)"; exit 0; }
body=$(tail -n "$lines" "$file" | cut -c1-300 | sed -e 's/%/%25/g' -e 's/\r//g' | awk '{printf "%s%%0A", $0}')
echo "::error title=$title::$body"

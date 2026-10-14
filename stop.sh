#!/usr/bin/env bash
# Shares the identity checks and lock in start.sh; no broad pkill commands.
set -Eeuo pipefail
comfy_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "$comfy_dir/start.sh" ]] || {
    printf 'Missing %s/start.sh; install both scripts together.\n' "$comfy_dir" >&2
    exit 1
}
exec bash "$comfy_dir/start.sh" --stop

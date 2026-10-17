#!/usr/bin/env bash
# Issues LiteLLM virtual keys for clients and writes them into .env.
# Only fills empty LITELLM_*_KEY variables, so re-running does not rotate existing keys.
set -euo pipefail

cd "$(dirname "$0")/.."
GATEWAY=${GATEWAY:-http://127.0.0.1:4000}
MASTER=$(grep -E '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-)
[ -n "$MASTER" ] || { echo "LITELLM_MASTER_KEY is empty in .env" >&2; exit 1; }

# Existing keys are updated by sync-key-models.sh. This script only fills empty variables.
ALL_MODELS='["coder","fast","reasoner","reviewer","vision","embed","agent"]'

issue() {
  local var=$1 alias=$2 models=$3
  local current
  current=$(grep -E "^${var}=" .env | cut -d= -f2- || true)
  if [ -n "$current" ]; then
    echo "$var: already set, skipped"
    return
  fi
  local key
  key=$(curl -fsS "$GATEWAY/key/generate" \
    -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
    -d "{\"key_alias\":\"$alias\",\"models\":$models,\"metadata\":{\"client\":\"$alias\"}}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"])')
  if grep -qE "^${var}=" .env; then
    sed -i "s|^${var}=.*|${var}=${key}|" .env
  else
    echo "${var}=${key}" >> .env
  fi
  echo "$var: issued ($alias)"
}

issue LITELLM_OPENWEBUI_KEY openwebui "$ALL_MODELS"
issue LITELLM_CONTINUE_KEY  continue  "$ALL_MODELS"
issue LITELLM_ROO_KEY       roo       "$ALL_MODELS"
issue LITELLM_OPENCODE_KEY  opencode  "$ALL_MODELS"
issue LITELLM_TASKFLOW_KEY  taskflow  "$ALL_MODELS"
issue LITELLM_DEVMEM_KEY    devmem    '["embed"]'

#!/usr/bin/env bash
# Updates the models allowlist of existing virtual keys. Does not rotate them.
# Prints variable names and status only, never key values.
# devmem stays embed-only. Re-running is a no-op when the allowlist already matches.
set -euo pipefail

cd "$(dirname "$0")/.."
GATEWAY=${GATEWAY:-http://127.0.0.1:4000}

ALL_MODELS='["coder","fast","reasoner","reviewer","vision","embed","agent"]'
EMBED_ONLY='["embed"]'

python3 - "$GATEWAY" "$ALL_MODELS" "$EMBED_ONLY" << 'END_SYNC_PY'
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

gateway, all_models_json, embed_json = sys.argv[1:]
all_models = json.loads(all_models_json)
embed_models = json.loads(embed_json)

env = {}
for line in Path(".env").read_text().splitlines():
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, value = line.split("=", 1)
    env[name] = value

master = env.get("LITELLM_MASTER_KEY", "")
if not master:
    print("LITELLM_MASTER_KEY is empty in .env", file=sys.stderr)
    sys.exit(1)

targets = [
    ("LITELLM_OPENWEBUI_KEY", all_models),
    ("LITELLM_CONTINUE_KEY", all_models),
    ("LITELLM_ROO_KEY", all_models),
    ("LITELLM_OPENCODE_KEY", all_models),
    ("LITELLM_TASKFLOW_KEY", all_models),
    ("LITELLM_DEVMEM_KEY", embed_models),
]


def request(method, path, payload, label):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        gateway + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + master,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        print(f"{label}: HTTP {exc.code}", file=sys.stderr)
        sys.exit(1)
    except Exception:
        print(f"{label}: request failed", file=sys.stderr)
        sys.exit(1)


def models_of(var, key):
    data = request("GET", "/key/info?" + urlencode({"key": key}), None, var)
    info = data.get("info") or {}
    models = info.get("models") or []
    if not isinstance(models, list):
        print(f"{var}: models field is not a list", file=sys.stderr)
        sys.exit(1)
    return [str(item) for item in models]


for var, desired in targets:
    key = env.get(var, "")
    if not key:
        print(f"{var}: empty, skipped")
        continue
    current = models_of(var, key)
    if sorted(current) == sorted(desired):
        print(f"{var}: already matches")
        continue
    request("POST", "/key/update", {"key": key, "models": desired}, var)
    updated = models_of(var, key)
    if sorted(updated) != sorted(desired):
        print(f"{var}: update did not stick", file=sys.stderr)
        sys.exit(1)
    print(f"{var}: updated")
END_SYNC_PY

#!/usr/bin/env bash
# Загружает рабочую пару моделей в фиксированном порядке: сначала coder, потом fast.
# Если fast загрузится первой, coder может не влезть в 16 ГБ VRAM целиком и частично уйдёт на CPU.
set -euo pipefail

OLLAMA=http://127.0.0.1:11434
CODER=qwen2.5-coder-14b-32k
FAST=qwen2.5-coder-3b-4k

for m in $(ollama ps | awk 'NR>1 {print $1}'); do
  ollama stop "$m"
done
while [ "$(ollama ps | wc -l)" -gt 1 ]; do sleep 1; done

load() {
  curl -fsS "$OLLAMA/api/generate" -d "{\"model\":\"$1\",\"keep_alive\":-1}" > /dev/null \
    || { echo "Не удалось загрузить $1" >&2; exit 1; }
}

load "$CODER"
load "$FAST"
ollama ps

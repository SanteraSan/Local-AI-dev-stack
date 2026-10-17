#!/usr/bin/env bash
# Загружает рабочую пару: сначала большая модель профиля, потом fast.
# Профили: coder (по умолчанию) — qwen2.5-coder-14b-32k; agent — qwen3-14b-32k.
# coder и agent вместе с fast в 16 ГБ не помещаются: смена профиля выгружает другую пару.
# Ollama выбирает CPU/GPU один раз при загрузке. Если fast не на 100% GPU, скрипт
# выгружает её и пробует ещё раз.
# --force-gpu грузит fast с num_gpu=99 (все слои на GPU). Если видеопамяти
# действительно не хватает, загрузка завершится ошибкой, а не частичным размещением.
# Коды: 0 — обе модели 100% GPU; 2 — загружены, но размещение неполное; 1 — сбой загрузки.
set -euo pipefail

OLLAMA=http://127.0.0.1:11434
CODER=qwen2.5-coder-14b-32k
AGENT=qwen3-14b-32k
FAST=qwen2.5-coder-3b-4k

PROFILE=coder
FORCE_GPU=0

usage() {
  echo "Использование: $0 [--force-gpu] [coder|agent]" >&2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --force-gpu) FORCE_GPU=1 ;;
    coder|agent) PROFILE=$1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Неизвестный аргумент: $1" >&2; usage; exit 1 ;;
  esac
  shift
done

case "$PROFILE" in
  coder) BIG=$CODER ;;
  agent) BIG=$AGENT ;;
esac

for m in $(ollama ps | awk 'NR>1 {print $1}'); do
  ollama stop "$m"
done
while [ "$(ollama ps | wc -l)" -gt 1 ]; do sleep 1; done

load() {
  local model=$1 options=${2:-}
  local payload
  if [ -n "$options" ]; then
    payload=$(printf '{"model":"%s","keep_alive":-1,"options":%s}' "$model" "$options")
  else
    payload=$(printf '{"model":"%s","keep_alive":-1}' "$model")
  fi
  curl -fsS "$OLLAMA/api/generate" -H 'Content-Type: application/json' -d "$payload" >/dev/null \
    || { echo "Не удалось загрузить $model" >&2; exit 1; }
}

# $5/$6 of `ollama ps` are the PROCESSOR column: "100% GPU" or "3%/97% CPU/GPU".
is_full_gpu() {
  local proc
  proc=$(ollama ps | awk -v model="$1" 'NR>1 && ($1 == model || index($1, model ":") == 1) {print $5, $6}')
  [ "$proc" = "100% GPU" ]
}

loaded() {
  ollama ps | awk -v model="$1" 'NR>1 && ($1 == model || index($1, model ":") == 1) {found=1} END {exit found ? 0 : 1}'
}

unload() {
  ollama stop "$1"
  while loaded "$1"; do sleep 1; done
}

load_fast() {
  if [ "$FORCE_GPU" -eq 1 ]; then
    load "$FAST" '{"num_gpu":99}'
  else
    load "$FAST"
  fi
}

warn_placement() {
  local mem used total
  mem=$(nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits | tr -d ' ')
  used=${mem%%,*}
  total=${mem##*,}
  echo "Предупреждение: модель не целиком на GPU. Видеопамять ${used}/${total} МиБ." >&2
  if ! is_full_gpu "$BIG"; then
    echo "  $BIG не на 100% GPU." >&2
  fi
  if ! is_full_gpu "$FAST"; then
    echo "  $FAST не на 100% GPU." >&2
  fi
  echo "Крупные потребители видеопамяти:" >&2
  nvidia-smi -q -d PIDS | awk '
    /Process ID/ { pid = $NF }
    /^[[:space:]]+Name[[:space:]]+:/ {
      line = $0
      sub(/^[^:]+:[[:space:]]*/, "", line)
      split(line, parts, " ")
      n = split(parts[1], a, "/")
      exe = a[n]
    }
    /Used GPU Memory/ { printf "%8s %s  pid %s  %s\n", $(NF-1), $NF, pid, exe }
  ' | sort -nr | head -n 5 >&2 || true
  if [ "$FORCE_GPU" -eq 1 ]; then
    echo "Даже с --force-gpu размещение неполное. Закройте видео или браузер и повторите прогрев." >&2
  else
    echo "Закройте видео или браузер и повторите прогрев, либо запустите с --force-gpu." >&2
    echo "--force-gpu грузит fast с num_gpu=99 и может упасть, если видеопамяти действительно не хватает." >&2
  fi
}

echo "Профиль: $PROFILE ($BIG, затем $FAST)" >&2
load "$BIG"
load_fast
if ! is_full_gpu "$FAST"; then
  echo "fast не на 100% GPU, повторная загрузка." >&2
  unload "$FAST"
  load_fast
fi

status=0
if ! is_full_gpu "$BIG" || ! is_full_gpu "$FAST"; then
  warn_placement
  status=2
fi
ollama ps
exit "$status"

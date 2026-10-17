#!/usr/bin/env python3
"""Tool-calling probe through the local LiteLLM gateway.

Reads a virtual key from the repo .env at runtime (LITELLM_ROO_KEY, otherwise
LITELLM_TASKFLOW_KEY). The key is never printed.

    python3 bench/tool_calling/run.py --alias coder --ollama-model qwen2.5-coder-14b-32k
    python3 bench/tool_calling/run.py --summarize
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from cases import CASES, SYSTEM, TOOLS, TOOL_NAMES

REPO = Path(__file__).resolve().parents[2]
GATEWAY = "http://127.0.0.1:4000/v1/chat/completions"
OLLAMA = "http://127.0.0.1:11434"
FAST_MODEL = "qwen2.5-coder-3b-4k"
REQUEST_TIMEOUT_S = 320
MAX_TOKENS = 2048

_PS_HEAD = re.compile(
    r"^(?P<name>\S+)\s+(?P<id>[0-9a-f]{6,})\s+"
    r"(?P<size>\d+(?:\.\d+)?\s+[A-Za-z]+)\s+(?P<rest>.+)$"
)
# Context is the first integer that is not a processor percentage ("20%").
_PS_CONTEXT = re.compile(r"(?<![\d.])(\d+)(?![\d%])")
_TOOL_NAME_RE = re.compile("|".join(re.escape(name) for name in sorted(TOOL_NAMES)))


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def gateway_key() -> str:
    env = read_dotenv(REPO / ".env")
    key = env.get("LITELLM_ROO_KEY") or env.get("LITELLM_TASKFLOW_KEY") or ""
    if not key:
        sys.exit("LITELLM_ROO_KEY or LITELLM_TASKFLOW_KEY is missing from .env")
    return key


def schema_for(tool_name: str) -> dict | None:
    for tool in TOOLS:
        if tool["function"]["name"] == tool_name:
            return tool["function"]["parameters"]
    return None


def parse_arguments(raw) -> tuple[dict | None, str | None]:
    if isinstance(raw, dict):
        return raw, None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, f"invalid JSON: {exc.msg}"
        if not isinstance(parsed, dict):
            return None, "arguments JSON must be an object"
        return parsed, None
    if raw is None:
        return None, "arguments are missing"
    return None, f"unsupported arguments type: {type(raw).__name__}"


def schema_errors(tool_name: str, args: dict | None) -> list[str]:
    if tool_name not in TOOL_NAMES:
        return [f"unknown tool {tool_name}"]
    if args is None:
        return ["arguments are not a JSON object"]
    spec = schema_for(tool_name)
    assert spec is not None
    props: dict = spec["properties"]
    errors: list[str] = []
    for key in spec.get("required", []):
        if key not in args:
            errors.append(f"missing {key}")
            continue
        if props[key].get("type") == "string" and not isinstance(args[key], str):
            errors.append(f"{key} must be string")
    for key, value in args.items():
        if key not in props:
            errors.append(f"unexpected {key}")
        elif props[key].get("type") == "string" and not isinstance(value, str):
            errors.append(f"{key} must be string")
    return errors


def args_match(spec: dict, args: dict | None) -> bool:
    if args is None:
        return False
    for key, want in spec.items():
        if key.endswith("_contains"):
            field = key[: -len("_contains")]
            got = args.get(field)
            if not isinstance(got, str):
                return False
            parts = want if isinstance(want, list) else [want]
            if any(part not in got for part in parts):
                return False
            continue
        got = args.get(key)
        if not isinstance(got, str) or got.strip() != want:
            return False
    return True


def content_has_tool_json(content: str) -> bool:
    if not content or "{" not in content:
        return False
    if not _TOOL_NAME_RE.search(content):
        return False
    markers = ('"arguments"', '"path"', '"command"', '"query"', "<tool_call>", '"name"')
    return any(marker in content for marker in markers)


def preview(text: str | None, limit: int = 400) -> str:
    if not text:
        return ""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


def parse_tool_calls(message: dict) -> tuple[list[dict], list[dict]]:
    """Return (scoring calls, follow-up tool_calls payload)."""
    raw_calls = message.get("tool_calls") or []
    scored: list[dict] = []
    follow: list[dict] = []
    for index, call in enumerate(raw_calls):
        function = call.get("function") or {}
        name = function.get("name") or ""
        raw_args = function.get("arguments")
        parsed, parse_error = parse_arguments(raw_args)
        if isinstance(raw_args, str):
            arg_text = raw_args
        else:
            arg_text = json.dumps(parsed or {}, ensure_ascii=False)
        call_id = call.get("id") or f"call_{index}"
        errors = schema_errors(name, parsed)
        if parse_error and "arguments are not a JSON object" not in errors:
            errors = [parse_error, *errors]
        scored.append(
            {
                "id": call_id,
                "name": name,
                "arguments": parsed,
                "schema_errors": errors,
                "schema_valid": not errors,
            }
        )
        follow.append(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arg_text},
            }
        )
    return scored, follow


def score_turns(case: dict, turns: list[dict]) -> dict:
    expected = case["expect"]
    tool_expected = bool(expected)
    names_ok = True
    schema_ok = True
    match_ok = True
    saw_call = False
    argument_errors: list[str] = []
    actual_names: list[list[str]] = []

    if tool_expected and len(turns) < len(expected):
        names_ok = False
        schema_ok = False
        match_ok = False
        argument_errors.append("missing turn")

    for index, expect in enumerate(expected):
        if index >= len(turns):
            break
        calls = turns[index]["calls"]
        actual_names.append([call["name"] for call in calls])
        if calls:
            saw_call = True
        if len(calls) != 1 or calls[0]["name"] != expect["tool"]:
            names_ok = False
        if len(calls) != 1 or not calls[0]["schema_valid"]:
            schema_ok = False
        if calls:
            for call in calls:
                for err in call["schema_errors"]:
                    argument_errors.append(f"turn {index + 1}: {err}")
        else:
            argument_errors.append(f"turn {index + 1}: no tool call")
        if len(calls) != 1 or not args_match(expect["args"], calls[0]["arguments"]):
            match_ok = False
    for turn in turns[len(expected) :]:
        if turn["calls"]:
            saw_call = True
            names_ok = False
            actual_names.append([call["name"] for call in turn["calls"]])

    false_positive = (not tool_expected) and saw_call
    if not tool_expected:
        names_ok = not saw_call
        match_ok = not saw_call
        schema_value = None
        if saw_call:
            schema_value = all(
                call["schema_valid"] for turn in turns for call in turn["calls"]
            )
            if not schema_value:
                argument_errors.extend(
                    f"turn {turn['turn']}: {err}"
                    for turn in turns
                    for call in turn["calls"]
                    for err in call["schema_errors"]
                )
    else:
        schema_value = schema_ok

    return {
        "tool_calls_present": saw_call,
        "tool_correct": names_ok,
        "arguments_valid": schema_value,
        "arguments_match": match_ok if tool_expected else None,
        "false_positive": false_positive,
        "actual_tools": actual_names,
        "argument_errors": argument_errors,
    }


def message_content(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def safe_error(exc: BaseException, key: str) -> str:
    text = f"{type(exc).__name__}: {exc}"
    if key:
        text = text.replace(key, "***")
    return text[:300]


def parse_ps_line(line: str) -> dict | None:
    match = _PS_HEAD.match(line.strip())
    if not match:
        return None
    context = _PS_CONTEXT.search(match.group("rest"))
    if not context:
        return None
    processor = match.group("rest")[: context.start()].strip()
    until = match.group("rest")[context.end() :].strip()
    if not processor or not until:
        return None
    return {
        "name": match.group("name"),
        "id": match.group("id"),
        "size": match.group("size"),
        "processor": processor,
        "context": context.group(1),
        "until": until,
    }


def post_chat(key: str, alias: str, messages: list[dict]) -> tuple[dict | None, str | None, float]:
    payload = {
        "model": alias,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    started = time.perf_counter()
    try:
        response = requests.post(
            GATEWAY,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=REQUEST_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        elapsed = time.perf_counter() - started
        return None, safe_error(exc, key), elapsed
    elapsed = time.perf_counter() - started
    if response.status_code != 200:
        body = response.text[:300].replace("\n", " ")
        return None, f"HTTP {response.status_code}: {body}", elapsed
    try:
        return response.json(), None, elapsed
    except json.JSONDecodeError:
        return None, "response is not JSON", elapsed


def run_case(key: str, alias: str, case: dict) -> dict:
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": case["user"]},
    ]
    turns: list[dict] = []
    error = None
    expected = case["expect"]
    turn_count = max(1, len(expected))

    for turn_index in range(turn_count):
        body, err, elapsed = post_chat(key, alias, messages)
        if err or body is None:
            error = err or "empty response"
            turns.append(
                {
                    "turn": turn_index + 1,
                    "latency_s": round(elapsed, 3),
                    "usage": None,
                    "finish_reason": None,
                    "calls": [],
                    "content_preview": "",
                    "content_has_tool_json": False,
                    "error": error,
                }
            )
            break
        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            error = "response has no choices[0].message"
            turns.append(
                {
                    "turn": turn_index + 1,
                    "latency_s": round(elapsed, 3),
                    "usage": body.get("usage"),
                    "finish_reason": None,
                    "calls": [],
                    "content_preview": "",
                    "content_has_tool_json": False,
                    "error": error,
                }
            )
            break
        calls, follow = parse_tool_calls(message)
        content = message_content(message)
        turns.append(
            {
                "turn": turn_index + 1,
                "latency_s": round(elapsed, 3),
                "usage": body.get("usage"),
                "finish_reason": (body.get("choices") or [{}])[0].get("finish_reason"),
                "calls": calls,
                "content_preview": preview(content),
                "content_has_tool_json": content_has_tool_json(content),
                "error": None,
            }
        )
        if turn_index + 1 >= len(expected) or not follow:
            break
        # History must carry tool_calls with empty content so the next turn
        # sees the call rather than a prose dump of the same JSON.
        messages.append({"role": "assistant", "content": None, "tool_calls": follow})
        synthetic = case.get("tool_result", "")
        expected_name = expected[turn_index]["tool"]
        matched = False
        for call in follow:
            if call["function"]["name"] == expected_name and not matched:
                tool_content = synthetic
                matched = True
            else:
                tool_content = "No result."
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": tool_content,
                }
            )

    scored = score_turns(case, [turn for turn in turns if turn["error"] is None])
    if error:
        scored["tool_correct"] = False
        if case["expect"]:
            scored["arguments_valid"] = False
            scored["arguments_match"] = False
    usage = sum_usage(turns)
    record = {
        "record_type": "case",
        "model": alias,
        "case_id": case["id"],
        "case_name": case["name"],
        "category": case["category"],
        "expected_tools": [item["tool"] for item in case["expect"]],
        "tool_calls_present": scored["tool_calls_present"],
        "tool_correct": scored["tool_correct"],
        "arguments_valid": scored["arguments_valid"],
        "arguments_match": scored["arguments_match"],
        "false_positive": scored["false_positive"],
        "actual_tools": scored["actual_tools"],
        "argument_errors": scored["argument_errors"],
        "content_has_tool_json": any(turn["content_has_tool_json"] for turn in turns),
        "latency_s": round(sum(turn["latency_s"] for turn in turns), 3),
        "usage": usage,
        "turns": turns,
        "error": error,
    }
    return record


def sum_usage(turns: list[dict]) -> dict | None:
    prompt = completion = total = 0
    seen = False
    for turn in turns:
        usage = turn.get("usage") or {}
        if not usage:
            continue
        seen = True
        prompt += int(usage.get("prompt_tokens") or 0)
        completion += int(usage.get("completion_tokens") or 0)
        total += int(usage.get("total_tokens") or 0)
    if not seen:
        return None
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def norm_model(name: str) -> str:
    return name.removesuffix(":latest")


def ollama_ps_rows() -> list[dict]:
    completed = subprocess.run(
        ["ollama", "ps"],
        check=True,
        capture_output=True,
        text=True,
    )
    rows = []
    for line in completed.stdout.splitlines()[1:]:
        parsed = parse_ps_line(line)
        if parsed:
            rows.append(parsed)
    return rows


def ollama_api_ps() -> list[dict]:
    response = requests.get(f"{OLLAMA}/api/ps", timeout=30)
    response.raise_for_status()
    return response.json().get("models") or []


def nvidia_mem() -> dict:
    completed = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    used, total = (part.strip() for part in completed.stdout.strip().split(","))
    return {"used_mib": int(used), "total_mib": int(total)}


def snapshot() -> dict:
    rows = ollama_ps_rows()
    api = ollama_api_ps()
    by_name = {norm_model(item.get("name") or item.get("model") or ""): item for item in api}
    models = []
    for row in rows:
        info = by_name.get(norm_model(row["name"]), {})
        models.append(
            {
                "name": row["name"],
                "size": row["size"],
                "processor": row["processor"],
                "context": int(row["context"]),
                "until": row["until"],
                "size_vram": info.get("size_vram"),
            }
        )
    return {"models": models, "nvidia": nvidia_mem()}


def ollama_stop(name: str) -> None:
    subprocess.run(["ollama", "stop", name], check=True, capture_output=True, text=True)


def ollama_load(name: str) -> None:
    response = requests.post(
        f"{OLLAMA}/api/generate",
        json={"model": name, "keep_alive": -1},
        timeout=REQUEST_TIMEOUT_S,
    )
    response.raise_for_status()


def find_model(models: list[dict], wanted: str) -> dict | None:
    for row in models:
        if norm_model(row["name"]) == norm_model(wanted):
            return row
    return None


def wait_unloaded(timeout_s: float = 60) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not ollama_ps_rows():
            return
        time.sleep(1)
    raise TimeoutError("ollama ps did not become empty")


def probe_vram(ollama_model: str, immediate: dict | None = None) -> dict:
    # immediate is the post-bench ps, taken before this function unloads anything.
    if immediate is None:
        immediate = snapshot()
    probe_error = None
    try:
        # Same order as warmup.sh: empty VRAM, large model, then fast.
        # Keeping the large model resident and loading fast second can spill
        # a layer onto CPU even when a clean load would not.
        for row in ollama_ps_rows():
            ollama_stop(row["name"])
        wait_unloaded()
        ollama_load(ollama_model)
        ollama_load(FAST_MODEL)
        with_fast = snapshot()
    except (subprocess.CalledProcessError, requests.RequestException, OSError, TimeoutError) as exc:
        probe_error = type(exc).__name__
        with_fast = None
    fits = False
    co_resident = False
    reason = probe_error or ""
    if with_fast is not None:
        candidate = find_model(with_fast["models"], ollama_model)
        fast = find_model(with_fast["models"], FAST_MODEL)
        used = with_fast["nvidia"]["used_mib"]
        total = with_fast["nvidia"]["total_mib"]
        if candidate is None:
            reason = f"{ollama_model} is not loaded next to {FAST_MODEL}"
        elif fast is None:
            reason = f"{FAST_MODEL} is not loaded next to {ollama_model}"
        else:
            co_resident = True
            both_gpu = candidate["processor"] == "100% GPU" and fast["processor"] == "100% GPU"
            fits = both_gpu
            reason = (
                f"{norm_model(candidate['name'])} {candidate['size']} {candidate['processor']}; "
                f"{norm_model(fast['name'])} {fast['size']} {fast['processor']}; "
                f"nvidia {used}/{total} MiB"
            )
    return {
        "ollama_model": ollama_model,
        "fast_model": FAST_MODEL,
        "method": "unload all, load candidate, load fast",
        "immediate": immediate,
        "with_fast": with_fast,
        "co_resident": co_resident,
        "fits_with_fast": fits,
        "reason": reason,
    }


def results_path(day: str) -> Path:
    return REPO / "bench" / "results" / f"{day}-tool-calling.jsonl"


def summary_path(day: str) -> Path:
    return REPO / "bench" / "results" / f"{day}-tool-calling-summary.md"


def upsert(path: Path, alias: str, records: list[dict]) -> None:
    kept: list[dict] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            if obj.get("model") != alias:
                kept.append(obj)
    kept.extend(records)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(obj, ensure_ascii=False) + "\n" for obj in kept)
    path.write_text(text, encoding="utf-8")


def load_records(path: Path) -> list[dict]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def pct(numer: int, denom: int) -> str:
    if denom == 0:
        return "n/a"
    return f"{numer}/{denom} ({100.0 * numer / denom:.0f}%)"


def model_order(records: list[dict]) -> list[str]:
    seen: list[str] = []
    for record in records:
        name = record.get("model")
        if name and name not in seen:
            seen.append(name)
    return seen


def summarize(day: str) -> Path:
    path = results_path(day)
    records = load_records(path)
    cases = [r for r in records if r.get("record_type") == "case"]
    hardware = {
        r["model"]: r for r in records if r.get("record_type") == "hardware"
    }
    lines = [
        f"# Tool calling, {day}",
        "",
        "Прогон через шлюз `http://127.0.0.1:4000/v1/chat/completions`, 15 кейсов, temperature 0.",
        "Инструменты не выполнялись: после `tool_calls` в историю подставлялся синтетический `role: tool`.",
        "Алиас `coder` этим файлом не выбирается и не меняется. `agent-candidate` — временный алиас на `qwen3-14b-32k`, в закоммиченный `litellm/config.yaml` он не входит.",
        "",
        "| Модель | Правильный инструмент | Из них где инструмент нужен | Валидные аргументы | Аргументы совпали | Ложные срабатывания | Средняя задержка, с | SIZE | PROCESSOR | С fast |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    notes: list[str] = []
    for alias in model_order(cases):
        rows = [r for r in cases if r["model"] == alias]
        tool_rows = [r for r in rows if r["category"] != "no_tool"]
        no_tool = [r for r in rows if r["category"] == "no_tool"]
        correct = sum(1 for r in rows if r["tool_correct"])
        needed_ok = sum(1 for r in tool_rows if r["tool_correct"])
        schema_ok = sum(1 for r in tool_rows if r["arguments_valid"] is True)
        matched = sum(1 for r in tool_rows if r["arguments_match"] is True)
        false_pos = sum(1 for r in no_tool if r["false_positive"])
        latencies = [r["latency_s"] for r in rows if r.get("latency_s") is not None]
        mean = sum(latencies) / len(latencies) if latencies else 0.0
        hw = hardware.get(alias) or {}
        probe = hw.get("probe") or {}
        immediate = (probe.get("immediate") or {}).get("models") or []
        resident = find_model(immediate, probe.get("ollama_model") or "")
        size = resident["size"] if resident else "—"
        processor = resident["processor"] if resident else "—"
        if probe.get("fits_with_fast") is True:
            fits = "да, обе 100% GPU"
        elif probe.get("co_resident") is True:
            fits = "вместе, не обе 100% GPU"
        elif probe.get("fits_with_fast") is False:
            fits = "нет"
        else:
            fits = "—"
        lines.append(
            "| {alias} | {correct} | {needed} | {schema} | {match} | {false} | {mean:.2f} | {size} | {processor} | {fits} |".format(
                alias=alias,
                correct=pct(correct, len(rows)),
                needed=pct(needed_ok, len(tool_rows)),
                schema=pct(schema_ok, len(tool_rows)),
                match=pct(matched, len(tool_rows)),
                false=pct(false_pos, len(no_tool)),
                mean=mean,
                size=size,
                processor=processor,
                fits=fits,
            )
        )
        text_json = sum(
            1
            for r in rows
            if r.get("content_has_tool_json") and not r.get("tool_calls_present")
        )
        failed = [r["case_id"] for r in rows if not r["tool_correct"] or r.get("error")]
        ollama_name = probe.get("ollama_model") or "—"
        reason = probe.get("reason") or "—"
        ctx = resident["context"] if resident else "—"
        notes.append(
            f"- `{alias}` (`{ollama_name}`, context {ctx}): "
            f"JSON вызова в тексте при пустых tool_calls — {text_json}/{len(rows)}. "
            f"Совместно с fast: {fits} ({reason}). "
            f"Неверный инструмент или ошибка: {', '.join(failed) if failed else 'нет'}."
        )
        by_cat = []
        for category in ("single", "cyrillic", "chain", "no_tool"):
            group = [r for r in rows if r["category"] == category]
            got = sum(1 for r in group if r["tool_correct"])
            by_cat.append(f"{category} {got}/{len(group)}")
        notes.append("  - по категориям: " + ", ".join(by_cat))

    lines.extend(
        [
            "",
            "Знаменатели: «правильный инструмент» — все 15 кейсов (для `no_tool` верно отсутствие вызова); "
            "«где инструмент нужен», валидные аргументы и совпадение аргументов — 12 кейсов `single`, `cyrillic` и `chain` "
            "(схема: обязательные поля и типы, ровно один вызов на шаг); "
            "ложные срабатывания — только 3 кейса `no_tool`.",
            "",
            "SIZE и PROCESSOR сняты `ollama ps` сразу после кейсов этой модели, до проверки совместной загрузки. "
            "«С fast» — выгрузить все модели, загрузить кандидата, затем `qwen2.5-coder-3b-4k` (тот же порядок, что в `warmup.sh`). "
            "«да» значит обе строки `100% GPU`.",
            "",
            "## Заметки по моделям",
            "",
            *notes,
            "",
        ]
    )
    out = summary_path(day)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def run_alias(alias: str, ollama_model: str, day: str, only: str | None, probe: bool) -> None:
    key = gateway_key()
    selected = CASES
    if only:
        selected = [case for case in CASES if case["id"] == only]
        if not selected:
            sys.exit(f"unknown case {only}")
    records = []
    for case in selected:
        record = run_case(key, alias, case)
        record["ollama_model"] = ollama_model
        records.append(record)
        flags = []
        if record["tool_correct"]:
            flags.append("tool-ok")
        if record["false_positive"]:
            flags.append("false+")
        if record["error"]:
            flags.append("error")
        print(
            f"{alias} {case['id']} {record['latency_s']:.1f}s "
            f"tools={record['actual_tools']} {' '.join(flags)}",
            file=sys.stderr,
        )
    if probe and not only:
        print(f"{alias} probing VRAM with {FAST_MODEL}", file=sys.stderr)
        hardware = probe_vram(ollama_model)
        records.append(
            {
                "record_type": "hardware",
                "model": alias,
                "ollama_model": ollama_model,
                "probe": hardware,
            }
        )
        print(
            f"{alias} fits_with_fast={hardware['fits_with_fast']} {hardware['reason']}",
            file=sys.stderr,
        )
    path = results_path(day)
    upsert(path, alias, records)
    print(path, file=sys.stderr)


def self_check() -> None:
    assert schema_errors("read_file", {"path": "a"}) == []
    assert "missing path" in schema_errors("read_file", {})
    assert "path must be string" in schema_errors("read_file", {"path": 1})
    assert "unexpected extra" in schema_errors("read_file", {"path": "a", "extra": "b"})
    assert schema_errors("read_file", None) == ["arguments are not a JSON object"]
    assert args_match({"path": "/home/user/Проект/файл.py"}, {"path": "/home/user/Проект/файл.py"})
    assert args_match({"content": "проверить тесты"}, {"content": "проверить тесты\n"})
    assert not args_match({"query": "счётчик_запросов"}, {"query": "счетчик_запросов"})
    assert args_match(
        {"content_contains": ["timeout: 60", "127.0.0.1"]},
        {"content": "timeout: 60\nhost: 127.0.0.1\n"},
    )
    sample = (
        "qwen2.5-coder-3b-4k:latest      19d1bf3af8d5    2.1 GB    100% GPU     4096       Forever\n"
        "qwen2.5-coder-14b-32k:latest    e1a8845ab4f9    12 GB     100% GPU     32768      Forever\n"
        "qwen3:14b:latest    bdbd181c33f2    11 GB    80% GPU 20% CPU    16384    29 minutes from now\n"
    )
    rows = [parse_ps_line(line) for line in sample.splitlines()]
    assert all(rows)
    assert rows[0]["processor"] == "100% GPU"
    assert rows[1]["size"] == "12 GB"
    assert rows[1]["context"] == "32768"
    assert rows[2]["processor"] == "80% GPU 20% CPU"
    assert rows[2]["context"] == "16384"
    assert rows[2]["until"] == "29 minutes from now"
    no_tool = score_turns({"expect": []}, [{"turn": 1, "calls": []}])
    assert no_tool["tool_correct"] and not no_tool["false_positive"]
    false = score_turns(
        {"expect": []},
        [
            {
                "turn": 1,
                "calls": [
                    {
                        "name": "read_file",
                        "arguments": {"path": "README.md"},
                        "schema_errors": [],
                        "schema_valid": True,
                    }
                ],
            }
        ],
    )
    assert false["false_positive"] and not false["tool_correct"]
    good = score_turns(
        {"expect": [{"tool": "read_file", "args": {"path": "src/app.py"}}]},
        [
            {
                "turn": 1,
                "calls": [
                    {
                        "name": "read_file",
                        "arguments": {"path": "src/app.py"},
                        "schema_errors": [],
                        "schema_valid": True,
                    }
                ],
            }
        ],
    )
    assert good["tool_correct"] and good["arguments_valid"] and good["arguments_match"]
    text = '{"name": "read_file", "arguments": {"path": "src/app.py"}}'
    assert content_has_tool_json(text)
    assert not content_has_tool_json("Рекурсия — это вызов функцией самой себя.")
    assert len(CASES) == 15
    ids = [case["id"] for case in CASES]
    assert len(ids) == len(set(ids))
    print("self-check ok", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe native tool calling via LiteLLM")
    parser.add_argument("--alias")
    parser.add_argument("--ollama-model")
    parser.add_argument("--date", default=datetime.now().astimezone().date().isoformat())
    parser.add_argument("--only", help="Run a single case id")
    parser.add_argument("--no-probe", action="store_true")
    parser.add_argument(
        "--probe-only",
        action="store_true",
        help="Refresh the co-residence probe and keep existing case rows",
    )
    parser.add_argument("--summarize", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if args.summarize:
        out = summarize(args.date)
        print(out)
        return
    if not args.alias or not args.ollama_model:
        sys.exit("--alias and --ollama-model are required")
    if args.probe_only:
        path = results_path(args.date)
        existing = load_records(path) if path.is_file() else []
        previous = next(
            (
                r
                for r in existing
                if r.get("record_type") == "hardware" and r.get("model") == args.alias
            ),
            None,
        )
        immediate = (previous or {}).get("probe", {}).get("immediate")
        hardware = probe_vram(args.ollama_model, immediate=immediate)
        kept = [
            r
            for r in existing
            if not (r.get("record_type") == "hardware" and r.get("model") == args.alias)
        ]
        kept.append(
            {
                "record_type": "hardware",
                "model": args.alias,
                "ollama_model": args.ollama_model,
                "probe": hardware,
            }
        )
        path.write_text(
            "".join(json.dumps(obj, ensure_ascii=False) + "\n" for obj in kept),
            encoding="utf-8",
        )
        print(
            f"{args.alias} fits_with_fast={hardware['fits_with_fast']} {hardware['reason']}",
            file=sys.stderr,
        )
        return
    run_alias(args.alias, args.ollama_model, args.date, args.only, probe=not args.no_probe)


if __name__ == "__main__":
    main()

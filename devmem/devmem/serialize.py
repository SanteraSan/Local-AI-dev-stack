"""Compact tool responses: json, toon, or csv.

The table carries only short homogeneous fields. Long `content` is truncated
and emitted as its own block under the row id. Embeddings, `content_hash`,
and `tsv` are never serialized.
"""

from __future__ import annotations

import csv
import io
import json
import re

MARKER = "context, verify against code"
TRUNCATION_MARK = "…[truncated]"
ENTRY_COLUMNS = ("id", "kind", "date", "commit_sha", "refs")
# Always present when they are part of the requested column list.
_REQUIRED_COLUMNS = frozenset({"id", "kind", "date", "status"})
_NUMBER_RE = re.compile(r"^[+-]?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$")


def truncate_content(text: str, limit: int) -> str:
    if limit < 1:
        raise ValueError("content limit must be >= 1")
    if len(text) <= limit:
        return text
    if limit <= len(TRUNCATION_MARK):
        return text[:limit]
    return text[: limit - len(TRUNCATION_MARK)] + TRUNCATION_MARK


def estimate_tokens(text: str, chars_per_token: int = 4) -> int:
    if chars_per_token < 1:
        raise ValueError("chars_per_token must be >= 1")
    if not text:
        return 0
    return (len(text) + chars_per_token - 1) // chars_per_token


def _empty(value: object) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _active_columns(rows: list[dict], columns: tuple[str, ...] | list[str]) -> list[str]:
    active: list[str] = []
    for column in columns:
        if column in _REQUIRED_COLUMNS:
            active.append(column)
            continue
        if any(not _empty(row.get(column)) for row in rows):
            active.append(column)
    return active


def _must_quote_toon(text: str) -> bool:
    if text == "":
        return True
    if text[0].isspace() or text[-1].isspace():
        return True
    if text in {"true", "false", "null"}:
        return True
    if text[0] in "-#":
        return True
    if _NUMBER_RE.fullmatch(text):
        return True
    for char in text:
        if char in ',:"\\[]{}' or ord(char) < 32:
            return True
    return False


def _quote_toon(text: str) -> str:
    if not _must_quote_toon(text):
        return text
    pieces = ['"']
    for char in text:
        if char == "\\":
            pieces.append("\\\\")
        elif char == '"':
            pieces.append('\\"')
        elif char == "\n":
            pieces.append("\\n")
        elif char == "\r":
            pieces.append("\\r")
        elif char == "\t":
            pieces.append("\\t")
        elif ord(char) < 32:
            pieces.append(f"\\u{ord(char):04X}")
        else:
            pieces.append(char)
    pieces.append('"')
    return "".join(pieces)


def _toon_cell(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return _quote_toon(json.dumps(value, separators=(",", ":"), ensure_ascii=False))
    if value is None:
        return _quote_toon("")
    return _quote_toon(str(value))


def _csv_cell(value: object) -> object:
    if _empty(value):
        return ""
    if isinstance(value, list):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return value


def _content_blocks(rows: list[dict], content_limit: int) -> list[tuple[int, str]]:
    blocks: list[tuple[int, str]] = []
    for row in rows:
        raw = row.get("content")
        if not raw:
            continue
        text = truncate_content(str(raw), content_limit)
        if text:
            blocks.append((int(row["id"]), text))
    return blocks


def _json_rows(rows: list[dict], columns: list[str]) -> list[dict]:
    rendered: list[dict] = []
    for row in rows:
        item: dict = {}
        for column in columns:
            value = row.get(column)
            if _empty(value):
                continue
            item[column] = value
        rendered.append(item)
    return rendered


def encode_table(
    rows: list[dict],
    columns: tuple[str, ...] | list[str],
    fmt: str,
    *,
    content_limit: int,
    table_name: str = "entries",
) -> str:
    fmt = fmt.strip().lower()
    if fmt not in {"json", "toon", "csv"}:
        raise ValueError(f"unknown response format {fmt!r}; expected json, toon, or csv")
    active = _active_columns(rows, columns)
    contents = _content_blocks(rows, content_limit)
    if fmt == "json":
        payload: dict = {table_name: _json_rows(rows, active)}
        if contents:
            payload["content"] = {str(entry_id): text for entry_id, text in contents}
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    if fmt == "csv":
        return _render_csv(active, rows, contents)
    return _render_toon(table_name, active, rows, contents)


def _render_csv(columns: list[str], rows: list[dict], contents: list[tuple[int, str]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(row.get(column, "")) for column in columns])
    table = buffer.getvalue().rstrip("\n")
    if not contents:
        return table
    content_buffer = io.StringIO(newline="")
    content_writer = csv.writer(content_buffer, lineterminator="\n")
    content_writer.writerow(["id", "text"])
    for entry_id, text in contents:
        content_writer.writerow([entry_id, text])
    return table + "\ncontent\n" + content_buffer.getvalue().rstrip("\n")


def _render_toon(
    table_name: str,
    columns: list[str],
    rows: list[dict],
    contents: list[tuple[int, str]],
) -> str:
    header = f"{table_name}[{len(rows)}]{{{','.join(columns)}}}:"
    lines = [header]
    for row in rows:
        cells = ",".join(_toon_cell("" if _empty(row.get(column)) else row.get(column)) for column in columns)
        lines.append(f"  {cells}")
    if contents:
        lines.append(f"content[{len(contents)}]{{id,text}}:")
        for entry_id, text in contents:
            lines.append(f"  {_toon_cell(entry_id)},{_toon_cell(text)}")
    return "\n".join(lines)


def serialize(entries: list[dict], fmt: str, *, content_limit: int = 500) -> str:
    return encode_table(entries, ENTRY_COLUMNS, fmt, content_limit=content_limit)


def format_response(body: str, *, note: str | None = None) -> str:
    parts = [MARKER]
    if note:
        parts.append(note)
    if body:
        parts.append(body)
    return "\n".join(parts)


def _format_tasks(tasks: list[dict]) -> str:
    lines = ["tasks:"]
    for task in tasks:
        line = f"- [{task['status']}] {task['title']} (id={task['id']})"
        plan = str(task.get("plan") or "").strip()
        if plan:
            clipped = plan if len(plan) <= 200 else plan[:200] + "…"
            line += f" plan: {clipped}"
        lines.append(line)
    return "\n".join(lines)


def assemble_brief(
    *,
    tasks: list[dict],
    decisions: list[dict],
    handoff: dict | None,
    lessons: list[dict],
    token_limit: int,
    chars_per_token: int,
    content_limit: int,
    fmt: str,
) -> str:
    """Fit a session brief into `token_limit * chars_per_token` characters.

    The latest handoff comes first: it is what the next model needs. Then open
    tasks, decisions, and lessons. A section that does not fit loses trailing
    items and is serialized again, so JSON, CSV, and TOON stay parseable. If
    even one item does not fit, the section is left out.
    """
    if token_limit < 1:
        raise ValueError("token_limit must be >= 1")
    budget = token_limit * chars_per_token
    blocks = [MARKER]
    sections: list[tuple[str, list[dict]]] = []
    if handoff is not None:
        sections.append(("handoff", [handoff]))
    if tasks:
        sections.append(("tasks", list(tasks)))
    if decisions:
        sections.append(("decisions", list(decisions)))
    if lessons:
        sections.append(("lessons", list(lessons)))

    for name, items in sections:
        block = _fit_section(name, items, blocks, budget, fmt, content_limit)
        if block is not None:
            blocks.append(block)

    while len("\n".join(blocks)) > budget and len(blocks) > 1:
        blocks.pop()
    text = "\n".join(blocks)
    if len(text) > budget:
        # Only the marker can still be over the budget. Do not slice a section.
        text = MARKER[:budget]
    return text


def _section_text(name: str, items: list[dict], fmt: str, content_limit: int) -> str:
    if name == "tasks":
        return _format_tasks(items)
    body = serialize(items, fmt, content_limit=content_limit)
    return f"{name}:\n{body}"


def _fit_section(
    name: str,
    items: list[dict],
    blocks: list[str],
    budget: int,
    fmt: str,
    content_limit: int,
) -> str | None:
    """Longest prefix of `items` whose serialization still fits, or nothing."""
    for count in range(len(items), 0, -1):
        block = _section_text(name, items[:count], fmt, content_limit)
        if len("\n".join([*blocks, block])) <= budget:
            return block
    return None

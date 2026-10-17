import json

from devmem.config import BRIEF_TOKEN_LIMIT, CHARS_PER_TOKEN
from devmem.serialize import MARKER, _format_tasks, assemble_brief, estimate_tokens, serialize


def _entry(entry_id: int, kind: str, content: str, commit_sha: str | None) -> dict:
    return {
        "id": entry_id,
        "kind": kind,
        "date": "2026-10-17T00:00:00Z",
        "commit_sha": commit_sha,
        "refs": ["src/memory.py"],
        "content": content,
    }


def test_small_brief_includes_decision_handoff_and_commit_sha():
    text = assemble_brief(
        tasks=[{"id": 1, "status": "open", "title": "Ship memory", "plan": None}],
        decisions=[_entry(2, "decision", "phase3briefdecision", "abc123")],
        handoff=_entry(3, "handoff", "phase3briefhandoff", "def456"),
        lessons=[_entry(4, "lesson", "a short lesson", "ghi789")],
        token_limit=BRIEF_TOKEN_LIMIT,
        chars_per_token=CHARS_PER_TOKEN,
        content_limit=500,
        fmt="json",
    )
    assert text.startswith(MARKER)
    assert "phase3briefdecision" in text
    assert "phase3briefhandoff" in text
    assert "abc123" in text
    assert "def456" in text
    assert len(text) <= BRIEF_TOKEN_LIMIT * CHARS_PER_TOKEN
    assert estimate_tokens(text, CHARS_PER_TOKEN) <= BRIEF_TOKEN_LIMIT


def test_brief_stays_inside_a_tight_budget():
    huge = "lesson " * 4000
    text = assemble_brief(
        tasks=[{"id": 1, "status": "in_progress", "title": "Trim", "plan": "x" * 500}],
        decisions=[_entry(2, "decision", "D" * 80, "abc123")],
        handoff=_entry(3, "handoff", "H" * 80, "def456"),
        lessons=[_entry(4, "lesson", huge, None)],
        token_limit=80,
        chars_per_token=4,
        content_limit=5000,
        fmt="json",
    )
    assert text.startswith(MARKER)
    assert len(text) <= 80 * 4
    assert estimate_tokens(text, 4) <= 80
    assert huge not in text


def _json_sections(text: str) -> dict[str, dict]:
    parsed: dict[str, dict] = {}
    for name in ("handoff", "decisions", "lessons"):
        header = f"{name}:\n"
        if header not in text:
            continue
        body = text.split(header, 1)[1].split("\n", 1)[0]
        parsed[name] = json.loads(body)
    return parsed


def test_handoff_survives_when_tasks_fill_the_budget():
    tasks = [
        {"id": index, "status": "open", "title": f"Task {index}", "plan": "p" * 200}
        for index in range(1, 21)
    ]
    handoff = _entry(100, "handoff", "phase3handoffkept", "sha100")
    handoff_block = "handoff:\n" + serialize([handoff], "json", content_limit=500)
    two_tasks = _format_tasks(tasks[:2])
    budget = len(MARKER) + 1 + len(handoff_block) + 1 + len(two_tasks)
    assert len(_format_tasks(tasks)) > len(two_tasks)

    text = assemble_brief(
        tasks=tasks,
        decisions=[_entry(2, "decision", "D" * 400, "sha2")],
        handoff=handoff,
        lessons=[_entry(4, "lesson", "L" * 400, None)],
        token_limit=budget,
        chars_per_token=1,
        content_limit=500,
        fmt="json",
    )
    assert "phase3handoffkept" in text
    assert "(id=20)" not in text
    assert len(text) <= budget
    parsed = _json_sections(text)
    assert parsed["handoff"]["content"]["100"] == "phase3handoffkept"


def test_json_sections_parse_when_trailing_items_are_dropped():
    decisions = [
        _entry(index, "decision", f"decision-{index}-" + ("d" * 80), f"sha{index}")
        for index in range(1, 6)
    ]
    handoff = _entry(9, "handoff", "phase3handoffkept", "sha9")
    one = "decisions:\n" + serialize(decisions[:1], "json", content_limit=500)
    two = "decisions:\n" + serialize(decisions[:2], "json", content_limit=500)
    handoff_block = "handoff:\n" + serialize([handoff], "json", content_limit=500)
    budget = len(MARKER) + 1 + len(handoff_block) + 1 + len(one)
    assert len(MARKER) + 1 + len(handoff_block) + 1 + len(two) > budget

    text = assemble_brief(
        tasks=[],
        decisions=decisions,
        handoff=handoff,
        lessons=[_entry(10, "lesson", "Z" * 2000, None)],
        token_limit=budget,
        chars_per_token=1,
        content_limit=500,
        fmt="json",
    )
    assert len(text) <= budget
    parsed = _json_sections(text)
    for body in parsed.values():
        assert set(body) >= {"entries", "content"}
    assert [row["id"] for row in parsed["decisions"]["entries"]] == [1]
    assert "lessons:" not in text

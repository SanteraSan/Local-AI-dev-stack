from devmem.config import BRIEF_TOKEN_LIMIT, CHARS_PER_TOKEN
from devmem.serialize import MARKER, assemble_brief, estimate_tokens


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

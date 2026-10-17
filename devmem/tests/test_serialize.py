import csv
import io
import json

from devmem.serialize import MARKER, TRUNCATION_MARK, format_response, serialize


def _rows() -> list[dict]:
    return [
        {
            "id": 1,
            "kind": "decision",
            "date": "2026-10-17T00:00:00Z",
            "commit_sha": "abc123",
            "refs": ["src/a.py", "docs/note.md"],
            "content": "keep the gateway",
            "embedding": [0.1, 0.2],
            "content_hash": "hash-1",
            "tsv": "keep gateway",
            "status": "active",
            "source": "agent",
        },
        {
            "id": 2,
            "kind": "fact",
            "date": "2026-10-17T01:02:03Z",
            "commit_sha": None,
            "refs": [],
            "content": 'he said "hi",\nthen left',
            "embedding": [0.3],
            "content_hash": "hash-2",
            "tsv": "'hi'",
        },
    ]


def test_three_formats_carry_the_same_entries():
    rows = _rows()
    rendered = {fmt: serialize(rows, fmt, content_limit=500) for fmt in ("json", "toon", "csv")}
    for fmt, text in rendered.items():
        assert "abc123" in text
        assert "decision" in text
        assert "keep the gateway" in text
        if fmt == "csv":
            assert '""hi""' in text
        elif fmt == "toon":
            assert '\\"hi\\"' in text
        else:
            assert "he said" in text and "hi" in text
        assert "embedding" not in text
        assert "content_hash" not in text
        assert "tsv" not in text
        assert "hash-1" not in text
        assert fmt in {"json", "toon", "csv"}


def test_json_is_compact_and_omits_empty_fields():
    raw = serialize(_rows(), "json", content_limit=500)
    assert raw == json.dumps(json.loads(raw), separators=(",", ":"), ensure_ascii=False)
    payload = json.loads(raw)
    assert "commit_sha" not in payload["entries"][1]
    assert "refs" not in payload["entries"][1]
    assert payload["entries"][0]["refs"] == ["src/a.py", "docs/note.md"]
    assert payload["content"]["2"] == 'he said "hi",\nthen left'
    assert ": " not in raw


def test_content_is_truncated_to_the_limit():
    tail = "UNIQUE_TAIL_MARKER"
    content = ("x" * 40) + tail
    raw = serialize(
        [
            {
                "id": 9,
                "kind": "lesson",
                "date": "2026-10-17T00:00:00Z",
                "commit_sha": "abc",
                "refs": [],
                "content": content,
            }
        ],
        "json",
        content_limit=20,
    )
    stored = json.loads(raw)["content"]["9"]
    assert len(stored) <= 20
    assert stored.endswith(TRUNCATION_MARK)
    assert tail not in raw


def test_csv_and_toon_escape_commas_quotes_and_newlines():
    rows = [
        {
            "id": 4,
            "kind": "fact",
            "date": "2026-10-17T00:00:00Z",
            "commit_sha": "abc",
            "refs": ["a,b.py"],
            "content": 'he said "hi",\nthen left',
        }
    ]
    csv_text = serialize(rows, "csv", content_limit=500)
    table_text, content_text = csv_text.split("\ncontent\n", 1)
    table = list(csv.reader(io.StringIO(table_text)))
    assert table[0] == ["id", "kind", "date", "commit_sha", "refs"]
    assert table[1][4] == '["a,b.py"]'
    content_rows = list(csv.reader(io.StringIO(content_text)))
    assert content_rows[1] == ["4", 'he said "hi",\nthen left']

    toon = serialize(rows, "toon", content_limit=500)
    content_header = next(index for index, line in enumerate(toon.split("\n")) if line.startswith("content["))
    content_line = toon.split("\n")[content_header + 1]
    assert "\\n" in content_line
    assert '\\"' in content_line
    assert "\n" not in content_line


def test_format_response_starts_with_the_verify_marker():
    text = format_response('{"entries":[]}', note="embeddings unavailable; full-text only")
    assert text.startswith(MARKER)
    assert "embeddings unavailable; full-text only" in text
    assert "context, verify against code" in text

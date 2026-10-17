"""Embeddings are computed only after the secret check and outside the transaction."""

from contextlib import nullcontext

import pytest

from devmem.importer import ImportSection
from devmem.secrets_filter import SecretRejected
from devmem.server import handoff_memory, import_prepared, save_memory, supersede_memory


def _connection(events: list[str]):
    def connection():
        events.append("connection")
        return nullcontext(object())

    return connection


def test_save_embeds_after_the_secret_check_and_outside_the_transaction(monkeypatch):
    events: list[str] = []

    def fake_embed(text: str):
        events.append("embed")
        assert "connection" not in events
        return [0.25], None

    def fake_save(conn, **kwargs):
        events.append("write")
        assert kwargs["embed"] is False
        assert kwargs["embedding"] == [0.25]
        return {"entry": {"id": 4}, "created": True, "note": None}

    monkeypatch.setattr("devmem.server.try_embed", fake_embed)
    monkeypatch.setattr("devmem.server.save_entry", fake_save)
    monkeypatch.setattr("devmem.server.connection", _connection(events))

    assert save_memory("phase3lab", "fact", "a plain fact") == "saved id=4"
    assert events == ["embed", "connection", "write"]


def test_secret_text_never_reaches_the_embedding_gateway(monkeypatch):
    def fake_embed(text: str):
        raise AssertionError("embedding gateway was called")

    monkeypatch.setattr("devmem.server.try_embed", fake_embed)
    secret = "sk-" + ("a" * 40)
    with pytest.raises(SecretRejected):
        save_memory("phase3lab", "fact", "note " + secret)


def test_unavailable_embeddings_degrade_without_a_gateway_call_inside_the_transaction(monkeypatch):
    seen: dict = {}

    def fake_embed(text: str):
        return None, "embeddings unavailable; full-text only"

    def fake_save(conn, **kwargs):
        seen.update(kwargs)
        return {"entry": {"id": 5}, "created": True, "note": None}

    monkeypatch.setattr("devmem.server.try_embed", fake_embed)
    monkeypatch.setattr("devmem.server.save_entry", fake_save)
    monkeypatch.setattr("devmem.server.connection", _connection([]))

    text = save_memory("phase3lab", "fact", "a plain fact")
    assert seen["embed"] is False
    assert seen["embedding"] is None
    assert "embeddings unavailable; full-text only" in text


def test_supersede_and_handoff_embed_before_the_connection(monkeypatch):
    events: list[str] = []

    def fake_embed(text: str):
        events.append("embed")
        assert "connection" not in events
        return [0.5], None

    def fake_supersede(conn, entry_id, new_content, reason, **kwargs):
        events.append("supersede")
        assert kwargs["embed"] is False
        assert kwargs["embedding"] == [0.5]
        return {"old_id": entry_id, "new_id": 8, "entry": {"id": 8}, "note": kwargs["note"]}

    monkeypatch.setattr("devmem.server.try_embed", fake_embed)
    monkeypatch.setattr("devmem.server.supersede_entry", fake_supersede)
    monkeypatch.setattr("devmem.server.connection", _connection(events))
    assert "new_id=8" in supersede_memory(3, "replacement text", "because")
    assert events == ["embed", "connection", "supersede"]

    events.clear()

    def fake_task_context(conn, task_id):
        return {"id": task_id}, "phase3lab"

    def fake_save(conn, **kwargs):
        events.append("handoff-write")
        assert kwargs["embed"] is False
        assert kwargs["kind"] == "handoff"
        return {
            "entry": {
                "id": 9,
                "kind": "handoff",
                "date": "2026-10-17T00:00:00Z",
                "commit_sha": None,
                "refs": [],
                "content": kwargs["content"],
            },
            "created": True,
            "note": None,
        }

    monkeypatch.setattr("devmem.server.task_context", fake_task_context)
    monkeypatch.setattr("devmem.server.save_entry", fake_save)
    text = handoff_memory(1, "summary text", "next", "none")
    assert events == ["embed", "connection", "handoff-write"]
    assert "summary text" in text


def test_import_checks_every_section_before_embedding(monkeypatch):
    calls: list[str] = []

    def fake_embed(text: str):
        calls.append(text)
        return [0.1], None

    monkeypatch.setattr("devmem.server.try_embed", fake_embed)
    good = ImportSection("research/a.md", "Ok", "clean synthetic section", "hash-a")
    bad = ImportSection("research/a.md", "Bad", "see " + ("AKIA" + ("Z" * 16)), "hash-b")
    with pytest.raises(SecretRejected):
        import_prepared("phase3lab", [good, bad], "imported")
    assert calls == []


def test_import_embeds_before_the_transaction(monkeypatch):
    events: list[str] = []

    def fake_embed(text: str):
        events.append("embed")
        assert "connection" not in events
        return [0.1], None

    def fake_import(conn, project, sections, kind, embeddings):
        events.append("write")
        assert embeddings == [[0.1], [0.1]]
        return {"inserted": 2, "skipped": 0, "note": None}

    monkeypatch.setattr("devmem.server.try_embed", fake_embed)
    monkeypatch.setattr("devmem.server.import_sections", fake_import)
    monkeypatch.setattr("devmem.server.connection", _connection(events))
    sections = [
        ImportSection("research/a.md", "One", "first synthetic section", "hash-1"),
        ImportSection("research/a.md", "Two", "second synthetic section", "hash-2"),
    ]
    result = import_prepared("phase3lab", sections, "imported")
    assert result["inserted"] == 2
    assert events == ["embed", "embed", "connection", "write"]

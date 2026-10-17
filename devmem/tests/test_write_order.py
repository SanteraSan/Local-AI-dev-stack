"""Embeddings are computed only after the secret check and outside the transaction."""

from contextlib import nullcontext

import pytest

from devmem.db import ProjectNotRegistered
from devmem.importer import ImportSection
from devmem.secrets_filter import SecretRejected
from devmem.server import (
    handoff_memory,
    import_prepared,
    save_memory,
    search_memory,
    supersede_memory,
)


def _connection(events: list[str]):
    def connection():
        events.append("connection")
        return nullcontext(object())

    return connection


def _read(events: list[str]):
    def read_connection():
        events.append("read")
        return nullcontext(object())

    return read_connection


def test_save_embeds_after_the_secret_check_and_outside_the_transaction(monkeypatch):
    events: list[str] = []

    def fake_embed(text: str):
        events.append("embed")
        assert events[0] == "read"
        assert "connection" not in events
        return [0.25], None

    def fake_save(conn, **kwargs):
        events.append("write")
        assert kwargs["embed"] is False
        assert kwargs["embedding"] == [0.25]
        return {"entry": {"id": 4}, "created": True, "note": None}

    monkeypatch.setattr("devmem.server.try_embed", fake_embed)
    monkeypatch.setattr("devmem.server.save_entry", fake_save)
    monkeypatch.setattr("devmem.server.read_connection", _read(events))
    monkeypatch.setattr("devmem.server.require_registered_target", lambda conn, project, task_id=None: {"id": 1})
    monkeypatch.setattr("devmem.server.connection", _connection(events))

    assert save_memory("phase3lab", "fact", "a plain fact") == "saved id=4"
    assert events == ["read", "embed", "connection", "write"]


def test_secret_text_never_reaches_the_embedding_gateway(monkeypatch):
    def fake_embed(text: str):
        raise AssertionError("embedding gateway was called")

    monkeypatch.setattr("devmem.server.read_connection", lambda: nullcontext(object()))
    monkeypatch.setattr(
        "devmem.server.require_registered_target",
        lambda conn, project, task_id=None: {"id": 1},
    )
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

    monkeypatch.setattr("devmem.server.read_connection", lambda: nullcontext(object()))
    monkeypatch.setattr(
        "devmem.server.require_registered_target",
        lambda conn, project, task_id=None: {"id": 1},
    )
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

    monkeypatch.setattr("devmem.server.read_connection", _read(events))
    monkeypatch.setattr("devmem.server.task_context", fake_task_context)
    monkeypatch.setattr("devmem.server.save_entry", fake_save)
    text = handoff_memory(1, "summary text", "next", "none")
    assert events == ["read", "embed", "connection", "handoff-write"]
    assert "summary text" in text


def test_import_checks_every_section_before_embedding(monkeypatch):
    calls: list[str] = []

    def fake_embed(text: str):
        calls.append(text)
        return [0.1], None

    monkeypatch.setattr("devmem.server.read_connection", lambda: nullcontext(object()))
    monkeypatch.setattr(
        "devmem.server.existing_content_hashes",
        lambda conn, project, hashes: set(),
    )
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

    monkeypatch.setattr("devmem.server.read_connection", _read(events))
    monkeypatch.setattr(
        "devmem.server.existing_content_hashes",
        lambda conn, project, hashes: set(),
    )
    monkeypatch.setattr("devmem.server.try_embed", fake_embed)
    monkeypatch.setattr("devmem.server.import_sections", fake_import)
    monkeypatch.setattr("devmem.server.connection", _connection(events))
    sections = [
        ImportSection("research/a.md", "One", "first synthetic section", "hash-1"),
        ImportSection("research/a.md", "Two", "second synthetic section", "hash-2"),
    ]
    result = import_prepared("phase3lab", sections, "imported")
    assert result["inserted"] == 2
    assert events == ["read", "embed", "embed", "connection", "write"]


def test_second_import_does_not_embed(monkeypatch):
    sections = [
        ImportSection("research/a.md", "One", "first synthetic section", "hash-1"),
        ImportSection("research/a.md", "Two", "second synthetic section", "hash-2"),
    ]
    calls: list[str] = []

    def fake_embed(text: str):
        calls.append(text)
        return [0.1], None

    def fake_import(conn, project, got, kind, embeddings):
        assert embeddings == [None, None]
        return {"inserted": 0, "skipped": len(got), "note": None}

    monkeypatch.setattr("devmem.server.read_connection", lambda: nullcontext(object()))
    monkeypatch.setattr(
        "devmem.server.existing_content_hashes",
        lambda conn, project, hashes: {section.content_hash for section in sections},
    )
    monkeypatch.setattr("devmem.server.try_embed", fake_embed)
    monkeypatch.setattr("devmem.server.import_sections", fake_import)
    monkeypatch.setattr("devmem.server.connection", lambda: nullcontext(object()))

    result = import_prepared("phase3lab", sections, "imported")
    assert calls == []
    assert result["skipped"] == len(sections)


def test_duplicate_section_in_one_batch_is_embedded_once(monkeypatch):
    section = ImportSection("research/a.md", "One", "same synthetic section", "hash-same")
    calls: list[str] = []

    def fake_embed(text: str):
        calls.append(text)
        return [0.1], None

    def fake_import(conn, project, got, kind, embeddings):
        assert embeddings == [[0.1], None]
        return {"inserted": 1, "skipped": 1, "note": None}

    monkeypatch.setattr("devmem.server.read_connection", lambda: nullcontext(object()))
    monkeypatch.setattr(
        "devmem.server.existing_content_hashes",
        lambda conn, project, hashes: set(),
    )
    monkeypatch.setattr("devmem.server.try_embed", fake_embed)
    monkeypatch.setattr("devmem.server.import_sections", fake_import)
    monkeypatch.setattr("devmem.server.connection", lambda: nullcontext(object()))

    import_prepared("phase3lab", [section, section], "imported")
    assert calls == ["same synthetic section"]


def test_unregistered_project_does_not_embed(monkeypatch):
    calls: list[str] = []

    def fake_read():
        raise ProjectNotRegistered("missing")

    monkeypatch.setattr("devmem.server.read_connection", fake_read)
    monkeypatch.setattr("devmem.server.try_embed", lambda text: calls.append(text))
    with pytest.raises(ProjectNotRegistered):
        save_memory("missing", "fact", "plain text that must not be embedded")
    with pytest.raises(ProjectNotRegistered):
        import_prepared(
            "missing",
            [ImportSection("research/a.md", "One", "plain section", "hash-1")],
            "imported",
        )
    assert calls == []


def test_unknown_task_does_not_embed(monkeypatch):
    calls: list[str] = []

    def fake_task_context(conn, task_id: int):
        raise ValueError(f"task {task_id} not found")

    monkeypatch.setattr("devmem.server.read_connection", lambda: nullcontext(object()))
    monkeypatch.setattr("devmem.server.task_context", fake_task_context)
    monkeypatch.setattr("devmem.server.try_embed", lambda text: calls.append(text))
    with pytest.raises(ValueError, match="not found"):
        handoff_memory(404, "summary text", "next", "none")
    assert calls == []


def test_secret_search_query_is_not_embedded(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("devmem.search.embed_text", lambda text: calls.append(text))
    secret = "sk-" + ("a" * 40)
    with pytest.raises(SecretRejected):
        search_memory("look at " + secret, "phase3lab")
    assert calls == []

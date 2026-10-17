import json
from pathlib import Path

import psycopg
import pytest
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from devmem.config import assert_test_database, derive_test_database_url, get_settings
from devmem.db import apply_migrations, close_pool, connection, count_entries, truncate_all
from devmem.importer import ImportSection, prepare_file
from devmem.secrets_filter import SecretRejected
from devmem.serialize import MARKER
from devmem.server import (
    create_task_record,
    handoff_memory,
    import_prepared,
    render_brief,
    save_memory,
    search_memory,
    supersede_memory,
)

pytestmark = pytest.mark.integration

# Held outside the fixture return value so pytest failure output does not print the DSN.
_TEST_URL: dict[str, str] = {"value": ""}


def _entry_id(text: str) -> int:
    return int(text.split("id=", 1)[1].split()[0])


def _payload(text: str) -> dict:
    return json.loads(text[text.index("{") :])


@pytest.fixture(scope="session")
def _test_database() -> None:
    try:
        url = derive_test_database_url()
    except Exception as exc:
        pytest.skip(f"test database URL is not available ({type(exc).__name__})")
    assert_test_database(url)
    try:
        with psycopg.connect(url, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception:
        pytest.skip("Postgres devmem_test unreachable")
    _TEST_URL["value"] = url
    yield
    _TEST_URL["value"] = ""


@pytest.fixture
def db(_test_database: None, monkeypatch: pytest.MonkeyPatch):
    test_url = _TEST_URL["value"]
    assert_test_database(test_url)
    with psycopg.connect(test_url, row_factory=dict_row) as conn:
        register_vector(conn)
        apply_migrations(conn)
        truncate_all(conn, test_url)
    monkeypatch.setenv("DEVMEM_DATABASE_URL", test_url)
    get_settings.cache_clear()
    close_pool()
    assert_test_database(get_settings().devmem_database_url)
    yield
    close_pool()
    get_settings.cache_clear()


def test_save_search_supersede_handoff_and_brief(db):
    render_brief("phase3lab", None, register=True, task_title="Phase 3 memory")
    saved = save_memory(
        "phase3lab",
        "decision",
        "phase3decisionmarker is the retrieval choice",
        commit_sha="abc1234",
    )
    entry_id = _entry_id(saved)
    found = search_memory("phase3decisionmarker", "phase3lab")
    assert found.startswith(MARKER)
    assert "abc1234" in found
    payload = _payload(found)
    assert payload["entries"][0]["id"] == entry_id
    assert payload["entries"][0]["commit_sha"] == "abc1234"
    assert "phase3decisionmarker" in payload["content"][str(entry_id)]

    with connection() as conn:
        row = conn.execute(
            "SELECT source, status FROM entries WHERE id = %s",
            (entry_id,),
        ).fetchone()
    assert row["source"] == "agent"
    assert row["status"] == "active"

    replaced = supersede_memory(
        entry_id,
        "phase3newdecision replaces the earlier choice",
        "moved the constant into config",
    )
    new_id = int(replaced.split("new_id=", 1)[1].split()[0])
    assert new_id != entry_id
    old_hits = _payload(search_memory("phase3decisionmarker", "phase3lab"))
    assert all(item["id"] != entry_id for item in old_hits["entries"])
    new_hits = _payload(search_memory("phase3newdecision", "phase3lab"))
    assert new_hits["entries"][0]["id"] == new_id
    assert all(item["id"] != entry_id for item in new_hits["entries"])

    with connection() as conn:
        old = conn.execute(
            "SELECT status, superseded_by FROM entries WHERE id = %s",
            (entry_id,),
        ).fetchone()
        new = conn.execute(
            "SELECT refs, status FROM entries WHERE id = %s",
            (new_id,),
        ).fetchone()
    assert old["status"] == "superseded"
    assert old["superseded_by"] == new_id
    assert new["status"] == "active"
    assert new["refs"][-1]["supersedes"] == entry_id
    assert new["refs"][-1]["reason"] == "moved the constant into config"

    task_id = _entry_id(create_task_record("phase3lab", "Finish the memory server"))
    handoff = handoff_memory(
        task_id,
        "phase3handoffmarker stored the decision",
        "run the inspector",
        "none",
    )
    assert handoff.startswith(MARKER)
    brief = render_brief("phase3lab", None, register=True, task_title=None)
    assert brief.startswith(MARKER)
    assert "phase3newdecision" in brief
    assert "phase3handoffmarker" in brief
    assert "abc1234" not in brief


def test_import_twice_is_deduplicated(db, tmp_path: Path):
    root = tmp_path
    folder = root / "research"
    folder.mkdir()
    path = folder / "synthetic-notes.md"
    path.write_text(
        "# Synthetic cache note\n\n"
        "The widget cache key is phase3importmarker and this file is not a project document.\n\n"
        "## Second heading\n\n"
        "Another synthetic section for the importer.\n",
        encoding="utf-8",
    )
    render_brief("phase3import", None, register=True, task_title=None)
    via_absolute = prepare_file(path.resolve(), root)
    via_relative = prepare_file(Path("research/synthetic-notes.md"), root)
    assert via_absolute[0].source_path == "research/synthetic-notes.md"
    assert via_relative[0].content_hash == via_absolute[0].content_hash
    first = import_prepared("phase3import", via_absolute, "imported")
    second = import_prepared("phase3import", via_relative, "imported")
    assert first["inserted"] == 2
    assert second["inserted"] == 0
    assert second["skipped"] == 2
    assert count_entries_for("phase3import") == 2
    found = search_memory("phase3importmarker", "phase3import")
    assert "phase3importmarker" in found
    assert found.startswith(MARKER)
    with connection() as conn:
        row = conn.execute(
            """
            SELECT source, source_path, content_hash
            FROM entries
            WHERE content LIKE %s
            """,
            ("%phase3importmarker%",),
        ).fetchone()
    assert row["source"] == "import"
    assert row["source_path"] == "research/synthetic-notes.md"
    assert row["content_hash"]


def test_unregistered_project_is_rejected(db):
    from devmem.db import ProjectNotRegistered

    with pytest.raises(ProjectNotRegistered, match="not registered"):
        save_memory("not-registered-zz", "fact", "this should not be stored")


def test_secrets_are_rejected_on_write_paths(db, tmp_path: Path):
    render_brief("phase3secrets", None, register=True, task_title=None)
    secret = "sk-" + ("a" * 40)
    with pytest.raises(SecretRejected) as caught:
        save_memory("phase3secrets", "fact", "note " + secret)
    assert caught.value.pattern_type == "openai_key"
    assert secret not in str(caught.value)
    assert count_entries_for("phase3secrets") == 0

    saved = save_memory("phase3secrets", "fact", "phase3secretbase is safe")
    entry_id = _entry_id(saved)
    with pytest.raises(SecretRejected):
        supersede_memory(entry_id, "ghp_" + ("b" * 36), "no")
    with connection() as conn:
        status = conn.execute(
            "SELECT status FROM entries WHERE id = %s",
            (entry_id,),
        ).fetchone()["status"]
    assert status == "active"

    with pytest.raises(SecretRejected):
        create_task_record("phase3secrets", "Leaky plan", "token" + "=" + "not-real")
    task_id = _entry_id(create_task_record("phase3secrets", "Safe task"))
    with pytest.raises(SecretRejected):
        handoff_memory(task_id, "ok", "password" + "=" + "not-real", "none")

    good = ImportSection(
        source_path=str(tmp_path / "synthetic.md"),
        heading="Ok",
        content="clean synthetic import section",
        content_hash="phase3-good-hash",
    )
    bad = ImportSection(
        source_path=str(tmp_path / "synthetic.md"),
        heading="Bad",
        content="see " + ("AKIA" + ("Z" * 16)),
        content_hash="phase3-bad-hash",
    )
    before = count_entries_for("phase3secrets")
    with pytest.raises(SecretRejected):
        import_prepared("phase3secrets", [good, bad], "imported")
    assert count_entries_for("phase3secrets") == before


def test_migrate_is_idempotent(db):
    with connection() as conn:
        assert apply_migrations(conn) == []


def count_entries_for(project: str) -> int:
    with connection() as conn:
        return count_entries(conn, project)

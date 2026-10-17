from pathlib import Path

import pytest

from devmem.config import (
    BIND_HOST,
    BIND_PORT,
    EMBED_DIM,
    EMBED_MODEL,
    EMBED_URL,
    MIGRATIONS_DIR,
    Settings,
    assert_test_database,
    get_settings,
    loopback_bind_host,
    require_server_token,
    swap_database,
)
from devmem.db import ENTRY_KINDS, TASK_STATUSES


def test_loopback_only():
    assert loopback_bind_host("127.0.0.1") == BIND_HOST
    assert loopback_bind_host("localhost") == "127.0.0.1"
    assert loopback_bind_host("::1") == "127.0.0.1"
    with pytest.raises(ValueError, match="non-loopback"):
        loopback_bind_host("0.0.0.0")
    with pytest.raises(ValueError):
        loopback_bind_host("192.168.1.10")


def test_embed_target_is_the_litellm_gateway():
    assert EMBED_URL == "http://127.0.0.1:4000/v1/embeddings"
    assert EMBED_MODEL == "embed"
    assert EMBED_DIM == 768
    assert "11434" not in EMBED_URL
    assert BIND_PORT == 8765


def test_database_name_guard_and_swap():
    production = "postgresql://devai:placeholder@127.0.0.1:5442/devmem"
    with pytest.raises(RuntimeError, match="devmem"):
        assert_test_database(production)
    swapped = swap_database(production, "devmem_test")
    assert assert_test_database(swapped) == "devmem_test"
    assert swapped.endswith("/devmem_test")


def test_empty_token_refuses_to_start():
    with pytest.raises(SystemExit, match="DEVMEM_TOKEN"):
        require_server_token(Settings(devmem_token=""))
    with pytest.raises(SystemExit):
        require_server_token(Settings(devmem_token="   "))


def test_env_file_is_the_repo_root_not_the_cwd(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.devmem_response_format in {"json", "toon", "csv"}
    get_settings.cache_clear()


def test_migration_sql_matches_the_plan():
    sql = (MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8")
    for snippet in (
        "CREATE TABLE projects",
        "CREATE TABLE tasks",
        "CREATE TABLE entries",
        "embedding vector(768)",
        "to_tsvector('simple', content)",
        "USING hnsw (embedding vector_cosine_ops)",
        "USING gin (tsv)",
        "(project_id, status, kind)",
        "(project_id, content_hash) WHERE content_hash IS NOT NULL",
        "status text NOT NULL DEFAULT 'open'",
        "status text NOT NULL DEFAULT 'active'",
    ):
        assert snippet in sql
    assert ENTRY_KINDS == {
        "decision",
        "step_summary",
        "fact",
        "lesson",
        "todo",
        "handoff",
        "imported",
    }
    assert TASK_STATUSES == {"open", "in_progress", "done", "dropped"}

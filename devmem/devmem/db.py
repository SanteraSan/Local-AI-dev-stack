"""Postgres access for projects, tasks, and memory entries.

Destructive helpers refuse to run unless the database name ends with `_test`
and `current_database()` agrees. Production `devmem` is never truncated here.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from devmem.config import (
    MIGRATIONS_DIR,
    assert_test_database,
    database_name,
    get_settings,
)
from devmem.embeddings import try_embed
from devmem.importer import ImportSection
from devmem.search import (
    candidate_limit,
    embed_query,
    full_text_ids,
    fuse_ids,
    search_settings_rrf_k,
    vector_ids,
)
from devmem.secrets_filter import reject_secrets_in

ENTRY_KINDS = frozenset(
    {"decision", "step_summary", "fact", "lesson", "todo", "handoff", "imported"}
)
TASK_STATUSES = frozenset({"open", "in_progress", "done", "dropped"})
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ENTRY_SELECT = """
    id, project_id, task_id, kind, content, status, superseded_by,
    refs, commit_sha, source, source_path, content_hash, client, model, created_at
"""

_pool: ConnectionPool | None = None
_pool_url: str | None = None
_pool_lock = Lock()


class ProjectNotRegistered(ValueError):
    def __init__(self, slug: str) -> None:
        super().__init__(
            f"project {slug!r} is not registered; "
            "call memory_start_session or `devmem project add` first"
        )


def require_kind(kind: str) -> str:
    if kind not in ENTRY_KINDS:
        allowed = ", ".join(sorted(ENTRY_KINDS))
        raise ValueError(f"invalid kind {kind!r}; expected one of {allowed}")
    return kind


def require_task_status(status: str) -> str:
    if status not in TASK_STATUSES:
        allowed = ", ".join(sorted(TASK_STATUSES))
        raise ValueError(f"invalid task status {status!r}; expected one of {allowed}")
    return status


def require_slug(slug: str) -> str:
    if not slug or not _SLUG_RE.fullmatch(slug):
        raise ValueError(
            "invalid project slug; use 1-64 letters, digits, '_' or '-', "
            "starting with a letter or digit"
        )
    return slug


def require_k(k: int) -> int:
    if k < 1 or k > 50:
        raise ValueError("k must be between 1 and 50")
    return k


def require_kinds(kinds: list[str] | None) -> list[str] | None:
    if kinds is None:
        return None
    return [require_kind(kind) for kind in kinds]


def split_sql(sql: str) -> list[str]:
    """Split a migration script on semicolons, dropping `--` comments."""
    lines: list[str] = []
    for line in sql.splitlines():
        if "--" in line:
            line = line[: line.index("--")]
        lines.append(line)
    return [part.strip() for part in "\n".join(lines).split(";") if part.strip()]


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    """Apply unapplied migrations. The caller commits."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version text PRIMARY KEY,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    version = "001_init"
    existing = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version = %s",
        (version,),
    ).fetchone()
    if existing:
        return []
    script = (MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8")
    for statement in split_sql(script):
        conn.execute(statement)
    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
    return [version]


def truncate_all(conn: psycopg.Connection, url: str) -> None:
    """Empty projects, tasks, and entries. Refuses any database not named `*_test`."""
    assert_test_database(url)
    current = conn.execute("SELECT current_database() AS name").fetchone()
    name = current["name"] if isinstance(current, dict) else current[0]
    if not str(name).endswith("_test"):
        raise RuntimeError(f"refusing to truncate database {name!r}")
    conn.execute("TRUNCATE TABLE entries, tasks, projects RESTART IDENTITY CASCADE")


def _configure(conn: psycopg.Connection) -> None:
    register_vector(conn)


def close_pool() -> None:
    global _pool, _pool_url
    with _pool_lock:
        if _pool is not None:
            _pool.close()
        _pool = None
        _pool_url = None


def get_pool() -> ConnectionPool:
    global _pool, _pool_url
    url = get_settings().devmem_database_url.strip()
    if not url:
        raise RuntimeError("DEVMEM_DATABASE_URL is empty")
    with _pool_lock:
        if _pool is not None and _pool_url == url:
            return _pool
        if _pool is not None:
            _pool.close()
        _pool = ConnectionPool(
            conninfo=url,
            min_size=1,
            max_size=8,
            kwargs={"row_factory": dict_row},
            configure=_configure,
            open=True,
            name="devmem",
        )
        _pool_url = url
        return _pool


@contextmanager
def connection():
    pool = get_pool()
    with pool.connection() as conn:
        with conn.transaction():
            yield conn


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry_from_row(row: dict) -> dict:
    refs = row["refs"] if row["refs"] is not None else []
    return {
        "id": int(row["id"]),
        "project_id": row["project_id"],
        "task_id": row["task_id"],
        "kind": row["kind"],
        "content": row["content"],
        "status": row["status"],
        "superseded_by": int(row["superseded_by"]) if row["superseded_by"] is not None else None,
        "refs": refs,
        "commit_sha": row["commit_sha"],
        "source": row["source"],
        "source_path": row["source_path"],
        "content_hash": row["content_hash"],
        "client": row["client"],
        "model": row["model"],
        "date": _iso(row["created_at"]),
    }


def _task_from_row(row: dict) -> dict:
    return {
        "id": int(row["id"]),
        "project_id": row["project_id"],
        "title": row["title"],
        "status": row["status"],
        "plan": row["plan"],
        "date": _iso(row["updated_at"] or row["created_at"]),
    }


def plan_supersede(old: dict, new_content: str, reason: str) -> dict:
    """Describe one supersede without touching the database.

    The old row becomes `superseded` and points at the new id once that id
    exists. The new row keeps the previous refs plus the id and reason.
    """
    if old.get("status") == "superseded":
        raise ValueError(f"entry {old.get('id')} is already superseded")
    if not new_content or not str(new_content).strip():
        raise ValueError("new_content is empty")
    cleaned_reason = (reason or "").strip()
    if not cleaned_reason:
        raise ValueError("reason is empty")
    refs = list(old.get("refs") or [])
    refs.append({"supersedes": int(old["id"]), "reason": cleaned_reason})
    return {
        "project_id": old["project_id"],
        "task_id": old.get("task_id"),
        "kind": old["kind"],
        "content": new_content,
        "refs": refs,
        "old_id": int(old["id"]),
        "old_status": "superseded",
    }


def _fetch_entry(conn: psycopg.Connection, entry_id: int) -> dict | None:
    row = conn.execute(
        f"SELECT {_ENTRY_SELECT} FROM entries WHERE id = %s",
        (entry_id,),
    ).fetchone()
    if row is None:
        return None
    return _entry_from_row(row)


def _insert_entry(
    conn: psycopg.Connection,
    *,
    project_id: int,
    task_id: int | None,
    kind: str,
    content: str,
    refs: list,
    commit_sha: str | None,
    source: str,
    source_path: str | None,
    content_hash: str | None,
    client: str | None,
    model: str | None,
    embedding: list[float] | None,
) -> dict | None:
    row = conn.execute(
        f"""
        INSERT INTO entries (
            project_id, task_id, kind, content, refs, commit_sha,
            source, source_path, content_hash, client, model, embedding
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (project_id, content_hash) WHERE content_hash IS NOT NULL
        DO NOTHING
        RETURNING {_ENTRY_SELECT}
        """,
        (
            project_id,
            task_id,
            kind,
            content,
            Jsonb(refs or []),
            commit_sha,
            source,
            source_path,
            content_hash,
            client,
            model,
            None if embedding is None else Vector(embedding),
        ),
    ).fetchone()
    if row is None:
        return None
    return _entry_from_row(row)


def require_project(conn: psycopg.Connection, slug: str) -> dict:
    require_slug(slug)
    row = conn.execute(
        "SELECT id, slug, root_path, git_remote FROM projects WHERE slug = %s",
        (slug,),
    ).fetchone()
    if row is None:
        raise ProjectNotRegistered(slug)
    return {
        "id": int(row["id"]),
        "slug": row["slug"],
        "root_path": row["root_path"],
        "git_remote": row["git_remote"],
    }


def ensure_project(conn: psycopg.Connection, slug: str) -> dict:
    """Register a project if `memory_start_session` has not seen it yet."""
    require_slug(slug)
    reject_secrets_in(slug)
    conn.execute(
        "INSERT INTO projects (slug) VALUES (%s) ON CONFLICT (slug) DO NOTHING",
        (slug,),
    )
    return require_project(conn, slug)


def add_project(
    conn: psycopg.Connection,
    slug: str,
    root_path: str,
    git_remote: str | None = None,
) -> dict:
    require_slug(slug)
    if not root_path or not root_path.strip():
        raise ValueError("root path is empty")
    reject_secrets_in(slug, root_path, git_remote)
    conn.execute(
        """
        INSERT INTO projects (slug, root_path, git_remote)
        VALUES (%s, %s, %s)
        ON CONFLICT (slug) DO UPDATE
        SET root_path = EXCLUDED.root_path,
            git_remote = COALESCE(EXCLUDED.git_remote, projects.git_remote)
        """,
        (slug, root_path.strip(), git_remote.strip() if git_remote else None),
    )
    return require_project(conn, slug)


def _require_task(conn: psycopg.Connection, task_id: int) -> dict:
    row = conn.execute(
        """
        SELECT id, project_id, title, status, plan, created_at, updated_at
        FROM tasks WHERE id = %s
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"task {task_id} not found")
    return _task_from_row(row)


def _require_task_in_project(conn: psycopg.Connection, task_id: int, project_id: int) -> dict:
    task = _require_task(conn, task_id)
    if task["project_id"] != project_id:
        raise ValueError("task does not belong to this project")
    return task


def ensure_task(conn: psycopg.Connection, project_id: int, title: str) -> dict:
    cleaned = title.strip()
    if not cleaned:
        raise ValueError("task title is empty")
    reject_secrets_in(cleaned)
    existing = conn.execute(
        """
        SELECT id, project_id, title, status, plan, created_at, updated_at
        FROM tasks
        WHERE project_id = %s AND title = %s AND status IN ('open', 'in_progress')
        ORDER BY id
        LIMIT 1
        """,
        (project_id, cleaned),
    ).fetchone()
    if existing:
        return _task_from_row(existing)
    row = conn.execute(
        """
        INSERT INTO tasks (project_id, title)
        VALUES (%s, %s)
        RETURNING id, project_id, title, status, plan, created_at, updated_at
        """,
        (project_id, cleaned),
    ).fetchone()
    return _task_from_row(row)


def create_task(conn: psycopg.Connection, project: str, title: str, plan: str | None) -> dict:
    project_row = require_project(conn, project)
    cleaned = title.strip()
    if not cleaned:
        raise ValueError("task title is empty")
    reject_secrets_in(cleaned, plan)
    row = conn.execute(
        """
        INSERT INTO tasks (project_id, title, plan)
        VALUES (%s, %s, %s)
        RETURNING id, project_id, title, status, plan, created_at, updated_at
        """,
        (project_row["id"], cleaned, plan),
    ).fetchone()
    return _task_from_row(row)


def update_task(
    conn: psycopg.Connection,
    task_id: int,
    status: str | None,
    plan: str | None,
) -> dict:
    if status is None and plan is None:
        raise ValueError("task_update needs a status or a plan")
    if status is not None:
        require_task_status(status)
    reject_secrets_in(plan)
    _require_task(conn, task_id)
    # NULL status/plan means "leave the current value".
    row = conn.execute(
        """
        UPDATE tasks
        SET status = COALESCE(%s, status),
            plan = COALESCE(%s, plan),
            updated_at = now()
        WHERE id = %s
        RETURNING id, project_id, title, status, plan, created_at, updated_at
        """,
        (status, plan, task_id),
    ).fetchone()
    return _task_from_row(row)


def task_context(conn: psycopg.Connection, task_id: int) -> tuple[dict, str]:
    task = _require_task(conn, task_id)
    row = conn.execute(
        "SELECT slug FROM projects WHERE id = %s",
        (task["project_id"],),
    ).fetchone()
    if row is None:
        raise ValueError(f"project for task {task_id} not found")
    return task, row["slug"]


def list_tasks(conn: psycopg.Connection, project: str, status: str | None) -> list[dict]:
    project_row = require_project(conn, project)
    if status is not None:
        require_task_status(status)
    rows = conn.execute(
        """
        SELECT id, project_id, title, status, plan, created_at, updated_at
        FROM tasks
        WHERE project_id = %s AND (%s::text IS NULL OR status = %s)
        ORDER BY updated_at DESC, id DESC
        """,
        (project_row["id"], status, status),
    ).fetchall()
    return [_task_from_row(row) for row in rows]


def save_entry(
    conn: psycopg.Connection,
    *,
    project: str,
    kind: str,
    content: str,
    task_id: int | None = None,
    refs: list | None = None,
    commit_sha: str | None = None,
    source: str = "agent",
    source_path: str | None = None,
    content_hash: str | None = None,
    client: str | None = None,
    model: str | None = None,
    embedding: list[float] | None = None,
    embed: bool = True,
) -> dict:
    require_kind(kind)
    if source not in {"agent", "user", "import"}:
        raise ValueError(f"invalid source {source!r}")
    if not content or not str(content).strip():
        raise ValueError("content is empty")
    reject_secrets_in(content, refs, commit_sha, source_path)
    project_row = require_project(conn, project)
    if task_id is not None:
        _require_task_in_project(conn, task_id, project_row["id"])
    note = None
    vector = embedding
    if embed and vector is None:
        vector, note = try_embed(content)
    entry = _insert_entry(
        conn,
        project_id=project_row["id"],
        task_id=task_id,
        kind=kind,
        content=content,
        refs=list(refs or []),
        commit_sha=commit_sha,
        source=source,
        source_path=source_path,
        content_hash=content_hash,
        client=client,
        model=model,
        embedding=vector,
    )
    return {"entry": entry, "created": entry is not None, "note": note}


def supersede_entry(
    conn: psycopg.Connection,
    entry_id: int,
    new_content: str,
    reason: str,
    *,
    client: str | None = None,
    model: str | None = None,
) -> dict:
    reject_secrets_in(new_content, reason)
    row = conn.execute(
        f"SELECT {_ENTRY_SELECT} FROM entries WHERE id = %s FOR UPDATE",
        (entry_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"entry {entry_id} not found")
    old = _entry_from_row(row)
    planned = plan_supersede(old, new_content, reason)
    reject_secrets_in(planned["refs"])
    vector, note = try_embed(new_content)
    created = _insert_entry(
        conn,
        project_id=planned["project_id"],
        task_id=planned["task_id"],
        kind=planned["kind"],
        content=planned["content"],
        refs=planned["refs"],
        commit_sha=None,
        source="agent",
        source_path=None,
        content_hash=None,
        client=client,
        model=model,
        embedding=vector,
    )
    if created is None:
        raise RuntimeError("failed to insert the replacement entry")
    conn.execute(
        """
        UPDATE entries
        SET status = 'superseded', superseded_by = %s
        WHERE id = %s
        """,
        (created["id"], planned["old_id"]),
    )
    return {"old_id": planned["old_id"], "new_id": created["id"], "entry": created, "note": note}


def entries_by_ids(conn: psycopg.Connection, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    rows = conn.execute(
        f"SELECT {_ENTRY_SELECT} FROM entries WHERE id = ANY(%s)",
        (ids,),
    ).fetchall()
    by_id = {int(row["id"]): _entry_from_row(row) for row in rows}
    return [by_id[entry_id] for entry_id in ids if entry_id in by_id]


def hybrid_search(
    conn: psycopg.Connection,
    *,
    query: str,
    project: str,
    kinds: list[str] | None,
    task_id: int | None,
    k: int,
) -> tuple[list[dict], str | None]:
    if not query or not query.strip():
        raise ValueError("query is empty")
    k = require_k(k)
    kinds = require_kinds(kinds)
    project_row = require_project(conn, project)
    if task_id is not None:
        _require_task_in_project(conn, task_id, project_row["id"])
    limit = candidate_limit(k)
    text_ids = full_text_ids(conn, project_row["id"], query.strip(), kinds, task_id, limit)
    vector, note = embed_query(query.strip())
    ranked_vectors = None
    if vector is not None:
        ranked_vectors = vector_ids(conn, project_row["id"], vector, kinds, task_id, limit)
    fused = fuse_ids(text_ids, ranked_vectors, k=k, rrf_k=search_settings_rrf_k())
    return entries_by_ids(conn, fused), note


def recent_entries(
    conn: psycopg.Connection,
    project_id: int,
    kind: str,
    limit: int,
) -> list[dict]:
    rows = conn.execute(
        f"""
        SELECT {_ENTRY_SELECT}
        FROM entries
        WHERE project_id = %s AND kind = %s AND status = 'active'
        ORDER BY created_at DESC, id DESC
        LIMIT %s
        """,
        (project_id, kind, limit),
    ).fetchall()
    return [_entry_from_row(row) for row in rows]


def brief_parts(conn: psycopg.Connection, project: str) -> dict:
    project_row = require_project(conn, project)
    tasks = conn.execute(
        """
        SELECT id, project_id, title, status, plan, created_at, updated_at
        FROM tasks
        WHERE project_id = %s AND status IN ('open', 'in_progress')
        ORDER BY updated_at DESC, id DESC
        LIMIT 20
        """,
        (project_row["id"],),
    ).fetchall()
    handoffs = recent_entries(conn, project_row["id"], "handoff", 1)
    return {
        "project": project_row,
        "tasks": [_task_from_row(row) for row in tasks],
        "decisions": recent_entries(conn, project_row["id"], "decision", 8),
        "handoff": handoffs[0] if handoffs else None,
        "lessons": recent_entries(conn, project_row["id"], "lesson", 8),
    }


def import_sections(
    conn: psycopg.Connection,
    project: str,
    sections: list[ImportSection],
    kind: str,
) -> dict:
    require_kind(kind)
    project_row = require_project(conn, project)
    inserted = 0
    skipped = 0
    note = None
    for section in sections:
        reject_secrets_in(section.content, section.source_path)
        vector, section_note = try_embed(section.content)
        if section_note and note is None:
            note = section_note
        created = _insert_entry(
            conn,
            project_id=project_row["id"],
            task_id=None,
            kind=kind,
            content=section.content,
            refs=[],
            commit_sha=None,
            source="import",
            source_path=section.source_path,
            content_hash=section.content_hash,
            client=None,
            model=None,
            embedding=vector,
        )
        if created is None:
            skipped += 1
        else:
            inserted += 1
    return {"inserted": inserted, "skipped": skipped, "note": note}


def export_project(conn: psycopg.Connection, project: str) -> list[dict]:
    project_row = require_project(conn, project)
    rows = conn.execute(
        f"""
        SELECT {_ENTRY_SELECT}
        FROM entries
        WHERE project_id = %s
        ORDER BY id
        """,
        (project_row["id"],),
    ).fetchall()
    exported = []
    for row in rows:
        entry = _entry_from_row(row)
        entry["project"] = project_row["slug"]
        exported.append(entry)
    return exported


def reindex(conn: psycopg.Connection, project: str | None) -> list[tuple[int, str]]:
    """Return `(id, content)` pairs to re-embed. The caller writes vectors back."""
    if project is None:
        rows = conn.execute(f"SELECT id, content FROM entries ORDER BY id").fetchall()
    else:
        project_row = require_project(conn, project)
        rows = conn.execute(
            "SELECT id, content FROM entries WHERE project_id = %s ORDER BY id",
            (project_row["id"],),
        ).fetchall()
    return [(int(row["id"]), row["content"]) for row in rows]


def set_embedding(conn: psycopg.Connection, entry_id: int, vector: list[float]) -> None:
    conn.execute(
        "UPDATE entries SET embedding = %s WHERE id = %s",
        (Vector(vector), entry_id),
    )


def count_entries(conn: psycopg.Connection, project: str) -> int:
    project_row = require_project(conn, project)
    row = conn.execute(
        "SELECT count(*) AS n FROM entries WHERE project_id = %s",
        (project_row["id"],),
    ).fetchone()
    return int(row["n"])


def migration_database_name() -> str:
    url = get_settings().devmem_database_url.strip()
    if not url:
        raise RuntimeError("DEVMEM_DATABASE_URL is empty")
    return database_name(url)

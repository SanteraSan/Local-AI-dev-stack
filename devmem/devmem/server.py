"""MCP memory server.

Streamable HTTP listens on 127.0.0.1:8765/mcp. Every HTTP request must carry
`Authorization: Bearer <DEVMEM_TOKEN>`. `--stdio` is the local fallback.
"""

from __future__ import annotations

import hmac
from contextvars import ContextVar

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import Receive, Scope, Send

from devmem.config import (
    BIND_HOST,
    BIND_PORT,
    get_settings,
    loopback_bind_host,
    require_server_token,
)
from devmem.db import (
    add_project,
    brief_parts,
    connection,
    create_task,
    ensure_project,
    ensure_task,
    export_project,
    get_pool,
    hybrid_search,
    import_sections,
    list_tasks,
    require_kind,
    require_project,
    save_entry,
    supersede_entry,
    task_context,
    update_task,
)
from devmem.embeddings import try_embed
from devmem.secrets_filter import reject_secrets_in
from devmem.serialize import assemble_brief, encode_table, format_response, serialize

_client_var: ContextVar[str | None] = ContextVar("devmem_client", default=None)
_model_var: ContextVar[str | None] = ContextVar("devmem_model", default=None)

mcp = FastMCP(
    "devmem",
    instructions=(
        "Project memory for coding agents. Records are context, not evidence: "
        "verify them against the code. Never store secrets, tokens, or private keys."
    ),
    host=BIND_HOST,
    port=BIND_PORT,
    streamable_http_path="/mcp",
    json_response=True,
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "127.0.0.1", "localhost:*", "localhost"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
    ),
)


class BearerAuthMiddleware:
    """Require a bearer token on every HTTP request. Non-HTTP scopes pass through."""

    def __init__(self, app, token: str) -> None:
        self.app = app
        self._expected = b"Bearer " + token.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        presented = _header_bytes(scope, b"authorization")
        if not hmac.compare_digest(presented, self._expected):
            await _send_unauthorized(send)
            return
        client_token = _client_var.set(_header_text(scope, b"x-devmem-client"))
        model_token = _model_var.set(_header_text(scope, b"x-devmem-model"))
        try:
            await self.app(scope, receive, send)
        finally:
            _client_var.reset(client_token)
            _model_var.reset(model_token)


def _header_bytes(scope: Scope, name: bytes) -> bytes:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value
    return b""


def _header_text(scope: Scope, name: bytes) -> str | None:
    raw = _header_bytes(scope, name)
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace").strip()
    return text or None


async def _send_unauthorized(send: Send) -> None:
    body = b'{"error":"unauthorized"}'
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"www-authenticate", b"Bearer"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def create_http_app(token: str):
    """ASGI app with bearer auth in front of the MCP endpoint.

    An empty token refuses to build the app, so the server cannot start open.
    """
    if token is None or not str(token).strip():
        raise RuntimeError("DEVMEM_TOKEN is empty; refusing to start")
    return BearerAuthMiddleware(mcp.streamable_http_app(), token)


def serve_http() -> None:
    settings = get_settings()
    token = require_server_token(settings)
    host = loopback_bind_host(settings.devmem_host)
    get_pool()
    app = create_http_app(token)
    import uvicorn

    uvicorn.run(app, host=host, port=BIND_PORT, log_level="info")


def serve_stdio() -> None:
    require_server_token(get_settings())
    mcp.run(transport="stdio")


def current_actor() -> tuple[str | None, str | None]:
    """Client and model when the MCP session or request headers provide them."""
    client = _client_var.get()
    model = _model_var.get()
    try:
        request_context = mcp.get_context().request_context
        if request_context is None:
            return client, model
        params = getattr(request_context.session, "client_params", None)
        info = getattr(params, "clientInfo", None) if params is not None else None
        name = getattr(info, "name", None) if info is not None else None
        if name and not client:
            client = str(name)
        request = getattr(request_context, "request", None)
        headers = getattr(request, "headers", None)
        if headers is not None and not model:
            header_model = headers.get("x-devmem-model")
            if header_model:
                model = header_model
    except Exception:
        return client, model
    return client, model


def _fmt(override: str | None) -> str:
    return get_settings().response_format(override)


def _limit() -> int:
    return get_settings().devmem_content_char_limit


def _embed_checked(text: str, *extra: object) -> tuple[list[float] | None, str | None]:
    """Reject secrets, then embed. The gateway never sees unchecked text.

    Call this before `connection()`. The database transaction must not wait on
    the embedding HTTP call, and a secret must not leave the process.
    """
    reject_secrets_in(text, *extra)
    return try_embed(text)


def render_brief(
    project: str,
    fmt: str | None,
    *,
    register: bool,
    task_title: str | None,
) -> str:
    settings = get_settings()
    chosen = settings.response_format(fmt)
    with connection() as conn:
        if register:
            project_row = ensure_project(conn, project)
            if task_title:
                ensure_task(conn, project_row["id"], task_title)
        parts = brief_parts(conn, project)
    return assemble_brief(
        tasks=parts["tasks"],
        decisions=parts["decisions"],
        handoff=parts["handoff"],
        lessons=parts["lessons"],
        token_limit=settings.devmem_brief_token_limit,
        chars_per_token=settings.devmem_chars_per_token,
        content_limit=settings.devmem_content_char_limit,
        fmt=chosen,
    )


def search_memory(
    query: str,
    project: str,
    kinds: list[str] | None = None,
    task_id: int | None = None,
    k: int = 8,
    fmt: str | None = None,
) -> str:
    with connection() as conn:
        entries, note = hybrid_search(
            conn,
            query=query,
            project=project,
            kinds=kinds,
            task_id=task_id,
            k=k,
        )
    body = serialize(entries, _fmt(fmt), content_limit=_limit())
    return format_response(body, note=note)


def save_memory(
    project: str,
    kind: str,
    content: str,
    task_id: int | None = None,
    refs: list | None = None,
    commit_sha: str | None = None,
    *,
    client: str | None = None,
    model: str | None = None,
    source: str = "agent",
) -> str:
    require_kind(kind)
    if source not in {"agent", "user", "import"}:
        raise ValueError(f"invalid source {source!r}")
    if not content or not str(content).strip():
        raise ValueError("content is empty")
    vector, note = _embed_checked(content, refs, commit_sha)
    with connection() as conn:
        result = save_entry(
            conn,
            project=project,
            kind=kind,
            content=content,
            task_id=task_id,
            refs=refs,
            commit_sha=commit_sha,
            source=source,
            client=client,
            model=model,
            embedding=vector,
            embed=False,
        )
    entry = result["entry"]
    if entry is None:
        raise RuntimeError("entry was not stored")
    lines = [f"saved id={entry['id']}"]
    if note:
        lines.append(note)
    return "\n".join(lines)


def supersede_memory(
    entry_id: int,
    new_content: str,
    reason: str,
    *,
    client: str | None = None,
    model: str | None = None,
) -> str:
    if not new_content or not str(new_content).strip():
        raise ValueError("new_content is empty")
    if not (reason or "").strip():
        raise ValueError("reason is empty")
    vector, note = _embed_checked(new_content, reason)
    with connection() as conn:
        result = supersede_entry(
            conn,
            entry_id,
            new_content,
            reason,
            client=client,
            model=model,
            embedding=vector,
            embed=False,
            note=note,
        )
    lines = [f"superseded id={result['old_id']} new_id={result['new_id']}"]
    if result["note"]:
        lines.append(result["note"])
    return "\n".join(lines)


def create_task_record(project: str, title: str, plan: str | None = None) -> str:
    with connection() as conn:
        task = create_task(conn, project, title, plan)
    return f"task id={task['id']} status={task['status']}"


def update_task_record(task_id: int, status: str | None = None, plan: str | None = None) -> str:
    with connection() as conn:
        task = update_task(conn, task_id, status, plan)
    return f"task id={task['id']} status={task['status']}"


def list_task_records(project: str, status: str | None = None, fmt: str | None = None) -> str:
    with connection() as conn:
        tasks = list_tasks(conn, project, status)
    rows = []
    for task in tasks:
        plan = str(task.get("plan") or "").strip()
        content = task["title"] if not plan else f"{task['title']}\n\n{plan}"
        rows.append(
            {
                "id": task["id"],
                "status": task["status"],
                "date": task["date"],
                "content": content,
            }
        )
    body = encode_table(
        rows,
        ("id", "status", "date"),
        _fmt(fmt),
        content_limit=_limit(),
        table_name="tasks",
    )
    return body


def handoff_memory(
    task_id: int,
    summary: str,
    next_steps: str,
    open_questions: str,
    *,
    client: str | None = None,
    model: str | None = None,
    fmt: str | None = None,
) -> str:
    for label, value in (
        ("summary", summary),
        ("next_steps", next_steps),
        ("open_questions", open_questions),
    ):
        if value is None or not str(value).strip():
            raise ValueError(f"{label} is empty")
    content = (
        f"summary:\n{summary.strip()}\n\n"
        f"next_steps:\n{next_steps.strip()}\n\n"
        f"open_questions:\n{open_questions.strip()}"
    )
    vector, note = _embed_checked(summary, next_steps, open_questions, content)
    with connection() as conn:
        _task, project = task_context(conn, task_id)
        result = save_entry(
            conn,
            project=project,
            kind="handoff",
            content=content,
            task_id=task_id,
            source="agent",
            client=client,
            model=model,
            embedding=vector,
            embed=False,
        )
    entry = result["entry"]
    body = serialize([entry], _fmt(fmt), content_limit=_limit())
    return format_response(body, note=note)


def register_project(slug: str, root_path: str, git_remote: str | None = None) -> str:
    with connection() as conn:
        project = add_project(conn, slug, root_path, git_remote)
    return f"project id={project['id']} slug={project['slug']}"


def project_root_path(slug: str) -> str | None:
    with connection() as conn:
        project = require_project(conn, slug)
    return project["root_path"]


def import_prepared(project: str, sections, kind: str) -> dict:
    require_kind(kind)
    for section in sections:
        reject_secrets_in(section.content, section.source_path)
    vectors: list[list[float] | None] = []
    note = None
    for section in sections:
        vector, section_note = try_embed(section.content)
        vectors.append(vector)
        if section_note and note is None:
            note = section_note
    with connection() as conn:
        result = import_sections(conn, project, sections, kind, vectors)
    if note:
        result["note"] = note
    return result


def export_json_ready(project: str) -> list[dict]:
    with connection() as conn:
        return export_project(conn, project)


def _actor_call(function, *args, **kwargs):
    client, model = current_actor()
    kwargs.setdefault("client", client)
    kwargs.setdefault("model", model)
    return function(*args, **kwargs)


@mcp.tool()
async def memory_start_session(
    project: str,
    task_title: str | None = None,
    fmt: str | None = None,
) -> str:
    """Register the project if needed and return a brief: open tasks, recent decisions, the latest handoff, and recent lessons. About 1500 tokens."""
    return await anyio.to_thread.run_sync(
        lambda: render_brief(project, fmt, register=True, task_title=task_title)
    )


@mcp.tool()
async def memory_search(
    query: str,
    project: str,
    kinds: list[str] | None = None,
    task_id: int | None = None,
    k: int = 8,
    fmt: str | None = None,
) -> str:
    """Hybrid full-text and vector search. Superseded entries are excluded. Every response says to verify against code and includes commit_sha when the entry has one."""
    return await anyio.to_thread.run_sync(
        lambda: search_memory(query, project, kinds, task_id, k, fmt)
    )


@mcp.tool()
async def memory_save(
    project: str,
    kind: str,
    content: str,
    task_id: int | None = None,
    refs: list[str] | None = None,
    commit_sha: str | None = None,
) -> str:
    """Store a memory entry in a registered project. Rejects secrets instead of saving them. kind: decision, step_summary, fact, lesson, todo, handoff, imported."""
    return await anyio.to_thread.run_sync(
        lambda: _actor_call(
            save_memory,
            project,
            kind,
            content,
            task_id,
            refs,
            commit_sha,
        )
    )


@mcp.tool()
async def memory_supersede(entry_id: int, new_content: str, reason: str) -> str:
    """Mark an entry superseded and store the replacement in the same transaction. History is kept."""
    return await anyio.to_thread.run_sync(
        lambda: _actor_call(supersede_memory, entry_id, new_content, reason)
    )


@mcp.tool()
async def task_create(project: str, title: str, plan: str | None = None) -> str:
    """Create a task in a registered project. status starts at open."""
    return await anyio.to_thread.run_sync(lambda: create_task_record(project, title, plan))


@mcp.tool()
async def task_update(
    task_id: int,
    status: str | None = None,
    plan: str | None = None,
) -> str:
    """Update a task. status: open, in_progress, done, dropped."""
    return await anyio.to_thread.run_sync(lambda: update_task_record(task_id, status, plan))


@mcp.tool()
async def task_list(
    project: str,
    status: str | None = None,
    fmt: str | None = None,
) -> str:
    """List tasks for a registered project, optionally filtered by status."""
    return await anyio.to_thread.run_sync(lambda: list_task_records(project, status, fmt))


@mcp.tool()
async def memory_handoff(
    task_id: int,
    summary: str,
    next_steps: str,
    open_questions: str,
    fmt: str | None = None,
) -> str:
    """Store a handoff for a task: summary, next steps, and open questions. Use it when switching models or ending a session."""
    return await anyio.to_thread.run_sync(
        lambda: _actor_call(
            handoff_memory,
            task_id,
            summary,
            next_steps,
            open_questions,
            fmt=fmt,
        )
    )


@mcp.resource("devmem://project/{slug}/brief")
def project_brief(slug: str) -> str:
    """Brief for a registered project: open tasks, recent decisions, the latest handoff, and recent lessons."""
    return render_brief(slug, None, register=False, task_title=None)

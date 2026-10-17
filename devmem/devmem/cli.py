"""Command line for devmem.

`serve` is the long-running MCP process. The other commands talk to Postgres
using DEVMEM_DATABASE_URL from the repo-root `.env` or the process environment.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from devmem.db import (
    apply_migrations,
    close_pool,
    connection,
    migration_database_name,
    reindex,
    set_embedding,
)
from devmem.embeddings import EmbeddingUnavailable, embed_text
from devmem.importer import collect_files, prepare_file
from devmem.secrets_filter import SecretRejected
from devmem.server import (
    export_json_ready,
    import_prepared,
    project_root_path,
    register_project,
    search_memory,
    serve_http,
    serve_stdio,
)


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def _cmd_serve(args: argparse.Namespace) -> int:
    if args.stdio:
        serve_stdio()
    else:
        serve_http()
    return 0


def _cmd_migrate(_args: argparse.Namespace) -> int:
    name = migration_database_name()
    with connection() as conn:
        applied = apply_migrations(conn)
    if applied:
        print(f"applied {', '.join(applied)} on database {name}", file=sys.stderr)
    else:
        print(f"database {name} is up to date", file=sys.stderr)
    return 0


def _cmd_project_add(args: argparse.Namespace) -> int:
    print(register_project(args.slug, args.root, args.git_remote))
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    root_value = project_root_path(args.project)
    root = Path(root_value) if root_value else Path.cwd()
    files = collect_files(root, args.path, args.glob)
    sections = []
    for path in files:
        sections.extend(prepare_file(path))
    result = import_prepared(args.project, sections, args.kind)
    print(
        f"imported files={len(files)} inserted={result['inserted']} skipped={result['skipped']}"
    )
    if result["note"]:
        print(result["note"])
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    kinds = [item.strip() for item in args.kinds.split(",") if item.strip()] if args.kinds else None
    print(
        search_memory(
            args.query,
            args.project,
            kinds,
            args.task_id,
            args.k,
            args.fmt,
        )
    )
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    for entry in export_json_ready(args.project):
        payload = {key: value for key, value in entry.items() if value is not None and value != []}
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


def _cmd_reindex(args: argparse.Namespace) -> int:
    with connection() as conn:
        pairs = reindex(conn, args.project)
    updated = 0
    for entry_id, content in pairs:
        vector = embed_text(content)
        with connection() as conn:
            set_embedding(conn, entry_id, vector)
        updated += 1
    print(f"reindexed {updated}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="devmem")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the MCP server")
    serve.add_argument("--stdio", action="store_true", help="stdio transport instead of HTTP")
    serve.set_defaults(func=_cmd_serve)

    migrate = sub.add_parser("migrate", help="apply database migrations")
    migrate.set_defaults(func=_cmd_migrate)

    project = sub.add_parser("project")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    add = project_sub.add_parser("add", help="register a project")
    add.add_argument("--slug", required=True)
    add.add_argument("--root", required=True)
    add.add_argument("--git-remote", dest="git_remote")
    add.set_defaults(func=_cmd_project_add)

    importer = sub.add_parser("import", help="import markdown into a project")
    importer.add_argument("--project", required=True)
    importer.add_argument("--path")
    importer.add_argument("--glob")
    importer.add_argument("--kind", required=True)
    importer.set_defaults(func=_cmd_import)

    search = sub.add_parser("search", help="hybrid search")
    search.add_argument("--project", required=True)
    search.add_argument("--query", required=True)
    search.add_argument("--kinds", help="comma-separated kinds")
    search.add_argument("--task-id", dest="task_id", type=int)
    search.add_argument("--k", type=int, default=8)
    search.add_argument("--fmt", choices=["json", "toon", "csv"])
    search.set_defaults(func=_cmd_search)

    export = sub.add_parser("export", help="write project entries as jsonl on stdout")
    export.add_argument("--project", required=True)
    export.set_defaults(func=_cmd_export)

    reindex_cmd = sub.add_parser("reindex", help="recompute embeddings")
    reindex_cmd.add_argument("--project")
    reindex_cmd.set_defaults(func=_cmd_reindex)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (SecretRejected, ValueError, EmbeddingUnavailable, RuntimeError) as exc:
        return _fail(str(exc))
    except SystemExit:
        raise
    finally:
        close_pool()


if __name__ == "__main__":
    sys.exit(main())

"""Hybrid retrieval: full text and vectors fused with Reciprocal Rank Fusion."""

from __future__ import annotations

import psycopg
from pgvector import Vector

from devmem.config import RRF_K, get_settings
from devmem.embeddings import EmbeddingUnavailable, embed_text


def rrf_merge(rankings: list[list[int]], k: int = RRF_K) -> list[int]:
    """Fuse ranked id lists.

    ``score(id) = sum 1 / (k + rank)`` with rank starting at 1. A repeated id
    inside one list counts once, at its best rank. Ties break by id so the
    order is stable.
    """
    if k < 1:
        raise ValueError("RRF k must be >= 1")
    scores: dict[int, float] = {}
    for ranking in rankings:
        seen: set[int] = set()
        for rank, doc_id in enumerate(ranking, start=1):
            if doc_id in seen:
                continue
            seen.add(doc_id)
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda doc_id: (-scores[doc_id], doc_id))


def _filters(
    project_id: int,
    kinds: list[str] | None,
    task_id: int | None,
) -> tuple[str, list]:
    clauses = ["e.project_id = %s", "e.status = 'active'"]
    params: list = [project_id]
    if kinds:
        clauses.append("e.kind = ANY(%s)")
        params.append(kinds)
    if task_id is not None:
        clauses.append("e.task_id = %s")
        params.append(task_id)
    return " AND ".join(clauses), params


def full_text_ids(
    conn: psycopg.Connection,
    project_id: int,
    query: str,
    kinds: list[str] | None,
    task_id: int | None,
    limit: int,
) -> list[int]:
    """Rank active entries with the `simple` tsvector.

    `websearch_to_tsquery` is tried first. A query it rejects falls back to
    `plainto_tsquery` inside a savepoint so the transaction stays usable.
    """
    where, params = _filters(project_id, kinds, task_id)
    template = """
        SELECT e.id
        FROM entries e
        CROSS JOIN {fn}('simple', %s) AS q
        WHERE {where} AND e.tsv @@ q
        ORDER BY ts_rank(e.tsv, q) DESC, e.id DESC
        LIMIT %s
    """
    for function_name in ("websearch_to_tsquery", "plainto_tsquery"):
        sql = template.format(fn=function_name, where=where)
        try:
            # A nested transaction is a savepoint, so a rejected tsquery
            # does not abort the caller's transaction.
            with conn.transaction():
                rows = conn.execute(sql, [query, *params, limit]).fetchall()
        except psycopg.Error:
            continue
        return [int(row["id"]) for row in rows]
    return []


def vector_ids(
    conn: psycopg.Connection,
    project_id: int,
    vector: list[float],
    kinds: list[str] | None,
    task_id: int | None,
    limit: int,
) -> list[int]:
    where, params = _filters(project_id, kinds, task_id)
    sql = f"""
        SELECT e.id
        FROM entries e
        WHERE {where} AND e.embedding IS NOT NULL
        ORDER BY e.embedding <=> %s
        LIMIT %s
    """
    rows = conn.execute(sql, [*params, Vector(vector), limit]).fetchall()
    return [int(row["id"]) for row in rows]


def embed_query(query: str) -> tuple[list[float] | None, str | None]:
    try:
        return embed_text(query), None
    except EmbeddingUnavailable:
        return None, "embeddings unavailable; full-text only"


def fuse_ids(
    text_ids: list[int],
    vector_ranked: list[int] | None,
    *,
    k: int,
    rrf_k: int | None = None,
) -> list[int]:
    settings_k = RRF_K if rrf_k is None else rrf_k
    rankings = [text_ids]
    if vector_ranked is not None:
        rankings.append(vector_ranked)
    return rrf_merge(rankings, k=settings_k)[:k]


def candidate_limit(k: int) -> int:
    return max(k * 5, k)


def search_settings_rrf_k() -> int:
    return get_settings().devmem_rrf_k

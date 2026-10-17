"""Embeddings via the LiteLLM gateway.

The URL and model are constants. This module never calls Ollama (port 11434).
"""

from __future__ import annotations

import logging

import httpx

from devmem.config import EMBED_DIM, EMBED_MODEL, EMBED_URL, Settings, get_settings

# httpx logs the request URL at INFO. The key stays in the header, but the
# line is noise on the CLI, so keep it off the default log.
logging.getLogger("httpx").setLevel(logging.WARNING)


class EmbeddingUnavailable(RuntimeError):
    def __init__(self, message: str, *, unreachable: bool) -> None:
        super().__init__(message)
        self.unreachable = unreachable


def embed_text(text: str, settings: Settings | None = None) -> list[float]:
    settings = settings or get_settings()
    if settings.devmem_embed_dim != EMBED_DIM:
        raise EmbeddingUnavailable(
            f"DEVMEM_EMBED_DIM must stay {EMBED_DIM} to match entries.embedding",
            unreachable=False,
        )
    key = settings.litellm_devmem_key.strip()
    if not key:
        raise EmbeddingUnavailable("LITELLM_DEVMEM_KEY is empty", unreachable=True)

    try:
        response = httpx.post(
            EMBED_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={"model": EMBED_MODEL, "input": text},
            timeout=httpx.Timeout(60.0, connect=3.0),
        )
    except httpx.TransportError as exc:
        raise EmbeddingUnavailable("embedding service unreachable", unreachable=True) from exc

    if response.status_code >= 400:
        raise EmbeddingUnavailable(
            f"embedding service returned HTTP {response.status_code}",
            unreachable=False,
        )
    try:
        vector = response.json()["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise EmbeddingUnavailable("embedding response has no vector", unreachable=False) from exc
    if not isinstance(vector, list) or len(vector) != EMBED_DIM:
        got = len(vector) if isinstance(vector, list) else None
        raise EmbeddingUnavailable(
            f"embedding dimension {got} != {EMBED_DIM}",
            unreachable=False,
        )
    return [float(item) for item in vector]


def try_embed(text: str, settings: Settings | None = None) -> tuple[list[float] | None, str | None]:
    """Return a vector, or `(None, note)` when the gateway cannot be used.

    Search and save keep working on full text instead of failing the request.
    """
    try:
        return embed_text(text, settings), None
    except EmbeddingUnavailable:
        return None, "embeddings unavailable; full-text only"

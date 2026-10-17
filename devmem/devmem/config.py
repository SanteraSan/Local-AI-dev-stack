"""Runtime configuration.

The env file is the repository-root `.env`, resolved from this file's location
so the process working directory does not matter. Real environment variables
override the file.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
REPO_ROOT = PROJECT_DIR.parent
ENV_FILE = REPO_ROOT / ".env"
MIGRATIONS_DIR = PROJECT_DIR / "migrations"

# Fixed by the entries.embedding column and the nomic-embed-text model.
EMBED_DIM = 768
# Reciprocal Rank Fusion constant from the phase 3 plan.
RRF_K = 60
BRIEF_TOKEN_LIMIT = 1500
CHARS_PER_TOKEN = 4
CONTENT_CHAR_LIMIT = 500
BIND_HOST = "127.0.0.1"
BIND_PORT = 8765
RESPONSE_FORMATS = frozenset({"json", "toon", "csv"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Embeddings go through the LiteLLM gateway only. Not configurable, so a
# setting cannot point this process at Ollama on port 11434.
EMBED_URL = "http://127.0.0.1:4000/v1/embeddings"
EMBED_MODEL = "embed"


def loopback_bind_host(host: str) -> str:
    """Return 127.0.0.1, or refuse to start.

    `localhost` is accepted and normalized to 127.0.0.1. Any other value,
    including 0.0.0.0, is rejected before a socket is opened.
    """
    if host not in LOOPBACK_HOSTS:
        raise ValueError(
            f"refusing to bind non-loopback host {host!r}; only 127.0.0.1 is allowed"
        )
    return BIND_HOST


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE) if ENV_FILE.is_file() else None,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    devmem_token: str = ""
    devmem_database_url: str = ""
    litellm_devmem_key: str = ""
    devmem_response_format: str = "json"
    devmem_content_char_limit: int = CONTENT_CHAR_LIMIT
    devmem_brief_token_limit: int = BRIEF_TOKEN_LIMIT
    devmem_chars_per_token: int = CHARS_PER_TOKEN
    devmem_rrf_k: int = RRF_K
    devmem_embed_dim: int = EMBED_DIM
    devmem_host: str = BIND_HOST
    devmem_test_database_url: str = ""

    def response_format(self, override: str | None = None) -> str:
        fmt = (override or self.devmem_response_format or "json").strip().lower()
        if fmt not in RESPONSE_FORMATS:
            raise ValueError(f"unknown response format {fmt!r}; expected json, toon, or csv")
        return fmt

    def bind_host(self) -> str:
        return loopback_bind_host(self.devmem_host)


def database_name(url: str) -> str:
    name = urlsplit(url).path.lstrip("/").split("/", 1)[0]
    if not name:
        raise ValueError("database URL has no database name")
    return name


def swap_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{name}"))


def assert_test_database(url: str) -> str:
    """Refuse truncate/drop unless the database name ends with `_test`."""
    name = database_name(url)
    if not name.endswith("_test"):
        raise RuntimeError(
            f"refusing destructive database operation: {name!r} does not end with _test"
        )
    return name


def derive_test_database_url(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    override = settings.devmem_test_database_url.strip()
    if override:
        url = override
    else:
        if not settings.devmem_database_url.strip():
            raise RuntimeError("DEVMEM_DATABASE_URL is empty")
        url = swap_database(settings.devmem_database_url.strip(), "devmem_test")
    assert_test_database(url)
    return url


def require_server_token(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    token = settings.devmem_token
    if not token or not token.strip():
        raise SystemExit("DEVMEM_TOKEN is empty; refusing to start")
    return token


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

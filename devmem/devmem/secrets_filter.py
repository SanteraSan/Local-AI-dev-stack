"""Reject content that looks like a secret.

A match is an error that names the pattern type. The matched text is never
included in the error and is never redacted in place: the write is refused.
"""

from __future__ import annotations

import re


class SecretRejected(ValueError):
    def __init__(self, pattern_type: str) -> None:
        self.pattern_type = pattern_type
        super().__init__(f"rejected: content matches secret pattern '{pattern_type}'")


# Name of a credential field. Prefixed forms such as PG_PASSWORD still match
# because the keyword does not need to start the token. `token` does not match
# `tokens`, so `max_tokens=512` stays ordinary text.
_SECRET_NAME = r"password|passwd|secret|token|api[_-]?key"

# More specific tokens come first so the reported type is the tightest match.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("private_key", re.compile(r"-----BEGIN")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    (
        "url_credentials",
        # scheme://user:password@host. A user without a password has no colon
        # before '@', so `postgresql://devai@host/db` is not a match.
        re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@"),
    ),
    (
        "secret_assignment",
        re.compile(
            rf"(?i)(?:(?:{_SECRET_NAME})\s*=\s*\S|"
            rf"[\"'](?:{_SECRET_NAME})[\"']\s*:\s*[\"'][^\"']+[\"']|"
            rf"(?:{_SECRET_NAME})\s*:\s*[\"'][^\"']+[\"'])"
        ),
    ),
)


def find_secret(text: str) -> str | None:
    if not text:
        return None
    for name, pattern in _PATTERNS:
        if pattern.search(text):
            return name
    return None


def reject_secrets(text: str) -> None:
    kind = find_secret(text)
    if kind is not None:
        raise SecretRejected(kind)


def reject_secrets_in(*parts: object) -> None:
    for part in parts:
        if part is None:
            continue
        if isinstance(part, str):
            reject_secrets(part)
        elif isinstance(part, (list, tuple)):
            for item in part:
                reject_secrets_in(item)
        elif isinstance(part, dict):
            for key, value in part.items():
                reject_secrets_in(key, value)
        else:
            reject_secrets(str(part))

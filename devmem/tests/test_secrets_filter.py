import base64
import json

import pytest

from devmem.secrets_filter import SecretRejected, find_secret, reject_secrets


def _b64(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _jwt() -> str:
    return ".".join(
        [
            _b64({"alg": "none"}),
            _b64({"sub": "fake-user"}),
            "sig" + ("n" * 8),
        ]
    )


@pytest.mark.parametrize(
    ("pattern_type", "text"),
    [
        ("openai_key", "prefix " + "sk-" + ("a" * 40) + " suffix"),
        ("github_token", "ghp_" + ("b" * 36)),
        ("github_pat", "github_pat_" + ("c" * 22)),
        ("aws_access_key", "AKIA" + ("Z" * 16)),
        ("private_key", "-----BEGIN" + " FAKE PRIVATE KEY-----"),
        ("private_key", "-----BEGIN"),
        ("jwt", "token header " + _jwt()),
        ("secret_assignment", "password" + "=" + "not-real"),
        ("secret_assignment", "passwd" + " = " + "not-real"),
        ("secret_assignment", "Secret" + "=" + "not-real"),
        ("secret_assignment", "token" + "=" + "not-real"),
        ("url_credentials", "db url postgresql://u:" + "p@h/db"),
        ("secret_assignment", "PG_PASSWORD" + "=x"),
        ("secret_assignment", "api_key" + "=x"),
        ("secret_assignment", "apikey" + "=x"),
        ("secret_assignment", "API-KEY" + "=x"),
        ("secret_assignment", '"password": "' + 'x"'),
        ("secret_assignment", "password: " + '"x"'),
    ],
)
def test_secret_patterns_reject(pattern_type: str, text: str):
    assert find_secret(text) == pattern_type
    with pytest.raises(SecretRejected) as caught:
        reject_secrets(text)
    assert caught.value.pattern_type == pattern_type
    assert text not in str(caught.value)
    assert "not-real" not in str(caught.value)


@pytest.mark.parametrize(
    "text",
    [
        "sketch the module layout before coding",
        "sk-" + "short",
        "AKIA" + ("Z" * 15),
        "the password policy is to refuse secrets",
        "postgresql://devai@127.0.0.1:5442/devmem",
        "https://example.com/a:b",
        "the token budget: 1500",
        "max_tokens=512",
        "tokenize the document",
        "eyJhbGci.eyJhbGci.abc",
        "a normal decision about the cache key",
        "",
    ],
)
def test_ordinary_text_is_kept(text: str):
    assert find_secret(text) is None
    reject_secrets(text)

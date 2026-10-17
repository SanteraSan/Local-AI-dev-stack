import pytest
from starlette.testclient import TestClient

from devmem.server import create_http_app

TOKEN = "test-token-not-a-secret"
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "devmem-pytest", "version": "0"},
    },
}


@pytest.fixture(scope="module")
def client():
    app = create_http_app(TOKEN)
    with TestClient(app, base_url="http://127.0.0.1:8765") as test_client:
        yield test_client


def _post(client: TestClient, token: str | None):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    return client.post("/mcp", json=INIT, headers=headers)


def test_missing_token_is_401(client: TestClient):
    response = _post(client, None)
    assert response.status_code == 401


def test_wrong_token_is_401(client: TestClient):
    different_length = _post(client, "nope")
    same_length = _post(client, "x" * len(TOKEN))
    assert different_length.status_code == 401
    assert same_length.status_code == 401
    assert TOKEN not in different_length.text


def test_correct_token_is_accepted(client: TestClient):
    response = _post(client, TOKEN)
    assert response.status_code == 200
    assert "devmem" in response.text

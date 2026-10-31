"""Bearer-token, token-file, CORS, and local-bind tests."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from tests.cli.conftest import invoke

from trackforge.interfaces.http.auth import is_local_host, is_local_origin, verify_bearer
from trackforge.interfaces.http.errors import error_body

pytestmark = pytest.mark.unit


def test_healthz_is_public(client: Any) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_api_requires_bearer_token(client: Any) -> None:
    response = client.get("/api/doctor")

    assert response.status_code == 401
    payload = response.json()
    assert payload["code"] == "unauthorized"
    assert payload["retryable"] is False
    assert "test-http-token" not in response.text


def test_wrong_token_is_rejected(client: Any) -> None:
    response = client.get(
        "/api/doctor",
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_correct_token_allows_api(client: Any, auth_headers: dict[str, str]) -> None:
    response = client.get("/api/doctor", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_token_file_is_owner_only(
    http_app: Any,
) -> None:
    path = Path(http_app.state.http_token_path)

    assert path.name == "http-token"
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="ascii") == http_app.state.http_token


def test_token_comparison_is_strict_and_timing_safe() -> None:
    token = "a" * 48

    assert verify_bearer(f"Bearer {token}", token) is True
    assert verify_bearer(f"bearer {token}", token) is True
    assert verify_bearer(token, token) is False
    assert verify_bearer(f"Basic {token}", token) is False
    assert verify_bearer(None, token) is False
    assert verify_bearer(f"Bearer {token}x", token) is False
    assert verify_bearer("Bearer токен", token) is False


def test_cors_allows_only_loopback_origins(client: Any) -> None:
    local = client.options(
        "/api/doctor",
        headers={
            "Origin": "http://127.0.0.1:8765",
            "Access-Control-Request-Method": "GET",
        },
    )
    localhost = client.options(
        "/api/doctor",
        headers={
            "Origin": "http://localhost:8765",
            "Access-Control-Request-Method": "GET",
        },
    )
    remote = client.options(
        "/api/doctor",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert local.headers.get("access-control-allow-origin") == "http://127.0.0.1:8765"
    assert localhost.headers.get("access-control-allow-origin") == "http://localhost:8765"
    assert "access-control-allow-credentials" not in local.headers
    assert remote.headers.get("access-control-allow-origin") is None


def test_local_host_and_origin_helpers() -> None:
    assert is_local_host("127.0.0.1")
    assert is_local_host("localhost")
    assert is_local_host("::1")
    assert not is_local_host("0.0.0.0")
    assert not is_local_host("192.168.1.5")
    assert is_local_origin("http://127.0.0.1:8765")
    assert is_local_origin("http://localhost:8765")
    assert not is_local_origin("https://localhost:8765")
    assert not is_local_origin("http://192.168.1.5:8765")


def test_serve_refuses_remote_bind(
    clean_env: None,
    tmp_path: Path,
) -> None:
    result = invoke(
        "serve",
        "--host",
        "0.0.0.0",
        "--port",
        "0",
        "--data-dir",
        str(tmp_path / "data"),
        "--json",
    )

    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["type"] == "ConfigError"
    assert "refusing to bind" in payload["message"]


def test_error_body_never_echoes_a_token() -> None:
    body = error_body(
        "domain_validation_error",
        "token=super-secret was rejected",
        context={"authorization": "Bearer super-secret"},
        secret="super-secret",
    )

    assert "super-secret" not in json.dumps(body)
    assert body["context"]["authorization"] == "***"

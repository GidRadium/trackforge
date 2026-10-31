"""Typed error mapping, redaction, validation, and layering contracts."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.http.fake import ITEM_ID, SOURCE, TEST_TOKEN

from trackforge.domain.errors import (
    DomainValidationError,
    ProviderUnavailable,
    RateLimited,
    RecordNotFound,
    ReviewRequired,
)
from trackforge.interfaces.http.app import MAX_REQUEST_BYTES

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (DomainValidationError("bad input"), 400, "domain_validation_error"),
        (RecordNotFound("missing"), 404, "record_not_found"),
        (ReviewRequired("review first"), 409, "review_required"),
        (RateLimited("slow down"), 429, "rate_limited"),
        (ProviderUnavailable("provider down"), 503, "provider_unavailable"),
    ],
)
def test_known_errors_map_to_typed_statuses(
    http_app: Any,
    fake_facade: Any,
    auth_headers: dict[str, str],
    error: BaseException,
    status: int,
    code: str,
) -> None:
    fake_facade.plan_error = error
    with TestClient(http_app) as client:
        response = client.post(
            "/api/plan",
            headers=auth_headers,
            json={"source": str(SOURCE)},
        )

    assert response.status_code == status
    payload = response.json()
    assert payload["code"] == code
    assert payload["message"]
    assert "retryable" in payload
    assert "user_action" in payload


def test_error_context_is_redacted(
    http_app: Any,
    fake_facade: Any,
    auth_headers: dict[str, str],
) -> None:
    fake_facade.plan_error = DomainValidationError(
        "unusable source",
        context={"token": "super-secret", "path": "/tmp/media"},
    )
    with TestClient(http_app) as client:
        response = client.post(
            "/api/plan",
            headers=auth_headers,
            json={"source": str(SOURCE)},
        )

    assert response.status_code == 400
    payload = response.json()
    assert payload["context"]["token"] == "***"
    assert payload["context"]["path"] == "/tmp/media"
    assert "super-secret" not in response.text


def test_unexpected_error_is_internal_and_redacted(
    http_app: Any,
    fake_facade: Any,
    auth_headers: dict[str, str],
) -> None:
    fake_facade.plan_error = RuntimeError("database password=super-secret")
    with TestClient(http_app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/plan",
            headers=auth_headers,
            json={"source": str(SOURCE)},
        )

    assert response.status_code == 500
    payload = response.json()
    assert payload["code"] == "internal_error"
    assert payload["message"] == "internal server error"
    assert "super-secret" not in response.text


def test_request_validation_error_is_typed(
    client: Any,
    auth_headers: dict[str, str],
) -> None:
    response = client.post("/api/plan", headers=auth_headers, json={})

    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == "validation_error"
    assert payload["context"]["errors"]


def test_invalid_item_id_is_400(
    client: Any,
    auth_headers: dict[str, str],
) -> None:
    response = client.get("/api/items/not-a-uuid", headers=auth_headers)

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_id"


def test_unknown_route_is_typed_404(client: Any, auth_headers: dict[str, str]) -> None:
    response = client.get("/api/library/../../etc/passwd", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_request_size_limit(
    client: Any,
    auth_headers: dict[str, str],
) -> None:
    response = client.post(
        "/api/plan",
        headers={**auth_headers, "Content-Type": "application/json"},
        content=b"{" + b" " * (MAX_REQUEST_BYTES + 1) + b"}",
    )

    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"


def test_error_body_has_no_token(
    http_app: Any,
) -> None:
    with TestClient(http_app, raise_server_exceptions=False) as client:
        response = client.get("/api/doctor", headers={"Authorization": "Bearer nope"})

    assert response.status_code == 401
    assert TEST_TOKEN not in response.text
    assert json.loads(response.text)["code"] == "unauthorized"


def test_routes_do_not_import_adapters_or_domain() -> None:
    routes_dir = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "trackforge"
        / "interfaces"
        / "http"
        / "routes"
    )
    forbidden = (
        "trackforge.adapters",
        "trackforge.domain",
        "trackforge.bootstrap",
        "trackforge.application.services",
    )
    violations: list[str] = []
    for path in routes_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules = [node.module]
            for module in modules:
                if any(module == item or module.startswith(f"{item}.") for item in forbidden):
                    violations.append(f"{path.name}: {module}")
    assert violations == []


def test_no_secret_is_serialized_from_a_dto(
    client: Any,
    auth_headers: dict[str, str],
) -> None:
    # The fake doctor check carries a token-shaped detail; the endpoint must
    # redact it before it reaches the wire.
    response = client.get("/api/doctor", headers=auth_headers)

    assert "super-secret" not in response.text
    assert response.json()["checks"][0]["details"]["token"] == "***"


def test_review_scope_wider_than_item_is_rejected(
    http_app: Any,
    fake_facade: Any,
    auth_headers: dict[str, str],
) -> None:
    from trackforge.domain.errors import UnsupportedScope

    fake_facade.review_error = UnsupportedScope("item scope only")
    with TestClient(http_app) as client:
        response = client.post(
            f"/api/items/{ITEM_ID}/review",
            headers=auth_headers,
            json={"corrections": {"title": "Changed"}},
        )

    assert response.status_code == 400
    assert response.json()["code"] == "unsupported_scope"

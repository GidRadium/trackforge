"""Item, review, explain, library, doctor, and models DTO tests."""

from __future__ import annotations

from typing import Any

import pytest
from tests.http.fake import ITEM_ID

pytestmark = pytest.mark.unit


def test_item_and_decisions_return_same_dtos(
    client: Any,
    auth_headers: dict[str, str],
) -> None:
    item = client.get(f"/api/items/{ITEM_ID}", headers=auth_headers)
    decisions = client.get(f"/api/items/{ITEM_ID}/decisions", headers=auth_headers)

    assert item.status_code == 200, item.text
    item_payload = item.json()
    assert item_payload["item_id"] == str(ITEM_ID)
    assert item_payload["title"] == "Fixture Song"
    assert item_payload["artist"] == "Fixture Artist"

    assert decisions.status_code == 200, decisions.text
    decision_payload = decisions.json()
    assert [entry["field"] for entry in decision_payload] == ["title", "artist"]
    assert decision_payload[0]["value"] == "Fixture Song"
    assert decision_payload[0]["confidence"] == 0.93


def test_review_applies_corrections_at_item_scope(
    client: Any,
    auth_headers: dict[str, str],
    fake_facade: Any,
) -> None:
    response = client.post(
        f"/api/items/{ITEM_ID}/review",
        headers=auth_headers,
        json={
            "corrections": [
                {
                    "field": "title",
                    "value": "Corrected Title",
                    "lock": True,
                    "reason_code": "user_edit",
                }
            ]
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["item_id"] == str(ITEM_ID)
    assert payload["locked_fields"] == ["title"]
    assert payload["requires_render"] is True
    item_id, corrections, kwargs = fake_facade.review_calls[-1]
    assert item_id == str(ITEM_ID)
    assert corrections == [
        {
            "field": "title",
            "value": "Corrected Title",
            "lock": True,
            "scope": "item",
            "reason_code": "user_edit",
            "reason_text": None,
        }
    ]
    assert kwargs["scope"] == "item"


def test_review_show_is_side_effect_free(
    client: Any,
    auth_headers: dict[str, str],
    fake_facade: Any,
) -> None:
    response = client.post(
        f"/api/items/{ITEM_ID}/review",
        headers=auth_headers,
        json={"action": "show"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["action"] == "show"
    assert fake_facade.review_calls[-1][1] is None


def test_explain_returns_explanation_view(
    client: Any,
    auth_headers: dict[str, str],
    fake_facade: Any,
) -> None:
    response = client.get(
        f"/api/items/{ITEM_ID}/explain",
        params={"field": "title"},
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["field"] == "title"
    assert payload["decision"] == "Fixture Song"
    assert payload["policy_id"] == "title"
    assert fake_facade.explain_calls == [(str(ITEM_ID), "title")]


def test_explain_rejects_field_path_traversal(
    client: Any,
    auth_headers: dict[str, str],
) -> None:
    response = client.get(
        f"/api/items/{ITEM_ID}/explain",
        params={"field": "../../etc/passwd"},
        headers=auth_headers,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_library_returns_entries_and_supports_filters(
    client: Any,
    auth_headers: dict[str, str],
) -> None:
    response = client.get("/api/library", headers=auth_headers)
    filtered = client.get(
        "/api/library",
        params={"item_id": str(ITEM_ID), "status": "published"},
        headers=auth_headers,
    )
    empty = client.get(
        "/api/library",
        params={"status": "missing"},
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()[0]["status"] == "published"
    assert response.json()[0]["target_format"] == "mp3"
    assert filtered.status_code == 200
    assert len(filtered.json()) == 1
    assert empty.json() == []


def test_library_rejects_invalid_status(
    client: Any,
    auth_headers: dict[str, str],
) -> None:
    response = client.get(
        "/api/library",
        params={"status": "not-a-status"},
        headers=auth_headers,
    )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_library_status"


def test_doctor_is_redacted(
    client: Any,
    auth_headers: dict[str, str],
) -> None:
    response = client.get("/api/doctor", headers=auth_headers)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["summary"] == "1 ok, 0 warning, 0 critical"
    check = payload["checks"][0]
    assert check["details"]["token"] == "***"
    assert "super-secret" not in response.text


def test_models_are_listed_without_download(
    client: Any,
    auth_headers: dict[str, str],
) -> None:
    response = client.get("/api/models", headers=auth_headers)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload[0]["id"] == "fixture/asr"
    assert payload[0]["status"] == "missing"

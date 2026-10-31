"""Plan, run, status, and cancel contract tests."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tests.http.fake import ITEM_ID, RUN_ID, SOURCE

from trackforge.application.dto.db import RunStatus
from trackforge.application.dto.plan import PlanView

pytestmark = pytest.mark.unit


def test_plan_returns_a_serialized_plan_view(
    client: Any,
    auth_headers: dict[str, str],
    fake_facade: Any,
) -> None:
    response = client.post(
        "/api/plan",
        headers=auth_headers,
        json={"source": str(SOURCE), "options": {"profile": "fast"}},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["type"] == "plan"
    assert payload["source"] == {"type": "path", "path": str(SOURCE)}
    assert payload["candidates"][0]["title"] == "Fixture Song"
    assert payload["steps"][0]["capability_id"] == "observe"
    assert payload["warnings"] == ["fixture warning"]
    assert fake_facade.plan_calls[0][1].profile == "fast"


def test_plan_accepts_url_source_objects(
    client: Any,
    auth_headers: dict[str, str],
    fake_facade: Any,
) -> None:
    response = client.post(
        "/api/plan",
        headers=auth_headers,
        json={"source": {"type": "url", "url": "https://example.com/watch?v=abc"}},
    )

    assert response.status_code == 200, response.text
    assert response.json()["source"]["type"] == "url"
    assert fake_facade.plan_calls[0][0].url == "https://example.com/watch?v=abc"


def test_plan_does_not_echo_signed_url_secrets(
    client: Any,
    auth_headers: dict[str, str],
) -> None:
    response = client.post(
        "/api/plan",
        headers=auth_headers,
        json={
            "source": {
                "type": "url",
                "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ&token=super-secret",
            }
        },
    )

    assert response.status_code == 200, response.text
    assert "super-secret" not in response.text
    assert response.json()["source"]["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_run_source_returns_run_result(
    client: Any,
    auth_headers: dict[str, str],
    fake_facade: Any,
) -> None:
    response = client.post(
        "/api/runs",
        headers=auth_headers,
        json={"source": str(SOURCE), "options": {"auto": True, "profile": "full"}},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["run_id"] == str(RUN_ID)
    assert payload["status"] == "completed"
    assert payload["succeeded"] is True
    assert payload["items"][0]["item_id"] == str(ITEM_ID)
    assert fake_facade.run_calls[0][1].profile == "full"


def test_run_accepts_a_previously_returned_plan(
    client: Any,
    auth_headers: dict[str, str],
    fake_facade: Any,
) -> None:
    planned = client.post(
        "/api/plan",
        headers=auth_headers,
        json={"source": str(SOURCE)},
    )
    assert planned.status_code == 200

    response = client.post(
        "/api/runs",
        headers=auth_headers,
        json={"plan": planned.json()},
    )

    assert response.status_code == 200, response.text
    target = fake_facade.run_calls[-1][0]
    assert isinstance(target, PlanView)
    assert target.candidates[0].title == "Fixture Song"


def test_status_returns_persisted_run_view(
    client: Any,
    auth_headers: dict[str, str],
) -> None:
    response = client.get(f"/api/runs/{RUN_ID}", headers=auth_headers)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["run_id"] == str(RUN_ID)
    assert payload["status"] == "running"
    assert payload["items"][0][0] == str(ITEM_ID)


def test_async_run_returns_202_and_is_cancellable(
    client: Any,
    auth_headers: dict[str, str],
    fake_facade: Any,
) -> None:
    fake_facade.run_gate = asyncio.Event()

    response = client.post(
        "/api/runs?wait=false",
        headers=auth_headers,
        json={"source": str(SOURCE)},
    )

    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["run_id"] == str(RUN_ID)
    assert payload["status"] == "running"

    cancelled = client.post(f"/api/runs/{RUN_ID}/cancel", headers=auth_headers)
    assert cancelled.status_code == 202
    assert cancelled.json()["cancelled"] is True
    assert fake_facade.cancel_calls == [str(RUN_ID)]


def test_cancel_terminal_run_is_conflict(
    client: Any,
    auth_headers: dict[str, str],
    fake_facade: Any,
) -> None:
    fake_facade.status_view = fake_facade.status_view.__class__(
        run_id=fake_facade.status_view.run_id,
        status=RunStatus.COMPLETED,
        profile=fake_facade.status_view.profile,
        mode=fake_facade.status_view.mode,
        steps=fake_facade.status_view.steps,
        items=fake_facade.status_view.items,
    )

    response = client.post(f"/api/runs/{RUN_ID}/cancel", headers=auth_headers)

    assert response.status_code == 409
    assert response.json()["code"] == "run_not_running"
    assert fake_facade.cancel_calls == []


def test_cancel_inactive_run_is_conflict(
    client: Any,
    auth_headers: dict[str, str],
    fake_facade: Any,
) -> None:
    fake_facade.cancel_result = False

    response = client.post(f"/api/runs/{RUN_ID}/cancel", headers=auth_headers)

    assert response.status_code == 409
    assert response.json()["code"] == "run_not_cancellable"


def test_invalid_run_id_is_400(
    client: Any,
    auth_headers: dict[str, str],
) -> None:
    response = client.get("/api/runs/not-a-uuid", headers=auth_headers)

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_id"


def test_unknown_run_is_404(
    client: Any,
    auth_headers: dict[str, str],
) -> None:
    other = "0199a000-0000-7000-8000-000000000000"
    response = client.get(f"/api/runs/{other}", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["code"] == "record_not_found"

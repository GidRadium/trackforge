"""Real facade round-trip over the local HTTP transport.

This is the integration counterpart to the fake-facade contract tests: it
exercises the same DTO pipeline as the Python API and verifies that the HTTP
layer does not become a second source of business logic.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.fixtures.media import generate_media, skip_without_media_tools
from tests.http.fake import TEST_TOKEN

from trackforge.config import TrackForgeConfig
from trackforge.interfaces.http.app import create_app

pytestmark = pytest.mark.integration


def test_real_plan_run_item_review_explain(
    tmp_path: Path,
    config: TrackForgeConfig,
    clean_env: None,
) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", "mp3")
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
    app = create_app(config, token=TEST_TOKEN)

    with TestClient(app) as client:
        planned = client.post(
            "/api/plan",
            headers=headers,
            json={"source": str(fixture.path)},
        )
        assert planned.status_code == 200, planned.text
        plan_payload = planned.json()
        assert plan_payload["candidates"]

        ran = client.post(
            "/api/runs",
            headers=headers,
            json={"plan": plan_payload, "options": {"auto": True}},
        )
        assert ran.status_code == 200, ran.text
        run_payload = ran.json()
        assert run_payload["status"] == "completed"
        run_id = run_payload["run_id"]
        item_id = run_payload["items"][0]["item_id"]
        assert run_payload["output_paths"]

        status = client.get(f"/api/runs/{run_id}", headers=headers)
        assert status.status_code == 200, status.text
        assert status.json()["status"] == "completed"

        with client.stream(
            "GET",
            f"/api/runs/{run_id}/events",
            headers=headers,
        ) as events:
            assert events.status_code == 200
            body = "".join(events.iter_text())
        assert "event: run.finished" in body

        item = client.get(f"/api/items/{item_id}", headers=headers)
        assert item.status_code == 200, item.text
        assert item.json()["status"] == "published"

        decisions = client.get(f"/api/items/{item_id}/decisions", headers=headers)
        assert decisions.status_code == 200, decisions.text
        fields = {entry["field"] for entry in decisions.json()}
        assert "title" in fields

        shown = client.post(
            f"/api/items/{item_id}/review",
            headers=headers,
            json={"action": "show"},
        )
        assert shown.status_code == 200, shown.text

        correction = client.post(
            f"/api/items/{item_id}/review",
            headers=headers,
            json={"corrections": {"title": "Corrected Fixture Song"}},
        )
        assert correction.status_code == 200, correction.text
        assert correction.json()["requires_render"] is True

        explanation = client.get(
            f"/api/items/{item_id}/explain",
            params={"field": "title"},
            headers=headers,
        )
        assert explanation.status_code == 200, explanation.text
        assert explanation.json()["decision"] == "Corrected Fixture Song"

        library = client.get("/api/library", headers=headers)
        assert library.status_code == 200, library.text
        assert library.json()

        doctor = client.get("/api/doctor", headers=headers)
        assert doctor.status_code == 200, doctor.text
        assert doctor.json()["checks"]

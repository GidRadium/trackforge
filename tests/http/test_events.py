"""SSE framing and event-schema tests."""

from __future__ import annotations

import json
from typing import Any

import pytest
from tests.http.fake import RUN_ID

from trackforge.pipeline.events import Event, EventType

pytestmark = pytest.mark.unit


def test_sse_streams_events_and_closes_on_terminal(
    client: Any,
    auth_headers: dict[str, str],
) -> None:
    with client.stream(
        "GET",
        f"/api/runs/{RUN_ID}/events",
        headers=auth_headers,
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = list(response.iter_lines())

    assert any(line == "event: run.started" for line in lines)
    assert any(line == "event: run.finished" for line in lines)
    data_lines = [line for line in lines if line.startswith("data: ")]
    payloads = [json.loads(line[len("data: ") :]) for line in data_lines]
    assert payloads
    assert payloads[-1]["type"] == "run.finished"
    assert payloads[-1]["run_id"] == str(RUN_ID)
    assert "sequence" in payloads[-1]


def test_sse_event_schema_matches_python_api(
    client: Any,
    auth_headers: dict[str, str],
    fake_facade: Any,
) -> None:
    fake_facade.events_list = (
        Event(
            type=EventType.STEP_PROGRESS,
            run_id=RUN_ID,
            capability="observe",
            progress=0.5,
            message="probing",
            stage="observe",
        ),
        Event(type=EventType.RUN_FINISHED, run_id=RUN_ID, status="completed"),
    )

    with client.stream(
        "GET",
        f"/api/runs/{RUN_ID}/events",
        headers=auth_headers,
    ) as response:
        lines = list(response.iter_lines())

    data = json.loads(next(line[len("data: ") :] for line in lines if line.startswith("data: ")))
    assert data["type"] == "step.progress"
    assert data["capability"] == "observe"
    assert data["progress"] == 0.5
    assert data["stage"] == "observe"


def test_sse_unknown_run_is_404(
    client: Any,
    auth_headers: dict[str, str],
) -> None:
    other = "0199a000-0000-7000-8000-000000000000"
    response = client.get(f"/api/runs/{other}/events", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["code"] == "record_not_found"


def test_sse_requires_auth(client: Any) -> None:
    response = client.get(f"/api/runs/{RUN_ID}/events")

    assert response.status_code == 401

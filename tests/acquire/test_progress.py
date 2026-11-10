"""Tests for progress sinks and cancellation."""

# pyright: standard

from __future__ import annotations

import pytest

from trackforge.acquire.errors import CancelledError
from trackforge.acquire.progress import (
    CancellationToken,
    NullProgressSink,
    ProgressEvent,
    ProgressStage,
    RecordingSink,
)


def test_null_sink_discards_events() -> None:
    sink = NullProgressSink()
    sink.emit(ProgressEvent(stage=ProgressStage.RESOLVE, job_id="job"))


def test_recording_sink_keeps_events() -> None:
    sink = RecordingSink()
    event = ProgressEvent(
        stage=ProgressStage.DOWNLOAD,
        job_id="job",
        item_index=1,
        item_count=2,
        downloaded_bytes=10,
        total_bytes=100,
        speed=5.0,
        eta=18.0,
    )
    sink.emit(event)
    assert sink.events == [event]
    assert len(sink) == 1
    sink.clear()
    assert sink.events == []


def test_cancellation_token() -> None:
    token = CancellationToken()
    assert token.cancelled is False
    token.raise_if_cancelled()

    token.cancel()
    assert token.cancelled is True
    with pytest.raises(CancelledError):
        token.raise_if_cancelled()

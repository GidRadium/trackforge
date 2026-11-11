"""Tests for progress sinks and cancellation."""

# pyright: standard

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fakes import make_item, make_source_info

from trackforge.acquire.download import YtDlpProgressHook
from trackforge.acquire.errors import CancelledError
from trackforge.acquire.models import (
    DownloadOptions,
    DownloadRequest,
    ItemSelection,
)
from trackforge.acquire.planner import DownloadPlan, YtDlpPlanner
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


def make_plan(tmp_path: Path) -> DownloadPlan:
    item = make_item("item", stem="Track")
    source = replace(make_source_info(), items=(item,))
    request = DownloadRequest(
        source_info=source,
        selections=(ItemSelection(item.id, item.variants[0].id),),
        options=DownloadOptions(target_dir=tmp_path / "staging"),
    )
    return YtDlpPlanner().plan(request, job_id="job-progress")


def test_progress_hook_maps_download_fields(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    sink = RecordingSink()
    hook = YtDlpProgressHook(
        sink,
        CancellationToken(),
        plan,
        plan.items[0],
        item_index=1,
    )

    hook.download(
        {
            "status": "downloading",
            "filename": "Track.m4a",
            "downloaded_bytes": 25,
            "total_bytes_estimate": 100,
            "speed": 12.5,
            "eta": 6.0,
            "fragment_index": 2,
            "fragment_count": 8,
        }
    )

    event = sink.events[-1]
    assert event.stage is ProgressStage.DOWNLOAD
    assert event.job_id == "job-progress"
    assert event.item_index == 1
    assert event.item_count == 1
    assert event.downloaded_bytes == 25
    assert event.total_bytes == 100
    assert event.speed == 12.5
    assert event.eta == 6.0
    assert event.fragment_index == 2
    assert event.fragment_count == 8


def test_unknown_total_stays_indeterminate(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    sink = RecordingSink()
    hook = YtDlpProgressHook(sink, CancellationToken(), plan, plan.items[0], 1)

    hook.download({"status": "downloading", "downloaded_bytes": 10})
    hook.download({"status": "finished", "downloaded_bytes": 10})

    assert sink.events[-2].total_bytes is None
    assert sink.events[-1].total_bytes is None
    assert all(event.total_bytes != 0 for event in sink.events)


@pytest.mark.parametrize(
    ("postprocessor", "stage"),
    [
        ("FFmpegMerger", ProgressStage.MERGE),
        ("FFmpegExtractAudio", ProgressStage.EXTRACT_AUDIO),
        ("FFmpegVideoRemuxer", ProgressStage.REMUX),
        ("FFmpegVideoConvertor", ProgressStage.CONVERT),
        ("FFmpegSubtitlesConvertor", ProgressStage.SUBTITLES),
        ("ExtractAudio", ProgressStage.EXTRACT_AUDIO),
        ("Merger", ProgressStage.MERGE),
        ("VideoRemuxer", ProgressStage.REMUX),
        ("MoveFiles", ProgressStage.MOVE),
        ("UnknownPostprocessor", ProgressStage.CONVERT),
    ],
)
def test_postprocessor_hook_mapping(
    tmp_path: Path,
    postprocessor: str,
    stage: ProgressStage,
) -> None:
    plan = make_plan(tmp_path)
    sink = RecordingSink()
    hook = YtDlpProgressHook(sink, CancellationToken(), plan, plan.items[0], 1)

    hook.postprocessor({"status": "started", "postprocessor": postprocessor})

    assert sink.events[-1].stage is stage
    assert sink.events[-1].message == "started"


def test_progress_hook_raises_download_cancelled(tmp_path: Path) -> None:
    from yt_dlp.utils import DownloadCancelled

    plan = make_plan(tmp_path)
    token = CancellationToken()
    token.cancel()
    hook = YtDlpProgressHook(
        RecordingSink(),
        token,
        plan,
        plan.items[0],
        1,
    )

    with pytest.raises(DownloadCancelled):
        hook.download({"status": "downloading"})


def test_progress_hook_accepts_arbitrary_mapping(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    sink = RecordingSink()
    hook = YtDlpProgressHook(sink, CancellationToken(), plan, plan.items[0], 1)
    data: dict[str, Any] = {"status": "finished", "total_bytes": 0}

    hook.download(data)

    assert sink.events[-1].total_bytes == 0

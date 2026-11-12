"""Human progress is a single, stable, in-place line."""

# pyright: standard

from __future__ import annotations

import io

from trackforge.acquire.progress import ProgressEvent, ProgressStage
from trackforge.cli.progress import HumanProgressSink, StatusLine


class TtyBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def download_event(
    *,
    filename: str,
    downloaded: int,
    total: int | None,
) -> ProgressEvent:
    return ProgressEvent(
        stage=ProgressStage.DOWNLOAD,
        job_id="job",
        item_index=1,
        item_count=1,
        filename=filename,
        downloaded_bytes=downloaded,
        total_bytes=total,
        speed=1_000_000.0,
        eta=1.0,
    )


def test_status_line_rewrites_instead_of_appending() -> None:
    buffer = TtyBuffer()
    status = StatusLine(buffer)

    status.update("[resolve] inspecting source...")
    status.update("[download]  42%")
    status.finish("[done] Track.m4a")

    rendered = buffer.getvalue()
    assert rendered.count("\n") == 1
    assert rendered.startswith("\r\x1b[2K[resolve]")
    assert "[download]  42%" in rendered
    assert rendered.endswith("[done] Track.m4a\n")


def test_progress_accumulates_streams_instead_of_resetting() -> None:
    buffer = TtyBuffer()
    status = StatusLine(buffer)
    sink = HumanProgressSink(status)

    sink.emit(download_event(filename="video", downloaded=100, total=100))
    sink.emit(download_event(filename="audio", downloaded=50, total=100))
    sink.emit(
        ProgressEvent(
            stage=ProgressStage.DONE,
            job_id="job",
            item_index=1,
            item_count=1,
            filename="out.mp4",
        )
    )

    rendered = buffer.getvalue()
    assert "[download] 100%" in rendered
    assert "[download]  75%" in rendered
    assert "[download]  50%" not in rendered
    assert rendered.endswith("[done] out.mp4\n")


def test_non_tty_progress_is_not_spammed() -> None:
    buffer = io.StringIO()
    status = StatusLine(buffer)
    sink = HumanProgressSink(status)

    for percent in range(0, 101):
        sink.emit(
            download_event(
                filename="video",
                downloaded=percent,
                total=100,
            )
        )
    sink.emit(
        ProgressEvent(
            stage=ProgressStage.DONE,
            job_id="job",
            filename="out.mp4",
        )
    )

    lines = [line for line in buffer.getvalue().splitlines() if line]
    assert 2 <= len(lines) <= 22
    assert lines[-1] == "[done] out.mp4"


def test_postprocessor_stage_replaces_the_download_line() -> None:
    buffer = TtyBuffer()
    status = StatusLine(buffer)
    sink = HumanProgressSink(status)

    sink.emit(download_event(filename="video", downloaded=100, total=100))
    sink.emit(
        ProgressEvent(
            stage=ProgressStage.MERGE,
            job_id="job",
            message="started",
        )
    )
    sink.emit(
        ProgressEvent(
            stage=ProgressStage.DONE,
            job_id="job",
            filename="out.mp4",
        )
    )

    rendered = buffer.getvalue()
    assert "[merge] started" in rendered
    assert rendered.count("\n") == 1

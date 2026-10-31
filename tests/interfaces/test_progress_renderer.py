"""CLI progress renderer and flag-resolution tests."""

from __future__ import annotations

import io
import time

import pytest

from trackforge.application.dto.progress import (
    STAGE_INDEX,
    CollectionProgressSnapshot,
    CollectionProgressStatus,
    ItemProgress,
    ProgressContext,
    ProgressStatus,
)
from trackforge.application.services.progress import ProgressBoard
from trackforge.domain.errors import DomainValidationError
from trackforge.interfaces.cli.progress import (
    PlainProgressRenderer,
    ProgressReporter,
    RichProgressRenderer,
    clamp_progress_interval,
    resolve_progress_mode,
    resolve_reports_mode,
)

pytestmark = pytest.mark.unit


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Broken(io.StringIO):
    def write(self, _text: str) -> int:
        raise OSError("stdout closed")


def _item(
    position: int,
    status: ProgressStatus,
    *,
    stage: str = "observe",
    output_path: str | None = None,
) -> ItemProgress:
    from trackforge.application.dto.progress import STAGE_INDEX

    return ItemProgress(
        scope_id="c",
        collection_id="c",
        item_id=f"i{position}",
        position=position,
        total=2,
        label=f"Track {position}",
        stage=stage,
        stage_index=STAGE_INDEX[stage],
        status=status,
        percent=0.42 if status is ProgressStatus.RUNNING else None,
        message="musicbrainz" if status is ProgressStatus.RUNNING else None,
        elapsed_ms=7_000,
        output_path=output_path,
    )


def _snapshot(*items: ItemProgress) -> CollectionProgressSnapshot:
    from trackforge.domain.model._validation import utc_now

    now = utc_now()
    return CollectionProgressSnapshot(
        collection_id="c",
        status=CollectionProgressStatus.RUNNING,
        started_at=now,
        updated_at=now,
        completed=0,
        failed=0,
        skipped=0,
        remaining=len(items),
        items=items,
    )


def test_auto_mode_is_plain_for_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    stream = io.StringIO()
    assert resolve_progress_mode("auto", stream=stream) == "plain"


def test_auto_mode_is_rich_for_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    assert resolve_progress_mode("auto", stream=_Tty()) == "rich"


def test_ci_and_dumb_and_json_force_plain_or_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "1")
    assert resolve_progress_mode("auto", stream=_Tty()) == "plain"
    monkeypatch.setenv("TERM", "dumb")
    assert resolve_progress_mode("rich", stream=_Tty()) == "plain"
    assert resolve_progress_mode("rich", as_json=True, stream=_Tty()) == "off"
    assert resolve_progress_mode("off", stream=_Tty()) == "off"


def test_interval_is_clamped() -> None:
    assert clamp_progress_interval(0.1) == 0.25
    assert clamp_progress_interval(100) == 10.0
    assert clamp_progress_interval(None) == 1.0
    assert clamp_progress_interval(2.5) == 2.5


def test_reports_mode_defaults() -> None:
    assert resolve_reports_mode(None, as_json=True, stream=_Tty()) == "summary"
    assert resolve_reports_mode("full", stream=io.StringIO()) == "full"
    with pytest.raises(DomainValidationError):
        resolve_reports_mode("verbose", stream=io.StringIO())


def test_rich_table_has_one_row_per_item_without_duplicates() -> None:
    renderer = RichProgressRenderer(stream=io.StringIO())
    table = renderer.render(
        _snapshot(
            _item(1, ProgressStatus.RUNNING),
            _item(2, ProgressStatus.DONE, stage="publish"),
        )
    )
    assert len(table.rows) == 2


def test_rich_done_row_shows_output_filename() -> None:
    from rich.console import Console

    renderer = RichProgressRenderer(stream=io.StringIO())
    table = renderer.render(
        _snapshot(
            _item(
                1,
                ProgressStatus.DONE,
                stage="publish",
                output_path="/music/Artist - Song (Nightcore).m4a",
            )
        )
    )
    stream = io.StringIO()
    Console(file=stream, width=300, color_system=None).print(table)
    assert "Artist - Song (Nightcore).m4a" in stream.getvalue()


def test_plain_renderer_prints_only_transitions() -> None:
    stream = io.StringIO()
    renderer = PlainProgressRenderer(stream=stream)
    renderer.consume(_snapshot(_item(1, ProgressStatus.QUEUED), _item(2, ProgressStatus.QUEUED)))
    assert stream.getvalue() == ""
    renderer.consume(_snapshot(_item(1, ProgressStatus.RUNNING), _item(2, ProgressStatus.QUEUED)))
    first = stream.getvalue()
    assert first.count("\n") == 1
    assert "running" in first
    assert "observe" in first
    renderer.consume(_snapshot(_item(1, ProgressStatus.RUNNING), _item(2, ProgressStatus.QUEUED)))
    assert stream.getvalue() == first
    renderer.consume(
        _snapshot(
            _item(1, ProgressStatus.RUNNING, stage="render"),
            _item(2, ProgressStatus.QUEUED),
        )
    )
    assert "render" in stream.getvalue().splitlines()[-1]
    renderer.consume(_snapshot(_item(1, ProgressStatus.DONE), _item(2, ProgressStatus.QUEUED)))
    assert "done" in stream.getvalue().splitlines()[-1]


def test_plain_renderer_survives_closed_stream() -> None:
    renderer = PlainProgressRenderer(stream=_Broken())
    renderer.consume(_snapshot(_item(1, ProgressStatus.RUNNING)))


def test_reporter_is_off_for_json_and_never_prints_stdout() -> None:
    board = ProgressBoard()
    reporter = ProgressReporter(board, mode="rich", as_json=True, stream=io.StringIO())
    assert reporter.mode == "off"
    reporter.start()
    reporter.stop()
    assert reporter.snapshot() is None


def test_reporter_starts_before_scope_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    board = ProgressBoard()
    stream = _Tty()
    reporter = ProgressReporter(board, mode="rich", interval=0.05, stream=stream)
    reporter.start()
    time.sleep(0.1)
    context = ProgressContext(
        scope_id="c",
        collection_id="c",
        item_id=None,
        position=1,
        total=1,
        label="Collide",
    )
    board.start_collection("c", (context.queued(),))
    board.update(
        ItemProgress(
            scope_id="c",
            collection_id="c",
            item_id=None,
            position=1,
            total=1,
            label="Collide",
            stage="observe",
            stage_index=STAGE_INDEX["observe"],
            status=ProgressStatus.RUNNING,
            percent=0.5,
        )
    )
    time.sleep(0.2)
    reporter.stop()

    assert reporter.error is None
    assert "Collide" in stream.getvalue()


def test_reporter_interval_is_clamped() -> None:
    reporter = ProgressReporter(
        ProgressBoard(),
        mode="plain",
        interval=0.01,
        stream=io.StringIO(),
    )
    assert reporter.interval == 0.25

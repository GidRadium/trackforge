"""Tests for batch scanning."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from trackforge.metadata import discover_media_files, read_many, scan


def test_scan_preserves_order_and_errors(tmp_path: Path, media_dir: Path) -> None:
    missing = tmp_path / "missing.mp3"
    results = scan([media_dir / "sample.mp3", missing], workers=2)

    assert [result.path for result in results] == [
        media_dir / "sample.mp3",
        missing,
    ]
    assert results[0].ok
    assert results[0].media.title == "MP3 Title"
    assert not results[1].ok
    assert results[1].error is not None


def test_read_many_raise_and_skip(tmp_path: Path, media_dir: Path) -> None:
    paths = [media_dir / "sample.mp3", media_dir / "sample.flac"]
    assert len(read_many(paths, workers=2)) == 2

    with pytest.raises(OSError):
        read_many([media_dir / "sample.mp3", tmp_path / "missing.mp3"])

    assert len(read_many([media_dir / "sample.mp3", tmp_path / "missing.mp3"], on_error="skip")) == 1


def test_scan_progress_callback(media_dir: Path) -> None:
    seen: list[tuple[int, int]] = []

    results = scan(
        [media_dir / "sample.mp3", media_dir / "sample.flac"],
        workers=1,
        on_progress=lambda done, total, _result: seen.append((done, total)),
    )

    assert len(results) == 2
    assert seen == [(1, 2), (2, 2)]


def test_scan_limit(media_dir: Path) -> None:
    results = scan(
        [media_dir / "sample.mp3", media_dir / "sample.flac"],
        workers=1,
        limit=1,
    )
    assert len(results) == 1
    assert results[0].path == media_dir / "sample.mp3"

    with pytest.raises(ValueError):
        scan([], limit=-1)


def test_scan_cancelled_before_start(media_dir: Path) -> None:
    cancel = threading.Event()
    cancel.set()

    results = scan(
        [media_dir / "sample.mp3", media_dir / "sample.flac"],
        workers=2,
        cancel=cancel,
    )

    assert len(results) == 2
    assert all(result.cancelled for result in results)
    assert all(not result.ok for result in results)


def test_scan_cancel_after_first(media_dir: Path) -> None:
    cancel = threading.Event()
    paths = [media_dir / "sample.mp3", media_dir / "sample.flac", media_dir / "sample.ogg"]

    def on_progress(done: int, total: int, result) -> None:
        if done == 1:
            cancel.set()

    results = scan(paths, workers=1, cancel=cancel, on_progress=on_progress)

    assert results[0].ok
    assert all(result.cancelled for result in results[1:])


def test_read_many_skips_cancelled(media_dir: Path) -> None:
    cancel = threading.Event()
    cancel.set()

    assert read_many(
        [media_dir / "sample.mp3"],
        cancel=cancel,
        on_error="raise",
    ) == []


def test_discover_media_files(media_dir: Path) -> None:
    files = discover_media_files([media_dir])

    assert files == sorted(files)
    assert media_dir / "sample.mp3" in files
    assert media_dir / "sample.mkv" in files
    assert all(path.suffix.lower() != ".txt" for path in files)

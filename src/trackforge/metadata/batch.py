"""Batch scanning helpers.

``MediaFile.open`` is intentionally synchronous and single-file; this module
adds the small amount of concurrency and error collection a library scanner
needs without hiding failures.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

from .errors import TrackForgeError
from .facade import MediaFile

MEDIA_SUFFIXES = frozenset(
    {
        ".aac", ".aif", ".aiff", ".ape", ".asf", ".avi", ".dff", ".dsf",
        ".flac", ".flv", ".m2ts", ".m4a", ".m4b", ".m4v", ".mka", ".mkv",
        ".mov", ".mp3", ".mp4", ".mpc", ".mpg", ".mpeg", ".oga", ".ogg",
        ".ogv", ".opus", ".tak", ".tta", ".ts", ".wav", ".webm", ".wma",
        ".wmv", ".wv", ".wvp",
    }
)


@dataclass(frozen=True, slots=True)
class ScanResult:
    path: Path
    media: MediaFile | None = None
    error: Exception | None = None
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return not self.cancelled and self.error is None and self.media is not None


def discover_media_files(paths: Iterable[str | Path]) -> list[Path]:
    """Expand files/directories into a sorted, suffix-filtered list."""
    result: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            result.extend(
                candidate
                for candidate in path.rglob("*")
                if candidate.is_file() and candidate.suffix.lower() in MEDIA_SUFFIXES
            )
        elif path.is_file():
            result.append(path)
    return sorted(set(result))


def scan(
    paths: Sequence[str | Path],
    *,
    workers: int = 4,
    on_progress: Callable[[int, int, ScanResult], None] | None = None,
    limit: int | None = None,
    cancel: threading.Event | None = None,
) -> list[ScanResult]:
    """Open every path, collecting per-file errors in input order.

    ``cancel`` is cooperative: running files finish, queued files are not
    opened, and untouched entries are returned with ``cancelled=True``.
    """
    if workers < 1:
        raise ValueError("workers must be positive")
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    files = [Path(path) for path in paths]
    if limit is not None:
        files = files[:limit]

    def one(path: Path) -> ScanResult:
        try:
            return ScanResult(path=path, media=MediaFile.open(path))
        except (TrackForgeError, OSError, ValueError) as exc:
            return ScanResult(path=path, error=exc)

    if workers == 1 or len(files) < 2:
        results: list[ScanResult] = []
        completed = 0
        for path in files:
            if cancel is not None and cancel.is_set():
                results.append(ScanResult(path=path, cancelled=True))
                continue
            result = one(path)
            results.append(result)
            completed += 1
            if on_progress is not None:
                on_progress(completed, len(files), result)
        return results

    results: list[ScanResult | None] = [None] * len(files)
    next_index = 0
    completed = 0
    window = max(workers * 2, 1)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending: dict = {}

        def submit_more() -> None:
            nonlocal next_index
            while next_index < len(files) and len(pending) < window:
                if cancel is not None and cancel.is_set():
                    return
                path = files[next_index]
                pending[executor.submit(one, path)] = next_index
                next_index += 1

        submit_more()
        while pending:
            done, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
            for future in done:
                index = pending.pop(future)
                result = future.result()
                results[index] = result
                completed += 1
                if on_progress is not None:
                    on_progress(completed, len(files), result)
            submit_more()

    for index, result in enumerate(results):
        if result is None:
            results[index] = ScanResult(path=files[index], cancelled=True)
    return [result for result in results if result is not None]


def read_many(
    paths: Sequence[str | Path],
    *,
    workers: int = 4,
    on_error: str = "raise",
    on_progress: Callable[[int, int, ScanResult], None] | None = None,
    limit: int | None = None,
    cancel: threading.Event | None = None,
) -> list[MediaFile]:
    """Like :func:`scan`, but returns only successful reads.

    ``on_error='raise'`` surfaces the first failure; ``'skip'`` drops it.
    """
    if on_error not in {"raise", "skip"}:
        raise ValueError("on_error must be 'raise' or 'skip'")
    results = scan(
        paths,
        workers=workers,
        on_progress=on_progress,
        limit=limit,
        cancel=cancel,
    )
    if on_error == "raise":
        for result in results:
            if result.error is not None:
                raise result.error
    return [result.media for result in results if result.media is not None]


__all__ = [
    "MEDIA_SUFFIXES",
    "ScanResult",
    "discover_media_files",
    "read_many",
    "scan",
]

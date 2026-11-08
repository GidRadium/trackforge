"""Batch scanning helpers.

``MediaFile.open`` is intentionally synchronous and single-file; this module
adds the small amount of concurrency and error collection a library scanner
needs without hiding failures.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
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

    @property
    def ok(self) -> bool:
        return self.error is None and self.media is not None


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
) -> list[ScanResult]:
    """Open every path, collecting per-file errors in input order."""
    if workers < 1:
        raise ValueError("workers must be positive")
    files = [Path(path) for path in paths]

    def one(path: Path) -> ScanResult:
        try:
            return ScanResult(path=path, media=MediaFile.open(path))
        except (TrackForgeError, OSError, ValueError) as exc:
            return ScanResult(path=path, error=exc)

    if workers == 1 or len(files) < 2:
        results = []
        for index, path in enumerate(files):
            result = one(path)
            results.append(result)
            if on_progress is not None:
                on_progress(index + 1, len(files), result)
        return results

    results: list[ScanResult | None] = [None] * len(files)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(one, path): index
            for index, path in enumerate(files)
        }
        completed = 0
        for future, index in futures.items():
            result = future.result()
            results[index] = result
            completed += 1
            if on_progress is not None:
                on_progress(completed, len(files), result)
    return [result for result in results if result is not None]


def read_many(
    paths: Sequence[str | Path],
    *,
    workers: int = 4,
    on_error: str = "raise",
    on_progress: Callable[[int, int, ScanResult], None] | None = None,
) -> list[MediaFile]:
    """Like :func:`scan`, but returns only successful reads.

    ``on_error='raise'`` surfaces the first failure; ``'skip'`` drops it.
    """
    if on_error not in {"raise", "skip"}:
        raise ValueError("on_error must be 'raise' or 'skip'")
    results = scan(paths, workers=workers, on_progress=on_progress)
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

"""Flat collection limits and the options sent to yt-dlp."""

# pyright: standard

from __future__ import annotations

from collections.abc import Iterator
from typing import Self

import pytest

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.loaders.ytdlp import (
    DEFAULT_FLAT_LIMIT,
    MAX_FLAT_LIMIT,
    YtDlpLoader,
    effective_flat_limit,
)
from trackforge.acquire.loaders.ytdlp_mapper import YtDlpMapper
from trackforge.acquire.models import ResolutionState, SourceKind, SourceRef


class CapturingYoutubeDL:
    def __init__(self, options: dict[str, object]) -> None:
        self.options = options
        self.info: dict[str, object] = {
            "_type": "playlist",
            "id": "PL1",
            "title": "Playlist",
            "entries": [],
        }

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def extract_info(self, url: str, *, download: bool) -> dict[str, object]:
        del url, download
        return self.info

    def sanitize_info(self, info: dict[str, object]) -> dict[str, object]:
        return info


def run_extract(
    monkeypatch: pytest.MonkeyPatch,
    loader: YtDlpLoader,
    *,
    flat: bool,
    limit: int | None,
    profile: SourceProfile | None = None,
) -> dict[str, object]:
    captured: dict[str, object] = {}

    def create(options: dict[str, object]) -> CapturingYoutubeDL:
        captured.update(options)
        return CapturingYoutubeDL(options)

    monkeypatch.setattr(loader, "_create_youtube_dl", create)
    ctx = LoaderContext(
        job_id="job-limits",
        profile=profile
        or SourceProfile(name="yt_dlp", cookies_from_browser=("chrome",)),
    )
    loader._extract(
        "https://www.youtube.com/playlist?list=PL1",
        ctx=ctx,
        flat=flat,
        limit=limit,
    )
    return captured


def test_flat_extract_uses_in_playlist_and_playlist_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = run_extract(
        monkeypatch,
        YtDlpLoader(),
        flat=True,
        limit=2,
    )

    assert options["extract_flat"] == "in_playlist"
    assert options["playlist_items"] == "1:2"
    assert options["noplaylist"] is False
    assert options["skip_download"] is True


def test_full_resolve_sets_noplaylist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = run_extract(
        monkeypatch,
        YtDlpLoader(),
        flat=False,
        limit=None,
    )

    assert options["extract_flat"] is False
    assert options["noplaylist"] is True
    assert options["playlist_items"] == f"1:{DEFAULT_FLAT_LIMIT}"


@pytest.mark.parametrize("limit", [None, 0, -1])
def test_missing_or_zero_limit_uses_the_default(
    monkeypatch: pytest.MonkeyPatch,
    limit: int | None,
) -> None:
    options = run_extract(
        monkeypatch,
        YtDlpLoader(),
        flat=True,
        limit=limit,
    )
    assert options["playlist_items"] == f"1:{DEFAULT_FLAT_LIMIT}"


def test_limit_is_capped_by_the_configured_maximum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SourceProfile(
        name="yt_dlp",
        extra={"flat_limit_max": 7},
    )
    options = run_extract(
        monkeypatch,
        YtDlpLoader(),
        flat=True,
        limit=999,
        profile=profile,
    )
    assert options["playlist_items"] == "1:7"
    assert effective_flat_limit(999, profile) == 7
    assert effective_flat_limit(None, profile) == 7


def test_default_and_max_limits_are_configurable() -> None:
    profile = SourceProfile(
        name="yt_dlp",
        extra={"flat_limit": 3, "flat_limit_max": 4},
    )
    assert effective_flat_limit(None, profile) == 3
    assert effective_flat_limit(10, profile) == 4
    assert effective_flat_limit(2, profile) == 2
    assert DEFAULT_FLAT_LIMIT == 50
    assert MAX_FLAT_LIMIT == 500


def test_lazy_entries_are_not_materialized_past_the_limit() -> None:
    consumed: list[int] = []

    def entries() -> Iterator[dict[str, object]]:
        for index in range(1, 101):
            consumed.append(index)
            yield {
                "_type": "url",
                "id": f"v{index}",
                "title": f"Track {index}",
                "webpage_url": f"https://www.youtube.com/watch?v=v{index}",
            }

    source = YtDlpMapper(SourceProfile(name="yt_dlp")).map(
        {
            "_type": "playlist",
            "id": "PL-lazy",
            "title": "Lazy",
            "entries": entries(),
        },
        SourceRef("yt-dlp", "https://www.youtube.com/playlist?list=PL-lazy"),
        flat=True,
        limit=3,
    )

    assert source.kind is SourceKind.COLLECTION
    assert len(source.items) == 3
    assert consumed == [1, 2, 3]
    assert all(item.resolution is ResolutionState.FLAT for item in source.items)

"""Flat playlist mapping: bounded, URL-addressable items without formats."""

# pyright: standard

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.loaders.ytdlp import YtDlpLoader
from trackforge.acquire.loaders.ytdlp_mapper import YtDlpMapper
from trackforge.acquire.models import (
    ResolutionState,
    SourceKind,
    SourceRef,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def reference(info: dict[str, Any]) -> SourceRef:
    return SourceRef(
        source="yt-dlp",
        url=str(info["webpage_url"]),
        id=str(info["id"]),
    )


def context() -> LoaderContext:
    return LoaderContext(
        job_id="job-flat",
        profile=SourceProfile(
            name="yt_dlp",
            cookies_from_browser=("chrome",),
        ),
    )


def test_flat_playlist_maps_to_collection_with_bounded_items() -> None:
    info = load_fixture("youtube_playlist_flat.info.json")
    source = YtDlpMapper(SourceProfile(name="yt_dlp")).map(
        info,
        reference(info),
        flat=True,
        limit=3,
    )

    assert source.kind is SourceKind.COLLECTION
    assert source.id == "PLflat1234567890"
    assert source.title == "TrackForge Flat Playlist"
    assert source.creator == "TrackForge Channel"
    assert source.description
    assert len(source.items) == 3


def test_flat_items_are_addressable_and_have_no_variants() -> None:
    info = load_fixture("youtube_playlist_flat.info.json")
    source = YtDlpMapper(SourceProfile(name="yt_dlp")).map(
        info,
        reference(info),
        flat=True,
        limit=10,
    )

    assert [item.position for item in source.items] == [1, 2, 3, 4, 5]
    for item in source.items:
        assert item.page_url is not None
        assert item.page_url.startswith("https://www.youtube.com/watch?v=")
        assert item.resolution is ResolutionState.FLAT
        assert item.variants == ()
        assert item.creator is not None
        assert item.thumbnail is not None


def test_flat_loader_does_not_full_resolve_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = load_fixture("youtube_playlist_flat.info.json")
    loader = YtDlpLoader()
    calls: list[dict[str, object]] = []

    def fake_extract(
        url: str,
        *,
        ctx: LoaderContext,
        flat: bool = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        del ctx
        calls.append({"url": url, "flat": flat, "limit": limit})
        return info

    monkeypatch.setattr(loader, "_extract", fake_extract)
    source = loader.get_info(
        reference(info),
        ctx=context(),
        flat=True,
        limit=2,
    )

    assert calls == [
        {"url": reference(info).url, "flat": True, "limit": 2}
    ]
    assert source.kind is SourceKind.COLLECTION
    assert len(source.items) == 2
    assert all(
        item.resolution is ResolutionState.FLAT and not item.variants
        for item in source.items
    )

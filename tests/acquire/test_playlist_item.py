"""Full resolve of a selected playlist item by its flat ``page_url``."""

# pyright: standard

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.loaders.ytdlp import YtDlpLoader
from trackforge.acquire.models import (
    ResolutionState,
    SourceKind,
    SourceRef,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def context() -> LoaderContext:
    return LoaderContext(
        job_id="job-item",
        profile=SourceProfile(
            name="yt_dlp",
            cookies_from_browser=("chrome",),
        ),
    )


def test_flat_item_page_url_resolves_to_single_with_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flat_info = load_fixture("youtube_playlist_flat.info.json")
    full_info = load_fixture("youtube_playlist_full.info.json")
    entry = full_info["entries"][0]
    flat_entry = flat_info["entries"][0]
    page_url = flat_entry["webpage_url"]
    assert entry["webpage_url"] == page_url

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
        return entry

    monkeypatch.setattr(loader, "_extract", fake_extract)
    source = loader.get_info(
        SourceRef("yt-dlp", page_url, id=str(flat_entry["id"])),
        ctx=context(),
    )

    assert calls == [{"url": page_url, "flat": False, "limit": 50}]
    assert source.kind is SourceKind.SINGLE
    assert source.url == page_url
    assert len(source.items) == 1
    item = source.items[0]
    assert item.page_url == page_url
    assert item.resolution is ResolutionState.FULL
    assert item.variants
    assert item.recommendations.video == "1080p-mp4"
    assert item.recommendations.audio is not None


def test_full_playlist_fixture_keeps_formats_on_entries() -> None:
    from trackforge.acquire.loaders.ytdlp_mapper import YtDlpMapper

    info = load_fixture("youtube_playlist_full.info.json")
    source = YtDlpMapper(SourceProfile(name="yt_dlp")).map(
        info,
        SourceRef("yt-dlp", str(info["webpage_url"])),
        flat=False,
        limit=1,
    )

    assert source.kind is SourceKind.COLLECTION
    assert len(source.items) == 1
    assert source.items[0].resolution is ResolutionState.FULL
    assert source.items[0].variants

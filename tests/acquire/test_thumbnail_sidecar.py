"""Cached thumbnails are written as sidecars without a second fetch."""

# pyright: standard

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fakes import FakeYoutubeDL, make_source_info

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.loaders.ytdlp import YtDlpLoader
from trackforge.acquire.models import (
    DownloadOptions,
    DownloadRequest,
    ItemSelection,
    Thumbnail,
    ThumbnailAsset,
)
from trackforge.acquire.progress import RecordingSink
from trackforge.acquire.staging import StagingArea
from trackforge.acquire.thumbnail_cache import ThumbnailCache

# Minimal JPEG header with SOF0: 320x120, enough for magic-byte detection.
JPEG_BYTES = (
    b"\xff\xd8\xff\xc0\x00\x11\x08\x00\x78\x01\x40"
    + b"\x00" * 32
)


@pytest.fixture(autouse=True)
def reset_fake() -> None:
    FakeYoutubeDL.reset()


def make_request(thumbnail: Thumbnail, tmp_path: Path) -> DownloadRequest:
    source = make_source_info()
    item = replace(
        source.items[0],
        thumbnails=(thumbnail,),
        thumbnail=thumbnail,
    )
    source = replace(source, items=(item,), thumbnail=thumbnail)
    return DownloadRequest(
        source_info=source,
        selections=(ItemSelection(item.id, item.variants[0].id),),
        options=DownloadOptions(
            target_dir=tmp_path / "staging",
            write_thumbnail=True,
        ),
    )


def test_cached_youtube_thumbnail_is_written_without_refetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thumbnail = Thumbnail(
        url="https://i.ytimg.com/vi/abc/maxresdefault.jpg",
        width=1280,
        height=720,
        ext="jpg",
        source="youtube:maxres",
    )
    request = make_request(thumbnail, tmp_path)
    cache = ThumbnailCache()
    cache.put(
        thumbnail.url,
        ThumbnailAsset(
            data=JPEG_BYTES,
            mime="image/jpeg",
            width=320,
            height=120,
            ext="jpg",
        ),
    )
    loader = YtDlpLoader()
    monkeypatch.setattr(
        loader,
        "_create_youtube_dl",
        lambda options: FakeYoutubeDL(options),
    )
    monkeypatch.setattr(
        loader,
        "_fetch_thumbnail_bytes",
        lambda *args, **kwargs: pytest.fail("cached thumbnail was refetched"),
    )
    ctx = LoaderContext(
        job_id="job-thumbnail",
        profile=SourceProfile(name="yt_dlp"),
        staging=StagingArea(tmp_path / "staging"),
        progress=RecordingSink(),
        thumbnail_cache=cache,
    )

    result = loader.download(request, ctx=ctx)

    assert result.failures == ()
    assert result.files[0].warnings == ()
    sidecar = result.files[0].sidecars[0]
    assert sidecar.name == "Fake media.jpg"
    assert sidecar.read_bytes() == JPEG_BYTES
    assert FakeYoutubeDL.instances[-1].options["writethumbnail"] is False


def test_thumbnail_failure_is_a_warning_not_a_download_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thumbnail = Thumbnail(
        url="https://i.ytimg.com/vi/abc/maxresdefault.jpg",
        width=1280,
        height=720,
        ext="jpg",
        source="youtube:maxres",
    )
    request = make_request(thumbnail, tmp_path)

    class ForcedThumbnailLoader(YtDlpLoader):
        def thumbnail_sidecar(self, item, *, ctx):
            del ctx
            return item.thumbnail

    loader = ForcedThumbnailLoader()
    monkeypatch.setattr(
        loader,
        "_create_youtube_dl",
        lambda options: FakeYoutubeDL(options),
    )
    monkeypatch.setattr(
        loader,
        "fetch_thumbnail",
        lambda thumbnail, *, ctx, max_bytes=None: None,
    )
    ctx = LoaderContext(
        job_id="job-thumbnail-fail",
        profile=SourceProfile(name="yt_dlp"),
        staging=StagingArea(tmp_path / "staging"),
        progress=RecordingSink(),
        thumbnail_cache=ThumbnailCache(),
    )

    result = loader.download(request, ctx=ctx)

    assert result.failures == ()
    assert len(result.files) == 1
    assert result.files[0].sidecars == ()
    assert any(
        "thumbnail is not available" in warning
        for warning in result.files[0].warnings
    )

"""In-memory thumbnail cache and single-fetch behavior."""

# pyright: standard

from __future__ import annotations

import pytest
from fakes import FakeLoader, make_source_info

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.loaders.ytdlp import YtDlpLoader
from trackforge.acquire.models import Thumbnail, ThumbnailAsset
from trackforge.acquire.registry import LoaderRegistry
from trackforge.acquire.service import AcquisitionService
from trackforge.acquire.thumbnail_cache import ThumbnailCache

JPEG = b"\xff\xd8\xff\xc0\x00\x11\x08\x00\x78\x01\x40" + b"x" * 16


def asset(data: bytes = b"image") -> ThumbnailAsset:
    return ThumbnailAsset(
        data=data,
        mime="image/jpeg",
        width=320,
        height=120,
        ext="jpg",
    )


def test_cache_get_put_and_clear() -> None:
    cache = ThumbnailCache()
    first = Thumbnail("https://example.test/1.jpg")
    second = Thumbnail("https://example.test/2.jpg")

    cache.put(first.url, asset(b"one"))
    cache.put(second.url, asset(b"two"))

    assert cache.get(first.url) is not None
    assert cache.get("") is None
    assert len(cache) == 2
    assert first.url in cache
    cache.clear()
    assert len(cache) == 0
    assert cache.total_bytes == 0


def test_cache_evicts_oldest_entries() -> None:
    cache = ThumbnailCache(max_bytes=6)
    first = "https://example.test/1.jpg"
    second = "https://example.test/2.jpg"

    cache.put(first, asset(b"1234"))
    cache.put(second, asset(b"5678"))

    assert cache.get(first) is None
    assert cache.get(second) is not None
    assert cache.total_bytes == 4


def test_oversized_asset_is_not_cached() -> None:
    cache = ThumbnailCache(max_bytes=3)

    cache.put("https://example.test/big.jpg", asset(b"1234"))

    assert len(cache) == 0


def test_fetch_thumbnail_stores_bytes_and_second_call_is_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_fetch(thumbnail: Thumbnail, *, ctx: LoaderContext, max_bytes: int):
        del ctx
        calls.append(thumbnail.url)
        assert max_bytes > 0
        return JPEG

    loader = YtDlpLoader()
    monkeypatch.setattr(loader, "_fetch_thumbnail_bytes", fake_fetch)
    thumbnail = Thumbnail(url="https://example.test/cover.jpg", ext="jpg")
    cache = ThumbnailCache()
    ctx = LoaderContext(
        job_id="job",
        profile=SourceProfile(name="yt_dlp", proxy="none"),
        thumbnail_cache=cache,
    )

    first = loader.fetch_thumbnail(thumbnail, ctx=ctx)
    second = loader.fetch_thumbnail(thumbnail, ctx=ctx)

    assert first is not None
    assert second == first
    assert calls == [thumbnail.url]
    assert cache.get(thumbnail.url) == first
    assert first.ext == "jpg"
    assert (first.width, first.height) == (320, 120)


def test_service_shares_cache_with_loader_context() -> None:
    cache = ThumbnailCache()
    loader = FakeLoader(info=make_source_info())
    service = AcquisitionService(
        registry=LoaderRegistry([loader]),
        thumbnail_cache=cache,
    )
    resolved = service.registry.resolve("https://example.test/media/1")

    ctx = service.context_for(resolved)

    assert ctx.thumbnail_cache is cache


def test_service_fetch_thumbnail_returns_cached_asset() -> None:
    cache = ThumbnailCache()
    thumbnail = Thumbnail(url="https://example.test/cover.jpg", ext="jpg")
    cached = asset(b"cached")
    cache.put(thumbnail.url, cached)
    service = AcquisitionService(
        registry=LoaderRegistry([]),
        thumbnail_cache=cache,
    )

    assert service.fetch_thumbnail(thumbnail) == cached


def test_service_fetch_thumbnail_without_cache_or_url_returns_none() -> None:
    service = AcquisitionService(registry=LoaderRegistry([]))

    assert (
        service.fetch_thumbnail(Thumbnail(url="https://example.test/cover.jpg"))
        is None
    )


def test_service_fetch_thumbnail_cold_path_uses_registered_loader() -> None:
    class FetchingLoader(FakeLoader):
        def fetch_thumbnail(self, thumbnail, *, ctx, max_bytes=None):
            del ctx, max_bytes
            return ThumbnailAsset(
                data=b"cold",
                mime="image/png",
                width=10,
                height=20,
                ext="png",
            )

    loader = FetchingLoader(info=make_source_info())
    service = AcquisitionService(registry=LoaderRegistry([loader]))
    thumbnail = Thumbnail(url="https://example.test/cold.png", ext="png")

    fetched = service.fetch_thumbnail(
        thumbnail,
        url="https://example.test/media/1",
        source="fake",
    )

    assert fetched is not None
    assert fetched.data == b"cold"
    assert service.thumbnail_cache.get(thumbnail.url) == fetched


def test_any_loader_uses_the_shared_thumbnail_api_with_injected_http() -> None:
    class Client:
        def get(self, url: str) -> bytes:
            assert url == "https://example.test/cover.jpg"
            return JPEG

    loader = FakeLoader()
    cache = ThumbnailCache()
    ctx = LoaderContext(
        job_id="job",
        profile=SourceProfile(name="fake"),
        http=Client(),
        thumbnail_cache=cache,
    )
    thumbnail = Thumbnail(url="https://example.test/cover.jpg", ext="jpg")

    asset = loader.fetch_thumbnail(thumbnail, ctx=ctx)

    assert asset is not None
    assert asset.ext == "jpg"
    assert (asset.width, asset.height) == (320, 120)
    assert cache.get(thumbnail.url) == asset

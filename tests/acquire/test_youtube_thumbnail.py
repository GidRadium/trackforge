"""YouTube thumbnail candidate and full-resolve selection tests."""

# pyright: standard

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.loaders.youtube import YouTubeLoader
from trackforge.acquire.models import MediaItem, Thumbnail, ThumbnailAsset
from trackforge.acquire.thumbnail_cache import ThumbnailCache


def make_item(*, position: int | None = 1, item_id: str = "abc123") -> MediaItem:
    return MediaItem(
        id=item_id,
        title="Video",
        position=position,
        thumbnails=(
            Thumbnail(
                url="https://i.ytimg.com/vi/abc123/mqdefault.jpg",
                width=320,
                height=180,
                ext="jpg",
            ),
            Thumbnail(
                url="https://i.ytimg.com/vi/abc123/hqdefault.jpg",
                width=480,
                height=360,
                ext="jpg",
            ),
        ),
    )


def context(*, cache: bool = True) -> LoaderContext:
    return LoaderContext(
        job_id="job-1",
        profile=SourceProfile(name="youtube"),
        thumbnail_cache=ThumbnailCache() if cache else None,
    )


def install_fetch(
    loader: YouTubeLoader,
    monkeypatch: pytest.MonkeyPatch,
    available: Callable[[str], bool],
) -> list[str]:
    calls: list[str] = []

    def fake_fetch(
        thumbnail: Thumbnail,
        *,
        ctx: LoaderContext,
        max_bytes: int | None = None,
    ) -> ThumbnailAsset | None:
        del ctx, max_bytes
        calls.append(thumbnail.url)
        if not available(thumbnail.url):
            return None
        return ThumbnailAsset(
            data=b"image-bytes",
            mime="image/jpeg",
            width=777,
            height=444,
            ext="jpg",
        )

    monkeypatch.setattr(loader, "fetch_thumbnail", fake_fetch)
    return calls


def test_candidates_are_ordered_without_network() -> None:
    loader = YouTubeLoader()
    ctx = context(cache=True)
    monkeypatch_calls: list[str] = []

    def fail_fetch(*args: object, **kwargs: object) -> None:
        del args, kwargs
        monkeypatch_calls.append("fetch")
        raise AssertionError("candidate generation must not fetch thumbnails")

    loader.fetch_thumbnail = fail_fetch  # type: ignore[method-assign]
    candidates = loader.thumbnail_candidates(make_item(), ctx=ctx)

    assert [candidate.url for candidate in candidates] == [
        "https://i.ytimg.com/vi/abc123/maxresdefault.jpg",
        "https://i.ytimg.com/vi/abc123/sddefault.jpg",
        "https://i.ytimg.com/vi/abc123/hqdefault.jpg",
        "https://i.ytimg.com/vi/abc123/mqdefault.jpg",
    ]
    assert (candidates[0].width, candidates[0].height) == (1280, 720)
    assert monkeypatch_calls == []


def test_youtube_query_is_stripped_and_known_size_applied() -> None:
    item = MediaItem(
        id="abc123",
        title="Video",
        position=1,
        thumbnails=(
            Thumbnail(
                url=(
                    "https://i.ytimg.com/vi/abc123/maxresdefault.jpg"
                    "?sqp=cache-busting&rs=signature"
                ),
                width=1920,
                height=1080,
                ext="jpg",
            ),
        ),
    )

    candidates = YouTubeLoader().thumbnail_candidates(item, ctx=context())

    assert candidates[0].url == "https://i.ytimg.com/vi/abc123/maxresdefault.jpg"
    assert (candidates[0].width, candidates[0].height) == (1280, 720)


def test_flat_items_never_fetch_thumbnails(monkeypatch: pytest.MonkeyPatch) -> None:
    from trackforge.acquire.models import ResolutionState

    item = replace(make_item(), resolution=ResolutionState.FLAT)
    loader = YouTubeLoader()
    calls = install_fetch(loader, monkeypatch, lambda url: True)

    chosen = loader.choose_thumbnail(item, ctx=context())

    assert chosen is not None
    assert chosen.url.endswith("maxresdefault.jpg")
    assert calls == []


def test_full_resolve_stops_at_the_first_working_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = YouTubeLoader()
    calls = install_fetch(loader, monkeypatch, lambda url: "maxresdefault" in url)

    chosen = loader.choose_thumbnail(make_item(), ctx=context())

    assert chosen is not None
    assert chosen.url.endswith("maxresdefault.jpg")
    assert (chosen.width, chosen.height) == (777, 444)
    assert calls == ["https://i.ytimg.com/vi/abc123/maxresdefault.jpg"]


def test_full_resolve_falls_back_to_the_next_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = YouTubeLoader()
    calls = install_fetch(loader, monkeypatch, lambda url: "sddefault" in url)

    chosen = loader.choose_thumbnail(make_item(), ctx=context())

    assert chosen is not None
    assert chosen.url.endswith("sddefault.jpg")
    assert (chosen.width, chosen.height) == (777, 444)
    assert calls == [
        "https://i.ytimg.com/vi/abc123/maxresdefault.jpg",
        "https://i.ytimg.com/vi/abc123/sddefault.jpg",
    ]


def test_full_resolve_returns_best_metadata_candidate_when_all_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = YouTubeLoader()
    calls = install_fetch(loader, monkeypatch, lambda _url: False)

    chosen = loader.choose_thumbnail(make_item(), ctx=context())

    assert chosen is not None
    assert chosen.url.endswith("maxresdefault.jpg")
    assert (chosen.width, chosen.height) == (1280, 720)
    assert len(calls) == 4


def test_fetched_bytes_are_cached_for_later_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jpeg = b"\xff\xd8\xff\xc0\x00\x11\x08\x00\x78\x01\x40" + b"x" * 16
    loader = YouTubeLoader()
    monkeypatch.setattr(
        loader,
        "_fetch_thumbnail_bytes",
        lambda thumbnail, *, ctx, max_bytes: jpeg,
    )
    ctx = context()

    chosen = loader.choose_thumbnail(make_item(), ctx=ctx)

    assert chosen is not None
    assert chosen.url.endswith("maxresdefault.jpg")
    assert ctx.thumbnail_cache is not None
    cached = ctx.thumbnail_cache.get(chosen.url)
    assert cached is not None
    assert cached.data == jpeg
    assert cached.ext == "jpg"


def test_no_fetch_without_a_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = YouTubeLoader()
    calls = install_fetch(loader, monkeypatch, lambda _url: True)

    chosen = loader.choose_thumbnail(make_item(), ctx=context(cache=False))

    assert chosen is not None
    assert chosen.url.endswith("maxresdefault.jpg")
    assert calls == []


def test_secondary_items_do_not_receive_synthetic_fallbacks() -> None:
    candidates = YouTubeLoader().thumbnail_candidates(
        make_item(position=2),
        ctx=context(),
    )

    assert [candidate.url for candidate in candidates] == [
        "https://i.ytimg.com/vi/abc123/hqdefault.jpg",
        "https://i.ytimg.com/vi/abc123/mqdefault.jpg",
    ]


def test_full_collection_only_fetches_primary_thumbnail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trackforge.acquire.models import SourceInfo, SourceKind

    loader = YouTubeLoader()
    calls = install_fetch(loader, monkeypatch, lambda _url: True)
    first = make_item(position=1)
    second = make_item(position=2)
    source = SourceInfo(
        source="youtube",
        url="https://www.youtube.com/playlist?list=test",
        kind=SourceKind.COLLECTION,
        items=(first, second),
    )
    ctx = context()

    finalized = loader._finalize(source, ctx=ctx)

    assert len(calls) == 1
    assert calls[0].endswith("maxresdefault.jpg")
    assert finalized.items[0].thumbnail is not None
    assert finalized.items[1].thumbnail is not None
    assert finalized.items[1].thumbnail.url.endswith("hqdefault.jpg")


def test_injected_http_client_is_used_for_thumbnail_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jpeg = b"\xff\xd8\xff\xc0\x00\x11\x08\x00\x78\x01\x40" + b"x" * 16

    class Client:
        def get(self, url: str) -> bytes:
            assert "maxresdefault" in url
            return jpeg

    loader = YouTubeLoader()
    monkeypatch.setattr(
        loader,
        "_fetch_thumbnail_bytes",
        lambda *args, **kwargs: pytest.fail("real network helper was used"),
    )
    ctx = context()
    ctx.http = Client()

    chosen = loader.choose_thumbnail(make_item(), ctx=ctx)

    assert chosen is not None
    assert chosen.url.endswith("maxresdefault.jpg")
    assert ctx.thumbnail_cache is not None
    assert ctx.thumbnail_cache.get(chosen.url) is not None

"""Tests for MediaLoader defaults and the offline FakeLoader."""

# pyright: standard

from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FakeLoader, make_context, make_source_info

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.errors import SearchNotSupported
from trackforge.acquire.loaders.base import (
    LoaderCapabilities,
    MediaLoader,
    default_recommendations,
    select_best_thumbnail,
)
from trackforge.acquire.loaders.ytdlp import YtDlpLoader
from trackforge.acquire.models import (
    DownloadVariant,
    MediaItem,
    MediaKind,
    SourceKind,
    StreamInfo,
    StreamRole,
    Thumbnail,
    UrlHint,
)


class MinimalLoader(MediaLoader):
    name = "minimal"
    capabilities = LoaderCapabilities()

    def supports_url(self, url: str) -> bool:
        return url.startswith("minimal:")

    def get_info(self, ref, *, ctx, flat=False, limit=None):
        del ref, ctx, flat, limit
        return make_source_info(source=self.name)

    def download(self, request, *, ctx):
        del request, ctx
        from trackforge.acquire.models import DownloadResult

        return DownloadResult(files=())


def test_cannot_instantiate_media_loader() -> None:
    with pytest.raises(TypeError):
        MediaLoader()  # pyright: ignore[reportAbstractUsage]


def test_loader_does_not_expose_is_playlist_url_or_is_post() -> None:
    assert not hasattr(MediaLoader, "is_playlist_url")
    assert not hasattr(MediaLoader, "is_post")
    assert not hasattr(YtDlpLoader, "is_playlist_url")
    assert not hasattr(YtDlpLoader, "is_post")


def test_defaults_and_profile_name() -> None:
    loader = MinimalLoader()
    assert loader.priority == 100
    assert loader.config_key is None
    assert loader.required_plugins == ()
    assert loader.profile_name() == "minimal"
    assert loader.classify_url("minimal:1") is UrlHint.UNKNOWN
    assert loader.is_ready(SourceProfile(name="minimal")) is True


def test_search_default_raises_search_not_supported() -> None:
    loader = MinimalLoader()
    ctx = make_context()
    with pytest.raises(SearchNotSupported):
        loader.search("query", limit=5, ctx=ctx)


def test_fetch_thumbnail_is_offline_noop() -> None:
    loader = MinimalLoader()
    ctx = make_context()
    thumbnail = Thumbnail(url="https://example.test/cover.jpg")
    assert loader.fetch_thumbnail(thumbnail, ctx=ctx, max_bytes=1000) is None


def test_fake_loader_returns_source_info_without_network() -> None:
    info = make_source_info()
    loader = FakeLoader(info=info)
    ctx = make_context()
    resolved = loader.get_info(info.ref(), ctx=ctx)

    assert resolved.source == "fake"
    assert resolved.url == info.url
    assert resolved.items[0].title == "Fake media"
    assert loader.info_calls == [info.ref()]


def test_fake_loader_respects_url_support() -> None:
    loader = FakeLoader(supports=("https://example.test/",))
    assert loader.supports_url("https://example.test/a") is True
    assert loader.supports_url("https://other.test/a") is False


def test_fake_loader_readiness_is_recorded() -> None:
    loader = FakeLoader(ready=False)
    profile = SourceProfile(name="fake")
    assert loader.is_ready(profile) is False
    assert loader.profiles == [profile]


def test_fake_loader_download_returns_empty_result() -> None:
    from trackforge.acquire.models import (
        DownloadOptions,
        DownloadRequest,
        ItemSelection,
    )

    info = make_source_info()
    loader = FakeLoader(info=info)
    request = DownloadRequest(
        source_info=info,
        selections=(ItemSelection(item_id="item-1", variant_id="audio-best"),),
        options=DownloadOptions(target_dir=Path("/tmp/out")),
    )
    result = loader.download(request, ctx=make_context())
    assert result.files == ()
    assert loader.download_calls == [request]


def test_choose_thumbnail_picks_best_candidate() -> None:
    loader = MinimalLoader()
    small = Thumbnail(url="https://example.test/small.jpg", width=320, height=180)
    large = Thumbnail(url="https://example.test/large.jpg", width=1280, height=720)
    item = MediaItem(
        id="1",
        title="T",
        thumbnails=(small, large),
    )
    assert loader.choose_thumbnail(item, ctx=make_context()) == large


def test_select_best_thumbnail_dedupes_and_filters_placeholders() -> None:
    best = Thumbnail(url="https://example.test/best.jpg", width=1280, height=720)
    candidates = [
        Thumbnail(url="https://example.test/best.jpg", width=1, height=1),
        Thumbnail(url="https://example.test/avatar.jpg", width=1280, height=720),
        Thumbnail(url="https://example.test/tiny.jpg", width=100, height=100),
        best,
    ]
    assert select_best_thumbnail(candidates) is best


def test_default_recommendations_choose_usable_variants() -> None:
    def variant(
        variant_id: str,
        kind: MediaKind,
        *,
        drm: bool = False,
        downloadable: bool = True,
        reason: str | None = None,
    ) -> DownloadVariant:
        return DownloadVariant(
            id=variant_id,
            label=variant_id,
            kind=kind,
            container="m4a",
            streams=(StreamInfo(role=StreamRole.AUDIO, drm=drm),),
            downloadable=downloadable,
            unavailable_reason=reason,
        )

    item = MediaItem(
        id="1",
        title="T",
        variants=(
            variant("drm", MediaKind.AUDIO, drm=True),
            variant("unavailable", MediaKind.VIDEO, downloadable=False),
            variant("audio", MediaKind.AUDIO),
            variant("muxed", MediaKind.AUDIO_VIDEO),
            variant("video", MediaKind.VIDEO),
            variant("image", MediaKind.IMAGE),
        ),
    )
    recommendations = default_recommendations(item)
    assert recommendations.audio == "audio"
    assert recommendations.video == "muxed"
    assert recommendations.image == "image"
    assert recommendations.document is None


def test_ytdlp_loader_maps_an_offline_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = YtDlpLoader()
    assert loader.supports_url("https://example.test/watch/1") is True
    assert loader.supports_url("http://example.test/x") is True
    assert loader.supports_url("ftp://example.test/x") is False
    assert loader.supports_url("not a url") is False
    assert loader.profile_name() == "yt_dlp"

    info = {
        "_type": "video",
        "id": "1",
        "title": "Video",
        "webpage_url": "https://example.test/watch/1",
        "formats": [
            {
                "format_id": "18",
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "mp4a",
                "height": 360,
                "url": "https://media.example.test/18.mp4",
            }
        ],
    }
    monkeypatch.setattr(loader, "_extract", lambda *args, **kwargs: info)
    ctx = make_context(SourceProfile(name="yt_dlp"))
    source = loader.get_info(info_ref(), ctx=ctx)

    assert source.kind is SourceKind.SINGLE
    assert source.url == "https://example.test/watch/1"
    assert source.items[0].variants
    assert source.items[0].recommendations.video is not None


def info_ref():
    from trackforge.acquire.models import SourceRef

    return SourceRef("yt-dlp", "https://example.test/watch/1")


def test_fake_source_info_shape() -> None:
    info = make_source_info()
    assert info.kind is SourceKind.SINGLE
    assert info.items[0].recommendations.audio == "audio-best"
    assert info.recommendations.audio == "audio-best"
    assert info.thumbnail is not None

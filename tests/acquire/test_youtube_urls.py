"""URL policy tests for the YouTube loader."""

# pyright: standard

from __future__ import annotations

import pytest

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.errors import SearchNotSupported, UnsupportedCollection
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.loaders.youtube import YouTubeLoader
from trackforge.acquire.loaders.ytdlp import YtDlpLoader
from trackforge.acquire.models import SourceRef, UrlHint
from trackforge.acquire.registry import default_registry


@pytest.fixture
def loader() -> YouTubeLoader:
    return YouTubeLoader()


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc123",
        "https://youtube.com/watch?v=abc123",
        "https://m.youtube.com/watch?v=abc123",
        "https://music.youtube.com/watch?v=abc123",
        "https://youtu.be/abc123",
        "https://youtu.be/abc123?t=42",
        "https://www.youtube.com/shorts/abc123",
        "https://www.youtube.com/live/abc123",
        "https://www.youtube.com/embed/abc123",
        "https://www.youtube.com/v/abc123",
        "https://www.youtube.com/clip/abc123",
    ],
)
def test_supports_video_urls(loader: YouTubeLoader, url: str) -> None:
    assert loader.supports_url(url) is True
    assert loader.classify_url(url) is UrlHint.SINGLE


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/playlist?list=PL123",
        "https://youtube.com/playlist?list=PL123",
        "https://music.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/watch?v=abc123&list=PL123",
        "https://youtu.be/abc123?list=PL123",
        "https://www.youtube.com/watch?list=PL123",
    ],
)
def test_supports_collection_urls(loader: YouTubeLoader, url: str) -> None:
    assert loader.supports_url(url) is True
    assert loader.classify_url(url) is UrlHint.COLLECTION


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/",
        "https://www.youtube.com/channel/UC123",
        "https://www.youtube.com/@trackforge",
        "https://www.youtube.com/user/someone",
        "https://www.youtube.com/c/someone",
        "https://www.youtube.com/results?search_query=x",
    ],
)
def test_unknown_youtube_urls(loader: YouTubeLoader, url: str) -> None:
    assert loader.supports_url(url) is True
    assert loader.classify_url(url) is UrlHint.UNKNOWN


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/watch?v=abc123",
        "https://notyoutube.com/watch?v=abc123",
        "https://youtube.com.evil.test/watch?v=abc123",
        "ftp://youtube.com/watch?v=abc123",
        "not a url",
    ],
)
def test_rejects_foreign_urls(loader: YouTubeLoader, url: str) -> None:
    assert loader.supports_url(url) is False
    assert loader.classify_url(url) is UrlHint.UNKNOWN


def test_empty_list_parameter_is_not_a_collection(loader: YouTubeLoader) -> None:
    assert loader.classify_url("https://www.youtube.com/watch?v=abc&list=") is UrlHint.SINGLE


def test_priority_and_profile() -> None:
    loader = YouTubeLoader()
    assert loader.name == "youtube"
    assert loader.config_key == "youtube"
    assert loader.profile_name() == "youtube"
    assert loader.priority < YtDlpLoader.priority


def test_default_registry_contains_youtube_and_generic_fallback() -> None:
    registry = default_registry()
    assert isinstance(registry.by_name("youtube"), YouTubeLoader)
    assert isinstance(registry.by_name("yt-dlp"), YtDlpLoader)


def test_search_is_not_supported_yet(loader: YouTubeLoader) -> None:
    ctx = LoaderContext(
        job_id="job-1",
        profile=SourceProfile(name="youtube"),
    )
    with pytest.raises(SearchNotSupported):
        loader.search("query", limit=5, ctx=ctx)


def test_playlist_inspection_fails_before_network(
    loader: YouTubeLoader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def forbidden_extract(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        calls.append("extract")
        raise AssertionError("playlist extraction must not run in L1.1")

    monkeypatch.setattr(loader, "_extract", forbidden_extract)
    ctx = LoaderContext(
        job_id="job-1",
        profile=SourceProfile(name="youtube"),
    )
    with pytest.raises(UnsupportedCollection, match="collections are not supported"):
        loader.get_info(
            SourceRef(
                "youtube",
                "https://www.youtube.com/playlist?list=PL123",
            ),
            ctx=ctx,
        )
    assert calls == []

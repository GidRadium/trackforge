"""URL policy tests for the Yandex Music loader."""

# pyright: standard

from __future__ import annotations

import pytest

from trackforge.acquire.loaders.yandex_music import YandexMusicLoader
from trackforge.acquire.loaders.ytdlp import YtDlpLoader
from trackforge.acquire.models import UrlHint
from trackforge.acquire.registry import default_registry


@pytest.fixture
def loader() -> YandexMusicLoader:
    return YandexMusicLoader()


@pytest.mark.parametrize(
    "url",
    [
        "https://music.yandex.ru/album/1/track/2",
        "https://music.yandex.com/album/1/track/2?si=abc",
        "https://music.yandex.kz/track/2",
        "https://music.yandex.by/album/1/track/2#comment",
        "https://music.yandex.ua/album/1/track/2",
    ],
)
def test_track_urls_are_single(loader: YandexMusicLoader, url: str) -> None:
    assert loader.supports_url(url) is True
    assert loader.classify_url(url) is UrlHint.SINGLE


@pytest.mark.parametrize(
    "url",
    [
        "https://music.yandex.ru/album/1",
        "https://music.yandex.ru/users/trackforge/playlists/3",
        "https://music.yandex.ru/users/trackforge/playlists/lk.0f8fad5b-d9cb-469f-a165-70867728950e",
        "https://music.yandex.ru/playlists/pl.0f8fad5b-d9cb-469f-a165-70867728950e",
        "https://music.yandex.ru/playlists/ch.0f8fad5b-d9cb-469f-a165-70867728950e",
        "https://music.yandex.ru/artist/123456",
        "https://music.yandex.ru/artist/123456/albums",
        "https://music.yandex.ru/artist/123456/tracks",
    ],
)
def test_collection_urls(loader: YandexMusicLoader, url: str) -> None:
    assert loader.supports_url(url) is True
    assert loader.classify_url(url) is UrlHint.COLLECTION


@pytest.mark.parametrize(
    "url",
    [
        "https://music.yandex.ru/",
        "https://music.yandex.ru/search?text=track",
        "https://music.yandex.ru/users/trackforge",
        "https://music.yandex.ru/album",
        "https://music.yandex.ru.evil.test/album/1/track/2",
        "https://notyandex.ru/album/1/track/2",
        "ftp://music.yandex.ru/album/1/track/2",
        "not a url",
    ],
)
def test_unknown_or_foreign_urls(loader: YandexMusicLoader, url: str) -> None:
    assert loader.classify_url(url) is UrlHint.UNKNOWN


@pytest.mark.parametrize(
    "url",
    [
        "https://music.yandex.ru.evil.test/album/1/track/2",
        "https://notyandex.ru/album/1/track/2",
        "ftp://music.yandex.ru/album/1/track/2",
        "not a url",
    ],
)
def test_rejects_foreign_urls(loader: YandexMusicLoader, url: str) -> None:
    assert loader.supports_url(url) is False


def test_priority_and_profile() -> None:
    loader = YandexMusicLoader()
    assert loader.name == "yandex_music"
    assert loader.config_key == "yandex_music"
    assert loader.profile_name() == "yandex_music"
    assert loader.priority == 20
    assert loader.priority < YtDlpLoader.priority


def test_default_registry_contains_yandex_before_generic() -> None:
    registry = default_registry()
    assert isinstance(registry.by_name("yandex_music"), YandexMusicLoader)
    yandex = registry.by_name("yandex_music")
    generic = registry.by_name("yt-dlp")
    assert yandex.priority < generic.priority

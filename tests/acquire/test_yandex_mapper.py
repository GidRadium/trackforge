"""Yandex Music metadata mapping and normalization tests (offline)."""

# pyright: standard

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.errors import AuthRequired, UnsupportedUrl
from trackforge.acquire.loaders import yandex_music
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.loaders.thumbnail_probe import ImageSize
from trackforge.acquire.loaders.yandex_music import (
    YandexMusicLoader,
    normalize_yandex_titles,
)
from trackforge.acquire.loaders.ytdlp_mapper import YtDlpMapper
from trackforge.acquire.models import MediaKind, SourceRef
from trackforge.metadata.model import PartialDate

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def reference(info: dict[str, Any]) -> SourceRef:
    return SourceRef(
        source="yandex_music",
        url=str(info["webpage_url"]),
        id=str(info["id"]),
    )


def context() -> LoaderContext:
    return LoaderContext(
        job_id="job-yandex",
        profile=SourceProfile(
            name="yandex_music",
            cookies_from_browser=("chrome",),
        ),
    )


def inspect_fixture(
    monkeypatch: pytest.MonkeyPatch,
    info: dict[str, Any],
    *,
    flat: bool = False,
    limit: int | None = None,
):
    loader = YandexMusicLoader()
    monkeypatch.setattr(yandex_music, "plugin_available", lambda: True)
    monkeypatch.setattr(
        yandex_music,
        "fetch_image_size",
        lambda *args, **kwargs: None,
    )

    def fake_extract(
        url: str,
        *,
        ctx: LoaderContext,
        flat: bool = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        del url, ctx, flat, limit
        return info

    monkeypatch.setattr(loader, "_extract", fake_extract)
    return loader.get_info(
        reference(info),
        ctx=context(),
        flat=flat,
        limit=limit,
    )


def test_mapper_preserves_yandex_metadata() -> None:
    info = load_fixture("yandex_track.info.json")
    item = YtDlpMapper(SourceProfile(name="yandex_music")).map(
        info,
        reference(info),
    ).items[0]

    assert item.title == "Midnight Signal"
    assert item.album == "Signals"
    assert item.album_artists == ("TrackForge Artist",)
    assert item.release_date == PartialDate(2024, 1, 1)
    assert item.duration == pytest.approx(215.0)
    # Track/disc/genre are source-specific and stay in extras.
    assert item.extras["track_number"] == 3
    assert item.extras["disc_number"] == 1
    assert item.extras["track_total"] == 10
    assert item.extras["genre"] == ["electronic", "synthpop"]


def test_loader_normalizes_joined_artists_and_stem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = load_fixture("yandex_track.info.json")
    source = inspect_fixture(monkeypatch, info)
    item = source.items[0]

    assert source.kind.value == "single"
    assert item.creator == "TrackForge Artist"
    assert item.artists == ("TrackForge Artist", "Guest Vocalist")
    assert item.album_artists == ("TrackForge Artist",)
    assert item.title == "Midnight Signal"
    assert item.suggested_stem == "TrackForge Artist - Midnight Signal"
    assert item.kind is MediaKind.AUDIO


def test_loader_strips_duplicated_artist_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = load_fixture("yandex_track.info.json")
    info["title"] = "TrackForge Artist - Midnight Signal"
    source = inspect_fixture(monkeypatch, info)
    item = source.items[0]

    assert source.title == "Midnight Signal"
    assert source.creator == "TrackForge Artist"
    assert item.title == "Midnight Signal"
    assert item.creator == "TrackForge Artist"
    assert item.suggested_stem == "TrackForge Artist - Midnight Signal"


def test_feat_stays_inside_title(monkeypatch: pytest.MonkeyPatch) -> None:
    info = load_fixture("yandex_track.info.json")
    info["title"] = "Midnight Signal (feat. Guest Vocalist)"
    item = inspect_fixture(monkeypatch, info).items[0]

    assert item.title == "Midnight Signal (feat. Guest Vocalist)"
    assert item.artists == ("TrackForge Artist", "Guest Vocalist")


def test_cover_is_the_1000x1000_yandex_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = load_fixture("yandex_track.info.json")
    source = inspect_fixture(monkeypatch, info)
    item = source.items[0]

    assert source.thumbnail is not None
    assert (source.thumbnail.width, source.thumbnail.height) == (1000, 1000)
    assert item.thumbnail is not None
    assert item.thumbnail.url.endswith("/1000x1000")
    assert (item.thumbnail.width, item.thumbnail.height) == (1000, 1000)


def test_cover_token_is_expanded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = load_fixture("yandex_track.info.json")
    info["thumbnail"] = "https://avatars.yandex.net/get-music-content/1/2/%%"
    monkeypatch.setattr(
        yandex_music,
        "fetch_image_size",
        lambda *args, **kwargs: None,
    )
    source = YtDlpMapper(SourceProfile(name="yandex_music")).map(
        info,
        reference(info),
    )
    item = YandexMusicLoader().choose_thumbnail(
        source.items[0],
        ctx=LoaderContext(job_id="job", profile=SourceProfile(name="yandex_music")),
    )

    assert item is not None
    assert item.url.endswith("/1000x1000")
    assert (item.width, item.height) == (1000, 1000)


def test_orig_cover_is_preferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = load_fixture("yandex_track.info.json")
    source = YtDlpMapper(SourceProfile(name="yandex_music")).map(
        info,
        reference(info),
    )
    monkeypatch.setattr(
        yandex_music,
        "fetch_image_size",
        lambda *args, **kwargs: ImageSize(3000, 3000, "jpg", "image/jpeg"),
    )

    item = YandexMusicLoader().choose_thumbnail(
        source.items[0],
        ctx=LoaderContext(job_id="job", profile=SourceProfile(name="yandex_music")),
    )

    assert item is not None
    assert item.url.endswith("/orig")
    assert (item.width, item.height) == (3000, 3000)
    assert item.source == "yandex:orig"


def test_orig_cover_is_not_probed_for_flat_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = load_fixture("yandex_album.info.json")
    source = YtDlpMapper(SourceProfile(name="yandex_music")).map(
        info,
        reference(info),
        flat=True,
    )
    calls: list[str] = []

    def fake_probe(url: str, **kwargs: object) -> ImageSize | None:
        del kwargs
        calls.append(url)
        return ImageSize(3000, 3000, "jpg", "image/jpeg")

    monkeypatch.setattr(yandex_music, "fetch_image_size", fake_probe)

    item = YandexMusicLoader().choose_thumbnail(
        source.items[0],
        ctx=LoaderContext(job_id="job", profile=SourceProfile(name="yandex_music")),
    )

    assert item is not None
    assert calls == []


def test_recommends_flac_when_the_server_served_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = load_fixture("yandex_track.info.json")
    item = inspect_fixture(monkeypatch, info).items[0]

    selected = next(
        variant
        for variant in item.variants
        if variant.id == item.recommendations.audio
    )
    audio = next(stream for stream in selected.streams if stream.role.value == "audio")
    assert (audio.codec or "").lower() == "flac"


def test_recommends_best_mp3_when_lossless_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = copy.deepcopy(load_fixture("yandex_track.info.json"))
    info["formats"] = [
        fmt for fmt in info["formats"] if fmt["ext"] != "flac"
    ]
    item = inspect_fixture(monkeypatch, info).items[0]

    selected = next(
        variant
        for variant in item.variants
        if variant.id == item.recommendations.audio
    )
    audio = next(stream for stream in selected.streams if stream.role.value == "audio")
    assert (audio.ext or "").lower() == "mp3"
    assert audio.bitrate_kbps == pytest.approx(320)


def test_no_cookies_fails_before_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = YandexMusicLoader()
    monkeypatch.setattr(yandex_music, "plugin_available", lambda: True)
    calls: list[str] = []

    def forbidden_extract(*args: object, **kwargs: object) -> dict[str, Any]:
        del args, kwargs
        calls.append("extract")
        raise AssertionError("Yandex extraction must not run without cookies")

    monkeypatch.setattr(loader, "_extract", forbidden_extract)
    info = load_fixture("yandex_track.info.json")
    ctx = LoaderContext(
        job_id="job-yandex",
        profile=SourceProfile(name="yandex_music"),
    )

    with pytest.raises(AuthRequired, match="logged-in session cookie"):
        loader.get_info(reference(info), ctx=ctx)
    assert calls == []


def test_normalization_is_pure_and_keeps_unknown_titles() -> None:
    info = load_fixture("yandex_track.info.json")
    source = YtDlpMapper(SourceProfile(name="yandex_music")).map(
        info,
        reference(info),
    )
    info["title"] = "A title without an artist prefix"
    plain = YtDlpMapper(SourceProfile(name="yandex_music")).map(
        info,
        reference(info),
    )
    normalized = normalize_yandex_titles(plain)
    assert normalized.items[0].title == "A title without an artist prefix"
    assert normalized.items[0].creator == "TrackForge Artist"
    assert source.items[0].title == "Midnight Signal"


def _api_track(
    track_id: str,
    title: str,
    *,
    album_id: str = "15569342",
    album_title: str = "KING",
    year: int = 2021,
    duration_ms: int = 134030,
    index: int = 1,
) -> dict[str, Any]:
    return {
        "id": track_id,
        "title": title,
        "durationMs": duration_ms,
        "artists": [{"name": "Sati Akura"}],
        "coverUri": (
            "avatars.yandex.net/get-music-content/2811629/"
            "07949444.p.6386657/%%"
        ),
        "albums": [
            {
                "id": album_id,
                "title": album_title,
                "year": year,
                "trackCount": 1,
                "trackPosition": {"volume": 1, "index": index},
                "genre": "pop",
                "artists": [{"name": "Sati Akura"}],
                "coverUri": (
                    "avatars.yandex.net/get-music-content/2811629/"
                    "07949444.p.6386657/%%"
                ),
            }
        ],
    }


def test_flat_artist_is_enriched_from_yandex_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = {
        "result": {
            "tracks": [
                _api_track("82977109", "KING"),
                _api_track(
                    "76994401",
                    "Kawaki wo Ameku",
                    album_id="13615036",
                    album_title="Kawaki wo Ameku",
                    duration_ms=250480,
                ),
            ],
            "pager": {"total": 2},
        }
    }

    def fake_api(
        self: YandexMusicLoader,
        url: str,
        *,
        ctx: LoaderContext,
    ) -> dict[str, Any] | None:
        del self, ctx
        return api if "/artists/6386657/tracks" in url else None

    monkeypatch.setattr(YandexMusicLoader, "_fetch_api_json", fake_api)
    info = load_fixture("yandex_artist_flat.info.json")
    source = inspect_fixture(monkeypatch, info, flat=True)

    assert source.kind.value == "collection"
    assert source.creator == "Sati Akura"
    assert [item.id for item in source.items] == ["82977109", "76994401"]
    assert [item.title for item in source.items] == [
        "KING",
        "Kawaki wo Ameku",
    ]
    assert all(item.kind is MediaKind.AUDIO for item in source.items)
    assert all(item.resolution.value == "flat" for item in source.items)
    first = source.items[0]
    assert first.creator == "Sati Akura"
    assert first.album == "KING"
    assert first.album_artists == ("Sati Akura",)
    assert first.duration == pytest.approx(134.03)
    assert first.extras["track_number"] == 1
    assert first.extras["disc_number"] == 1
    assert first.extras["genre"] == "pop"
    assert first.thumbnail is not None
    assert (first.thumbnail.width, first.thumbnail.height) == (1000, 1000)
    assert first.thumbnail.url.startswith("https://avatars.yandex.net/")
    assert source.thumbnail is not None


def test_flat_bare_urls_have_stable_fallback_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        YandexMusicLoader,
        "_fetch_api_json",
        lambda self, url, *, ctx: None,
    )
    info = load_fixture("yandex_artist_flat.info.json")
    source = inspect_fixture(monkeypatch, info, flat=True)

    assert [item.id for item in source.items] == ["82977109", "76994401"]
    assert [item.title for item in source.items] == ["82977109", "76994401"]
    assert all(item.id != "item-1" for item in source.items)
    assert all(item.page_url for item in source.items)


def test_flat_tracks_for_album_and_playlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = _api_track("82977109", "KING")
    calls: list[str] = []

    def fake_api(
        self: YandexMusicLoader,
        url: str,
        *,
        ctx: LoaderContext,
    ) -> dict[str, Any] | None:
        del self, ctx
        calls.append(url)
        if "with-tracks" in url:
            return {
                "result": {
                    "volumes": [[track]],
                    "artists": [{"name": "Sati Akura"}],
                }
            }
        if "/users/" in url:
            return {
                "result": {
                    "tracks": [{"track": track}],
                    "owner": {"name": "Playlist Owner", "login": "owner"},
                }
            }
        return None

    monkeypatch.setattr(YandexMusicLoader, "_fetch_api_json", fake_api)
    loader = YandexMusicLoader()
    ctx = context()

    album, album_creator = loader._fetch_flat_tracks(
        ("album", ("15569342",)),
        ctx=ctx,
        limit=10,
    )
    playlist, playlist_creator = loader._fetch_flat_tracks(
        ("playlist", ("yamusic-similar", "5701276")),
        ctx=ctx,
        limit=10,
    )

    assert [entry["id"] for entry in album] == ["82977109"]
    assert [entry["id"] for entry in playlist] == ["82977109"]
    assert album_creator == "Sati Akura"
    assert playlist_creator == "Playlist Owner"
    assert any("with-tracks" in url for url in calls)
    assert any("/users/yamusic-similar/playlists/5701276" in url for url in calls)


def test_playlist_creator_does_not_come_from_first_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracks = [
        _api_track("22345678", "Liked One", album_title="Liked Album"),
        _api_track("22345679", "Liked Two", album_title="Another Album"),
    ]

    def fake_api(
        self: YandexMusicLoader,
        url: str,
        *,
        ctx: LoaderContext,
    ) -> dict[str, Any] | None:
        del self, ctx, url
        return {"result": {"tracks": [{"track": track} for track in tracks]}}

    monkeypatch.setattr(YandexMusicLoader, "_fetch_api_json", fake_api)
    info = load_fixture("yandex_playlist.info.json")
    source = inspect_fixture(monkeypatch, info, flat=True)

    # The tracks are enriched, but a missing owner must stay unknown.
    assert source.creator is None
    assert source.items[0].title == "Liked One"
    assert source.items[0].creator == "Sati Akura"


def test_playlist_creator_uses_api_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    track = _api_track("22345678", "Liked One", album_title="Liked Album")

    def fake_api(
        self: YandexMusicLoader,
        url: str,
        *,
        ctx: LoaderContext,
    ) -> dict[str, Any] | None:
        del self, ctx, url
        return {
            "result": {
                "tracks": [{"track": track}],
                "owner": {"name": "Playlist Owner", "login": "owner"},
            }
        }

    monkeypatch.setattr(YandexMusicLoader, "_fetch_api_json", fake_api)
    info = load_fixture("yandex_playlist.info.json")
    source = inspect_fixture(monkeypatch, info, flat=True)

    assert source.creator == "Playlist Owner"


def test_ar_playlist_falls_back_to_yandex_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = (
        "https://music.yandex.ru/playlists/"
        "ar.bef02a8f-a2bb-4b45-b15f-2b625c8c9559"
    )
    track = _api_track("1387082", "Sparks Fly", album_title="Sparks Fly")
    api = {
        "result": {
            "title": "Лучшее: Sati Akura",
            "description": "Playlist description",
            "owner": {
                "name": "Яндекс Музыка",
                "login": "yamusic-bestsongs",
            },
            "coverUri": (
                "avatars.yandex.net/get-music-content/192707/"
                "3be03de3.a.6146219-1/%%"
            ),
            "tracks": [{"track": track}],
            "trackCount": 1,
        }
    }
    loader = YandexMusicLoader()
    monkeypatch.setattr(yandex_music, "plugin_available", lambda: True)
    monkeypatch.setattr(
        yandex_music,
        "fetch_image_size",
        lambda *args, **kwargs: None,
    )

    def unsupported_extract(
        url: str,
        *,
        ctx: LoaderContext,
        flat: bool | None = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        del ctx, flat, limit
        raise UnsupportedUrl(url)

    def fake_api(
        api_url: str,
        *,
        ctx: LoaderContext,
    ) -> dict[str, Any]:
        del api_url, ctx
        return api

    monkeypatch.setattr(loader, "_extract", unsupported_extract)
    monkeypatch.setattr(loader, "_fetch_api_json", fake_api)

    source = loader.get_info(
        SourceRef("yandex_music", url, "ar.bef02a8f-a2bb-4b45-b15f-2b625c8c9559"),
        ctx=context(),
        flat=True,
    )

    assert source.kind.value == "collection"
    assert source.id == "bef02a8f-a2bb-4b45-b15f-2b625c8c9559"
    assert source.title == "Лучшее: Sati Akura"
    assert source.creator == "Яндекс Музыка"
    assert source.description == "Playlist description"
    assert source.items[0].title == "Sparks Fly"
    assert source.items[0].creator == "Sati Akura"
    assert source.items[0].page_url == (
        "https://music.yandex.ru/album/15569342/track/1387082"
    )
    assert source.items[0].resolution.value == "flat"
    assert source.thumbnail is not None
    assert (source.thumbnail.width, source.thumbnail.height) == (1000, 1000)

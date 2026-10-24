"""Playlist enumeration tests using the fake yt-dlp executable."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.adapters.ytdlp.conftest import read_log

from trackforge.adapters.sources.ytdlp.client import YtDlpClient
from trackforge.adapters.sources.ytdlp.playlist import YtDlpPlaylistSource
from trackforge.application.ports.sources import CollectionResolver
from trackforge.domain.model.source import Url

pytestmark = pytest.mark.unit

PLAYLIST_URL = "https://www.youtube.com/playlist?list=fixture-five"


def test_playlist_source_satisfies_collection_protocol(fake_client: YtDlpClient) -> None:
    assert isinstance(YtDlpPlaylistSource(client=fake_client), CollectionResolver)


@pytest.mark.asyncio
async def test_flat_enumeration_maps_five_items(
    fake_client: YtDlpClient,
    fake_log: Path,
) -> None:
    source = YtDlpPlaylistSource(client=fake_client)

    collection = await source.resolve(
        Url(PLAYLIST_URL),
        {"playlist_start": 1, "playlist_end": 5},
    )

    assert collection.kind.value == "playlist"
    assert collection.title == "Fixture Playlist Five"
    assert collection.owner == "Fixture Playlist Owner"
    assert [item.position for item in collection.items] == [1, 2, 3, 4, 5]
    assert collection.items[0].candidate.external_id == "pl-one"
    assert collection.items[0].candidate.formats == ()
    assert any(claim.field == "cover" for claim in collection.context_claims)
    args = read_log(fake_log)[-1]
    assert "--flat-playlist" in args
    assert "--playlist-start" in args
    assert "--playlist-end" in args
    assert "--no-playlist" not in args
    assert "-o" not in args


def test_playlist_support_is_url_only(fake_client: YtDlpClient) -> None:
    source = YtDlpPlaylistSource(client=fake_client)
    assert source.supports_collection(Url(PLAYLIST_URL)) is True
    assert source.supports_collection(Url("https://www.youtube.com/watch?v=demo")) is False
    assert source.supports(Url(PLAYLIST_URL)) is True
    assert source.supports(Url("https://www.youtube.com/watch?v=demo")) is False

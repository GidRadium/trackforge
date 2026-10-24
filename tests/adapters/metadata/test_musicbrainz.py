"""MusicBrainz provider tests with respx fixtures (never live)."""

from __future__ import annotations

import copy
from collections.abc import Callable

import httpx
import pytest
import respx
from tests.adapters.conftest import fixture

from trackforge.adapters.http.client import HttpClient
from trackforge.adapters.metadata.musicbrainz import (
    MusicBrainzProvider,
    build_search_query,
    escape_lucene,
)
from trackforge.application.dto.providers import MetadataProvider, MetadataQuery
from trackforge.domain.errors import ProviderUnavailable

BASE = "https://musicbrainz.org/ws/2"


def _provider(
    client: HttpClient, contact: str = "trackforge-tests@example.com"
) -> MusicBrainzProvider:
    return MusicBrainzProvider(client, contact=contact)


@pytest.mark.asyncio
async def test_provider_satisfies_metadata_port(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    provider = _provider(fake_http_client())
    assert isinstance(provider, MetadataProvider)
    await provider.aclose()


@pytest.mark.asyncio
async def test_search_normalizes_recordings_releases_and_artists(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    provider = _provider(fake_http_client())
    with respx.mock:
        route = respx.get(f"{BASE}/recording").mock(
            return_value=httpx.Response(200, json=fixture("musicbrainz_search"))
        )
        observations = await provider.search(
            MetadataQuery(title="Fixture Song", artist="Fixture Artist", duration=0.75)
        )

    assert len(observations) == 1
    observation = observations[0]
    assert observation.type == "musicbrainz.search.v1"
    assert observation.source == "musicbrainz"
    assert observation.expires_at is not None
    payload = observation.model()
    assert payload.query.title == "Fixture Song"
    recording = payload.recordings[0]
    assert recording.mbid == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert recording.title == "Fixture Song"
    assert recording.duration == pytest.approx(0.75)
    assert recording.artists[0].name == "MusicBrainz Artist"
    assert recording.artists[0].mbid == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    assert recording.isrcs == ["USRC17607839"]
    assert recording.score == pytest.approx(1.0)
    assert payload.releases[0].title == "MusicBrainz Album"
    assert payload.releases[0].date == "2024-05-03"
    assert payload.releases[0].label == "Fixture Label"
    assert payload.artists[0].mbid == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    request = route.calls[0].request
    assert "trackforge-tests@example.com" in request.headers["user-agent"]
    assert "recording" in request.url.params["query"]
    await provider.aclose()


@pytest.mark.asyncio
async def test_search_drops_invalid_mbids_and_isrcs(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    document = copy.deepcopy(fixture("musicbrainz_search"))
    recording = document["recordings"][0]
    recording["artist-credit"][0]["artist"]["id"] = "not-an-mbid"
    recording["isrcs"] = ["not-an-isrc"]
    recording["releases"][0]["id"] = "also-not-an-mbid"
    provider = _provider(fake_http_client())
    with respx.mock:
        respx.get(f"{BASE}/recording").mock(return_value=httpx.Response(200, json=document))
        observation = (await provider.search(MetadataQuery(title="Fixture Song")))[0]

    payload = observation.model()
    assert payload.recordings[0].artists[0].mbid is None
    assert payload.recordings[0].isrcs == []
    assert "invalid_isrc" in observation.warnings
    assert "invalid_artist_mbid" in observation.warnings
    assert "invalid_release_mbid" in observation.warnings
    await provider.aclose()


@pytest.mark.asyncio
async def test_lookup_recording(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    provider = _provider(fake_http_client())
    mbid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    with respx.mock:
        route = respx.get(f"{BASE}/recording/{mbid}").mock(
            return_value=httpx.Response(200, json=fixture("musicbrainz_lookup"))
        )
        observation = await provider.lookup(mbid)

    payload = observation.model()
    assert payload.query.recording_mbid == mbid
    assert payload.recordings[0].title == "Fixture Song"
    assert "inc" in route.calls[0].request.url.params
    await provider.aclose()


@pytest.mark.asyncio
async def test_missing_user_agent_contact_never_issues_a_request(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    provider = _provider(fake_http_client(), contact="")

    async def forbidden(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        pytest.fail(f"unexpected request: {request.url}")

    with respx.mock:
        respx.route(host="musicbrainz.org").mock(side_effect=forbidden)
        with pytest.raises(ProviderUnavailable, match="User-Agent"):
            await provider.search(MetadataQuery(title="Fixture Song"))
    await provider.aclose()


@pytest.mark.asyncio
async def test_invalid_json_is_a_typed_failure(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    provider = _provider(fake_http_client(max_retries=0))
    with respx.mock:
        respx.get(f"{BASE}/recording").mock(return_value=httpx.Response(200, content=b"not json"))
        with pytest.raises(ProviderUnavailable, match="invalid JSON"):
            await provider.search(MetadataQuery(title="Fixture Song"))
    await provider.aclose()


def test_build_search_query_escapes_lucene_specials() -> None:
    query = build_search_query(MetadataQuery(title='Song: "Live"', artist="A/B", duration=12.5))
    assert 'recording:"Song\\: \\"Live\\""' in query
    assert 'artist:"A\\/B"' in query
    assert escape_lucene("a+b") == "a\\+b"

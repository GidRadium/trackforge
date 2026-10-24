"""LRCLIB provider tests: plain/LRC parsing, timings, and no-match handling."""

from __future__ import annotations

import copy
from collections.abc import Callable

import httpx
import pytest
import respx
from tests.adapters.conftest import fixture

from trackforge.adapters.http.client import HttpClient
from trackforge.adapters.metadata.lrclib import LrclibProvider, parse_lrc
from trackforge.application.dto.providers import MetadataProvider, MetadataQuery

GET_URL = "https://lrclib.net/api/get"


@pytest.mark.asyncio
async def test_provider_satisfies_metadata_port(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    provider = LrclibProvider(fake_http_client())
    assert isinstance(provider, MetadataProvider)
    await provider.aclose()


@pytest.mark.asyncio
async def test_get_returns_plain_and_valid_lrc(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    provider = LrclibProvider(fake_http_client())
    with respx.mock:
        route = respx.get(GET_URL).mock(
            return_value=httpx.Response(200, json=fixture("lrclib_get"))
        )
        observations = await provider.search(
            MetadataQuery(
                title="Fixture Song",
                artist="Fixture Artist",
                album="Fixture Album",
                duration=0.75,
            )
        )

    candidate = observations[0].model().candidates[0]
    assert candidate.source == "lrclib"
    assert candidate.plain is not None and "First line" in candidate.plain
    assert candidate.lrc is not None
    assert candidate.has_timing is True
    assert candidate.score > 0.8
    assert candidate.duration == pytest.approx(0.75)
    request = route.calls[0].request
    assert request.url.params["track_name"] == "Fixture Song"
    assert request.url.params["artist_name"] == "Fixture Artist"
    await provider.aclose()


@pytest.mark.asyncio
async def test_broken_timings_are_kept_with_warning(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    document = copy.deepcopy(fixture("lrclib_get"))
    document["syncedLyrics"] = "[00:05.00]Late\n[00:01.00]Early\nnot-a-timestamp"
    provider = LrclibProvider(fake_http_client())
    with respx.mock:
        respx.get(GET_URL).mock(return_value=httpx.Response(200, json=document))
        observations = await provider.search(
            MetadataQuery(title="Fixture Song", artist="Fixture Artist", duration=0.75)
        )

    candidate = observations[0].model().candidates[0]
    assert candidate.lrc is not None
    assert candidate.has_timing is False
    assert "lrc_timings_not_increasing" in observations[0].warnings
    assert "lrc_invalid_timestamp" in observations[0].warnings
    await provider.aclose()


@pytest.mark.asyncio
async def test_no_match_returns_empty_candidates(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    provider = LrclibProvider(fake_http_client(max_retries=0))
    with respx.mock:
        respx.get(GET_URL).mock(return_value=httpx.Response(404))
        observations = await provider.search(MetadataQuery(title="Missing", artist="Nobody"))
    assert observations[0].payload["candidates"] == []
    assert "no_match" in observations[0].warnings
    await provider.aclose()


@pytest.mark.asyncio
async def test_instrumental_track_is_not_a_lyrics_candidate(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    document = copy.deepcopy(fixture("lrclib_get"))
    document["instrumental"] = True
    document["plainLyrics"] = None
    document["syncedLyrics"] = None
    provider = LrclibProvider(fake_http_client())
    with respx.mock:
        respx.get(GET_URL).mock(return_value=httpx.Response(200, json=document))
        observations = await provider.search(
            MetadataQuery(title="Fixture Song", artist="Fixture Artist")
        )
    assert observations[0].payload["candidates"] == []
    assert "instrumental" in observations[0].warnings
    await provider.aclose()


@pytest.mark.asyncio
async def test_search_without_title_or_artist_skips_network(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    provider = LrclibProvider(fake_http_client())
    with respx.mock:
        observations = await provider.search(MetadataQuery(title="Only a title"))
    assert observations[0].payload["candidates"] == []
    assert "no_query" in observations[0].warnings
    await provider.aclose()


def test_parse_lrc_supports_multiple_timestamps_and_hours() -> None:
    lines, warnings = parse_lrc("[00:01.00][01:02.50]Repeated\n[01:00:00.00]One hour\n[offset:500]")
    assert warnings == ()
    assert lines[0] == (1.0, "Repeated")
    assert lines[1] == (62.5, "Repeated")
    assert lines[2] == (3600.0, "One hour")


def test_parse_lrc_rejects_impossible_seconds() -> None:
    lines, warnings = parse_lrc("[00:99.00]Impossible")
    assert lines == ()
    assert "lrc_invalid_timestamp" in warnings

"""AcoustID provider tests: key handling, normalization, and errors."""

from __future__ import annotations

import copy
from collections.abc import Callable

import httpx
import pytest
import respx
from tests.adapters.conftest import fixture

from trackforge.adapters.http.client import HttpClient
from trackforge.adapters.metadata.acoustid import AcoustIdProvider
from trackforge.application.dto.providers import MetadataProvider, MetadataQuery
from trackforge.domain.errors import ProviderUnavailable

LOOKUP = "https://api.acoustid.org/v2/lookup"


@pytest.mark.asyncio
async def test_provider_satisfies_metadata_port(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    provider = AcoustIdProvider(fake_http_client(), api_key="test-key")
    assert isinstance(provider, MetadataProvider)
    await provider.aclose()


@pytest.mark.asyncio
async def test_lookup_parses_candidates_and_sorted_scores(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    provider = AcoustIdProvider(fake_http_client(), api_key="test-key")
    with respx.mock:
        route = respx.get(LOOKUP).mock(
            return_value=httpx.Response(200, json=fixture("acoustid_lookup"))
        )
        observations = await provider.search(MetadataQuery(fingerprint="AQADtEmi", duration=0.75))

    assert len(observations) == 1
    payload = observations[0].model()
    assert [candidate.score for candidate in payload.candidates] == [0.97, 0.41]
    assert payload.candidates[0].recording_mbids == ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"]
    assert payload.candidates[0].title == "Fixture Song"
    assert payload.candidates[0].artist == "MusicBrainz Artist"
    request = route.calls[0].request
    assert request.url.params["client"] == "test-key"
    assert request.url.params["duration"] == "1"
    assert "test-key" not in observations[0].payload
    await provider.aclose()


@pytest.mark.asyncio
async def test_missing_key_skips_without_network(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    provider = AcoustIdProvider(fake_http_client(), api_key="")

    async def forbidden(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        pytest.fail(f"unexpected request: {request.url}")

    with respx.mock:
        respx.route(host="api.acoustid.org").mock(side_effect=forbidden)
        with pytest.raises(ProviderUnavailable, match="API key"):
            await provider.search(MetadataQuery(fingerprint="AQADtEmi", duration=1.0))
    await provider.aclose()


@pytest.mark.asyncio
async def test_invalid_acoustid_and_recording_ids_are_dropped(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    document = copy.deepcopy(fixture("acoustid_lookup"))
    document["results"][0]["id"] = "!!!"
    document["results"][1]["recordings"][0]["id"] = "not-a-mbid"
    provider = AcoustIdProvider(fake_http_client(), api_key="test-key")
    with respx.mock:
        respx.get(LOOKUP).mock(return_value=httpx.Response(200, json=document))
        observations = await provider.search(MetadataQuery(fingerprint="AQADtEmi", duration=0.75))

    payload = observations[0].model()
    assert len(payload.candidates) == 1
    assert payload.candidates[0].recording_mbids == []
    assert "invalid_acoustid_id" in observations[0].warnings
    assert "invalid_recording_mbid" in observations[0].warnings
    await provider.aclose()


@pytest.mark.asyncio
async def test_error_status_is_reported(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    provider = AcoustIdProvider(fake_http_client(max_retries=0), api_key="test-key")
    with respx.mock:
        respx.get(LOOKUP).mock(
            return_value=httpx.Response(200, json={"status": "error", "error": {"message": "bad"}})
        )
        with pytest.raises(ProviderUnavailable, match="status error"):
            await provider.search(MetadataQuery(fingerprint="AQADtEmi", duration=1.0))
    await provider.aclose()


@pytest.mark.asyncio
async def test_404_is_a_provider_error(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    provider = AcoustIdProvider(fake_http_client(max_retries=0), api_key="test-key")
    with respx.mock:
        respx.get(LOOKUP).mock(return_value=httpx.Response(404))
        with pytest.raises(ProviderUnavailable):
            await provider.search(MetadataQuery(fingerprint="AQADtEmi", duration=1.0))
    await provider.aclose()

"""Cover-art provider tests: CAA, fallbacks, dimensions, and size limits."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import respx
from tests.adapters.conftest import fixture
from tests.fixtures.media import generate_png

from trackforge.adapters.http.client import HttpClient
from trackforge.adapters.metadata.coverart import (
    CoverArtArchiveProvider,
    CoverArtProvider,
    DeezerProvider,
    ItunesProvider,
    image_dimensions,
)
from trackforge.application.dto.providers import MetadataProvider, MetadataQuery

RELEASE_MBID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
CAA_URL = f"https://coverartarchive.org/release/{RELEASE_MBID}"


class FakeCas:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put_bytes(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        self.blobs[digest] = data
        return digest


@pytest.mark.asyncio
async def test_caa_prefers_front_cover(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    provider = CoverArtArchiveProvider(fake_http_client())
    with respx.mock:
        respx.get(CAA_URL).mock(return_value=httpx.Response(200, json=fixture("caa_release")))
        observation = await provider.lookup(RELEASE_MBID)

    payload = observation.model()
    assert len(payload.candidates) == 1
    assert payload.candidates[0].source == "cover_art_archive"
    assert payload.candidates[0].release_mbid == RELEASE_MBID
    assert payload.candidates[0].url.endswith("front-1200.jpg")
    assert payload.candidates[0].score >= 0.9
    await provider.aclose()


@pytest.mark.asyncio
async def test_caa_404_returns_no_candidates(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    provider = CoverArtArchiveProvider(fake_http_client(max_retries=0))
    with respx.mock:
        respx.get(CAA_URL).mock(return_value=httpx.Response(404))
        observation = await provider.lookup(RELEASE_MBID)

    assert observation.payload["candidates"] == []
    assert "caa_not_found" in observation.warnings
    await provider.aclose()


@pytest.mark.asyncio
async def test_caa_fetches_bytes_into_cas_and_reads_dimensions(
    fake_http_client: Callable[..., HttpClient],
    tmp_path: Path,
) -> None:
    png = generate_png(tmp_path, width=1200, height=1200)
    data = png.read_bytes()
    cas = FakeCas()
    provider = CoverArtArchiveProvider(
        fake_http_client(),
        cas=cas,  # type: ignore[arg-type]
        fetch_images=True,
    )
    image_url = f"https://coverartarchive.org/release/{RELEASE_MBID}/front-1200.jpg"
    with respx.mock:
        respx.get(CAA_URL).mock(return_value=httpx.Response(200, json=fixture("caa_release")))
        respx.get(image_url).mock(
            return_value=httpx.Response(200, content=data, headers={"Content-Type": "image/png"})
        )
        observation = await provider.lookup(RELEASE_MBID)

    candidate = observation.model().candidates[0]
    assert candidate.width == 1200
    assert candidate.height == 1200
    assert candidate.size_bytes == len(data)
    assert candidate.mime == "image/png"
    assert candidate.artifact_ref in cas.blobs
    await provider.aclose()


@pytest.mark.asyncio
async def test_itunes_upscales_artwork(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    provider = ItunesProvider(fake_http_client())
    with respx.mock:
        respx.get("https://itunes.apple.com/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "resultCount": 1,
                    "results": [
                        {
                            "trackName": "Fixture Song",
                            "artistName": "Fixture Artist",
                            "artworkUrl100": (
                                "https://is1.mzstatic.com/image/thumb/x/100x100bb.jpg"
                            ),
                        }
                    ],
                },
            )
        )
        observations = await provider.search(
            MetadataQuery(title="Fixture Song", artist="Fixture Artist")
        )

    candidate = observations[0].model().candidates[0]
    assert candidate.source == "itunes"
    assert candidate.url is not None
    assert "3000x3000bb" in candidate.url
    assert candidate.width == 3000
    await provider.aclose()


@pytest.mark.asyncio
async def test_deezer_uses_cover_xl(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    provider = DeezerProvider(fake_http_client())
    with respx.mock:
        respx.get("https://api.deezer.com/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "title": "Fixture Song",
                            "artist": {"name": "Fixture Artist"},
                            "album": {
                                "title": "Fixture Album",
                                "cover_xl": "https://cdn.deezer.com/cover/1000x1000.jpg",
                            },
                        }
                    ]
                },
            )
        )
        observations = await provider.search(
            MetadataQuery(title="Fixture Song", artist="Fixture Artist")
        )

    candidate = observations[0].model().candidates[0]
    assert candidate.source == "deezer"
    assert candidate.width == 1000
    await provider.aclose()


@pytest.mark.asyncio
async def test_combined_provider_falls_back_to_itunes(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    client = fake_http_client()
    provider = CoverArtProvider(
        CoverArtArchiveProvider(client),
        ItunesProvider(client),
        DeezerProvider(client),
    )
    with respx.mock:
        respx.get(CAA_URL).mock(return_value=httpx.Response(404))
        respx.get("https://itunes.apple.com/search").mock(
            return_value=httpx.Response(
                200,
                json={"results": [{"artworkUrl100": "https://example.com/100x100bb.jpg"}]},
            )
        )
        observations = await provider.search(
            MetadataQuery(
                title="Fixture Song",
                artist="Fixture Artist",
                release_mbid=RELEASE_MBID,
            )
        )
    assert observations[0].payload["candidates"][0]["source"] == "itunes"
    await provider.aclose()


def test_image_dimensions_for_png_gif_and_jpeg(tmp_path: Path) -> None:
    png = generate_png(tmp_path, width=321, height=123).read_bytes()
    assert image_dimensions(png) == (321, 123)
    gif = b"GIF89a" + (55).to_bytes(2, "little") + (66).to_bytes(2, "little") + b"\x00" * 8
    assert image_dimensions(gif) == (55, 66)
    jpeg = (
        b"\xff\xd8"
        + b"\xff\xc0"
        + (17).to_bytes(2, "big")
        + b"\x08"
        + (456).to_bytes(2, "big")
        + (789).to_bytes(2, "big")
        + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
        + b"\xff\xd9"
    )
    assert image_dimensions(jpeg) == (789, 456)
    assert image_dimensions(b"not an image") is None


def test_providers_satisfy_metadata_port(
    fake_http_client: Callable[..., HttpClient],
) -> None:
    client = fake_http_client()
    assert isinstance(CoverArtArchiveProvider(client), MetadataProvider)
    assert isinstance(ItunesProvider(client), MetadataProvider)
    assert isinstance(DeezerProvider(client), MetadataProvider)

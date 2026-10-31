"""Source-thumbnail cover observer tests (no network)."""

from __future__ import annotations

from typing import Any

import pytest

from trackforge.adapters.metadata.source_cover import SourceCoverObserver
from trackforge.application.dto.observations import (
    SourceMetadataPayload,
    SourceThumbnail,
)
from trackforge.application.ports.execution import (
    BudgetSnapshot,
    StepContext,
    StepInput,
    StepResultStatus,
)
from trackforge.application.ports.observations import (
    COVER_ARTIFACT,
    SOURCE_METADATA_ARTIFACT,
)
from trackforge.application.ports.storage import ContentHash
from trackforge.application.services.canonical_json import canonical_json, sha256_hex
from trackforge.domain.errors import ProviderUnavailable
from trackforge.domain.model.ids import ItemId, RunId, StepId, new_uuid7

pytestmark = pytest.mark.asyncio


class _FakeCas:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    async def put_bytes(self, data: bytes) -> ContentHash:
        self.payloads.append(data)
        return ContentHash("a" * 64)


class _FakeClient:
    def __init__(self, responses: dict[str, bytes | Exception]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def get_bytes(self, url: str, max_bytes: int | None = None) -> bytes:
        del max_bytes
        self.calls.append(url)
        value = self.responses.get(url)
        if value is None:
            raise ProviderUnavailable("not found")
        if isinstance(value, Exception):
            raise value
        return value


def _png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + b"\x00" * 4
    )


def _webp(width: int, height: int) -> bytes:
    return (
        b"RIFF"
        + (22).to_bytes(4, "little")
        + b"WEBP"
        + b"VP8X"
        + (10).to_bytes(4, "little")
        + b"\x00\x00\x00\x00"
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )


def _source(**overrides: Any) -> SourceMetadataPayload:
    data: dict[str, Any] = {
        "external_id": "k9MNcoWj1fo",
        "extractor": "youtube",
        "title": "Nightcore | Queen of Shadow",
        "webpage_url": "https://www.youtube.com/watch?v=k9MNcoWj1fo",
        "thumbnails": [
            SourceThumbnail(
                url="https://i.ytimg.com/vi/k9MNcoWj1fo/maxresdefault.jpg",
                width=1920,
                height=1080,
            )
        ],
    }
    data.update(overrides)
    return SourceMetadataPayload(**data)


def _context(source: SourceMetadataPayload) -> StepContext:
    payload = source.model_dump_json().encode("utf-8")
    return StepContext(
        run_id=RunId(new_uuid7()),
        item_id=ItemId(new_uuid7()),
        step_id=StepId(new_uuid7()),
        inputs={
            SOURCE_METADATA_ARTIFACT: StepInput(
                type=SOURCE_METADATA_ARTIFACT,
                artifact_id="source-1",
                schema_version=1,
                cache_key="cache",
                payload_hash=sha256_hex(canonical_json(source.model_dump(mode="json"))),
                payload=payload,
            )
        },
        budget=BudgetSnapshot(network_calls=10),
        config_subset={"offline": False},
    )


async def test_youtube_maxresdefault_is_fetched_and_stored() -> None:
    cas = _FakeCas()
    image = _png(1920, 1080)
    client = _FakeClient({"https://img.youtube.com/vi/k9MNcoWj1fo/maxresdefault.jpg": image})
    observer = SourceCoverObserver(client=client, cas=cas)  # type: ignore[arg-type]

    result = await observer.run(_context(_source()))

    assert result.status is StepResultStatus.SUCCEEDED
    draft = result.artifacts[0]
    assert draft.type == COVER_ARTIFACT
    candidate = draft.payload["candidates"][0]
    assert candidate["source"] == "source_thumbnail"
    assert candidate["artifact_ref"] == "a" * 64
    assert candidate["width"] == 1920
    assert candidate["height"] == 1080
    assert cas.payloads == [image]
    assert client.calls[0].endswith("/maxresdefault.jpg")


async def test_falls_back_to_next_thumbnail_when_maxres_is_missing() -> None:
    cas = _FakeCas()
    image = _png(640, 480)
    client = _FakeClient(
        {
            "https://img.youtube.com/vi/k9MNcoWj1fo/maxresdefault.jpg": ProviderUnavailable(
                "missing"
            ),
            "https://img.youtube.com/vi/k9MNcoWj1fo/hq720.jpg": ProviderUnavailable("missing"),
            "https://img.youtube.com/vi/k9MNcoWj1fo/maxresdefault.webp": ProviderUnavailable(
                "missing"
            ),
            "https://img.youtube.com/vi/k9MNcoWj1fo/sddefault.jpg": image,
        }
    )
    observer = SourceCoverObserver(client=client, cas=cas)  # type: ignore[arg-type]

    result = await observer.run(_context(_source()))

    assert result.status is StepResultStatus.SUCCEEDED
    assert result.artifacts[0].payload["candidates"][0]["width"] == 640
    assert any(warning.code == "source_cover_fetch_failed" for warning in result.warnings)


async def test_small_maxres_placeholder_falls_back_to_hq720() -> None:
    cas = _FakeCas()
    placeholder = _png(120, 90)
    high_quality = _png(1280, 720)
    client = _FakeClient(
        {
            "https://img.youtube.com/vi/k9MNcoWj1fo/maxresdefault.jpg": placeholder,
            "https://img.youtube.com/vi/k9MNcoWj1fo/hq720.jpg": high_quality,
        }
    )
    observer = SourceCoverObserver(client=client, cas=cas)  # type: ignore[arg-type]

    result = await observer.run(_context(_source()))

    assert result.status is StepResultStatus.SUCCEEDED
    candidate = result.artifacts[0].payload["candidates"][0]
    assert candidate["width"] == 1280
    assert candidate["height"] == 720
    assert candidate["url"].endswith("/hq720.jpg")
    assert any(warning.code == "source_cover_too_small" for warning in result.warnings)
    assert cas.payloads == [high_quality]


async def test_better_lower_priority_candidate_replaces_smaller_one() -> None:
    cas = _FakeCas()
    medium = _png(640, 360)
    high_quality = _png(1280, 720)
    client = _FakeClient(
        {
            "https://img.youtube.com/vi/k9MNcoWj1fo/maxresdefault.jpg": medium,
            "https://img.youtube.com/vi/k9MNcoWj1fo/hq720.jpg": high_quality,
        }
    )
    observer = SourceCoverObserver(client=client, cas=cas)  # type: ignore[arg-type]

    result = await observer.run(_context(_source()))

    candidate = result.artifacts[0].payload["candidates"][0]
    assert (candidate["width"], candidate["height"]) == (1280, 720)
    assert candidate["url"].endswith("/hq720.jpg")
    assert cas.payloads == [high_quality]


async def test_webp_cover_is_converted_to_png() -> None:
    cas = _FakeCas()
    converted = _png(1280, 720)
    client = _FakeClient(
        {
            "https://img.youtube.com/vi/k9MNcoWj1fo/maxresdefault.jpg": ProviderUnavailable(
                "missing"
            ),
            "https://img.youtube.com/vi/k9MNcoWj1fo/hq720.jpg": ProviderUnavailable("missing"),
            "https://img.youtube.com/vi/k9MNcoWj1fo/maxresdefault.webp": _webp(1280, 720),
        }
    )
    observer = SourceCoverObserver(
        client=client,  # type: ignore[arg-type]
        cas=cas,
        webp_converter=lambda data: converted,
    )

    result = await observer.run(_context(_source()))

    assert result.status is StepResultStatus.SUCCEEDED
    candidate = result.artifacts[0].payload["candidates"][0]
    assert candidate["mime"] == "image/png"
    assert candidate["url"].endswith("/maxresdefault.webp")
    assert cas.payloads == [converted]


async def test_yandex_thumbnail_is_used_without_youtube_urls() -> None:
    cas = _FakeCas()
    image = _png(1000, 1000)
    client = _FakeClient({"https://avatars.yandex.net/get-music-content/cover": image})
    source = _source(
        external_id="101539985",
        extractor="yandexmusic",
        webpage_url="https://music.yandex.ru/album/1/track/2",
        thumbnails=[
            SourceThumbnail(
                url="https://avatars.yandex.net/get-music-content/cover",
                width=1000,
                height=1000,
            )
        ],
    )
    observer = SourceCoverObserver(client=client, cas=cas)  # type: ignore[arg-type]

    result = await observer.run(_context(source))

    assert result.status is StepResultStatus.SUCCEEDED
    assert result.artifacts[0].payload["candidates"][0]["artifact_ref"] == "a" * 64
    assert all("ytimg" not in call for call in client.calls)


async def test_missing_thumbnails_are_degraded() -> None:
    observer = SourceCoverObserver(client=_FakeClient({}), cas=_FakeCas())  # type: ignore[arg-type]
    source = _source(extractor="unknown", webpage_url=None, thumbnails=[])

    result = await observer.run(_context(source))

    assert result.status is StepResultStatus.SKIPPED
    assert result.warnings[0].code == "source_cover_missing"


async def test_offline_does_not_fetch() -> None:
    client = _FakeClient({})
    observer = SourceCoverObserver(client=client, cas=_FakeCas())  # type: ignore[arg-type]
    ctx = _context(_source())
    object.__setattr__(ctx, "config_subset", {"offline": True})

    result = await observer.run(ctx)

    assert result.status is StepResultStatus.SKIPPED
    assert result.warnings[0].code == "offline"
    assert client.calls == []

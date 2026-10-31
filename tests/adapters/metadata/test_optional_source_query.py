"""Provider observers use normalized source metadata as an optional input."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest

from trackforge.adapters.metadata.observers import LrclibObserver, MusicBrainzObserver
from trackforge.application.dto.observations import (
    InitialTagsPayload,
    SourceMetadataPayload,
)
from trackforge.application.dto.providers import MetadataQuery, ProviderObservation
from trackforge.application.ports.execution import StepContext, StepInput
from trackforge.application.ports.observations import (
    INITIAL_TAGS_ARTIFACT,
    SOURCE_METADATA_ARTIFACT,
)
from trackforge.domain.model.ids import ItemId, RunId, StepId, new_uuid7

pytestmark = pytest.mark.unit

NOISY = "{642} Nightcore (End Of The Dream) - Collide (with lyrics)"


class RecordingProvider:
    source = "fixture"

    def __init__(self) -> None:
        self.queries: list[MetadataQuery] = []

    async def search(self, query: MetadataQuery) -> Sequence[ProviderObservation]:
        self.queries.append(MetadataQuery.coerce(query))
        return ()

    async def aclose(self) -> None:
        return None


def _input(artifact_type: Any, payload: dict[str, Any]) -> StepInput:
    import json

    data = json.dumps(payload).encode("utf-8")
    return StepInput(
        type=artifact_type,
        artifact_id=new_uuid7(),
        schema_version=artifact_type.schema_version,
        cache_key="0" * 64,
        payload_hash="a" * 64,
        payload=data,
    )


def _ctx(*inputs: StepInput) -> StepContext:
    return StepContext(
        run_id=RunId(new_uuid7()),
        item_id=ItemId(new_uuid7()),
        step_id=StepId(new_uuid7()),
        inputs={step.type: step for step in inputs},
        cancelled=asyncio.Event(),
    )


def _tags(title: str | None = None, artist: tuple[str, ...] = ()) -> StepInput:
    payload = InitialTagsPayload.model_validate(
        {
            "tags": {"title": title, "artist": list(artist)},
            "raw": {"format": "MP4", "mime_types": [], "tag_keys": []},
            "reader": {"id": "mutagen", "version": "1"},
        }
    )
    return _input(INITIAL_TAGS_ARTIFACT, payload.model_dump(mode="json"))


def _source(title: str, uploader: str | None = None) -> StepInput:
    payload = SourceMetadataPayload(external_id="fixture", title=title, uploader=uploader)
    return _input(SOURCE_METADATA_ARTIFACT, payload.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_musicbrainz_query_uses_original_artist_not_remixer() -> None:
    provider = RecordingProvider()
    observer = MusicBrainzObserver(provider)  # type: ignore[arg-type]
    result = await observer.run(_ctx(_tags(), _source(NOISY, "Rock Goes Nightcore")))

    assert provider.queries
    query = provider.queries[0]
    assert query.title == "Collide"
    assert query.artist == "End Of The Dream"
    assert result.warnings


@pytest.mark.asyncio
async def test_lrclib_title_without_artist_emits_specific_warning() -> None:
    provider = RecordingProvider()
    observer = LrclibObserver(provider)  # type: ignore[arg-type]
    result = await observer.run(_ctx(_tags(), _source("Plain Song")))

    assert not provider.queries
    assert [warning.code for warning in result.warnings] == ["lyrics_artist_unknown"]

"""LLM-assisted source identity extraction tests."""

from __future__ import annotations

from typing import Any

import pytest

from trackforge.adapters.metadata.observers import _query_values
from trackforge.adapters.metadata.source_identity import SourceIdentityObserver
from trackforge.application.dto.observations import (
    SourceIdentityPayload,
    SourceMetadataPayload,
)
from trackforge.application.ports.execution import (
    ArtifactDraft,
    BudgetSnapshot,
    StepContext,
    StepInput,
)
from trackforge.application.ports.llm import LLMClaim, LLMProvider, LLMRequest, LLMResponse
from trackforge.application.ports.observations import (
    SOURCE_IDENTITY_ARTIFACT,
    SOURCE_METADATA_ARTIFACT,
)
from trackforge.application.services.canonical_json import canonical_json, sha256_hex
from trackforge.domain.model.ids import ItemId, RunId, StepId, new_uuid7


class _FakeProvider:
    def __init__(self, claims: tuple[LLMClaim, ...]) -> None:
        self.claims = claims
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            claims=self.claims,
            model="fake-model",
            prompt_version="source_extract/v1",
            cost_usd=0.001,
        )


def _metadata() -> SourceMetadataPayload:
    return SourceMetadataPayload(
        external_id="video-1",
        title="{71.3} Nightcore (Breakaway) - Invincible (with lyrics)",
        uploader="Rock Goes Nightcore",
        description="Original artist: Breakaway\nOriginal song: Invincible",
        duration_seconds=180.0,
    )


def _input(artifact_type: Any, payload: bytes) -> StepInput:
    return StepInput(
        type=artifact_type,
        artifact_id=str(new_uuid7()),
        schema_version=1,
        cache_key=sha256_hex(payload),
        payload_hash=sha256_hex(payload),
        payload=payload,
    )


def _context(*extra: StepInput) -> StepContext:
    metadata = _metadata()
    payload = canonical_json(metadata.model_dump(mode="json"))
    inputs = {SOURCE_METADATA_ARTIFACT: _input(SOURCE_METADATA_ARTIFACT, payload)}
    for item in extra:
        inputs[item.type] = item
    return StepContext(
        run_id=RunId(new_uuid7()),
        item_id=ItemId(new_uuid7()),
        step_id=StepId(new_uuid7()),
        inputs=inputs,
        budget=BudgetSnapshot(network_calls=2),
    )


@pytest.mark.asyncio
async def test_llm_extracts_original_artist_from_description() -> None:
    provider = _FakeProvider(
        (
            LLMClaim(
                field="originalartist",
                value="Breakaway",
                confidence=0.93,
                quote="Original artist: Breakaway",
                reason="explicit original artist line",
            ),
            LLMClaim(
                field="title",
                value="Invincible",
                confidence=0.9,
                quote="Original song: Invincible",
                reason="explicit original song line",
            ),
            LLMClaim(
                field="qualifier",
                value="Nightcore",
                confidence=0.86,
                quote="Nightcore (Breakaway)",
                reason="derivative channel marker",
            ),
        )
    )
    observer = SourceIdentityObserver(llm_provider=provider)

    result = await observer.run(_context())

    assert len(result.artifacts) == 1
    draft: ArtifactDraft = result.artifacts[0]
    assert draft.type == SOURCE_IDENTITY_ARTIFACT
    payload = SourceIdentityPayload.model_validate(draft.payload)
    assert payload.method == "llm"
    assert payload.title == "Invincible"
    assert payload.artist == "Breakaway"
    assert payload.originalartist == "Breakaway"
    assert payload.qualifier == "Nightcore"
    assert result.summary and "Breakaway" in result.summary
    assert provider.requests
    assert provider.requests[0].task.value == "source_extract"
    assert "description" in {snippet.label for snippet in provider.requests[0].source_snippets}


@pytest.mark.asyncio
async def test_identity_falls_back_to_heuristic_without_provider() -> None:
    observer = SourceIdentityObserver(llm_provider=None)

    result = await observer.run(_context())

    draft = result.artifacts[0]
    payload = SourceIdentityPayload.model_validate(draft.payload)
    assert payload.method == "heuristic"
    assert payload.originalartist == "Breakaway"
    assert payload.title == "Invincible"


@pytest.mark.asyncio
async def test_metadata_query_prefers_extracted_identity() -> None:
    observer = SourceIdentityObserver(llm_provider=None)
    result = await observer.run(_context())
    identity = result.artifacts[0]
    payload = canonical_json(identity.payload)
    context = _context(
        _input(SOURCE_IDENTITY_ARTIFACT, payload),
    )

    title, artist, album, duration = _query_values(context)

    assert title == "Invincible"
    assert artist == "Breakaway"
    assert album is None
    assert duration == 180.0


def test_fake_provider_satisfies_protocol() -> None:
    assert isinstance(_FakeProvider(()), LLMProvider)

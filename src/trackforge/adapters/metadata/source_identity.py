"""Extract the original recording identity from noisy source metadata.

The capability runs *before* AcoustID/MusicBrainz/lyrics/cover lookups.  It
uses an optional LLM to read the uploader title, channel, and description,
then falls back to the deterministic source-title parser.  The output is an
artifact (not claims yet) so the observation DAG stays pure.
"""

from __future__ import annotations

from typing import Any

from trackforge.application.dto.observations import (
    SourceIdentityPayload,
    SourceMetadataPayload,
)
from trackforge.application.ports.execution import (
    ArtifactDraft,
    StepContext,
    StepResult,
    StepWarning,
)
from trackforge.application.ports.llm import LLMProvider, LLMRequest, LLMTask, SourceSnippet
from trackforge.application.ports.observations import (
    SOURCE_IDENTITY_ARTIFACT,
    SOURCE_METADATA_ARTIFACT,
)
from trackforge.application.services.llm_guardrails import LLMGuardrails
from trackforge.application.services.prompts import OUTPUT_SCHEMA, SOURCE_EXTRACT_FIELDS
from trackforge.application.services.source_identity import (
    looks_like_mix,
    normalize_source_identity,
)
from trackforge.domain.errors import (
    BudgetExceeded,
    LLMInvalidOutput,
    ProviderUnavailable,
    RateLimited,
)
from trackforge.domain.model.capabilities import (
    CapabilityKind,
    CapabilityManifest,
    Cost,
    Determinism,
    SideEffect,
)

_LLM_WARNING = "source_identity_llm"


class SourceIdentityObserver:
    """LLM-assisted original title/artist extraction from source metadata."""

    manifest = CapabilityManifest(
        id="source_identity",
        version="1",
        kind=CapabilityKind.OBSERVER,
        requires=(SOURCE_METADATA_ARTIFACT,),
        provides=(SOURCE_IDENTITY_ARTIFACT,),
        cost=Cost(cpu_seconds=0.2, memory_mb=32.0, network_calls=1),
        determinism=Determinism.NON_DETERMINISTIC,
        # The artifact must be persisted: interpretation consumes it later
        # from the artifact store, not from the executor's in-memory state.
        cacheable=True,
        side_effects=SideEffect.NETWORK,
        description="Extract original title/artist from noisy source text with an LLM.",
        config_schema={
            "type": "object",
            "properties": {"offline": {"type": "boolean"}},
        },
    )

    def __init__(
        self,
        *,
        llm_provider: LLMProvider | None = None,
        guardrails: LLMGuardrails | None = None,
        max_cost_usd: float | None = None,
        max_tokens: int = 2_000,
        temperature: float = 0.1,
    ) -> None:
        self._provider = llm_provider
        self._guardrails = guardrails or LLMGuardrails(
            max_cost_usd_per_item=max_cost_usd,
        )
        self._max_cost_usd = max_cost_usd
        self._max_tokens = max_tokens
        self._temperature = temperature

    async def run(self, ctx: StepContext) -> StepResult:
        source_step = ctx.inputs.get(SOURCE_METADATA_ARTIFACT)
        if source_step is None:
            return StepResult.skipped(
                warnings=(
                    StepWarning(
                        code="source_identity_missing_source",
                        message="source identity requires source metadata",
                        capability_id=self.manifest.id,
                    ),
                ),
                summary="source metadata is unavailable",
            )
        try:
            source = SourceMetadataPayload.model_validate_json(source_step.payload)
        except Exception:
            return StepResult.skipped(
                warnings=(
                    StepWarning(
                        code="source_identity_invalid_source",
                        message="source metadata is invalid",
                        capability_id=self.manifest.id,
                    ),
                ),
                summary="source metadata is invalid",
            )

        duration = source.duration_seconds or source.duration
        if looks_like_mix(
            source.title,
            duration,
            uploader=source.uploader,
            description=source.description,
        ):
            artist = _clean(source.uploader) or _clean(source.artist)
            payload = SourceIdentityPayload(
                title=_clean(source.title),
                artist=artist,
                originalartist=None,
                qualifier=None,
                source_title=source.title,
                uploader=source.uploader,
                confidence=0.9,
                method="mix",
                warnings=["mix_track_detected"],
            )
            return StepResult(
                artifacts=(
                    ArtifactDraft(
                        type=SOURCE_IDENTITY_ARTIFACT,
                        payload=payload.model_dump(mode="json"),
                    ),
                ),
                warnings=(
                    StepWarning(
                        code="mix_track_detected",
                        message="mix/compilation track keeps its source title",
                        capability_id=self.manifest.id,
                    ),
                ),
                actual_cost=Cost(cpu_seconds=0.1, memory_mb=16.0),
                summary=f"mix, {artist or 'unknown uploader'} — {payload.title}",
                details={
                    "method": "mix",
                    "title": payload.title,
                    "artist": payload.artist,
                },
            )
        deterministic = normalize_source_identity(
            source.title,
            source.artist or source.uploader,
            source.description,
        )
        title = deterministic.title
        original_artist = deterministic.artist
        qualifier = deterministic.qualifier
        method = "heuristic"
        confidence = deterministic.confidence
        warnings: list[str] = list(deterministic.warnings)
        details: dict[str, Any] = {"method": method}

        if bool(ctx.config_subset.get("offline")):
            warnings.append(f"{_LLM_WARNING}:offline")
        elif self._provider is None:
            warnings.append(f"{_LLM_WARNING}:disabled")
        else:
            response = await self._extract(ctx, source, warnings)
            if response is not None:
                title, original_artist, qualifier, confidence = _merge_llm_values(
                    response=response,
                    title=title,
                    original_artist=original_artist,
                    qualifier=qualifier,
                    confidence=confidence,
                )
                method = "llm"
                if title:
                    warnings = [
                        item
                        for item in warnings
                        if item
                        not in {
                            "source_title_unparsed",
                            "source_title_low_confidence",
                            "source_title_empty",
                        }
                    ]
                if original_artist:
                    warnings = [item for item in warnings if item != "original_artist_unknown"]
                details.update(
                    {
                        "model": response.model,
                        "prompt_version": response.prompt_version,
                        "cost_usd": response.cost_usd,
                    }
                )

        artist = original_artist
        payload = SourceIdentityPayload(
            title=_clean(title),
            artist=_clean(artist),
            originalartist=_clean(original_artist),
            qualifier=_clean(qualifier),
            source_title=source.title,
            uploader=source.uploader,
            confidence=max(0.0, min(1.0, float(confidence))),
            method=method,
            warnings=list(dict.fromkeys(warnings)),
        )
        details.update(
            {
                "title": payload.title,
                "artist": payload.artist,
                "originalartist": payload.originalartist,
                "qualifier": payload.qualifier,
            }
        )
        summary_parts = [method]
        if payload.originalartist:
            summary_parts.append(f"original artist: {payload.originalartist}")
        if payload.title:
            summary_parts.append(f"title: {payload.title}")
        return StepResult(
            artifacts=(
                ArtifactDraft(
                    type=SOURCE_IDENTITY_ARTIFACT,
                    payload=payload.model_dump(mode="json"),
                ),
            ),
            warnings=tuple(
                StepWarning(
                    code=_warning_code(item),
                    message=item,
                    capability_id=self.manifest.id,
                )
                for item in payload.warnings
            ),
            actual_cost=Cost(cpu_seconds=0.2, memory_mb=32.0, network_calls=1),
            summary=", ".join(summary_parts),
            details=details,
        )

    async def _extract(
        self,
        ctx: StepContext,
        source: SourceMetadataPayload,
        warnings: list[str],
    ) -> Any | None:
        provider = self._provider
        if provider is None:
            return None
        snippets = _source_snippets(source)
        if not snippets:
            warnings.append(f"{_LLM_WARNING}:no_source_text")
            return None
        request = LLMRequest(
            task=LLMTask.SOURCE_EXTRACT,
            schema=OUTPUT_SCHEMA,
            claims=(),
            source_snippets=snippets,
            target_fields=tuple(SOURCE_EXTRACT_FIELDS),
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            max_cost_usd=self._max_cost_usd,
        )
        try:
            response = await provider.complete(request)
        except BudgetExceeded as exc:
            warnings.append(f"{_LLM_WARNING}:budget_exceeded: {exc.message}")
            return None
        except (ProviderUnavailable, RateLimited, LLMInvalidOutput) as exc:
            warnings.append(f"{_LLM_WARNING}:fallback: {type(exc).__name__}: {exc.message}")
            return None
        try:
            outcome = self._guardrails.check_response(
                response,
                claims=(),
                target_fields=tuple(SOURCE_EXTRACT_FIELDS),
                budget_usd=self._max_cost_usd,
            )
        except BudgetExceeded as exc:
            warnings.append(f"{_LLM_WARNING}:budget_exceeded: {exc.message}")
            return None
        warnings.extend(
            item for item in outcome.all_warnings if not item.startswith("llm reported ")
        )
        if not outcome.claims:
            warnings.append(f"{_LLM_WARNING}:no_claims")
            return None
        response = _response_with_claims(response, outcome.claims)
        return response


def _merge_llm_values(
    *,
    response: Any,
    title: str | None,
    original_artist: str | None,
    qualifier: str | None,
    confidence: float,
) -> tuple[str | None, str | None, str | None, float]:
    values: dict[str, Any] = {}
    scores: list[float] = []
    for claim in response.claims:
        field = str(claim.field).strip().casefold()
        if field in values:
            continue
        values[field] = claim.value
        scores.append(float(claim.confidence))
    llm_title = _clean(values.get("title"))
    llm_original = _clean(values.get("originalartist"))
    llm_artist = _clean(values.get("artist"))
    llm_qualifier = _clean(values.get("qualifier"))
    if llm_title:
        title = llm_title
    if llm_qualifier:
        qualifier = llm_qualifier
    if llm_original:
        original_artist = llm_original
    elif llm_artist and original_artist is None:
        original_artist = llm_artist
    if scores:
        confidence = max(scores)
    return title, original_artist, qualifier, confidence


def _response_with_claims(response: Any, claims: tuple[Any, ...]) -> Any:
    from dataclasses import replace

    return replace(response, claims=claims)


def _source_snippets(source: SourceMetadataPayload) -> tuple[SourceSnippet, ...]:
    snippets: list[SourceSnippet] = []
    for label, value in (
        ("title", source.title),
        ("uploader", source.uploader),
        ("artist", source.artist),
        ("album", source.album),
    ):
        text = _clean(value)
        if text:
            snippets.append(SourceSnippet(text=text, source="source_metadata", label=label))
    description = _clean(source.description)
    if description:
        snippets.append(
            SourceSnippet(
                text=description[:4_000],
                source="source_metadata",
                label="description",
            )
        )
    return tuple(snippets)


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    return text or None


def _warning_code(message: str) -> str:
    head = message.split(":", 1)[0].strip().casefold().replace(" ", "_")
    return head or "source_identity_warning"


__all__ = ["SourceIdentityObserver"]

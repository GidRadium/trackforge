"""End-to-end derivative evidence → claims → resolver behavior."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from trackforge.application.dto.derivatives import DerivativeHypothesesPayload
from trackforge.application.dto.providers import (
    MusicBrainzRecording,
    MusicBrainzRelationship,
    MusicBrainzSearchPayload,
)
from trackforge.application.dto.stems import (
    StemFeatures,
    StemRatios,
    StemsModelInfo,
    StemsPayload,
)
from trackforge.application.use_cases.interpret_item import (
    DERIVATIVE_HYPOTHESES_ARTIFACT,
)
from trackforge.bootstrap.container import CatalogContext
from trackforge.domain.model.claims import Subject

pytestmark = pytest.mark.asyncio


def _stems(
    *,
    vocals: float,
    drums: float,
    bass: float,
    other: float,
    centroid: float = 2400.0,
    rms: float = 0.2,
) -> bytes:
    payload = StemsPayload(
        model=StemsModelInfo(id="htdemucs", version="test", backend="demucs"),
        duration_seconds=180.0,
        ratios=StemRatios(
            vocals=vocals,
            drums=drums,
            bass=bass,
            other=other,
            instrumental=drums + bass + other,
        ),
        features=StemFeatures(
            vocal_energy=vocals,
            spectral_centroid=centroid,
            rms=rms,
        ),
    )
    return payload.model_dump_json().encode("utf-8")


def _musicbrainz(
    *,
    duration: float | None = 180.0,
    bpm: float | None = None,
    relationships: tuple[MusicBrainzRelationship, ...] = (),
) -> bytes:
    payload = MusicBrainzSearchPayload(
        recordings=[
            MusicBrainzRecording(
                mbid="11111111-1111-1111-1111-111111111111",
                title="Song",
                duration=duration,
                bpm=bpm,
                relationships=list(relationships),
                score=0.95,
            )
        ]
    )
    return payload.model_dump_json().encode("utf-8")


def _claims(view: Any, field: str) -> list[Any]:
    return [
        claim
        for claim in view.claims
        if claim.subject is Subject.DERIVATIVE and claim.field == field
    ]


async def test_nightcore_metadata_creates_claims_and_derived_title(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    item_id = await create_item()
    observed = make_observations(
        title="Song",
        artists=("Artist",),
        comment="nightcore remix",
        path=None,
    )

    view = await catalog_context.interpret_item().interpret(item_id, observed)
    resolved = await catalog_context.resolve_item().resolve(item_id)

    transformations = {claim.value for claim in _claims(view, "transformation")}
    assert transformations >= {"nightcore", "remix"}
    assert [claim.value for claim in _claims(view, "status")] == ["proposed"]
    assert view.derivative is not None
    payload = DerivativeHypothesesPayload.model_validate(view.derivative)
    assert payload.status == "proposed"
    assert payload.hypotheses
    assert payload.hypotheses[0].transformations
    assert resolved.fields["title"] == "Song"
    assert resolved.fields["qualifier"] == "Nightcore"
    assert resolved.fields["artist"] == "Artist"


async def test_musicbrainz_official_relationship_sets_official_flag(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    item_id = await create_item()
    observed = make_observations(title="Song", artists=("Artist",), path=None)
    observed["musicbrainz.search.v1"] = _musicbrainz(
        relationships=(MusicBrainzRelationship(type="remix", official=True),)
    )

    view = await catalog_context.interpret_item().interpret(item_id, observed)
    resolved = await catalog_context.resolve_item().resolve(item_id)

    assert {claim.value for claim in _claims(view, "transformation")} == {"remix"}
    official = _claims(view, "is_official")
    assert [claim.value for claim in official] == [True]
    assert official[0].source == "musicbrainz"
    assert resolved.fields["title"] == "Song"
    assert resolved.fields["qualifier"] == "Remix"


async def test_no_official_claim_without_musicbrainz_evidence(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    item_id = await create_item()
    observed = make_observations(title="Song", comment="remix", path=None)

    view = await catalog_context.interpret_item().interpret(item_id, observed)

    assert {claim.value for claim in _claims(view, "transformation")} == {"remix"}
    assert _claims(view, "is_official") == []


async def test_conflicting_evidence_is_ambiguous_and_does_not_change_title(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    item_id = await create_item()
    observed = make_observations(
        title="Song (Nightcore)",
        artists=("Artist",),
        duration=200.0,
        path=None,
    )
    observed["musicbrainz.search.v1"] = _musicbrainz(duration=180.0)

    view = await catalog_context.interpret_item().interpret(item_id, observed)
    resolved = await catalog_context.resolve_item().resolve(item_id)

    assert [claim.value for claim in _claims(view, "status")] == ["ambiguous"]
    assert view.derivative is not None
    assert view.derivative["status"] == "ambiguous"
    assert resolved.fields["title"] == "Song"
    assert any("derivative_ambiguous" in warning for warning in resolved.warnings)


async def test_low_vocal_energy_yields_instrumental(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    item_id = await create_item()
    observed = make_observations(title="Song", artists=("Artist",), path=None)
    observed["audio.stems.v1"] = _stems(
        vocals=0.02,
        drums=0.20,
        bass=0.28,
        other=0.50,
    )

    view = await catalog_context.interpret_item().interpret(item_id, observed)
    resolved = await catalog_context.resolve_item().resolve(item_id)

    assert {claim.value for claim in _claims(view, "transformation")} == {"instrumental"}
    assert resolved.fields["title"] == "Song (Instrumental)"


async def test_high_vocal_without_band_yields_acapella(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    item_id = await create_item()
    observed = make_observations(title="Song", artists=("Artist",), path=None)
    observed["audio.stems.v1"] = _stems(
        vocals=0.75,
        drums=0.05,
        bass=0.08,
        other=0.12,
    )

    view = await catalog_context.interpret_item().interpret(item_id, observed)
    resolved = await catalog_context.resolve_item().resolve(item_id)

    assert {claim.value for claim in _claims(view, "transformation")} == {"acapella"}
    assert resolved.fields["title"] == "Song (Acapella)"


async def test_source_description_and_bpm_are_evidence(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    from trackforge.application.dto.observations import SourceMetadataPayload

    item_id = await create_item()
    observed = make_observations(title="Song", artists=("Artist",), path=None)
    observed["source.metadata.v1"] = (
        SourceMetadataPayload(
            external_id="video-1",
            title="Song",
            description="slowed + reverb version",
            duration=180.0,
        )
        .model_dump_json()
        .encode("utf-8")
    )

    view = await catalog_context.interpret_item().interpret(item_id, observed)
    resolved = await catalog_context.resolve_item().resolve(item_id)

    assert {claim.value for claim in _claims(view, "transformation")} >= {
        "slowed",
        "reverb",
    }
    assert resolved.fields["title"] == "Song"
    assert resolved.fields["qualifier"] == "Slowed"


async def test_no_evidence_produces_no_derivative_claims(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    item_id = await create_item()
    observed = make_observations(title="Song", artists=("Artist",), path=None)

    view = await catalog_context.interpret_item().interpret(item_id, observed)
    resolved = await catalog_context.resolve_item().resolve(item_id)

    assert not any(claim.subject is Subject.DERIVATIVE for claim in view.claims)
    assert view.derivative is not None
    assert view.derivative["status"] == "none"
    assert view.derivative["hypotheses"] == []
    assert any(payload.get("status") == "none" for payload in view.artifact_payloads)
    assert resolved.fields["title"] == "Song"


async def test_derivative_claims_carry_provenance_and_evidence(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    item_id = await create_item()
    observed = make_observations(title="Song", comment="nightcore", path=None)

    view = await catalog_context.interpret_item().interpret(item_id, observed)

    for claim in view.claims:
        if claim.subject is not Subject.DERIVATIVE:
            continue
        assert claim.evidence
        assert claim.source
        assert claim.method == "derivative_detector/v1"
        assert claim.notes


async def test_qualifier_is_not_duplicated_when_source_title_has_it(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    item_id = await create_item()
    observed = make_observations(title="Song (Nightcore)", artists=("Artist",), path=None)

    view = await catalog_context.interpret_item().interpret(item_id, observed)
    resolved = await catalog_context.resolve_item().resolve(item_id)

    assert [claim.value for claim in _claims(view, "status")] == ["proposed"]
    assert resolved.fields["title"] == "Song"
    assert resolved.fields["qualifier"] == "Nightcore"


async def test_bpm_ratio_from_tags_and_musicbrainz_is_evidence(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    item_id = await create_item()
    observed = make_observations(
        title="Song",
        artists=("Artist",),
        bpm=150.0,
        path=None,
    )
    observed["musicbrainz.search.v1"] = _musicbrainz(bpm=120.0)

    view = await catalog_context.interpret_item().interpret(item_id, observed)
    resolved = await catalog_context.resolve_item().resolve(item_id)

    assert "sped_up" in {claim.value for claim in _claims(view, "transformation")}
    speed_factor = [claim.value for claim in _claims(view, "bpm_ratio")]
    assert speed_factor == [pytest.approx(1.25)]
    assert resolved.fields["title"] == "Song"
    assert resolved.fields["qualifier"] == "Sped Up"


async def test_hypotheses_artifact_type_is_exposed(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    item_id = await create_item()
    observed = make_observations(title="Song (Remix)", path=None)

    view = await catalog_context.interpret_item().interpret(item_id, observed)

    assert view.derivative is not None
    assert any(
        payload.get("schema_version") == 1
        and payload.get("status") in {"proposed", "ambiguous", "none"}
        for payload in view.artifact_payloads
    )
    assert DERIVATIVE_HYPOTHESES_ARTIFACT == "derivative.hypotheses.v1"


async def test_youtube_description_gives_artist_and_base_title(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    from trackforge.application.dto.observations import SourceMetadataPayload

    item_id = await create_item()
    observed = make_observations(title="", artists=(), album=None, path=None)
    observed["source.metadata.v1"] = (
        SourceMetadataPayload(
            external_id="k9MNcoWj1fo",
            extractor="youtube",
            title="Nightcore | Queen of Shadow",
            uploader="Purely's Metal Nightcore",
            description="Queen of Shadow by Delain from Dark Waters.",
            tags=["music", "nightcore", "delain", "queen of shadow", "dark waters"],
            webpage_url="https://www.youtube.com/watch?v=k9MNcoWj1fo",
        )
        .model_dump_json()
        .encode("utf-8")
    )

    view = await catalog_context.interpret_item().interpret(item_id, observed)
    resolved = await catalog_context.resolve_item().resolve(item_id)

    assert resolved.fields["artist"] == ["Purely's Metal Nightcore", "Delain"]
    assert resolved.fields["title"] == "Queen of Shadow"
    assert resolved.fields["qualifier"] == "Nightcore"
    assert resolved.fields["remixer"] == "Purely's Metal Nightcore"
    assert resolved.fields["originalartist"] == "Delain"
    assert resolved.fields["album"] == "Dark Waters"
    assert {claim.value for claim in _claims(view, "transformation")} >= {"nightcore"}


async def test_source_thumbnail_cover_becomes_artwork_claim(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    make_observations: Callable[..., dict[str, bytes]],
) -> None:
    from trackforge.application.dto.providers import CoverCandidate, CoverCandidatesPayload

    item_id = await create_item()
    observed = make_observations(title="Song", artists=("Artist",), path=None)
    observed["cover.candidates.v1"] = (
        CoverCandidatesPayload(
            candidates=[
                CoverCandidate(
                    source="source_thumbnail",
                    url="https://i.ytimg.com/vi/demo/maxresdefault.jpg",
                    width=1920,
                    height=1080,
                    score=0.95,
                    artifact_ref="a" * 64,
                )
            ]
        )
        .model_dump_json()
        .encode("utf-8")
    )

    await catalog_context.interpret_item().interpret(item_id, observed)
    resolved = await catalog_context.resolve_item().resolve(item_id)

    assert resolved.fields["artwork"] == "a" * 64

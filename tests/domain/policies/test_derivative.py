"""Derivative evidence rules and conservative ambiguity handling."""

from __future__ import annotations

import math

import pytest

from trackforge.domain.model.identity import Transformation
from trackforge.domain.policies.derivative import (
    DerivativeStatus,
    RelationshipEvidence,
    TextEvidence,
    evaluate_derivative,
)

pytestmark = pytest.mark.unit


def test_nightcore_metadata_adds_sped_up_and_estimates_pitch() -> None:
    hypothesis = evaluate_derivative(
        texts=[TextEvidence("Song (Nightcore)", confidence=0.8)],
        measured_duration=140.0,
        original_duration=180.0,
    )

    assert hypothesis is not None
    assert hypothesis.status is DerivativeStatus.PROPOSED
    assert set(hypothesis.transformations) >= {
        Transformation.NIGHTCORE,
        Transformation.SPED_UP,
    }
    assert hypothesis.parameters.speed_factor == pytest.approx(180.0 / 140.0)
    assert hypothesis.parameters.pitch_semitones == pytest.approx(12.0 * math.log2(180.0 / 140.0))
    assert {item.source.value for item in hypothesis.evidence} >= {
        "metadata",
        "duration",
        "tempo",
    }


def test_sped_up_metadata_is_detected() -> None:
    hypothesis = evaluate_derivative(texts=[TextEvidence("Song (Sped Up)", confidence=0.8)])

    assert hypothesis is not None
    assert hypothesis.transformations == (Transformation.SPED_UP,)
    assert hypothesis.parameters.speed_factor is None


def test_slowed_plus_reverb_keeps_both_transformations() -> None:
    hypothesis = evaluate_derivative(
        texts=[TextEvidence("Song (Slowed + Reverb)", confidence=0.8)],
        measured_duration=200.0,
        original_duration=180.0,
    )

    assert hypothesis is not None
    assert hypothesis.status is DerivativeStatus.PROPOSED
    assert set(hypothesis.transformations) == {
        Transformation.SLOWED,
        Transformation.REVERB,
    }
    assert hypothesis.parameters.speed_factor == pytest.approx(0.9)


def test_remix_and_cover_metadata_are_detected() -> None:
    remix = evaluate_derivative(texts=[TextEvidence("Song (Remix)", confidence=0.8)])
    cover = evaluate_derivative(texts=[TextEvidence("Song (Cover)", confidence=0.8)])

    assert remix is not None and remix.transformations == (Transformation.REMIX,)
    assert cover is not None and cover.transformations == (Transformation.COVER,)
    assert remix.is_official is None
    assert cover.is_official is None


def test_official_and_unofficial_musicbrainz_relationships() -> None:
    official = evaluate_derivative(relationships=[RelationshipEvidence("remix", official=True)])
    unofficial = evaluate_derivative(relationships=[RelationshipEvidence("cover", official=False)])

    assert official is not None
    assert official.transformations == (Transformation.REMIX,)
    assert official.is_official is True
    assert unofficial is not None
    assert unofficial.transformations == (Transformation.COVER,)
    assert unofficial.is_official is False


def test_duration_and_bpm_alone_are_evidence() -> None:
    by_duration = evaluate_derivative(measured_duration=140.0, original_duration=180.0)
    by_bpm = evaluate_derivative(measured_bpm=150.0, original_bpm=120.0)

    assert by_duration is not None
    assert by_duration.transformations == (Transformation.SPED_UP,)
    assert by_bpm is not None
    assert by_bpm.transformations == (Transformation.SPED_UP,)
    assert by_bpm.parameters.bpm_ratio == pytest.approx(1.25)


def test_low_vocal_energy_is_instrumental() -> None:
    hypothesis = evaluate_derivative(
        vocal_energy=0.03,
        drum_energy=0.10,
        bass_energy=0.15,
        other_energy=0.72,
    )

    assert hypothesis is not None
    assert hypothesis.transformations == (Transformation.INSTRUMENTAL,)
    assert hypothesis.evidence[0].source.value == "vocal"


def test_high_vocal_with_no_band_is_acapella() -> None:
    hypothesis = evaluate_derivative(
        vocal_energy=0.75,
        drum_energy=0.05,
        bass_energy=0.08,
        other_energy=0.12,
    )

    assert hypothesis is not None
    assert hypothesis.transformations == (Transformation.ACAPELLA,)


def test_conflicting_speed_evidence_is_ambiguous() -> None:
    hypothesis = evaluate_derivative(
        texts=[TextEvidence("Song (Nightcore)", confidence=0.8)],
        measured_duration=200.0,
        original_duration=180.0,
    )

    assert hypothesis is not None
    assert hypothesis.status is DerivativeStatus.AMBIGUOUS
    assert hypothesis.is_ambiguous is True
    assert Transformation.NIGHTCORE in hypothesis.transformations
    assert Transformation.SLOWED in hypothesis.transformations
    assert hypothesis.score <= 0.5


def test_duplicate_evidence_is_deduped() -> None:
    hypothesis = evaluate_derivative(
        texts=[
            TextEvidence("Song (Nightcore)", origin="title", confidence=0.8),
            TextEvidence("Song (Nightcore)", origin="comment", confidence=0.4),
        ]
    )

    assert hypothesis is not None
    assert hypothesis.transformations == (
        Transformation.NIGHTCORE,
        Transformation.SPED_UP,
    )
    values = [item.value for item in hypothesis.evidence]
    assert values.count("nightcore") == 1


def test_missing_evidence_is_not_a_negative_claim() -> None:
    assert evaluate_derivative() is None


def test_unknown_llm_transformation_is_ignored() -> None:
    hypothesis = evaluate_derivative(
        llm_transformations=[("warp_drive", 0.9), (Transformation.REMIX, 0.6)]
    )

    assert hypothesis is not None
    assert hypothesis.transformations == (Transformation.REMIX,)
    assert hypothesis.evidence[0].source.value == "llm"


def test_llm_cannot_assert_official_status() -> None:
    hypothesis = evaluate_derivative(
        llm_transformations=[(Transformation.REMIX, 0.5)],
    )

    assert hypothesis is not None
    assert hypothesis.is_official is None


def test_spectral_centroid_alone_supports_speed_direction() -> None:
    brighter = evaluate_derivative(
        spectral_centroid=4200.0,
        reference_spectral_centroid=3000.0,
    )
    darker = evaluate_derivative(
        spectral_centroid=2100.0,
        reference_spectral_centroid=3000.0,
    )

    assert brighter is not None and brighter.transformations == (Transformation.SPED_UP,)
    assert darker is not None and darker.transformations == (Transformation.SLOWED,)


def test_bootleg_text_sets_unofficial_when_musicbrainz_is_silent() -> None:
    hypothesis = evaluate_derivative(texts=[TextEvidence("Song (Bootleg Remix)", confidence=0.8)])

    assert hypothesis is not None
    assert Transformation.BOOTLEG in hypothesis.transformations
    assert hypothesis.is_official is False


def test_unknown_transformation_is_rejected() -> None:
    from trackforge.domain.errors import DomainValidationError
    from trackforge.domain.policies.derivative import DerivativeHypothesis

    with pytest.raises(DomainValidationError, match="transformation"):
        DerivativeHypothesis(transformations=("warp_drive",), score=0.5)  # type: ignore[arg-type]

    with pytest.raises(DomainValidationError, match="status"):
        DerivativeHypothesis(
            transformations=(Transformation.REMIX,),
            score=0.5,
            status="maybe",  # type: ignore[arg-type]
        )

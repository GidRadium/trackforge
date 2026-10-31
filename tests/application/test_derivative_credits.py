"""Unit tests for D11 derivative credits."""

from __future__ import annotations

import pytest

from trackforge.application.services.derivative_credits import build_derivative_credits
from trackforge.application.services.source_identity import normalize_source_identity
from trackforge.domain.model.claims import Claim, ClaimStatus, Confidence, Subject
from trackforge.domain.model.ids import ClaimId, ItemId, new_uuid7

pytestmark = pytest.mark.unit


def _claim(field: str, value: object, source: str, *, role: str | None = None) -> Claim:
    return Claim(
        id=ClaimId(new_uuid7()),
        item_id=ItemId(new_uuid7()),
        subject=Subject.ITEM,
        field=field,
        value=value,
        role=role,
        source=source,
        method="fixture/v1",
        confidence=Confidence(0.7),
        evidence=("fixture",),
        status=ClaimStatus.PROPOSED,
    )


def test_target_credits_match_d11_filename_inputs() -> None:
    identity = normalize_source_identity(
        "{642} Nightcore (End Of The Dream) - Collide (with lyrics)",
        "Rock Goes Nightcore",
        None,
    )

    credits = build_derivative_credits(
        ["nightcore", "sped_up"],
        "Rock Goes Nightcore",
        identity,
        (),
    )

    assert credits.remixer == "Rock Goes Nightcore"
    assert credits.original_artist == "End Of The Dream"
    assert credits.title == "Collide"
    assert credits.qualifier == "Nightcore"
    assert credits.comment == ("Nightcore by Rock Goes Nightcore; original by End Of The Dream")


def test_original_artist_can_come_from_claims() -> None:
    identity = normalize_source_identity("{9} Nightcore - Song", "Nightcore Channel", None)
    claims = (_claim("artist", "Original Artist", "musicbrainz"),)

    credits = build_derivative_credits("nightcore", "Nightcore Channel", identity, claims)

    assert credits.original_artist == "Original Artist"
    assert credits.title == "Song"


def test_missing_original_keeps_remixer_main_with_warning() -> None:
    identity = normalize_source_identity("Nightcore - Song", "Nightcore Channel", None)

    credits = build_derivative_credits("nightcore", "Nightcore Channel", identity, ())

    assert credits.remixer == "Nightcore Channel"
    assert credits.original_artist is None
    assert "original_artist_unknown" in credits.warnings


def test_missing_remixer_falls_back_to_original_main() -> None:
    identity = normalize_source_identity("Original Artist - Song (Nightcore)", None, None)

    credits = build_derivative_credits("nightcore", None, identity, ())

    assert credits.remixer is None
    assert credits.original_artist == "Original Artist"
    assert "derivative_remixer_unknown" in credits.warnings


def test_identical_remixer_and_original_are_not_duplicated() -> None:
    identity = normalize_source_identity("Same Artist - Song (Nightcore)", "Same Artist", None)

    credits = build_derivative_credits("nightcore", "Same Artist", identity, ())

    assert credits.original_artist is None
    assert "derivative_credit_same_artist" in credits.warnings


def test_cover_is_not_rewritten_by_derivative_credit_rules() -> None:
    identity = normalize_source_identity("Cover Artist - Song (Cover)", "Channel", None)

    credits = build_derivative_credits("cover", "Channel", identity, ())

    assert credits.remixer is None
    assert credits.original_artist is None
    assert credits.qualifier is None

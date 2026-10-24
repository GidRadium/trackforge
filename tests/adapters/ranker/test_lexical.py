"""Lexical ranker and optional cross-encoder degradation tests."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from trackforge.adapters.analysis.ranker import CrossEncoderRanker, LexicalRanker
from trackforge.domain.model.claims import Claim

pytestmark = pytest.mark.unit


def test_exact_normalized_match_scores_one(make_claim: Callable[..., Claim]) -> None:
    claim = make_claim(field="title", value="  SONG!  ")
    ranker = LexicalRanker()

    result = ranker.rank("song", [claim], limit=1)

    assert len(result) == 1
    assert result[0].score == pytest.approx(1.0)
    assert result[0].ranker == "lexical/rapidfuzz-v1"
    assert result[0].evidence["calibrated"] is False
    assert result[0].evidence["score_kind"] == "similarity"


def test_ranking_orders_noisy_candidate_below_exact(
    make_claim: Callable[..., Claim],
) -> None:
    exact = make_claim(field="title", value="Song")
    noisy = make_claim(field="title", value="Song (Official Video)")
    other = make_claim(field="title", value="Completely Different")
    ranker = LexicalRanker()

    result = ranker.rank("Song", [other, noisy, exact], limit=3)

    assert [item.claim.id for item in result] == [exact.id, noisy.id, other.id]
    assert result[0].score > result[1].score > result[2].score


def test_limit_and_empty_query(make_claim: Callable[..., Claim]) -> None:
    claims = [make_claim(field="title", value=f"Song {index}") for index in range(5)]
    ranker = LexicalRanker()

    assert len(ranker.rank("Song", claims, limit=2)) == 2
    with pytest.raises(ValueError):
        ranker.rank("   ", claims)
    with pytest.raises(ValueError):
        ranker.rank("Song", claims, limit=0)


def test_multi_value_claim_is_joined(make_claim: Callable[..., Claim]) -> None:
    claim = make_claim(field="artist", value=["Artist", "Featured"])
    ranker = LexicalRanker()

    result = ranker.rank("Artist Featured", [claim])

    assert result[0].score >= 0.9


def test_cross_encoder_skips_without_model(make_claim: Callable[..., Claim]) -> None:
    claim = make_claim(field="title", value="Song")
    ranker = CrossEncoderRanker(None, None)

    result = ranker.rank("Song", [claim])

    assert result == []
    assert ranker.available is False
    assert any("skipped" in warning for warning in ranker.last_warnings)


def test_lexical_ranker_satisfies_contract() -> None:
    from trackforge.application.ports.ranking import ClaimRanker

    assert isinstance(LexicalRanker(), ClaimRanker)
    assert isinstance(CrossEncoderRanker(None, None), ClaimRanker)

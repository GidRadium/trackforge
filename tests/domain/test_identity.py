"""Lightweight identity type tests."""

from __future__ import annotations

import pytest

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.identity import (
    Artist,
    ContentKind,
    Derivative,
    IdentityHypothesis,
    IdentityStatus,
    IdentityTargetType,
    Recording,
    Release,
    ReleaseGroup,
    Transformation,
    Work,
)
from trackforge.domain.model.ids import ItemId, new_uuid7


def test_identity_types_are_lightweight() -> None:
    work = Work(id=new_uuid7(), title="Song")
    recording = Recording(id=new_uuid7(), title="Song", work_id=work.id)
    release_group = ReleaseGroup(id=new_uuid7(), title="Album")
    release = Release(
        id=new_uuid7(),
        title="Album",
        release_group_id=release_group.id,
        date="2024",
    )
    artist = Artist(id=new_uuid7(), name="Artist", mbid=str(new_uuid7()))
    derivative = Derivative(
        id=new_uuid7(),
        source_recording_id=recording.id,
        transformations=(Transformation.NIGHTCORE,),
        derivative_title="Song (Nightcore)",
    )

    assert recording.work_id == work.id
    assert release.release_group_id == release_group.id
    assert artist.mbid is not None
    assert derivative.transformations == (Transformation.NIGHTCORE,)


def test_identity_hypothesis() -> None:
    hypothesis = IdentityHypothesis(
        id=new_uuid7(),
        item_id=ItemId(new_uuid7()),
        target_type=IdentityTargetType.RECORDING,
        target_id=str(new_uuid7()),
        score=0.86,
        external_ids={"mbid": str(new_uuid7())},
        status=IdentityStatus.PROPOSED,
    )

    assert hypothesis.target_type is IdentityTargetType.RECORDING
    assert 0 <= hypothesis.score <= 1


def test_identity_hypothesis_rejects_invalid_score() -> None:
    with pytest.raises(DomainValidationError):
        IdentityHypothesis(
            id=new_uuid7(),
            item_id=ItemId(new_uuid7()),
            target_type=IdentityTargetType.RECORDING,
            score=1.5,
        )


def test_content_kind_is_separate_from_transformation() -> None:
    assert ContentKind.MUSIC.value == "music"
    assert Transformation.SPED_UP.value == "sped_up"

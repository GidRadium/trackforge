"""Tests for the path-structure observation."""

from __future__ import annotations

import pytest

from trackforge.adapters.analysis.path_structure import (
    PathStructureObserver,
    build_path_structure,
)
from trackforge.application.dto.observations import PathStructurePayload
from trackforge.application.ports.execution import Capability, StepContext
from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.ids import ItemId, RunId, StepId, new_uuid7

pytestmark = pytest.mark.unit


def test_path_structure_satisfies_capability_protocol() -> None:
    assert isinstance(PathStructureObserver(), Capability)


def test_build_path_structure_keeps_components_uninterpreted() -> None:
    payload = build_path_structure("/music/Artist/Album/03 - Track.mp3")

    assert payload.path == "/music/Artist/Album/03 - Track.mp3"
    assert payload.parent_names == ["music", "Artist", "Album"]
    assert payload.stem == "03 - Track"
    assert payload.suffix == ".mp3"


@pytest.mark.asyncio
async def test_observer_emits_path_structure_artifact() -> None:
    ctx = StepContext(
        run_id=RunId(new_uuid7()),
        item_id=ItemId(new_uuid7()),
        step_id=StepId(new_uuid7()),
        params={"path": "/music/Artist/Album/song.flac"},
    )

    result = await PathStructureObserver().run(ctx)

    payload = PathStructurePayload.model_validate(result.artifacts[0].payload)
    assert payload.parent_names == ["music", "Artist", "Album"]
    assert payload.stem == "song"
    assert payload.suffix == ".flac"


@pytest.mark.asyncio
async def test_missing_path_parameter_is_rejected() -> None:
    ctx = StepContext(
        run_id=RunId(new_uuid7()),
        item_id=ItemId(new_uuid7()),
        step_id=StepId(new_uuid7()),
    )
    with pytest.raises(DomainValidationError):
        await PathStructureObserver().run(ctx)

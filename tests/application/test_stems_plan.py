"""Profile/flag wiring for the opt-in Demucs observer."""

from __future__ import annotations

from pathlib import Path

import pytest

from trackforge.adapters.analysis.ffprobe import FfprobeProbe
from trackforge.adapters.analysis.path_structure import PathStructureObserver
from trackforge.adapters.sources.local import LocalFileSource
from trackforge.adapters.tagging.mutagen.reader import InitialTagsObserver
from trackforge.application.dto.plan import PlanOptions
from trackforge.application.ports.execution import StepContext, StepResult
from trackforge.application.ports.observations import MEDIA_ARTIFACT, STEMS_ARTIFACT
from trackforge.application.use_cases.plan_item import PlanItem
from trackforge.domain.model.capabilities import (
    CapabilityKind,
    CapabilityManifest,
    Cost,
    Determinism,
    SideEffect,
)
from trackforge.domain.model.source import LocalPath

pytestmark = pytest.mark.asyncio


class _FakeStems:
    manifest = CapabilityManifest(
        id="demucs",
        version="1",
        kind=CapabilityKind.OBSERVER,
        requires=(MEDIA_ARTIFACT,),
        provides=(STEMS_ARTIFACT,),
        cost=Cost(cpu_seconds=240.0, memory_mb=3072.0),
        determinism=Determinism.DETERMINISTIC,
        cacheable=True,
        side_effects=SideEffect.NONE,
    )

    async def run(self, ctx: StepContext) -> StepResult:  # pragma: no cover - plan only
        raise AssertionError("planning must not execute capabilities")


def _plan_item() -> PlanItem:
    return PlanItem(
        capabilities=(
            FfprobeProbe(),
            InitialTagsObserver(),
            PathStructureObserver(),
            _FakeStems(),
        ),
        resolvers=(LocalFileSource(),),
    )


async def test_full_profile_plans_stems(tmp_path: Path) -> None:
    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"not-real-audio")

    plan = await _plan_item().plan(
        LocalPath(str(audio)),
        PlanOptions(profile="full"),
    )

    assert plan.blocked_reason is None
    assert "demucs" in {step.capability_id for step in plan.steps}


async def test_disable_stems_removes_demucs_from_full_plan(tmp_path: Path) -> None:
    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"not-real-audio")

    plan = await _plan_item().plan(
        LocalPath(str(audio)),
        PlanOptions(profile="full", disabled=("stems",)),
    )

    assert plan.blocked_reason is None
    assert "demucs" not in {step.capability_id for step in plan.steps}
    assert any("demucs" in warning for warning in plan.warnings)


async def test_enable_stems_adds_it_to_balanced_plan(tmp_path: Path) -> None:
    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"not-real-audio")

    plan = await _plan_item().plan(
        LocalPath(str(audio)),
        PlanOptions(profile="balanced", enabled=("stems",)),
    )

    assert plan.blocked_reason is None
    assert "demucs" in {step.capability_id for step in plan.steps}


@pytest.mark.asyncio
async def test_url_plan_includes_source_cover(tmp_path: Path) -> None:
    from tests.adapters.ytdlp.conftest import FAKE_YTDLP, YOUTUBE_URL

    from trackforge.bootstrap.container import build_plan_item
    from trackforge.config import TrackForgeConfig
    from trackforge.domain.model.source import Url

    config = TrackForgeConfig(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
        library_dir=tmp_path / "library",
        ytdlp_binary=str(FAKE_YTDLP),
    )

    plan = await build_plan_item(config=config).plan(Url(YOUTUBE_URL))
    steps = {step.capability_id for step in plan.steps}

    assert "source_metadata" in steps
    assert "source_cover" in steps

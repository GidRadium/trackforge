"""End-to-end local ingest → CAS → probe/tags/path across all MVP formats."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from tests.fixtures.media import (
    CONTAINER_CODECS,
    MBID_RECORDING,
    MediaFixture,
    generate_media,
    skip_without_media_tools,
)

from trackforge.application.dto.observations import (
    AudioProbePayload,
    InitialTagsPayload,
    ObservationView,
    PathStructurePayload,
)
from trackforge.application.dto.plan import AcquireOptions, PlanOptions
from trackforge.bootstrap.container import CatalogContext, build_catalog_context
from trackforge.config import TrackForgeConfig
from trackforge.domain.model.item import ItemStatus
from trackforge.domain.model.source import LocalPath

pytestmark = pytest.mark.integration


def cas_files(config: TrackForgeConfig) -> list[Path]:
    root = config.data_dir / "cas"
    return sorted(path for path in root.rglob("*") if path.is_file()) if root.exists() else []


def artifact(view: ObservationView, artifact_type: str):
    summary = view.artifact(artifact_type)
    assert summary is not None, f"missing artifact {artifact_type}"
    return summary


async def load_payload(
    context: CatalogContext,
    view: ObservationView,
    artifact_type: str,
) -> bytes:
    summary = artifact(view, artifact_type)
    reference = await context.artifact_store.find(summary.cache_key)
    assert reference is not None, "artifact manifest is missing from the store"
    manifest, payload = await context.artifact_store.load(reference)
    assert manifest.type == artifact_type
    assert manifest.payload_hash
    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize("container", sorted(CONTAINER_CODECS))
async def test_local_e2e_observe(
    tmp_path: Path,
    config: TrackForgeConfig,
    container: str,
) -> None:
    skip_without_media_tools()
    fixture: MediaFixture = generate_media(tmp_path / "media", container)
    original_hash = hashlib.sha256(fixture.path.read_bytes()).hexdigest()
    original_mtime = fixture.path.stat().st_mtime_ns

    with build_catalog_context(config) as context:
        plan = await context.plan_item().plan(
            LocalPath(str(fixture.path)),
            PlanOptions(),
            context.source_resolvers(),
        )
        assert plan.blocked_reason is None
        assert len(plan.candidates) == 1

        acquired = await context.acquire_item().acquire(
            plan.candidates[0],
            AcquireOptions(),
            context.acquirers(),
        )
        assert acquired.content_hash == original_hash
        assert len(cas_files(config)) == 1

        view = await context.observe_item().observe(acquired.item_id, "balanced")

        assert view.status is ItemStatus.OBSERVED
        assert view.cache_hit is False
        assert view.media.content_hash == original_hash
        assert view.media.container == container
        assert view.media.codec
        assert view.media.duration_seconds is not None
        assert view.media.duration_seconds > 0
        assert view.media.sample_rate is not None and view.media.sample_rate > 0
        assert view.media.channels == 1
        assert view.media.probe_artifact_id == artifact(view, "audio.probe.v1").artifact_id

        probe_bytes = await load_payload(context, view, "audio.probe.v1")
        probe = AudioProbePayload.model_validate_json(probe_bytes)
        assert probe.container == container
        assert probe.duration_seconds is not None and probe.duration_seconds > 0
        assert probe.format_name
        assert any(stream.codec_type == "audio" for stream in probe.streams)

        tags_bytes = await load_payload(context, view, "initial.tags.v1")
        tags = InitialTagsPayload.model_validate_json(tags_bytes)
        assert tags.tags.title == "Fixture Song"
        assert tags.tags.artist == ["Fixture Artist"]
        assert tags.reader.id == "mutagen"
        assert tags.reader.version
        if container != "m4a":
            assert tags.tags.mbids["recording"] == [MBID_RECORDING]

        path_bytes = await load_payload(context, view, "path.structure.v1")
        path_payload = PathStructurePayload.model_validate_json(path_bytes)
        assert path_payload.path == str(fixture.path.absolute())
        assert path_payload.stem == fixture.path.stem
        assert path_payload.suffix == f".{container}"

        async with context.unit_of_work() as uow:
            item = await uow.items.get(acquired.item_id)
            media = await uow.media.get(acquired.media_id)
            probe_record = await uow.artifacts.find_by_cache_key(
                artifact(view, "audio.probe.v1").cache_key
            )
        assert item is not None and item.status is ItemStatus.OBSERVED
        assert media is not None and media.duration_seconds is not None
        assert probe_record is not None
        assert probe_record.payload_hash
        assert probe_record.producer_id == "ffprobe"

        second = await context.observe_item().observe(acquired.item_id, "balanced")
        assert second.cache_hit is True
        assert [step.capability_id for step in second.steps] == [
            "ffprobe",
            "initial_tags",
            "path_structure",
        ]
        assert all(step.status == "cached" for step in second.steps)
        assert all(summary.cached for summary in second.artifacts)
        assert {summary.cache_key for summary in second.artifacts} == {
            summary.cache_key for summary in view.artifacts
        }

        again = await context.acquire_item().acquire(
            plan.candidates[0],
            AcquireOptions(),
            context.acquirers(),
        )
        assert again.reused_media is True
        assert again.content_hash == original_hash
        assert len(cas_files(config)) == 1

    assert hashlib.sha256(fixture.path.read_bytes()).hexdigest() == original_hash
    assert fixture.path.stat().st_mtime_ns == original_mtime


@pytest.mark.asyncio
async def test_full_profile_without_asr_model_skips_but_observes(
    tmp_path: Path,
    config: TrackForgeConfig,
) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", "mp3")

    with build_catalog_context(config) as context:
        plan = await context.plan_item().plan(
            LocalPath(str(fixture.path)),
            PlanOptions(profile="full"),
            context.source_resolvers(),
        )
        assert plan.blocked_reason is None
        assert any("asr.transcript.v1" in step.provides for step in plan.steps)

        acquired = await context.acquire_item().acquire(
            plan.candidates[0],
            AcquireOptions(),
            context.acquirers(),
        )
        view = await context.observe_item().observe(acquired.item_id, "full")

        assert view.status is ItemStatus.OBSERVED
        assert view.artifact("asr.transcript.v1") is None
        steps = {step.capability_id: step for step in view.steps}
        assert steps["whisper"].status == "skipped"
        assert "model_missing" in steps["whisper"].warning_codes
        assert "model_missing" in {warning.code for warning in view.warnings}

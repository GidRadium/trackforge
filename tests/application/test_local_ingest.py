"""Application-level tests for local plan/acquire semantics."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from trackforge.adapters.analysis.ffprobe import FfprobeProbe
from trackforge.adapters.analysis.path_structure import PathStructureObserver
from trackforge.adapters.tagging.mutagen.reader import InitialTagsObserver
from trackforge.application.dto.plan import AcquireOptions, PlanOptions
from trackforge.application.ports.acquisition import Acquirer
from trackforge.application.ports.sources import SourceResolver
from trackforge.bootstrap.container import build_catalog_context, build_observe_item
from trackforge.config import TrackForgeConfig
from trackforge.domain.errors import AcquisitionFailed, ProviderUnavailable, UnsupportedSource
from trackforge.domain.model.item import ItemStatus
from trackforge.domain.model.source import LocalPath

pytestmark = pytest.mark.integration


def write_source(path: Path, payload: bytes = b"fake-audio-bytes") -> Path:
    path.write_bytes(payload)
    return path


def cas_files(config: TrackForgeConfig) -> list[Path]:
    root = config.data_dir / "cas"
    return sorted(path for path in root.rglob("*") if path.is_file()) if root.exists() else []


@pytest.mark.asyncio
async def test_plan_and_acquire_persist_catalog_and_cas(
    tmp_path: Path,
    config: TrackForgeConfig,
) -> None:
    path = write_source(tmp_path / "Artist - Song.mp3")
    before_stat = path.stat()

    with build_catalog_context(config) as context:
        plan = await context.plan_item().plan(
            LocalPath(str(path)),
            PlanOptions(),
            context.source_resolvers(),
        )

        assert plan.blocked_reason is None
        assert plan.resolver_id == "local_file"
        assert isinstance(context.source_resolvers()[0], SourceResolver)
        assert isinstance(context.acquirers()[0], Acquirer)
        assert len(plan.candidates) == 1
        assert [step.node_id for step in plan.steps] == [
            "acquire",
            "ffprobe",
            "initial_tags",
            "path_structure",
        ]

        view = await context.acquire_item().acquire(
            plan.candidates[0],
            AcquireOptions(),
            context.acquirers(),
        )

        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert view.status is ItemStatus.ACQUIRED
        assert view.content_hash == digest
        assert view.size_bytes == before_stat.st_size
        assert view.reused_media is False
        assert Path(view.cas_path).read_bytes() == path.read_bytes()
        assert Path(view.source_path) == path.absolute()
        assert len(cas_files(config)) == 1
        assert path.read_bytes() == b"fake-audio-bytes"
        assert path.stat().st_mtime_ns == before_stat.st_mtime_ns

        async with context.unit_of_work() as uow:
            item = await uow.items.get(view.item_id)
            media = await uow.media.get(view.media_id)
            intake = await uow.intakes.get(view.intake_id)
            instances = await uow.file_instances.find_by_media(view.media_id)
        assert item is not None and item.status is ItemStatus.ACQUIRED
        assert item.primary_intake_id == view.intake_id
        assert media is not None and media.content_hash == digest
        assert media.container.value == "mp3"
        assert media.codec is None
        assert intake is not None and intake.downloader == "local_copy"
        assert intake.media_id == view.media_id
        assert len(instances) == 1
        assert instances[0].path == str(path.absolute())
        assert instances[0].role.value == "source"


@pytest.mark.asyncio
async def test_repeated_acquire_reuses_media_and_cas_object(
    tmp_path: Path,
    config: TrackForgeConfig,
) -> None:
    path = write_source(tmp_path / "song.flac", b"same-bytes")

    with build_catalog_context(config) as context:
        plan = await context.plan_item().plan(
            LocalPath(str(path)),
            PlanOptions(),
            context.source_resolvers(),
        )
        candidate = plan.candidates[0]
        first = await context.acquire_item().acquire(
            candidate,
            AcquireOptions(),
            context.acquirers(),
        )
        second = await context.acquire_item().acquire(
            candidate,
            AcquireOptions(),
            context.acquirers(),
        )

        assert first.content_hash == second.content_hash
        assert second.reused_media is True
        assert first.item_id != second.item_id
        assert len(cas_files(config)) == 1
        async with context.unit_of_work() as uow:
            media = await uow.media.find_by_hash(first.content_hash)
            first_intake = await uow.intakes.get(first.intake_id)
            second_intake = await uow.intakes.get(second.intake_id)
        assert media is not None
        assert first_intake is not None and second_intake is not None
        assert first_intake.media_id == second_intake.media_id == media.id


@pytest.mark.asyncio
async def test_file_disappearing_between_plan_and_acquire_fails(
    tmp_path: Path,
    config: TrackForgeConfig,
) -> None:
    path = write_source(tmp_path / "vanishing.opus")

    with build_catalog_context(config) as context:
        plan = await context.plan_item().plan(
            LocalPath(str(path)),
            PlanOptions(),
            context.source_resolvers(),
        )
        candidate = plan.candidates[0]
        path.unlink()

        with pytest.raises(AcquisitionFailed):
            await context.acquire_item().acquire(
                candidate,
                AcquireOptions(),
                context.acquirers(),
            )


@pytest.mark.asyncio
async def test_directory_is_reported_as_unsupported_plan(
    tmp_path: Path,
    config: TrackForgeConfig,
) -> None:
    directory = tmp_path / "album.mp3"
    directory.mkdir()

    with build_catalog_context(config) as context:
        plan = await context.plan_item().plan(
            LocalPath(str(directory)),
            PlanOptions(),
            context.source_resolvers(),
        )

    assert plan.blocked_reason is not None
    assert plan.candidates == ()
    assert plan.steps == ()


@pytest.mark.asyncio
async def test_unsupported_extension_is_reported_as_unsupported_plan(
    tmp_path: Path,
    config: TrackForgeConfig,
) -> None:
    path = write_source(tmp_path / "song.wav")

    with build_catalog_context(config) as context:
        plan = await context.plan_item().plan(
            LocalPath(str(path)),
            PlanOptions(),
            context.source_resolvers(),
        )

    assert plan.blocked_reason is not None
    assert (
        "supports" in " ".join(plan.warnings).lower()
        or "supported" in " ".join(plan.warnings).lower()
    )


@pytest.mark.asyncio
async def test_direct_resolve_rejects_unsupported_extension(
    tmp_path: Path,
    config: TrackForgeConfig,
) -> None:
    path = write_source(tmp_path / "song.wav")

    with build_catalog_context(config) as context:
        with pytest.raises(UnsupportedSource):
            await context.source_resolvers()[0].resolve(LocalPath(str(path)))


@pytest.mark.asyncio
async def test_missing_ffprobe_fails_observe_without_empty_observation(
    tmp_path: Path,
    config: TrackForgeConfig,
) -> None:
    path = write_source(tmp_path / "song.mp3", b"fake audio bytes")

    with build_catalog_context(config) as context:
        plan = await context.plan_item().plan(
            LocalPath(str(path)),
            PlanOptions(),
            context.source_resolvers(),
        )
        acquired = await context.acquire_item().acquire(
            plan.candidates[0],
            AcquireOptions(),
            context.acquirers(),
        )
        observer = build_observe_item(
            config,
            context.unit_of_work,
            artifact_store=context.artifact_store,
            capabilities=(
                FfprobeProbe(executable="trackforge-definitely-missing-ffprobe"),
                InitialTagsObserver(),
                PathStructureObserver(),
            ),
        )

        with pytest.raises(ProviderUnavailable) as caught:
            await observer.observe(acquired.item_id, "balanced")

        assert caught.value.retryable is False
        assert caught.value.user_action is not None
        assert "run_id" in caught.value.context

        async with context.unit_of_work() as uow:
            item = await uow.items.get(acquired.item_id)
            media = await uow.media.get(acquired.media_id)
        assert item is not None and item.status is ItemStatus.ACQUIRED
        assert media is not None and media.probe_artifact_id is None

"""Integration tests for the SQLite repositories and unit of work."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from trackforge.adapters.persistence.sqlite.engine import Catalog
from trackforge.adapters.persistence.sqlite.models import LockRow
from trackforge.adapters.persistence.sqlite.unit_of_work import SqliteUnitOfWork
from trackforge.application.dto.db import (
    ArtifactRecord,
    RunItemStatus,
    RunStatus,
    StepStatus,
)
from trackforge.application.ports.repositories import (
    ArtifactRepository,
    ClaimRepository,
    DecisionRepository,
    FeedbackRepository,
    FileInstanceRepository,
    IntakeRepository,
    ItemRepository,
    LibraryRepository,
    LockRepository,
    MediaRepository,
    RenderRepository,
    RunRepository,
    UnitOfWork,
)
from trackforge.domain.errors import (
    DuplicateCacheKey,
    DuplicateContentHash,
    DuplicateRecord,
    ForeignKeyViolation,
)
from trackforge.domain.model.claims import Claim, ClaimStatus, Subject
from trackforge.domain.model.decisions import Decision, DecisionRecord
from trackforge.domain.model.feedback import (
    CorrectionScope,
    FeedbackEvent,
    FeedbackKind,
)
from trackforge.domain.model.ids import (
    CandidateId,
    new_uuid7,
)
from trackforge.domain.model.item import Item, ItemStatus
from trackforge.domain.model.media import FileInstance, FileRole, Intake, MediaObject
from trackforge.domain.model.outputs import (
    LibraryEntry,
    LibraryStatus,
    OutputBundle,
    Render,
    RenderStatus,
    TargetFormat,
)
from trackforge.domain.model.source import Candidate, Format, LocalPath

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 22, 13, 0, tzinfo=UTC)


def test_repository_protocols_match_concrete_implementations() -> None:
    assert issubclass(SqliteUnitOfWork, object)
    methods = {
        "items": ["get", "save", "update_status", "find_by_media", "list_by_status"],
        "media": ["get", "find_by_hash", "save"],
        "file_instances": ["get", "find_by_media", "find_by_path", "save"],
        "intakes": ["get", "find_by_item", "save"],
        "artifacts": ["get", "find_by_cache_key", "save"],
        "claims": ["append", "list_by_item", "list_by_field", "append_status_event"],
        "decisions": ["save_record", "get_record", "list_records", "list_decisions"],
        "locks": ["get", "list_by_item", "upsert", "delete"],
        "feedback": ["append_event", "list_by_item", "list_by_field", "stats"],
        "runs": [
            "create_run",
            "get_run",
            "update_run",
            "add_item",
            "set_item_status",
            "create_step",
            "update_step",
            "list_steps",
            "find_resumable",
        ],
        "renders": ["save_render", "save_bundle", "get_bundle", "list_by_item"],
        "library": ["save_entry", "find_by_path", "list_entries", "update_status"],
    }
    from trackforge.adapters.persistence.sqlite import repositories

    classes = {
        "items": repositories.SqliteItemRepository,
        "media": repositories.SqliteMediaRepository,
        "file_instances": repositories.SqliteFileInstanceRepository,
        "intakes": repositories.SqliteIntakeRepository,
        "artifacts": repositories.SqliteArtifactRepository,
        "claims": repositories.SqliteClaimRepository,
        "decisions": repositories.SqliteDecisionRepository,
        "locks": repositories.SqliteLockRepository,
        "feedback": repositories.SqliteFeedbackRepository,
        "runs": repositories.SqliteRunRepository,
        "renders": repositories.SqliteRenderRepository,
        "library": repositories.SqliteLibraryRepository,
    }
    for attribute, names in methods.items():
        for name in names:
            assert hasattr(classes[attribute], name), f"{attribute}.{name}"


@pytest.mark.asyncio
async def test_item_and_media_round_trip(
    catalog: Catalog,
    uow_factory: Callable[[], SqliteUnitOfWork],
    make_media: Callable[..., MediaObject],
    make_item: Callable[..., Item],
) -> None:
    media = make_media()
    item = make_item(media_id=media.id)

    async with uow_factory() as uow:
        await uow.media.save(media)
        await uow.items.save(item)

    async with uow_factory() as uow:
        loaded_media = await uow.media.get(media.id)
        assert loaded_media is not None
        assert loaded_media.content_hash == media.content_hash
        assert loaded_media.container is media.container
        assert loaded_media.duration_seconds == pytest.approx(12.5)
        assert await uow.media.find_by_hash(media.content_hash) == loaded_media

        loaded = await uow.items.get(item.id)
        assert loaded is not None
        assert loaded.media_id == media.id
        assert loaded.status is ItemStatus.DISCOVERED
        assert loaded.created_at == item.created_at

        same_media = await uow.items.find_by_media(media.id)
        assert [candidate.id for candidate in same_media] == [item.id]

        transitioned_at = item.created_at + timedelta(seconds=1)
        updated = await uow.items.update_status(item.id, ItemStatus.ACQUIRED, at=transitioned_at)
        assert updated.status is ItemStatus.ACQUIRED
        assert updated.updated_at == transitioned_at
        assert await uow.items.list_by_status(ItemStatus.ACQUIRED) == [updated]
        assert await uow.items.list_by_status(ItemStatus.DISCOVERED) == []

    with catalog.connect() as connection:
        raw = connection.execute(
            text("SELECT created_at, updated_at FROM items WHERE id = :id"),
            {"id": item.id},
        ).one()
    assert raw.created_at.endswith("Z")
    assert raw.updated_at.endswith("Z")
    assert datetime.fromisoformat(raw.updated_at.replace("Z", "+00:00")) == transitioned_at


@pytest.mark.asyncio
async def test_intake_round_trip_preserves_source_and_candidate(
    uow_factory: Callable[[], SqliteUnitOfWork],
    make_media: Callable[..., MediaObject],
    make_item: Callable[..., Item],
) -> None:
    media = make_media()
    item = make_item(media_id=media.id)
    local_path = LocalPath(path="/music/source.mp3")
    candidate = Candidate(
        id=CandidateId(new_uuid7()),
        source_ref=local_path,
        external_id="/music/source.mp3",
        title="Source",
        uploader="uploader",
        duration_seconds=42.5,
        formats=(Format(codec="mp3", bitrate=320000, container="mp3"),),
        extra={"origin": "test"},
    )
    intake = Intake(
        id=new_uuid7(),
        item_id=item.id,
        source_ref=local_path,
        candidate=candidate,
        downloader="local_copy",
        downloader_version="1.0",
        media_id=media.id,
        created_at=NOW,
    )

    file_instance = FileInstance(
        id=new_uuid7(),
        media_id=media.id,
        path="/music/source.mp3",
        role=FileRole.SOURCE,
        size_bytes=1024,
        mtime=NOW,
        intake_id=intake.id,
    )

    async with uow_factory() as uow:
        await uow.media.save(media)
        await uow.items.save(item)
        await uow.intakes.save(intake)
        await uow.file_instances.save(file_instance)

    async with uow_factory() as uow:
        loaded_instance = await uow.file_instances.get(file_instance.id)
        assert loaded_instance is not None
        assert loaded_instance.mtime == NOW
        assert loaded_instance.intake_id == intake.id
        assert [candidate.id for candidate in await uow.file_instances.find_by_media(media.id)] == [
            file_instance.id
        ]
        assert [
            candidate.id for candidate in await uow.file_instances.find_by_path(file_instance.path)
        ] == [file_instance.id]

        loaded = await uow.intakes.get(intake.id)
        assert loaded is not None
        assert loaded.source_ref == local_path
        assert loaded.candidate == candidate
        assert loaded.downloader == "local_copy"
        assert loaded.media_id == media.id
        assert [found.id for found in await uow.intakes.find_by_item(item.id)] == [intake.id]


@pytest.mark.asyncio
async def test_claim_and_status_events_are_append_only(
    catalog: Catalog,
    uow_factory: Callable[[], SqliteUnitOfWork],
    make_item: Callable[..., Item],
    make_claim: Callable[..., Claim],
) -> None:
    item = make_item()
    claim = make_claim(
        item.id,
        value=["Alpha", "Beta"],
        role="main",
        order=1,
        locale="en",
        confidence=0.75,
        notes="kept for audit",
    )

    async with uow_factory() as uow:
        await uow.items.save(item)
        saved = await uow.claims.append(claim)
        assert saved.id == claim.id

    async with uow_factory() as uow:
        by_item = await uow.claims.list_by_item(item.id)
        by_field = await uow.claims.list_by_field(item.id, "title")
        assert [candidate.id for candidate in by_item] == [claim.id]
        assert by_field[0].value == ["Alpha", "Beta"]
        assert by_field[0].confidence == pytest.approx(0.75)
        assert by_field[0].method == "test_method/v1"
        assert by_field[0].status is ClaimStatus.PROPOSED

        event = await uow.claims.append_status_event(
            claim.id,
            ClaimStatus.ACCEPTED,
            reason="reviewed by user",
        )
        assert event.status is ClaimStatus.ACCEPTED
        assert (await uow.claims.list_by_field(item.id, "title"))[0].status is (
            ClaimStatus.ACCEPTED
        )

        assert not hasattr(uow.claims, "update")
        assert not hasattr(uow.claims, "delete")
        assert not hasattr(uow.decisions, "update")
        assert not hasattr(uow.decisions, "delete")
        assert not hasattr(uow.feedback, "update")
        assert not hasattr(uow.feedback, "delete")

    with catalog.connect() as connection:
        events = connection.execute(
            text("SELECT status, reason FROM claim_events WHERE claim_id = :id"),
            {"id": claim.id},
        ).all()
    assert events == [("accepted", "reviewed by user")]


@pytest.mark.asyncio
async def test_decision_record_round_trip_and_generated_ids(
    uow_factory: Callable[[], SqliteUnitOfWork],
    make_item: Callable[..., Item],
    make_claim: Callable[..., Claim],
) -> None:
    item = make_item()
    claim = make_claim(item.id, field="artist", value="Artist", role="main")
    decision = Decision(
        field="artist",
        subject=Subject.ITEM,
        value="Artist",
        chosen_claim_id=claim.id,
        alternatives=(claim.id,),
        policy_id="default.artist.v1",
        confidence=0.8,
        rationale="single high-confidence source",
    )
    record = DecisionRecord(
        id=new_uuid7(),
        item_id=item.id,
        version=1,
        decisions=(decision,),
        policy_versions=("default.artist.v1",),
        input_claim_ids=(claim.id,),
        created_at=NOW,
    )

    async with uow_factory() as uow:
        await uow.items.save(item)
        await uow.claims.append(claim)
        saved = await uow.decisions.save_record(record)
        assert saved.decisions[0].id is not None

    async with uow_factory() as uow:
        loaded = await uow.decisions.get_record(record.id)
        assert loaded is not None
        assert loaded.version == 1
        assert loaded.policy_versions == ("default.artist.v1",)
        assert loaded.input_claim_ids == (claim.id,)
        loaded_decision = loaded.decisions[0]
        assert loaded_decision.id is not None
        assert loaded_decision.chosen_claim_id == claim.id
        assert loaded_decision.alternatives == (claim.id,)
        assert loaded_decision.value == "Artist"
        assert loaded_decision.locked is False

        records = await uow.decisions.list_records(item.id)
        assert [candidate.id for candidate in records] == [record.id]
        decisions = await uow.decisions.list_decisions(record.id)
        assert [candidate.field for candidate in decisions] == ["artist"]


@pytest.mark.asyncio
async def test_locks_upsert_uniqueness_and_item_level_field(
    catalog: Catalog,
    uow_factory: Callable[[], SqliteUnitOfWork],
    make_item: Callable[..., Item],
) -> None:
    item = make_item()

    async with uow_factory() as uow:
        await uow.items.save(item)
        item_lock = await uow.locks.upsert(item.id, "", "Item lock")
        assert item_lock.field == ""
        changed = await uow.locks.upsert(
            item.id,
            "artist",
            "Locked Artist",
        )
        assert changed.field == "artist"
        replaced = await uow.locks.upsert(item.id, "artist", "New Artist")
        assert replaced.value == "New Artist"
        assert replaced.created_at == changed.created_at
        assert len(await uow.locks.list_by_item(item.id)) == 2
        assert (await uow.locks.get(item.id, "")) is not None
        assert await uow.locks.delete(item.id, "artist") is True
        assert await uow.locks.delete(item.id, "artist") is False

    with pytest.raises(IntegrityError):
        with catalog.connect() as connection:
            connection.execute(
                insert(LockRow).values(
                    id=new_uuid7(),
                    item_id=item.id,
                    field="",
                    value_json='"duplicate"',
                    created_at="2026-10-22T13:00:00.000000Z",
                    updated_at="2026-10-22T13:00:00.000000Z",
                )
            )
            connection.commit()


@pytest.mark.asyncio
async def test_feedback_round_trip_filters_and_stats(
    uow_factory: Callable[[], SqliteUnitOfWork],
    make_item: Callable[..., Item],
) -> None:
    item = make_item()
    correction = FeedbackEvent(
        id=new_uuid7(),
        kind=FeedbackKind.CORRECTION,
        item_id=item.id,
        field="title",
        system_value="Old",
        user_value="New",
        scope=CorrectionScope.ITEM,
        reason="wrong_title",
        context_snapshot={"claims": ["a", "b"], "record": "r1"},
        created_at=NOW,
    )
    approval = FeedbackEvent(
        id=new_uuid7(),
        kind=FeedbackKind.APPROVAL,
        item_id=item.id,
        field="artist",
        user_value="Artist",
        created_at=NOW + timedelta(seconds=1),
    )

    async with uow_factory() as uow:
        await uow.items.save(item)
        await uow.feedback.append_event(correction)
        await uow.feedback.append_event(approval, applied=True)

    async with uow_factory() as uow:
        loaded = await uow.feedback.list_by_item(item.id)
        assert [event.id for event in loaded] == [correction.id, approval.id]
        assert loaded[0].context_snapshot == {"claims": ["a", "b"], "record": "r1"}
        assert loaded[0].reason == "wrong_title"
        corrections = await uow.feedback.list_by_item(
            item.id,
            kind=FeedbackKind.CORRECTION,
        )
        assert [event.kind for event in corrections] == [FeedbackKind.CORRECTION]
        assert [event.id for event in await uow.feedback.list_by_field(item.id, "title")] == [
            correction.id
        ]
        stats = await uow.feedback.stats(item_id=item.id)
        assert stats.total == 2
        assert stats.by_kind == {"correction": 1, "approval": 1}
        assert stats.by_field == {"title": 1, "artist": 1}


@pytest.mark.asyncio
async def test_runs_steps_resume_and_run_item_status(
    uow_factory: Callable[[], SqliteUnitOfWork],
    make_item: Callable[..., Item],
) -> None:
    item = make_item()

    async with uow_factory() as uow:
        await uow.items.save(item)
        run = await uow.runs.create_run(
            "fast",
            "policy",
            status=RunStatus.RUNNING,
            started_at=NOW,
            config_snapshot_hash="a" * 64,
        )
        run_item = await uow.runs.add_item(
            run.id,
            item.id,
            status=RunItemStatus.RUNNING,
        )
        assert run_item.status is RunItemStatus.RUNNING
        step = await uow.runs.create_step(
            run.id,
            item.id,
            "audio.probe",
            "1",
            status=StepStatus.READY,
            cache_key="b" * 64,
        )
        running = await uow.runs.update_step(
            step.id,
            status=StepStatus.RUNNING,
            attempts=1,
            started_at=NOW,
        )
        assert running.attempts == 1
        finished = await uow.runs.update_step(
            step.id,
            status=StepStatus.SUCCEEDED,
            finished_at=NOW + timedelta(seconds=2),
            warnings=({"code": "low_quality"}, "used_fallback"),
        )
        assert finished.warnings == ({"code": "low_quality"}, "used_fallback")

        resumable = await uow.runs.find_resumable()
        assert [candidate.id for candidate in resumable] == [run.id]

        completed = await uow.runs.update_run(
            run.id,
            status=RunStatus.COMPLETED,
            finished_at=NOW + timedelta(seconds=3),
        )
        assert completed.status is RunStatus.COMPLETED
        assert await uow.runs.find_resumable() == []
        assert [candidate.id for candidate in await uow.runs.list_steps(run.id)] == [step.id]
        assert await uow.runs.list_steps(run.id, item_id=item.id) != []

        assert await uow.runs.get_run(run.id) is not None


@pytest.mark.asyncio
async def test_render_bundle_and_library_round_trip(
    uow_factory: Callable[[], SqliteUnitOfWork],
    make_item: Callable[..., Item],
    make_claim: Callable[..., Claim],
) -> None:
    item = make_item()
    claim = make_claim(item.id)
    record = DecisionRecord(
        id=new_uuid7(),
        item_id=item.id,
        version=1,
        decisions=(
            Decision(
                field="title",
                subject=Subject.ITEM,
                value="Song",
                chosen_claim_id=claim.id,
                policy_id="default.title.v1",
                confidence=0.9,
                rationale="selected",
            ),
        ),
        created_at=NOW,
    )
    render = Render(
        id=new_uuid7(),
        item_id=item.id,
        decision_record_id=record.id,
        target_format=TargetFormat.MP3,
        naming_template="{artist} - {title}",
        tag_mapping_version="1",
        options={"cover_embed": True},
        status=RenderStatus.SUCCEEDED,
        created_at=NOW,
    )
    bundle_hash = hashlib.sha256(b"bundle").hexdigest()
    bundle = OutputBundle(
        id=new_uuid7(),
        render_id=render.id,
        files=("track.mp3", "trackforge.json"),
        bundle_hash=bundle_hash,
        created_at=NOW,
    )
    entry = LibraryEntry(
        id=new_uuid7(),
        item_id=item.id,
        bundle_id=bundle.id,
        path="/library/Artist - Song.mp3",
        status=LibraryStatus.PUBLISHED,
        published_at=NOW,
    )

    async with uow_factory() as uow:
        await uow.items.save(item)
        await uow.claims.append(claim)
        await uow.decisions.save_record(record)
        await uow.renders.save_render(render)
        await uow.renders.save_render(replace(render, status=RenderStatus.COMPLETED))
        saved_bundle = await uow.renders.save_bundle(bundle)
        assert saved_bundle.item_id == item.id
        await uow.library.save_entry(entry)

    async with uow_factory() as uow:
        renders = await uow.renders.list_by_item(item.id)
        assert [candidate.id for candidate in renders] == [render.id]
        assert renders[0].options == {"cover_embed": True}
        assert renders[0].status is RenderStatus.COMPLETED
        loaded_bundle = await uow.renders.get_bundle(bundle.id)
        assert loaded_bundle is not None
        assert loaded_bundle.files == ("track.mp3", "trackforge.json")
        assert loaded_bundle.item_id == item.id

        found = await uow.library.find_by_path(entry.path)
        assert found is not None
        assert found.status is LibraryStatus.PUBLISHED
        assert [candidate.id for candidate in await uow.library.list_entries(item_id=item.id)] == [
            entry.id
        ]
        missing = await uow.library.update_status(entry.id, LibraryStatus.MISSING)
        assert missing.status is LibraryStatus.MISSING


@pytest.mark.asyncio
async def test_artifact_record_round_trip_and_cache_key_uniqueness(
    uow_factory: Callable[[], SqliteUnitOfWork],
    manifest_factory: Callable[..., Any],
) -> None:
    manifest = manifest_factory(payload_hash="c" * 64)
    cache_key = "d" * 64
    record = ArtifactRecord.from_manifest(
        manifest,
        cache_key=cache_key,
        payload_path=f"artifacts/{cache_key}/payload.json",
    )

    async with uow_factory() as uow:
        saved = await uow.artifacts.save(record)
        assert saved.id == manifest.artifact_id
        loaded = await uow.artifacts.get(record.id)
        assert loaded is not None
        assert loaded.params == manifest.params
        assert loaded.to_manifest().artifact_id == manifest.artifact_id
        assert await uow.artifacts.find_by_cache_key(cache_key) == loaded

        conflicting = ArtifactRecord(
            id=new_uuid7(),
            artifact_type=record.artifact_type,
            schema_version=1,
            producer_id="other",
            producer_version="1",
            cache_key=cache_key,
            payload_path="artifacts/other/payload.json",
            payload_hash="e" * 64,
        )
        with pytest.raises(DuplicateCacheKey):
            await uow.artifacts.save(conflicting)


@pytest.mark.asyncio
async def test_duplicate_content_hash_and_foreign_key_violation(
    uow_factory: Callable[[], SqliteUnitOfWork],
    make_media: Callable[..., MediaObject],
    make_item: Callable[..., Item],
) -> None:
    media = make_media()
    duplicate = make_media(content_hash=media.content_hash)
    valid_item = make_item(media_id=media.id)
    broken_item = make_item(media_id="00000000-0000-7000-8000-000000000099")

    async with uow_factory() as uow:
        await uow.media.save(media)

    async with uow_factory() as uow:
        with pytest.raises(DuplicateContentHash):
            await uow.media.save(duplicate)

    async with uow_factory() as uow:
        await uow.items.save(valid_item)

    async with uow_factory() as uow:
        with pytest.raises(ForeignKeyViolation):
            await uow.items.save(broken_item)


@pytest.mark.asyncio
async def test_transaction_rollback_and_concurrent_readers(
    uow_factory: Callable[[], SqliteUnitOfWork],
    make_item: Callable[..., Item],
) -> None:
    item = make_item()
    with pytest.raises(RuntimeError):
        async with uow_factory() as uow:
            await uow.items.save(item)
            raise RuntimeError("simulate use-case failure")

    async with uow_factory() as uow:
        assert await uow.items.get(item.id) is None
        await uow.items.save(item)

    async def read_item() -> Item | None:
        async with uow_factory() as uow:
            return await uow.items.get(item.id)

    results = await asyncio.gather(*(read_item() for _ in range(10)))
    assert all(result is not None for result in results)


@pytest.mark.asyncio
async def test_protocol_runtime_checks_and_duplicate_run_item(
    uow_factory: Callable[[], SqliteUnitOfWork],
    make_item: Callable[..., Item],
) -> None:
    item = make_item()
    async with uow_factory() as uow:
        assert isinstance(uow, UnitOfWork)
        assert isinstance(uow.items, ItemRepository)
        assert isinstance(uow.media, MediaRepository)
        assert isinstance(uow.file_instances, FileInstanceRepository)
        assert isinstance(uow.intakes, IntakeRepository)
        assert isinstance(uow.artifacts, ArtifactRepository)
        assert isinstance(uow.claims, ClaimRepository)
        assert isinstance(uow.decisions, DecisionRepository)
        assert isinstance(uow.locks, LockRepository)
        assert isinstance(uow.feedback, FeedbackRepository)
        assert isinstance(uow.runs, RunRepository)
        assert isinstance(uow.renders, RenderRepository)
        assert isinstance(uow.library, LibraryRepository)

        await uow.items.save(item)
        run = await uow.runs.create_run("fast", "policy")
        await uow.runs.add_item(run.id, item.id)
        with pytest.raises(DuplicateRecord):
            await uow.runs.add_item(run.id, item.id)

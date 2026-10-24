"""Application-level render, sidecar, and failure tests with fake adapters."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import NOW
from tests.fixtures.media import LRC_TEXT, generate_png

from trackforge.application.dto.rendering import (
    CoverArt,
    RenderedAudio,
    RenderOptions,
    TagMismatch,
    TagSet,
    extension_for,
    target_format_for,
)
from trackforge.application.use_cases.render_item import RenderItem
from trackforge.bootstrap.container import CatalogContext
from trackforge.domain.errors import RenderFailed
from trackforge.domain.model.claims import Claim, Subject
from trackforge.domain.model.decisions import Decision, DecisionRecord
from trackforge.domain.model.identity import ContentKind
from trackforge.domain.model.ids import (
    CandidateId,
    ClaimId,
    DecisionRecordId,
    IntakeId,
    ItemId,
    MediaId,
    new_uuid7,
)
from trackforge.domain.model.item import Item, ItemStatus
from trackforge.domain.model.media import Intake, IntakeStatus, MediaContainer, MediaObject
from trackforge.domain.model.source import Candidate, Url

FIXED_RENDER_ID = "01900000-0000-7000-8000-000000000001"
FIXED_BUNDLE_ID = "01900000-0000-7000-8000-000000000002"
FIXED_TIME = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


class FakeRenderer:
    """Copy a fake CAS file to the expected workspace name."""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path]] = []

    def render(
        self, media: Any, decisions: Any, options: RenderOptions, workdir: Path
    ) -> RenderedAudio:
        target = target_format_for(media.container, options.target_format)
        output = workdir / f"track.{extension_for(target)}"
        shutil.copyfile(media.cas_path, output)
        self.calls.append((Path(media.cas_path), output))
        return RenderedAudio(path=output, format=target)


class FakeTagWriter:
    def __init__(self) -> None:
        self.writes: list[tuple[TagSet, CoverArt | None, RenderOptions]] = []
        self.fail_verify = False

    def write(
        self,
        path: Path,
        tagset: TagSet,
        cover: CoverArt | None,
        options: RenderOptions,
    ) -> None:
        self.writes.append((tagset, cover, options))

    def verify(
        self,
        path: Path,
        tagset: TagSet,
        cover: CoverArt | None,
    ) -> list[TagMismatch]:
        if self.fail_verify:
            return [TagMismatch("title", tagset.title, "wrong")]
        return []


class FakeTagReader:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def read(self, path: Path) -> TagSet:
        self.paths.append(path)
        return TagSet(title="read-back")


@dataclass(frozen=True, slots=True)
class RenderSetup:
    item_id: ItemId
    media_id: MediaId
    record_id: DecisionRecordId
    cas_path: Path
    cas_hash: str


async def build_setup(
    context: CatalogContext,
    tmp_path: Path,
    *,
    lyrics: str | None = "plain lyrics",
    synced_lyrics: str | None = None,
    elrc: str | None = None,
    status: ItemStatus = ItemStatus.RESOLVED,
) -> RenderSetup:
    cas_path = tmp_path / "cas" / "source.mp3"
    cas_path.parent.mkdir(parents=True, exist_ok=True)
    cas_path.write_bytes(b"immutable fake audio bytes")
    digest = hashlib.sha256(cas_path.read_bytes()).hexdigest()
    item_id = ItemId(new_uuid7())
    media = MediaObject(
        id=MediaId(new_uuid7()),
        content_hash=digest,
        size_bytes=cas_path.stat().st_size,
        cas_path=str(cas_path),
        container=MediaContainer.MP3,
        codec="mp3",
    )
    item = Item(
        id=item_id,
        media_id=media.id,
        kind=ContentKind.MUSIC,
        status=status,
        created_at=NOW,
    )
    record_id = DecisionRecordId(new_uuid7())
    fields: dict[str, Any] = {
        "title": "Rendered Song",
        "artist": ["Main Artist", "Featured Artist"],
        "albumartist": "Main Artist",
        "album": "Rendered Album",
        "date": "2024-05-01",
        "track": 3,
        "tracktotal": 12,
        "genre": ["Rock", "Pop"],
        "bpm": 172,
        "key": "F#m",
        "language": "eng",
        "comment": "short",
        "lyrics": lyrics,
        "synced_lyrics": synced_lyrics,
        "elrc": elrc,
        "source_url": "https://example.com/watch?v=abc&token=secret",
        "mb_recording_id": "11111111-1111-1111-1111-111111111111",
    }
    item_claim = Claim(
        id=ClaimId(new_uuid7()),
        item_id=item_id,
        subject=Subject.ITEM,
        field="title",
        value="Rendered Song",
        source="fixture",
        method="fixture/v1",
        confidence=1.0,
        evidence=("fixture",),
        created_at=NOW,
    )
    decisions = tuple(
        Decision(
            field=field_name,
            subject=Subject.ITEM,
            value=value,
            chosen_claim_id=item_claim.id,
            policy_id=field_name,
            confidence=1.0,
            rationale="fixture decision",
            created_at=NOW,
        )
        for field_name, value in fields.items()
        if value is not None
    )
    record = DecisionRecord(
        id=record_id,
        item_id=item_id,
        version=1,
        decisions=decisions,
        policy_versions=("title/v1",),
        created_at=NOW,
    )
    source_ref = Url("https://youtube.com/watch?v=abc&token=secret")
    candidate = Candidate(
        id=CandidateId(new_uuid7()),
        source_ref=source_ref,
        external_id="youtube-123",
        title="source title",
    )
    intake = Intake(
        id=IntakeId(new_uuid7()),
        item_id=item_id,
        source_ref=source_ref,
        candidate=candidate,
        downloader="fixture",
        downloader_version="1",
        media_id=media.id,
        status=IntakeStatus.SUCCEEDED,
        created_at=NOW,
    )
    claim = Claim(
        id=ClaimId(new_uuid7()),
        item_id=item_id,
        subject=Subject.DERIVATIVE,
        field="transformation",
        value="nightcore",
        source="fixture",
        method="fixture/v1",
        confidence=0.9,
        evidence=("fixture",),
        created_at=NOW,
    )
    async with context.unit_of_work() as uow:
        await uow.media.save(media)
        await uow.items.save(item)
        await uow.claims.append(item_claim)
        await uow.decisions.save_record(record)
        item.decision_record_id = record.id
        await uow.items.save(item)
        await uow.claims.append(claim)
        await uow.intakes.save(intake)
    return RenderSetup(item_id, media.id, record_id, cas_path, digest)


def build_use_case(
    context: CatalogContext,
    *,
    config: Any,
    writer: FakeTagWriter | None = None,
    reader: FakeTagReader | None = None,
) -> tuple[RenderItem, FakeRenderer, FakeTagWriter, FakeTagReader]:
    renderer = FakeRenderer()
    tag_writer = writer or FakeTagWriter()
    tag_reader = reader or FakeTagReader()
    use_case = RenderItem(
        context.unit_of_work,
        renderer,
        tag_writer,
        tag_reader,
        config.data_dir,
        clock=lambda: FIXED_TIME,
        id_factory=new_uuid7,
    )
    return use_case, renderer, tag_writer, tag_reader


@pytest.mark.asyncio
async def test_render_builds_verified_bundle_with_sidecar(
    catalog_context: CatalogContext,
    config: Any,
    tmp_path: Path,
) -> None:
    setup = await build_setup(catalog_context, tmp_path)
    cover = CoverArt.from_path(generate_png(tmp_path / "covers"))
    use_case, renderer, writer, reader = build_use_case(catalog_context, config=config)

    result = await use_case.render(
        setup.item_id,
        RenderOptions(cover_sidecar=True),
        cover=cover,
        render_id=FIXED_RENDER_ID,
        bundle_id=FIXED_BUNDLE_ID,
        created_at=FIXED_TIME,
    )

    assert (
        result.directory == config.data_dir / "items" / setup.item_id / "renders" / FIXED_RENDER_ID
    )
    assert (result.directory / "track.mp3").is_file()
    assert (result.directory / "cover.jpg").is_file()
    assert (result.directory / "trackforge.json").is_file()
    assert renderer.calls[0][0] == setup.cas_path
    assert writer.writes[0][0].title == "Rendered Song"
    assert len(reader.paths) == 1
    assert reader.paths[0].name == "track.mp3"

    sidecar = json.loads((result.directory / "trackforge.json").read_text(encoding="utf-8"))
    assert sidecar["schema_version"] == 1
    assert sidecar["trackforge"]["item_id"] == setup.item_id
    assert sidecar["trackforge"]["content_hash"] == setup.cas_hash
    assert sidecar["trackforge"]["bundle_hash"] == result.bundle.bundle_hash
    assert sidecar["metadata"]["title"] == "Rendered Song"
    assert sidecar["metadata"]["artist"] == ["Main Artist", "Featured Artist"]
    assert sidecar["identity"]["kind"] == "music"
    assert sidecar["identity"]["derivative"] == ["nightcore"]
    assert sidecar["identity"]["recording_mbid"] == "11111111-1111-1111-1111-111111111111"
    assert sidecar["source"]["url"] == "https://youtube.com/watch"
    assert sidecar["source"]["extractor"] == "youtube.com"
    assert sidecar["source"]["source_id"] == "youtube-123"
    assert "token" not in (result.directory / "trackforge.json").read_text(encoding="utf-8")
    assert result.bundle.files == (
        "cover.jpg",
        "track.mp3",
        "trackforge.json",
    )

    async with catalog_context.unit_of_work() as uow:
        item = await uow.items.get(setup.item_id)
        assert item is not None
        assert item.status is ItemStatus.RENDERED
        renders = await uow.renders.list_by_item(setup.item_id)
        assert [render.id for render in renders] == [FIXED_RENDER_ID]
        bundle = await uow.renders.get_bundle(FIXED_BUNDLE_ID)
        assert bundle is not None
        assert bundle.bundle_hash == result.bundle.bundle_hash

    assert hashlib.sha256(setup.cas_path.read_bytes()).hexdigest() == setup.cas_hash


@pytest.mark.asyncio
async def test_lrc_is_written_only_to_sidecar(
    catalog_context: CatalogContext,
    config: Any,
    tmp_path: Path,
) -> None:
    setup = await build_setup(catalog_context, tmp_path, lyrics=LRC_TEXT)
    use_case, _, writer, _ = build_use_case(catalog_context, config=config)

    result = await use_case.render(
        setup.item_id,
        RenderOptions(lyrics_sidecar=True),
        render_id=FIXED_RENDER_ID,
        bundle_id=FIXED_BUNDLE_ID,
        created_at=FIXED_TIME,
    )

    tagset, _, _ = writer.writes[0]
    assert tagset.lyrics is None
    assert (result.directory / "track.mp3.lrc").read_text(encoding="utf-8") == LRC_TEXT
    assert "track.mp3.lrc" in result.bundle.files
    sidecar = json.loads((result.directory / "trackforge.json").read_text(encoding="utf-8"))
    assert sidecar["metadata"]["lyrics"] is None


@pytest.mark.asyncio
async def test_readback_mismatch_fails_without_catalog_rows(
    catalog_context: CatalogContext,
    config: Any,
    tmp_path: Path,
) -> None:
    setup = await build_setup(catalog_context, tmp_path)
    writer = FakeTagWriter()
    writer.fail_verify = True
    use_case, _, _, _ = build_use_case(catalog_context, config=config, writer=writer)

    with pytest.raises(RenderFailed, match="read-back"):
        await use_case.render(
            setup.item_id,
            render_id=FIXED_RENDER_ID,
            bundle_id=FIXED_BUNDLE_ID,
            created_at=FIXED_TIME,
        )

    render_dir = config.data_dir / "items" / setup.item_id / "renders" / FIXED_RENDER_ID
    assert not render_dir.exists()
    async with catalog_context.unit_of_work() as uow:
        assert await uow.renders.list_by_item(setup.item_id) == []
        assert await uow.renders.get_bundle(FIXED_BUNDLE_ID) is None
        item = await uow.items.get(setup.item_id)
        assert item is not None
        assert item.status is ItemStatus.RESOLVED


@pytest.mark.asyncio
async def test_bundle_hash_is_stable_with_fixed_time(
    catalog_context: CatalogContext,
    config: Any,
    tmp_path: Path,
) -> None:
    setup = await build_setup(catalog_context, tmp_path)
    first_use_case, _, _, _ = build_use_case(catalog_context, config=config)
    first = await first_use_case.render(
        setup.item_id,
        render_id="01900000-0000-7000-8000-000000000011",
        bundle_id="01900000-0000-7000-8000-000000000012",
        created_at=FIXED_TIME,
    )
    second_use_case, _, _, _ = build_use_case(catalog_context, config=config)
    second = await second_use_case.render(
        setup.item_id,
        render_id="01900000-0000-7000-8000-000000000021",
        bundle_id="01900000-0000-7000-8000-000000000022",
        created_at=FIXED_TIME,
    )
    assert first.bundle.bundle_hash == second.bundle.bundle_hash


@pytest.mark.asyncio
async def test_unresolved_item_is_rejected(
    catalog_context: CatalogContext, config: Any, tmp_path: Path
) -> None:
    setup = await build_setup(catalog_context, tmp_path, status=ItemStatus.OBSERVED)
    use_case, _, _, _ = build_use_case(catalog_context, config=config)
    with pytest.raises(RenderFailed, match="resolved"):
        await use_case.render(setup.item_id, created_at=FIXED_TIME)


def test_sidecar_hash_is_stable_and_sensitive_to_files() -> None:
    from trackforge.application.services.sidecar import (
        SidecarData,
        build_sidecar_document,
        compute_bundle_hash,
        finalize_sidecar,
        sidecar_bytes,
    )

    data = SidecarData(
        item_id="item",
        content_hash="a" * 64,
        decision_record_id="decision",
        created_at=FIXED_TIME,
        metadata={"title": "Song"},
    )
    document = build_sidecar_document(data)
    files = [("track.mp3", "b" * 64)]
    first = compute_bundle_hash(files, document)
    second = compute_bundle_hash(files, document)
    assert first == second
    assert first != compute_bundle_hash([("track.mp3", "c" * 64)], document)
    final = finalize_sidecar(document, first)
    assert final["trackforge"]["bundle_hash"] == first
    assert json.loads(sidecar_bytes(final))["schema_version"] == 1


@pytest.mark.asyncio
async def test_plain_lrc_and_elrc_sidecars_coexist(
    catalog_context: CatalogContext,
    config: Any,
    tmp_path: Path,
) -> None:
    elrc_text = "[00:01.00]<00:01.00>First <00:01.40>line\n"
    setup = await build_setup(
        catalog_context,
        tmp_path,
        lyrics="plain lyrics",
        synced_lyrics=LRC_TEXT,
        elrc=elrc_text,
    )
    use_case, _, writer, _ = build_use_case(catalog_context, config=config)

    result = await use_case.render(
        setup.item_id,
        RenderOptions(lyrics_sidecar=True),
        render_id=FIXED_RENDER_ID,
        bundle_id=FIXED_BUNDLE_ID,
        created_at=FIXED_TIME,
    )

    tagset, _, _ = writer.writes[0]
    assert tagset.lyrics == "plain lyrics"
    assert (result.directory / "track.mp3.lrc").read_text(encoding="utf-8") == LRC_TEXT
    assert (result.directory / "track.mp3.elrc").read_text(encoding="utf-8") == elrc_text
    assert {"track.mp3.lrc", "track.mp3.elrc"} <= set(result.bundle.files)
    sidecar = json.loads((result.directory / "trackforge.json").read_text(encoding="utf-8"))
    assert sidecar["metadata"]["lyrics"] == "plain lyrics"

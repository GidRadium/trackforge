"""Resolved metadata and output-pipeline record tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.ids import (
    BundleId,
    DecisionRecordId,
    ItemId,
    LibraryEntryId,
    RenderId,
    new_uuid7,
)
from trackforge.domain.model.outputs import (
    LibraryEntry,
    LibraryStatus,
    OutputBundle,
    Render,
    RenderStatus,
    ResolvedMetadata,
    TargetFormat,
)

NOW = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
HASH = "c" * 64


def test_resolved_metadata() -> None:
    metadata = ResolvedMetadata(
        item_id=ItemId(new_uuid7()),
        decision_record_id=DecisionRecordId(new_uuid7()),
        fields={"title": "Song", "artists": ["Artist"], "bpm": 172.0, "compilation": False},
        unknowns=["album"],
        warnings=["no cover"],
    )

    assert metadata.fields["title"] == "Song"
    assert metadata.fields["artists"] == ["Artist"]
    assert metadata.unknowns == ("album",)


def test_resolved_metadata_rejects_non_json_field() -> None:
    with pytest.raises(DomainValidationError):
        ResolvedMetadata(
            item_id=ItemId(new_uuid7()),
            decision_record_id=DecisionRecordId(new_uuid7()),
            fields={"cover": object()},  # type: ignore[dict-item]
        )


def test_render_and_bundle() -> None:
    item_id = ItemId(new_uuid7())
    record_id = DecisionRecordId(new_uuid7())
    render = Render(
        id=RenderId(new_uuid7()),
        item_id=item_id,
        decision_record_id=record_id,
        target_format=TargetFormat.FLAC,
        naming_template="{artist} - {title}",
        tag_mapping_version="1",
        options={"embed_cover": True},
        status=RenderStatus.SUCCEEDED,
        created_at=NOW,
    )
    bundle = OutputBundle(
        id=BundleId(new_uuid7()),
        render_id=render.id,
        files=("track.flac", "trackforge.json"),
        bundle_hash=HASH,
        item_id=item_id,
        created_at=NOW,
    )

    assert render.target_format is TargetFormat.FLAC
    assert bundle.render_id == render.id
    assert bundle.files == ("track.flac", "trackforge.json")


def test_render_rejects_invalid_status() -> None:
    with pytest.raises(DomainValidationError):
        Render(
            id=RenderId(new_uuid7()),
            item_id=ItemId(new_uuid7()),
            decision_record_id=DecisionRecordId(new_uuid7()),
            target_format=TargetFormat.MP3,
            naming_template="{title}",
            tag_mapping_version="1",
            status="bogus",  # type: ignore[arg-type]
        )


def test_output_bundle_requires_files_and_sha256() -> None:
    with pytest.raises(DomainValidationError):
        OutputBundle(
            id=BundleId(new_uuid7()),
            render_id=RenderId(new_uuid7()),
            files=(),
            bundle_hash=HASH,
        )
    with pytest.raises(DomainValidationError):
        OutputBundle(
            id=BundleId(new_uuid7()),
            render_id=RenderId(new_uuid7()),
            files=("track.mp3",),
            bundle_hash="not-a-hash",
        )


def test_library_entry() -> None:
    entry = LibraryEntry(
        id=LibraryEntryId(new_uuid7()),
        item_id=ItemId(new_uuid7()),
        bundle_id=BundleId(new_uuid7()),
        path="/library/Artist - Song.flac",
        status=LibraryStatus.PUBLISHED,
        published_at=NOW,
    )

    assert entry.status is LibraryStatus.PUBLISHED
    assert entry.path.endswith(".flac")

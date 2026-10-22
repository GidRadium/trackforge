"""Media, file instance, and intake tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.ids import (
    ArtifactId,
    FileInstanceId,
    IntakeId,
    ItemId,
    MediaId,
    new_uuid7,
)
from trackforge.domain.model.media import (
    FileInstance,
    FileRole,
    Intake,
    IntakeStatus,
    MediaContainer,
    MediaObject,
)
from trackforge.domain.model.source import Candidate, Url

NOW = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
HASH = "a" * 64


def _media(**overrides: object) -> MediaObject:
    data: dict[str, object] = {
        "id": MediaId(new_uuid7()),
        "content_hash": HASH,
        "size_bytes": 1024,
        "cas_path": "/cas/aa/aa",
        "container": MediaContainer.FLAC,
        "codec": "flac",
        "sample_rate": 44100,
        "channels": 2,
        "duration_seconds": 180.5,
        "created_at": NOW,
    }
    data.update(overrides)
    return MediaObject(**data)  # type: ignore[arg-type]


def _candidate() -> Candidate:
    return Candidate(
        id="candidate-1",
        source_ref=Url("https://example.com/track"),
        external_id="external-1",
        title="Song",
    )


def test_media_object_valid() -> None:
    media = _media()

    assert media.container is MediaContainer.FLAC
    assert media.content_hash == HASH
    assert media.probe_artifact_id is None


def test_media_object_content_hash_must_be_lowercase_sha256() -> None:
    with pytest.raises(DomainValidationError):
        _media(content_hash="A" * 64)
    with pytest.raises(DomainValidationError):
        _media(content_hash="abc")


@pytest.mark.parametrize(
    "overrides",
    [
        {"size_bytes": -1},
        {"container": "exe"},
        {"sample_rate": 0},
        {"channels": 0},
        {"duration_seconds": -0.1},
    ],
)
def test_media_object_rejects_invalid_properties(overrides: dict[str, object]) -> None:
    with pytest.raises(DomainValidationError):
        _media(**overrides)


def test_media_object_is_frozen() -> None:
    media = _media()

    with pytest.raises(FrozenInstanceError):
        media.size_bytes = 0  # type: ignore[misc]


def test_file_instance_supports_datetime_and_posix_mtime() -> None:
    media = _media()
    by_datetime = FileInstance(
        id=FileInstanceId(new_uuid7()),
        media_id=media.id,
        path="/music/source.flac",
        role=FileRole.SOURCE,
        size_bytes=1024,
        mtime=NOW,
    )
    by_timestamp = FileInstance(
        id=FileInstanceId(new_uuid7()),
        media_id=media.id,
        path="/music/copy.flac",
        role=FileRole.LIBRARY,
        size_bytes=1024,
        mtime=1735732800.0,
    )

    assert by_datetime.mtime == NOW
    assert by_timestamp.mtime == 1735732800.0


def test_file_instance_rejects_invalid_role() -> None:
    media = _media()
    with pytest.raises(DomainValidationError):
        FileInstance(
            id=FileInstanceId(new_uuid7()),
            media_id=media.id,
            path="/music/file",
            role="bogus",  # type: ignore[arg-type]
            size_bytes=1,
            mtime=NOW,
        )


def test_intake_succeeded() -> None:
    media = _media()
    candidate = _candidate()
    intake = Intake(
        id=IntakeId(new_uuid7()),
        item_id=ItemId(new_uuid7()),
        source_ref=candidate.source_ref,
        candidate=candidate,
        downloader="yt-dlp",
        downloader_version="2025.1.1",
        source_snapshot_artifact_id=ArtifactId(new_uuid7()),
        media_id=media.id,
        status=IntakeStatus.SUCCEEDED,
        created_at=NOW,
    )

    assert intake.status is IntakeStatus.SUCCEEDED
    assert intake.media_id == media.id


def test_intake_failure_can_have_no_media() -> None:
    candidate = _candidate()
    intake = Intake(
        id=IntakeId(new_uuid7()),
        item_id=ItemId(new_uuid7()),
        source_ref=candidate.source_ref,
        candidate=candidate,
        downloader="yt-dlp",
        downloader_version="2025.1.1",
        status=IntakeStatus.FAILED,
    )

    assert intake.media_id is None
    assert intake.status is IntakeStatus.FAILED


def test_intake_rejects_invalid_status() -> None:
    candidate = _candidate()
    with pytest.raises(DomainValidationError):
        Intake(
            id=IntakeId(new_uuid7()),
            item_id=ItemId(new_uuid7()),
            source_ref=candidate.source_ref,
            candidate=candidate,
            downloader="yt-dlp",
            downloader_version="1",
            status="bogus",  # type: ignore[arg-type]
        )

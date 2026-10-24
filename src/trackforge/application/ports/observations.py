"""Observation ports and helpers shared by intrinsic analyzers.

The pipeline passes media through a tiny JSON pseudo-artifact rather than
copying audio bytes through the capability interface.  Adapters decode that
descriptor with :func:`media_input`; the bytes themselves live in CAS.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from trackforge.application.dto.observations import MediaInput
from trackforge.application.ports.execution import Capability, StepInput
from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.capabilities import ArtifactType

# The pseudo-artifact that represents an acquired media object in the DAG.
# It is supplied by ObserveItem as an initial input and is never persisted as
# an observation artifact.
MEDIA_ARTIFACT = ArtifactType.parse("media.raw.v1")

# Intrinsic observation artifact types shared by use cases and adapters.
PROBE_ARTIFACT = ArtifactType.parse("audio.probe.v1")
INITIAL_TAGS_ARTIFACT = ArtifactType.parse("initial.tags.v1")
PATH_STRUCTURE_ARTIFACT = ArtifactType.parse("path.structure.v1")
SOURCE_SNAPSHOT_ARTIFACT = ArtifactType.parse("source.snapshot.v1")
SOURCE_METADATA_ARTIFACT = ArtifactType.parse("source.metadata.v1")
FINGERPRINT_ARTIFACT = ArtifactType.parse("audio.fingerprint.v1")
ACOUSTID_ARTIFACT = ArtifactType.parse("acoustid.lookup.v1")
MUSICBRAINZ_ARTIFACT = ArtifactType.parse("musicbrainz.search.v1")
COVER_ARTIFACT = ArtifactType.parse("cover.candidates.v1")
LYRICS_ARTIFACT = ArtifactType.parse("lyrics.candidates.v1")
ASR_TRANSCRIPT_ARTIFACT = ArtifactType.parse("asr.transcript.v1")
STEMS_ARTIFACT = ArtifactType.parse("audio.stems.v1")

METADATA_ARTIFACTS = (
    FINGERPRINT_ARTIFACT,
    ACOUSTID_ARTIFACT,
    MUSICBRAINZ_ARTIFACT,
    COVER_ARTIFACT,
    LYRICS_ARTIFACT,
)


def media_input(step_input: StepInput) -> MediaInput:
    """Validate and decode the media descriptor passed to an observer."""

    if step_input.type != MEDIA_ARTIFACT:
        raise DomainValidationError(
            "observer requires the media input artifact",
            context={"artifact_type": str(step_input.type)},
        )
    try:
        return MediaInput.model_validate_json(step_input.payload)
    except ValidationError as exc:
        raise DomainValidationError(
            "media input payload is not a valid descriptor",
            context={"error": str(exc)},
        ) from exc


@runtime_checkable
class Observer(Capability, Protocol):
    """A capability that emits intrinsic observation artifacts for media."""


__all__ = [
    "ACOUSTID_ARTIFACT",
    "ASR_TRANSCRIPT_ARTIFACT",
    "COVER_ARTIFACT",
    "FINGERPRINT_ARTIFACT",
    "INITIAL_TAGS_ARTIFACT",
    "LYRICS_ARTIFACT",
    "MEDIA_ARTIFACT",
    "METADATA_ARTIFACTS",
    "MUSICBRAINZ_ARTIFACT",
    "PATH_STRUCTURE_ARTIFACT",
    "PROBE_ARTIFACT",
    "SOURCE_METADATA_ARTIFACT",
    "SOURCE_SNAPSHOT_ARTIFACT",
    "STEMS_ARTIFACT",
    "Observer",
    "media_input",
]

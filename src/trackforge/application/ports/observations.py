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
    "INITIAL_TAGS_ARTIFACT",
    "MEDIA_ARTIFACT",
    "PATH_STRUCTURE_ARTIFACT",
    "PROBE_ARTIFACT",
    "Observer",
    "media_input",
]

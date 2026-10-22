"""Acquisition ports.

An acquirer turns a chosen candidate into immutable bytes in CAS plus the
catalog records that explain where those bytes came from.  It must never write
to the original source file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from trackforge.application.dto.plan import AcquireOptions
from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.media import FileInstance, Intake, MediaObject
from trackforge.domain.model.source import Candidate


@dataclass(frozen=True, slots=True)
class IntakeResult:
    """The catalog facts created or reused by one acquisition."""

    intake: Intake
    media: MediaObject
    file_instance: FileInstance
    item_id: str
    reused_media: bool = False
    source_snapshot_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.intake, Intake):
            raise DomainValidationError("intake must be an Intake")
        if not isinstance(self.media, MediaObject):
            raise DomainValidationError("media must be a MediaObject")
        if not isinstance(self.file_instance, FileInstance):
            raise DomainValidationError("file_instance must be a FileInstance")
        object.__setattr__(self, "item_id", str(self.item_id))
        if self.source_snapshot_ref is not None:
            object.__setattr__(
                self,
                "source_snapshot_ref",
                str(self.source_snapshot_ref),
            )


@runtime_checkable
class Acquirer(Protocol):
    """Copy/download one candidate into CAS and persist its provenance."""

    id: str
    version: str

    def supports(self, candidate: Candidate) -> bool:
        """Return whether this acquirer can acquire ``candidate``."""

        ...

    async def acquire(
        self,
        candidate: Candidate,
        opts: AcquireOptions | None = None,
    ) -> IntakeResult:
        """Acquire ``candidate`` or raise a typed acquisition error."""

        ...


__all__ = ["AcquireOptions", "Acquirer", "IntakeResult"]

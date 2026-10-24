"""Publishing contract between render output and the managed library.

The adapter owns filesystem naming, sanitization, collision handling, and
atomic copies.  The use case owns catalog state and idempotency checks, so a
publisher never needs to open SQLite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.outputs import LibraryEntry, OutputBundle

DEFAULT_NAMING_TEMPLATE = "{artist} - {title} ({qualifier})"
FALLBACK_NAMING_TEMPLATE = "Unknown Artist - Unknown Title [{short_id}]"
MAX_FILENAME_BYTES = 180

CollisionPolicy = Literal["suffix"]


@dataclass(frozen=True, slots=True)
class PublishOptions:
    """Options for one managed-library publication."""

    copy: bool = True
    sidecar: bool = True
    lrc: bool = True
    collision: CollisionPolicy = "suffix"
    subfolder: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.copy, bool):
            raise DomainValidationError("copy must be a boolean")
        if not isinstance(self.sidecar, bool):
            raise DomainValidationError("sidecar must be a boolean")
        if not isinstance(self.lrc, bool):
            raise DomainValidationError("lrc must be a boolean")
        if self.collision != "suffix":
            raise DomainValidationError(
                "only the 'suffix' collision policy is supported in v1",
                context={"collision": str(self.collision)},
            )
        if self.subfolder is not None:
            if not isinstance(self.subfolder, str) or not self.subfolder.strip():
                raise DomainValidationError("subfolder must be a non-empty string")
            text = self.subfolder.strip()
            segments = [part for part in re.split(r"[\\/]+", text) if part]
            if (
                Path(text).is_absolute()
                or not segments
                or any(part in {".", ".."} for part in segments)
            ):
                raise DomainValidationError(
                    "subfolder must not contain path traversal",
                    context={"subfolder": text},
                )
            object.__setattr__(self, "subfolder", text)


@dataclass(frozen=True, slots=True)
class PublishedBundle:
    """Files written by one publish call, before catalog persistence."""

    entry: LibraryEntry
    audio_path: Path
    sidecar_path: Path | None = None
    lrc_path: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "audio_path", Path(self.audio_path))
        if self.sidecar_path is not None:
            object.__setattr__(self, "sidecar_path", Path(self.sidecar_path))
        if self.lrc_path is not None:
            object.__setattr__(self, "lrc_path", Path(self.lrc_path))


@runtime_checkable
class Publisher(Protocol):
    """Copy an output bundle into the managed library without overwriting it."""

    async def publish(
        self,
        bundle: OutputBundle,
        library_root: Path,
        naming: str,
        options: PublishOptions,
        *,
        bundle_dir: Path | None = None,
    ) -> LibraryEntry:
        """Return the published entry or raise :class:`PublishFailed`."""

        ...


__all__ = [
    "DEFAULT_NAMING_TEMPLATE",
    "FALLBACK_NAMING_TEMPLATE",
    "MAX_FILENAME_BYTES",
    "CollisionPolicy",
    "PublishOptions",
    "PublishedBundle",
    "Publisher",
]

"""Source discovery ports.

A resolver discovers what can be imported; it never downloads or copies
bytes.  Acquisition is a separate port so a source adapter can be reused with
different acquisition strategies.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.source import Candidate, SourceRef

# The extension set supported by the local MVP.  It is a policy constant, not
# a property of the domain model: adding a format there must not silently
# change which files a local source will ingest.
LOCAL_AUDIO_EXTENSIONS: frozenset[str] = frozenset({".mp3", ".m4a", ".flac", ".ogg", ".opus"})


def absolute_local_path(value: str | Path) -> Path:
    """Return an absolute path without resolving symlinks or requiring existence."""

    path = Path(value).expanduser()
    return Path(os.path.abspath(path))


def local_audio_extension(path: Path) -> str | None:
    """Return the lowercase supported extension, or ``None`` when unsupported."""

    suffix = path.suffix.lower()
    return suffix if suffix in LOCAL_AUDIO_EXTENSIONS else None


@dataclass(frozen=True, slots=True)
class CandidateList:
    """Candidates offered for one source reference, in source order."""

    source: SourceRef
    candidates: tuple[Candidate, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        candidates = tuple(self.candidates)
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, Candidate):
                raise DomainValidationError(f"candidates[{index}] must be a Candidate")
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "warnings", tuple(str(item) for item in self.warnings))

    def __len__(self) -> int:
        return len(self.candidates)

    def __bool__(self) -> bool:
        return bool(self.candidates)


@runtime_checkable
class SourceResolver(Protocol):
    """Discover candidates for a source reference without copying bytes."""

    id: str
    version: str

    def supports(self, ref: SourceRef) -> bool:
        """Return whether this resolver understands ``ref``."""

        ...

    async def resolve(
        self,
        ref: SourceRef,
        opts: Mapping[str, Any] | None = None,
    ) -> CandidateList:
        """Return candidates for ``ref`` or raise a typed source error."""

        ...


__all__ = ["CandidateList", "SourceResolver"]

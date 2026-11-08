"""Backend protocol and result objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..capabilities import Capabilities
from ..formats import ContainerFormat, DetectedFormat
from ..model import Artwork, MediaMetadata


@dataclass(slots=True)
class ReadResult:
    """Normalized metadata plus the parts that are not text."""

    metadata: MediaMetadata
    artwork: list[Artwork] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class MetadataBackend(Protocol):
    """A reader for one family of containers.

    Writing is a separate protocol on purpose: not every backend that can read
    a format can write it, and the facade should be able to expose that.
    """

    name: str
    containers: frozenset[ContainerFormat]

    def read(self, path: Path, detected: DetectedFormat) -> ReadResult:
        """Read normalized metadata from ``path``."""
        ...

    def capabilities(self, container: ContainerFormat) -> Capabilities:
        """Describe what the backend can store for ``container``."""
        ...

"""Public facade for reading a media file's metadata.

The facade is deliberately thin: it sniffs the container, asks the registry for
a backend, and turns the backend's normalized result into an object with the
common, audio and video facets.  Format specifics stay in the backends.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path

from .backends.base import MetadataBackend
from .capabilities import Capabilities, WritePolicy, WriteReport
from .errors import NoAudioStreamError, NoVideoStreamError
from .formats import ContainerFormat, DetectedFormat, sniff
from .model import (
    ArtworkCollection,
    AudioMetadata,
    MediaMetadata,
    VideoMetadata,
)
from .registry import BackendRegistry, default_registry


@dataclass(slots=True)
class MediaFile(MediaMetadata):
    """A media file's normalized metadata plus its container context."""

    path: Path | None = None
    detected: DetectedFormat | None = None
    artwork: ArtworkCollection = field(default_factory=ArtworkCollection)
    warnings: list[str] = field(default_factory=list)
    _backend: MetadataBackend | None = field(default=None, repr=False)

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        registry: BackendRegistry | None = None,
    ) -> MediaFile:
        """Read metadata from ``path``, detecting the container automatically."""
        source = Path(path)
        detected = sniff(source)
        backend = (registry or default_registry()).resolve(
            detected,
            path=str(source),
        )
        result = backend.read(source, detected)

        common_fields = {
            item.name: getattr(result.metadata, item.name)
            for item in fields(MediaMetadata)
        }
        return cls(
            **common_fields,
            path=source,
            detected=detected,
            artwork=ArtworkCollection(result.artwork),
            warnings=list(result.warnings),
            _backend=backend,
        )

    @property
    def container(self) -> ContainerFormat | None:
        return self.detected.container if self.detected is not None else None

    @property
    def mime_type(self) -> str | None:
        return self.detected.mime_type if self.detected is not None else None

    @property
    def capabilities(self) -> Capabilities | None:
        if self._backend is None or self.detected is None:
            return None
        return self._backend.capabilities(self.detected.container)

    def require_audio(self) -> AudioMetadata:
        """Return the audio facet or raise if the document has no audio."""
        if self.audio is None:
            raise NoAudioStreamError(str(self.path))
        return self.audio

    def require_video(self) -> VideoMetadata:
        """Return the video facet or raise if the document has no video."""
        if self.video is None:
            raise NoVideoStreamError(str(self.path))
        return self.video

    def save(self, *, policy: WritePolicy | None = None) -> WriteReport:
        """Write changes back to the file.

        Writing is the next implementation step; the method exists now so the
        facade's shape is stable.
        """
        raise NotImplementedError("metadata writing is not implemented yet")

    def __repr__(self) -> str:
        title = self.title or "<untitled>"
        return f"MediaFile(path={self.path!r}, title={title!r}, kind={self.kind.value!r})"


__all__ = ["MediaFile"]

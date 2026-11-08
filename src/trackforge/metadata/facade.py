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
from .errors import (
    MetadataWriteError,
    NoAudioStreamError,
    NoVideoStreamError,
    UnsupportedFormatError,
)
from .formats import ContainerFormat, DetectedFormat, sniff
from .model import (
    ArtworkCollection,
    AudioMetadata,
    MediaMetadata,
    VideoMetadata,
)
from .probe import probe_streams
from .registry import BackendRegistry, default_registry


def _correct_facets(
    path: Path,
    detected: DetectedFormat,
    backend: MetadataBackend,
    metadata: MediaMetadata,
) -> None:
    """Use real stream information when the container is ambiguous.

    Mutagen's MP4/ASF readers know about audio properties but not whether a
    video stream exists, so an MP4 with only video would still get an audio
    facet.  ffprobe is optional; without it the heuristic remains.
    """
    if backend.name != "mutagen":
        return
    if detected.container not in {ContainerFormat.MP4, ContainerFormat.ASF}:
        return
    probe = probe_streams(path)
    if probe is None or (not probe.has_audio and not probe.has_video):
        return
    if probe.has_audio and metadata.audio is None:
        metadata.audio = AudioMetadata()
    if not probe.has_audio:
        metadata.audio = None
    if probe.has_video and metadata.video is None:
        metadata.video = VideoMetadata()
    if not probe.has_video:
        metadata.video = None


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
        _correct_facets(source, detected, backend, result.metadata)

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

        ``None``/empty values are left untouched by default; pass
        ``WritePolicy(remove_missing=True)`` to delete them.  The write is
        atomic by default: tags are applied to a temporary copy and the
        original is replaced only after verification succeeds.
        """
        if self.path is None or self.detected is None or self._backend is None:
            raise MetadataWriteError("<memory>", "no source file to write")
        write = getattr(self._backend, "write", None)
        if write is None:
            raise UnsupportedFormatError(str(self.path))
        report = write(
            self.path,
            self.detected,
            self,
            policy=policy or WritePolicy(),
        )
        self.warnings.extend(report.warnings)
        return report

    def __repr__(self) -> str:
        title = self.title or "<untitled>"
        return f"MediaFile(path={self.path!r}, title={title!r}, kind={self.kind.value!r})"


__all__ = ["MediaFile"]

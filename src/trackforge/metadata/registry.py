"""Backend selection."""

from __future__ import annotations

from collections.abc import Iterable

from .backends.base import MetadataBackend
from .errors import UnsupportedFormatError
from .formats import DetectedFormat


class BackendRegistry:
    """Pick the first registered backend that supports a container."""

    def __init__(self, backends: Iterable[MetadataBackend] = ()) -> None:
        self._backends: list[MetadataBackend] = list(backends)

    def register(self, backend: MetadataBackend) -> None:
        self._backends.append(backend)

    @property
    def backends(self) -> tuple[MetadataBackend, ...]:
        return tuple(self._backends)

    def resolve(
        self,
        detected: DetectedFormat,
        *,
        path: str | None = None,
    ) -> MetadataBackend:
        for backend in self._backends:
            if not getattr(backend, "available", True):
                continue
            if detected.container in backend.containers:
                return backend
        raise UnsupportedFormatError(path or detected.container.value)


def default_registry() -> BackendRegistry:
    """Registry used by :class:`~trackforge.metadata.facade.MediaFile`.

    Imported lazily so importing the metadata model does not pull in mutagen
    when the caller only needs container sniffing.
    """
    from .backends.ffmpeg import FfmpegBackend
    from .backends.mutagen import MutagenBackend

    backends: list[MetadataBackend] = [MutagenBackend()]
    ffmpeg = FfmpegBackend()
    if ffmpeg.available:
        backends.append(ffmpeg)
    return BackendRegistry(backends)

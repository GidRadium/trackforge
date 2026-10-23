"""Ports for materializing audio and writing/verifying canonical tags.

The renderer and tagger are synchronous boundary adapters: the application use
case runs them in a worker thread.  Keeping ffmpeg and mutagen behind these
protocols is what allows the core layers to remain free of GPL tag libraries.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from trackforge.application.dto.rendering import (
    CoverArt,
    RenderedAudio,
    RenderOptions,
    TagMismatch,
    TagSet,
)
from trackforge.domain.model.media import MediaObject
from trackforge.domain.model.outputs import ResolvedMetadata


@runtime_checkable
class Renderer(Protocol):
    """Produce an untagged audio file for a target format."""

    def render(
        self,
        media: MediaObject,
        decisions: ResolvedMetadata,
        options: RenderOptions,
        workdir: Path,
    ) -> RenderedAudio: ...


@runtime_checkable
class TagReader(Protocol):
    """Read canonical values from a media file without modifying it."""

    def read(self, path: Path) -> TagSet: ...


@runtime_checkable
class TagWriter(Protocol):
    """Write canonical values and confirm them with read-back."""

    def write(
        self,
        path: Path,
        tagset: TagSet,
        cover: CoverArt | None,
        options: RenderOptions,
    ) -> None: ...

    def verify(
        self,
        path: Path,
        tagset: TagSet,
        cover: CoverArt | None,
    ) -> list[TagMismatch]: ...


__all__ = ["Renderer", "TagReader", "TagWriter"]

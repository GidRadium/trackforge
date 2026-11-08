"""Metadata backends."""

from .base import MetadataBackend, ReadResult, WritableMetadataBackend
from .ffmpeg import FfmpegBackend
from .mutagen import MutagenBackend

__all__ = [
    "FfmpegBackend",
    "MetadataBackend",
    "MutagenBackend",
    "ReadResult",
    "WritableMetadataBackend",
]

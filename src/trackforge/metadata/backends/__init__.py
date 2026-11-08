"""Metadata backends."""

from .base import MetadataBackend, ReadResult
from .mutagen import MutagenBackend

__all__ = ["MetadataBackend", "MutagenBackend", "ReadResult"]

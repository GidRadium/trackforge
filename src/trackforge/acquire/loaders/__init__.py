"""Loader contracts and built-in loaders."""

# pyright: standard

from .base import (
    LoaderCapabilities,
    LoaderContext,
    MediaLoader,
    default_recommendations,
    select_best_thumbnail,
)
from .ytdlp import YtDlpLoader

__all__ = [
    "LoaderCapabilities",
    "LoaderContext",
    "MediaLoader",
    "YtDlpLoader",
    "default_recommendations",
    "select_best_thumbnail",
]

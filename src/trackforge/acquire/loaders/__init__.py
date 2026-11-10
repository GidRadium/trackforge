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
from .ytdlp_options import (
    build_ydl_options,
    create_youtube_dl,
    deno_js_runtimes,
    prepare_ydl_options,
)

__all__ = [
    "LoaderCapabilities",
    "LoaderContext",
    "MediaLoader",
    "YtDlpLoader",
    "build_ydl_options",
    "create_youtube_dl",
    "default_recommendations",
    "deno_js_runtimes",
    "prepare_ydl_options",
    "select_best_thumbnail",
]

"""Loader contracts and built-in loaders."""

# pyright: standard

from .base import (
    LoaderCapabilities,
    LoaderContext,
    MediaLoader,
    default_recommendations,
    select_best_thumbnail,
)
from .youtube import YouTubeLoader
from .ytdlp import YtDlpLoader
from .ytdlp_errors import (
    classify_ytdlp_error,
    humanize_error,
    is_retryable,
    map_ytdlp_error,
)
from .ytdlp_mapper import (
    YtDlpMapper,
    best_audio_variant,
    best_variant,
    best_video_variant,
    build_variants,
    dedupe_and_sort,
    map_audio,
    map_progressive,
    map_subtitles,
    map_thumbnails,
    pair_video_with_audio,
    parse_partial_date,
)
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
    "YouTubeLoader",
    "YtDlpLoader",
    "YtDlpMapper",
    "best_audio_variant",
    "best_variant",
    "best_video_variant",
    "build_variants",
    "build_ydl_options",
    "classify_ytdlp_error",
    "create_youtube_dl",
    "dedupe_and_sort",
    "default_recommendations",
    "deno_js_runtimes",
    "humanize_error",
    "is_retryable",
    "map_audio",
    "map_progressive",
    "map_subtitles",
    "map_thumbnails",
    "map_ytdlp_error",
    "pair_video_with_audio",
    "parse_partial_date",
    "prepare_ydl_options",
    "select_best_thumbnail",
]

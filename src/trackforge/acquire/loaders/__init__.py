"""Loader contracts and built-in loaders."""

# pyright: standard

from .base import (
    LoaderCapabilities,
    LoaderContext,
    MediaLoader,
    default_recommendations,
    dynamic_range_rank,
    rank_thumbnails,
    select_best_thumbnail,
    video_codec_rank,
)
from .soundcloud import SoundCloudLoader
from .yandex_music import YandexMusicLoader
from .youtube import YouTubeLoader
from .ytdlp import (
    DownloadPlan,
    ItemPlan,
    YtDlpLoader,
    YtDlpPlanner,
    YtDlpProgressHook,
)
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
    "DownloadPlan",
    "ItemPlan",
    "LoaderCapabilities",
    "LoaderContext",
    "MediaLoader",
    "SoundCloudLoader",
    "YandexMusicLoader",
    "YouTubeLoader",
    "YtDlpLoader",
    "YtDlpMapper",
    "YtDlpPlanner",
    "YtDlpProgressHook",
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
    "dynamic_range_rank",
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
    "rank_thumbnails",
    "select_best_thumbnail",
    "video_codec_rank",
]

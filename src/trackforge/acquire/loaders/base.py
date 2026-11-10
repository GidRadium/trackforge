"""Loader contracts shared by every acquisition source."""

# pyright: standard

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import ClassVar
from urllib.parse import urlsplit

from ..config import SourceProfile
from ..errors import AcquireError, SearchNotSupported
from ..models import (
    DownloadRequest,
    DownloadResult,
    DownloadVariant,
    MediaItem,
    MediaKind,
    Recommendations,
    SearchCandidate,
    SourceInfo,
    SourceRef,
    StreamInfo,
    StreamRole,
    Thumbnail,
    ThumbnailAsset,
    UrlHint,
)
from ..progress import CancellationToken, NullProgressSink, ProgressSink
from ..staging import StagingArea


@dataclass(frozen=True, slots=True)
class LoaderCapabilities:
    """What a loader can do, without performing any network access."""

    search: bool = False
    playlists: bool = False
    posts: bool = False
    images: bool = False
    documents: bool = False
    live: bool = False
    subtitles: bool = False
    cookies: bool = False
    proxy: bool = False
    auth: bool = False


def _default_logger() -> logging.Logger:
    return logging.getLogger("trackforge.acquire")


@dataclass(slots=True)
class LoaderContext:
    """Per-call dependencies passed to a loader.

    ``http`` and ``cache`` are intentionally opaque at L0: their concrete
    implementations arrive with the download layer.  Loaders must not create
    their own global clients.
    """

    job_id: str
    profile: SourceProfile
    progress: ProgressSink = field(default_factory=NullProgressSink)
    cancel: CancellationToken = field(default_factory=CancellationToken)
    staging: StagingArea | None = None
    http: object | None = None
    logger: logging.Logger = field(default_factory=_default_logger)
    cache: object | None = None


class MediaLoader(ABC):
    """Base class for a source adapter.

    URL checks are strictly offline.  ``get_info`` and ``download`` are the
    only methods allowed to touch the network, and they receive all
    dependencies through :class:`LoaderContext`.
    """

    name: ClassVar[str]
    config_key: ClassVar[str | None] = None
    required_plugins: ClassVar[tuple[str, ...]] = ()
    capabilities: ClassVar[LoaderCapabilities] = LoaderCapabilities()
    priority: ClassVar[int] = 100

    @abstractmethod
    def supports_url(self, url: str) -> bool:
        """Return whether this loader can attempt ``url`` (offline only)."""

    def classify_url(self, url: str) -> UrlHint:
        """Return an offline hint; the final shape comes from ``get_info``."""
        del url
        return UrlHint.UNKNOWN

    @abstractmethod
    def get_info(
        self,
        ref: SourceRef,
        *,
        ctx: LoaderContext,
        flat: bool | None = False,
        limit: int | None = None,
    ) -> SourceInfo:
        """Resolve a source reference into normalized metadata."""

    @abstractmethod
    def download(
        self,
        request: DownloadRequest,
        *,
        ctx: LoaderContext,
    ) -> DownloadResult:
        """Download the selected items into staging and commit them."""

    def search(
        self,
        query: str,
        *,
        limit: int,
        ctx: LoaderContext,
    ) -> list[SearchCandidate]:
        """Search this source.  Sources without search raise."""
        del query, limit, ctx
        raise SearchNotSupported(self.name)

    def choose_thumbnail(
        self,
        item: MediaItem,
        *,
        ctx: LoaderContext,
    ) -> Thumbnail | None:
        del ctx
        candidates = list(item.thumbnails)
        if item.thumbnail is not None:
            candidates.append(item.thumbnail)
        return select_best_thumbnail(candidates)

    def fetch_thumbnail(
        self,
        thumbnail: Thumbnail,
        *,
        ctx: LoaderContext,
        max_bytes: int | None = None,
    ) -> ThumbnailAsset | None:
        """Fetch thumbnail bytes.

        L0 has no HTTP client, so the default is a safe ``None``.  The real
        implementation arrives with the HTTP layer and must honour
        ``thumbnail.http_headers`` and ``max_bytes``.
        """
        del thumbnail, ctx, max_bytes
        return None

    def recommend(
        self,
        item: MediaItem,
        *,
        ctx: LoaderContext,
    ) -> Recommendations:
        del ctx
        return default_recommendations(item)

    @classmethod
    def profile_name(cls) -> str:
        """Configuration key for this loader (``config_key`` or ``name``)."""
        return cls.config_key or cls.name

    def is_ready(self, profile: SourceProfile) -> bool:
        """Whether the profile contains enough configuration to run."""
        del profile
        return True

    def unready_error(self, profile: SourceProfile) -> AcquireError | None:
        """Typed error explaining why this loader cannot run.

        By default an unready loader is simply skipped so a lower-priority
        fallback can handle the URL.  A site-specific loader that must never
        fall through to the generic yt-dlp extractor overrides this and
        returns a user-facing error instead.
        """
        del profile
        return None


def select_best_thumbnail(
    candidates: Sequence[Thumbnail],
    *,
    min_width: int = 320,
    min_height: int = 180,
) -> Thumbnail | None:
    """Pick the largest plausible thumbnail, preferring 16:9 and JPEG/PNG.

    This is a pure, offline helper.  It does not probe URLs.
    """
    best: Thumbnail | None = None
    best_score = float("-inf")
    best_by_url: dict[str, tuple[float, Thumbnail]] = {}
    for thumbnail in candidates:
        url = thumbnail.url.strip()
        if not url:
            continue
        # Inspect the filename, not the whole URL: Yandex covers live on
        # ``avatars.yandex.net`` and must not be mistaken for a placeholder.
        try:
            name = urlsplit(url).path.rsplit("/", 1)[-1].lower()
        except ValueError:
            continue
        if any(
            marker in name
            for marker in ("no_thumbnail", "no-thumbnail", "placeholder", "avatar")
        ):
            continue
        width = thumbnail.width or 0
        height = thumbnail.height or 0
        if width and height and (width < min_width or height < min_height):
            continue
        area = width * height
        ratio_bonus = (
            1.0
            if width and height and abs(width / height - 16 / 9) < 0.1
            else 0.9
        )
        ext = (thumbnail.ext or "").lower().lstrip(".")
        format_bonus = 1.05 if ext in {"jpg", "jpeg", "png"} else 1.0
        score = area * ratio_bonus * format_bonus + thumbnail.preference
        previous = best_by_url.get(url)
        if previous is None or score > previous[0]:
            best_by_url[url] = (score, thumbnail)
        if best is None or score > best_score:
            best = thumbnail
            best_score = score
    # If the same URL appeared with different dimensions, the highest-scoring
    # copy wins rather than the first one encountered.
    if best_by_url:
        best = max(best_by_url.values(), key=lambda pair: pair[0])[1]
    return best


def _stream(variant: DownloadVariant, role: StreamRole) -> StreamInfo | None:
    return next(
        (stream for stream in variant.streams if stream.role is role),
        None,
    )


def _codec_stem(codec: str | None) -> str:
    if not codec:
        return ""
    return codec.split(".", 1)[0].strip().lower()


def default_recommendations(item: MediaItem) -> Recommendations:
    """Choose one usable variant per media use from the item's variants.

    The generic policy mirrors the source table in the architecture: prefer
    MP4/H.264/AAC for video and M4A/AAC for audio, then fall back to the best
    original stream.  Site-specific loaders may override this.
    """
    variants = [
        variant
        for variant in item.variants
        if variant.downloadable
        and variant.unavailable_reason is None
        and not any(stream.drm for stream in variant.streams)
    ]

    def first(*kinds: MediaKind) -> str | None:
        for kind in kinds:
            for variant in variants:
                if variant.kind is kind:
                    return variant.id
        return None

    def video_score(variant: DownloadVariant) -> tuple[float, ...]:
        video = _stream(variant, StreamRole.VIDEO)
        audio = _stream(variant, StreamRole.AUDIO)
        container = (variant.container or "").lower()
        video_codec = _codec_stem(video.codec if video else None)
        audio_codec = _codec_stem(audio.codec if audio else None)
        return (
            1.0 if container == "mp4" else 0.0,
            1.0 if video_codec in {"avc1", "h264"} else 0.0,
            1.0 if audio_codec in {"aac", "mp4a"} else 0.0,
            float(audio.language_preference or 0.0) if audio else 0.0,
            float(video.height or 0) if video else 0.0,
            float(video.fps or 0.0) if video else 0.0,
            float(video.bitrate_kbps or 0.0) if video else 0.0,
        )

    def audio_score(variant: DownloadVariant) -> tuple[float, ...]:
        audio = _stream(variant, StreamRole.AUDIO)
        container = (variant.container or "").lower()
        codec = _codec_stem(audio.codec if audio else None)
        return (
            1.0 if container in {"m4a", "mp4"} else 0.0,
            1.0 if codec in {"aac", "mp4a"} else 0.0,
            float(audio.language_preference or 0.0) if audio else 0.0,
            float(audio.bitrate_kbps or 0.0) if audio else 0.0,
            float(audio.filesize or 0) if audio else 0.0,
        )

    def best(
        *kinds: MediaKind,
        score: Callable[[DownloadVariant], tuple[float, ...]],
    ) -> str | None:
        candidates = [variant for variant in variants if variant.kind in kinds]
        if not candidates:
            return None
        return max(candidates, key=score).id

    return Recommendations(
        audio=best(
            MediaKind.AUDIO,
            score=audio_score,
        )
        or best(
            MediaKind.AUDIO,
            MediaKind.AUDIO_VIDEO,
            score=audio_score,
        ),
        video=best(
            MediaKind.AUDIO_VIDEO,
            MediaKind.VIDEO,
            score=video_score,
        ),
        image=first(MediaKind.IMAGE, MediaKind.ANIMATED_IMAGE),
        document=first(MediaKind.DOCUMENT),
    )

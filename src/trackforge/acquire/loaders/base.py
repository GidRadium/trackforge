"""Loader contracts shared by every acquisition source."""

# pyright: standard

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import ClassVar
from urllib.parse import urlsplit

from ..config import SourceProfile
from ..errors import AcquireError, CancelledError, SearchNotSupported
from ..models import (
    DownloadRequest,
    DownloadResult,
    DownloadVariant,
    MediaItem,
    MediaKind,
    Recommendations,
    ResolutionState,
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
from ..redaction import redact_text
from ..staging import StagingArea
from ..thumbnail_cache import ThumbnailCache
from .thumbnail_probe import parse_image_size

MAX_THUMBNAIL_BYTES = 20_000_000


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
    thumbnail_cache: ThumbnailCache | None = None


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

    def thumbnail_candidates(
        self,
        item: MediaItem,
        *,
        ctx: LoaderContext,
    ) -> tuple[Thumbnail, ...]:
        """Return potential thumbnails ordered best to worst, offline.

        Source adapters may add synthetic candidates (for example a known
        high-resolution CDN URL), but this method must not perform any network
        request or availability check.
        """
        del ctx
        candidates = list(item.thumbnails)
        if item.thumbnail is not None:
            candidates.append(item.thumbnail)
        return rank_thumbnails(candidates)

    def choose_thumbnail(
        self,
        item: MediaItem,
        *,
        ctx: LoaderContext,
    ) -> Thumbnail | None:
        """Pick a thumbnail; only a full resolve may fetch image bytes.

        Flat collection entries stay cheap: the best metadata candidate is
        returned without any network request.  A full resolve walks the
        ordered candidates and stops at the first one that can be fetched.
        """
        candidates = self.thumbnail_candidates(item, ctx=ctx)
        if not candidates:
            return None
        if item.resolution is ResolutionState.FLAT:
            return candidates[0]
        if ctx.thumbnail_cache is None:
            # Without a cache there is nowhere to keep the fetched bytes for
            # a later sidecar; stay offline and return the best candidate.
            return candidates[0]
        return self._resolve_thumbnail(candidates, ctx=ctx)

    def _resolve_thumbnail(
        self,
        candidates: Sequence[Thumbnail],
        *,
        ctx: LoaderContext,
    ) -> Thumbnail | None:
        for thumbnail in candidates:
            asset = self.fetch_thumbnail(thumbnail, ctx=ctx)
            if asset is None:
                continue
            return replace(
                thumbnail,
                width=asset.width,
                height=asset.height,
                ext=asset.ext or thumbnail.ext,
                mime=asset.mime,
                filesize=len(asset.data),
            )
        return candidates[0] if candidates else None

    def fetch_thumbnail(
        self,
        thumbnail: Thumbnail,
        *,
        ctx: LoaderContext,
        max_bytes: int | None = None,
    ) -> ThumbnailAsset | None:
        """Fetch and cache thumbnail bytes through the loader's transport.

        The orchestration lives here so every loader behaves the same way:
        cache first, one bounded fetch on a miss, parse the real image type,
        store the asset, and return it.  Adapters only implement
        :meth:`_fetch_thumbnail_bytes` (or provide ``ctx.http``).
        """
        limit = (
            max_bytes
            if max_bytes is not None
            else MAX_THUMBNAIL_BYTES
        )
        if limit < 1:
            return None
        if ctx.thumbnail_cache is not None:
            cached = ctx.thumbnail_cache.get(thumbnail.url)
            if cached is not None:
                if max_bytes is not None and len(cached.data) > max_bytes:
                    return None
                return cached

        ctx.cancel.raise_if_cancelled()
        try:
            if ctx.http is not None:
                data = self._fetch_thumbnail_from_client(thumbnail, ctx=ctx)
                if data is None:
                    return None
            else:
                data = self._fetch_thumbnail_bytes(
                    thumbnail,
                    ctx=ctx,
                    max_bytes=limit,
                )
        except CancelledError:
            raise
        except NotImplementedError:
            return None
        except Exception as exc:  # noqa: BLE001 - thumbnail is optional
            ctx.logger.debug(
                "thumbnail fetch failed for %s: %s",
                redact_text(thumbnail.url),
                exc,
            )
            return None
        if not data:
            return None
        if len(data) > limit:
            return None
        size = parse_image_size(data)
        asset = ThumbnailAsset(
            data=data,
            mime=(
                size.mime
                if size is not None
                else (thumbnail.mime or "application/octet-stream")
            ),
            width=size.width if size is not None else thumbnail.width,
            height=size.height if size is not None else thumbnail.height,
            ext=size.ext if size is not None else (thumbnail.ext or "jpg"),
        )
        if ctx.thumbnail_cache is not None:
            ctx.thumbnail_cache.put(thumbnail.url, asset)
        return asset

    @staticmethod
    def _fetch_thumbnail_from_client(
        thumbnail: Thumbnail,
        *,
        ctx: LoaderContext,
    ) -> bytes | None:
        """Use an injected HTTP client instead of a loader-specific transport."""
        client = ctx.http
        if client is None:
            return None
        for method_name in ("get_bytes", "get", "read"):
            method = getattr(client, method_name, None)
            if not callable(method):
                continue
            result = method(thumbnail.url)
            if isinstance(result, (bytes, bytearray)):
                return bytes(result)
            return None
        return None

    def _fetch_thumbnail_bytes(
        self,
        thumbnail: Thumbnail,
        *,
        ctx: LoaderContext,
        max_bytes: int,
    ) -> bytes:
        """Return raw image bytes; adapters override this for their transport."""
        del thumbnail, ctx, max_bytes
        raise NotImplementedError("loader has no thumbnail transport")

    def thumbnail_sidecar(
        self,
        item: MediaItem,
        *,
        ctx: LoaderContext,
    ) -> Thumbnail | None:
        """Return a thumbnail TrackForge should write as a sidecar.

        The default is cache-aware: if a full resolve already fetched the
        chosen thumbnail, the downloader can write those exact bytes.  When
        there is no cached asset, return ``None`` and let the underlying
        downloader handle its own thumbnail behavior.
        """
        thumbnail = item.thumbnail
        if thumbnail is None or not thumbnail.url.strip():
            return None
        if ctx.thumbnail_cache is None:
            return None
        if ctx.thumbnail_cache.get(thumbnail.url) is None:
            return None
        return thumbnail

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

    def requires_auth(self, url: str) -> bool:
        """Whether a network call for ``url`` needs configured cookies."""
        del url
        return False

    def unready_error(self, profile: SourceProfile) -> AcquireError | None:
        """Typed error explaining why this loader cannot run.

        By default an unready loader is simply skipped so a lower-priority
        fallback can handle the URL.  A site-specific loader that must never
        fall through to the generic yt-dlp extractor overrides this and
        returns a user-facing error instead.
        """
        del profile
        return None


def _thumbnail_score(
    thumbnail: Thumbnail,
    *,
    min_width: int,
    min_height: int,
) -> float | None:
    url = thumbnail.url.strip()
    if not url:
        return None
    # Inspect the filename, not the whole URL: Yandex covers live on
    # ``avatars.yandex.net`` and must not be mistaken for a placeholder.
    try:
        name = urlsplit(url).path.rsplit("/", 1)[-1].lower()
    except ValueError:
        return None
    if any(
        marker in name
        for marker in ("no_thumbnail", "no-thumbnail", "placeholder", "avatar")
    ):
        return None
    width = thumbnail.width or 0
    height = thumbnail.height or 0
    if width and height and (width < min_width or height < min_height):
        return None
    area = width * height
    ratio_bonus = (
        1.0
        if width and height and abs(width / height - 16 / 9) < 0.1
        else 0.9
    )
    ext = (thumbnail.ext or "").lower().lstrip(".")
    format_bonus = 1.05 if ext in {"jpg", "jpeg", "png"} else 1.0
    return area * ratio_bonus * format_bonus + thumbnail.preference


def rank_thumbnails(
    candidates: Sequence[Thumbnail],
    *,
    min_width: int = 320,
    min_height: int = 180,
) -> tuple[Thumbnail, ...]:
    """Return unique plausible thumbnails ordered best to worst.

    This is a pure, offline helper.  It does not probe URLs or check whether
    a thumbnail is reachable.
    """
    scored: dict[str, tuple[float, Thumbnail]] = {}
    for thumbnail in candidates:
        score = _thumbnail_score(
            thumbnail,
            min_width=min_width,
            min_height=min_height,
        )
        if score is None:
            continue
        url = thumbnail.url.strip()
        previous = scored.get(url)
        if previous is None or score > previous[0]:
            scored[url] = (score, thumbnail)
    ordered = sorted(
        scored.values(),
        key=lambda pair: pair[0],
        reverse=True,
    )
    return tuple(thumbnail for _score, thumbnail in ordered)


def select_best_thumbnail(
    candidates: Sequence[Thumbnail],
    *,
    min_width: int = 320,
    min_height: int = 180,
) -> Thumbnail | None:
    """Pick the largest plausible thumbnail, preferring 16:9 and JPEG/PNG.

    This is a pure, offline helper.  It does not probe URLs.
    """
    ranked = rank_thumbnails(
        candidates,
        min_width=min_width,
        min_height=min_height,
    )
    return ranked[0] if ranked else None


def _stream(variant: DownloadVariant, role: StreamRole) -> StreamInfo | None:
    return next(
        (stream for stream in variant.streams if stream.role is role),
        None,
    )


def _codec_stem(codec: str | None) -> str:
    if not codec:
        return ""
    return codec.split(".", 1)[0].strip().lower()


_VIDEO_CODEC_ALIASES = {
    "avc1": "h264",
    "avc3": "h264",
    "h264": "h264",
    "hev1": "h265",
    "hvc1": "h265",
    "h265": "h265",
    "hevc": "h265",
    "vp09": "vp9",
    "vp9": "vp9",
    "av01": "av1",
    "av1": "av1",
}
_VIDEO_CODEC_RANK = {
    "h264": 0,
    "h265": 1,
    "vp9": 2,
    "av1": 3,
}


def video_codec_rank(codec: str | None) -> int:
    """Return a compatibility rank; lower is preferred at equal quality."""
    normalized = _VIDEO_CODEC_ALIASES.get(_codec_stem(codec), _codec_stem(codec))
    return _VIDEO_CODEC_RANK.get(normalized, len(_VIDEO_CODEC_RANK))


def dynamic_range_rank(value: str | None) -> int:
    """Rank HDR above SDR without treating unknown metadata as HDR."""
    normalized = (value or "").strip().lower()
    if normalized in {"", "sdr", "unknown"}:
        return 0
    if "dolby" in normalized or normalized == "dv":
        return 2
    return 1


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
        video_codec = video.codec if video else None
        audio_codec = _codec_stem(audio.codec if audio else None)
        return (
            float(video.height or 0) if video else 0.0,
            float(video.fps or 0.0) if video else 0.0,
            float(dynamic_range_rank(video.dynamic_range if video else None)),
            1.0 if container == "mp4" else 0.0,
            float(-video_codec_rank(video_codec)),
            1.0 if audio_codec in {"aac", "mp4a"} else 0.0,
            float(audio.language_preference or 0.0) if audio else 0.0,
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

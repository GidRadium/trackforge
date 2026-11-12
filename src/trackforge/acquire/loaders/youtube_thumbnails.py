"""YouTube thumbnail URL policy.

This module is the only place that knows about ``i.ytimg.com`` URL shapes and
well-known YouTube thumbnail dimensions.  Generic yt-dlp mapping stays
source-agnostic.
"""

# pyright: standard

from __future__ import annotations

from dataclasses import replace
from urllib.parse import quote, urlsplit, urlunsplit

from ..models import MediaItem, Thumbnail
from .base import rank_thumbnails

_YOUTUBE_THUMBNAIL_SIZES = {
    "maxresdefault": (1280, 720),
    "maxresdefault.jpg": (1280, 720),
    "maxresdefault.webp": (1280, 720),
    "sddefault": (640, 480),
    "sddefault.jpg": (640, 480),
    "sddefault.webp": (640, 480),
    "hqdefault": (480, 360),
    "hqdefault.jpg": (480, 360),
    "hqdefault.webp": (480, 360),
    "hq720": (1280, 720),
    "hq720.jpg": (1280, 720),
    "hq720.webp": (1280, 720),
    "mqdefault": (320, 180),
    "mqdefault.jpg": (320, 180),
    "mqdefault.webp": (320, 180),
    "default": (120, 90),
    "default.jpg": (120, 90),
    "default.webp": (120, 90),
}

_THUMBNAIL_FALLBACKS = (
    (
        "https://i.ytimg.com/vi/{id}/maxresdefault.jpg",
        "youtube:maxres",
        1280,
        720,
    ),
    (
        "https://i.ytimg.com/vi/{id}/sddefault.jpg",
        "youtube:sd",
        640,
        480,
    ),
)


def youtube_thumbnail_size(url: str) -> tuple[int, int] | None:
    """Return known YouTube CDN dimensions instead of trusting bad metadata.

    YouTube reports ``maxresdefault`` as 1920x1080, but the CDN actually
    serves a 1280x720 JPEG/WebP.  The same normalization applies to the other
    well-known thumbnail names.
    """
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        name = parsed.path.rsplit("/", 1)[-1].lower()
    except ValueError:
        return None
    if not _is_youtube_cdn_host(host):
        return None
    return _YOUTUBE_THUMBNAIL_SIZES.get(name)


def normalize_thumbnail_url(url: str) -> str:
    """Drop cache-busting query/fragment from YouTube's thumbnail CDN URLs.

    ``i.ytimg.com`` serves the same image from ``.../maxresdefault.jpg``
    regardless of ``sqp``/``rs`` parameters.  Other sources may use signed
    thumbnail URLs, so their query strings are preserved.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    host = (parsed.hostname or "").lower()
    if _is_youtube_cdn_host(host):
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return url


def normalize_youtube_thumbnail(thumbnail: Thumbnail) -> Thumbnail:
    """Normalize one thumbnail URL and apply known YouTube dimensions."""
    url = normalize_thumbnail_url(thumbnail.url)
    size = youtube_thumbnail_size(url)
    if size is None:
        return replace(thumbnail, url=url)
    return replace(thumbnail, url=url, width=size[0], height=size[1])


def youtube_thumbnail_candidates(item: MediaItem) -> tuple[Thumbnail, ...]:
    """Return potential YouTube thumbnails ordered best to worst, offline.

    Existing metadata candidates are combined with the well-known
    ``maxresdefault``/``sddefault`` URLs.  Existence is deliberately not
    checked here: a full resolve fetches candidates in order and stops at the
    first working image.
    """
    candidates: list[Thumbnail] = []
    seen: set[str] = set()
    for thumbnail in (*item.thumbnails, item.thumbnail):
        if thumbnail is None or not thumbnail.url.strip():
            continue
        normalized = normalize_youtube_thumbnail(thumbnail)
        if normalized.url in seen:
            continue
        seen.add(normalized.url)
        candidates.append(normalized)

    if item.id and item.position in (None, 1):
        for template, source, width, height in _THUMBNAIL_FALLBACKS:
            url = template.format(id=quote(item.id, safe=""))
            if url in seen:
                continue
            seen.add(url)
            candidates.append(
                Thumbnail(
                    url=url,
                    width=width,
                    height=height,
                    ext="jpg",
                    source=source,
                )
            )
    return rank_thumbnails(candidates)


def _is_youtube_cdn_host(host: str) -> bool:
    return (
        host in {"i.ytimg.com", "img.youtube.com"}
        or host.endswith(".ytimg.com")
    )


__all__ = [
    "normalize_thumbnail_url",
    "normalize_youtube_thumbnail",
    "youtube_thumbnail_candidates",
    "youtube_thumbnail_size",
]

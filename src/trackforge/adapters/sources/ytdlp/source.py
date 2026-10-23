"""yt-dlp source resolver: URL → audio-only candidates.

Resolution is metadata-only.  The resolver never downloads bytes, rejects
playlist containers until Plan 18, filters video-only formats, and refuses
DRM-protected items.  Cookie selection is per host and validated for 0600
permissions before a path is handed to the downloader.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from trackforge.adapters.http.url_safety import (
    UrlSafetyGuard,
    sanitize_url,
)
from trackforge.adapters.sources.ytdlp.client import DumpOptions, YtDlpClient
from trackforge.adapters.sources.ytdlp.cookies import select_cookie_file
from trackforge.adapters.sources.ytdlp.metadata import (
    build_source_metadata,
    normalize_source_metadata,
)
from trackforge.application.ports.sources import CandidateList
from trackforge.domain.errors import (
    DrmProtected,
    OfflineModeError,
    Unavailable,
    UnsafeUrl,
    UnsupportedSource,
    UnsupportedUrl,
)
from trackforge.domain.model.ids import CandidateId, new_uuid7
from trackforge.domain.model.source import Candidate, Format, SourceRef, Thumbnail, Url

_AUDIO_CONTAINERS = frozenset(
    {"m4a", "opus", "webm", "ogg", "oga", "mp3", "flac", "aac", "wav", "mka", "alac"}
)
_VIDEO_CONTAINERS = frozenset({"mp4", "mkv", "avi", "mov", "flv", "ts", "m4v", "3gp"})
_PLAYLIST_PATHS = ("/playlist", "/sets/", "/album/", "/channel/", "/user/")
_MAX_AUDIO_FORMATS = 64


class YtDlpSource:
    """Resolve one public URL into an audio-only candidate list."""

    id = "ytdlp"
    version = "1"

    def __init__(
        self,
        client: YtDlpClient | None = None,
        *,
        cookie_dir: Path | None = None,
        guard: UrlSafetyGuard | None = None,
    ) -> None:
        self._client = client if client is not None else YtDlpClient()
        self._cookie_dir = Path(cookie_dir) if cookie_dir is not None else None
        self._guard = guard if guard is not None else UrlSafetyGuard()

    @property
    def client(self) -> YtDlpClient:
        return self._client

    def supports(self, ref: SourceRef) -> bool:
        """Return true for an http(s) URL that passes the SSRF policy."""

        if not isinstance(ref, Url):
            return False
        if not self._guard.supports(ref.url):
            return False
        try:
            self._guard.validate(ref.url)
        except UnsafeUrl:
            return False
        return True

    async def resolve(
        self,
        ref: SourceRef,
        opts: Mapping[str, Any] | None = None,
    ) -> CandidateList:
        """Fetch metadata and build the single-track candidate."""

        if not isinstance(ref, Url):
            raise UnsupportedSource(
                "ytdlp source only supports URLs",
                user_action="Pass an http(s) URL for a single track.",
                context={"source_type": type(ref).__name__},
            )
        options = dict(opts or {})
        if bool(options.get("offline")):
            raise OfflineModeError(
                "offline mode does not allow URL resolution",
                context={"url": sanitize_url(ref.url)},
            )
        self._guard.validate(ref.url)
        if _looks_like_playlist_url(ref.url):
            raise UnsupportedUrl(
                "playlist and album URLs are not supported yet",
                user_action="Use a single-track URL; playlist ingest arrives in Plan 18.",
                context={"url": sanitize_url(ref.url)},
            )
        cookie = select_cookie_file(
            ref.url,
            self._cookie_dir,
            site_hint=ref.site_hint,
        )
        document = self._client.dump_json(
            ref.url,
            DumpOptions(
                cookies_path=cookie.path if cookie is not None else None,
                site_hint=ref.site_hint,
            ),
        )
        if _is_playlist_document(document):
            raise UnsupportedUrl(
                "yt-dlp returned a playlist instead of a single track",
                user_action="Use a single-track URL; playlist ingest arrives in Plan 18.",
                context={"url": sanitize_url(ref.url)},
            )
        metadata = build_source_metadata(document)
        if metadata.webpage_url is not None:
            # Catch page-level redirects to a private/local address before
            # any media bytes are requested.
            self._guard.validate(metadata.webpage_url)
        if _raw_has_drm(document):
            raise DrmProtected(
                "the source is DRM protected",
                context={"external_id": metadata.external_id},
            )
        raw_formats = _raw_formats(document)
        formats, warnings = _audio_formats(raw_formats)
        if not formats:
            if any(_raw_has_drm(item) for item in raw_formats):
                raise DrmProtected(
                    "only DRM-protected formats are available",
                    context={"external_id": metadata.external_id},
                )
            raise Unavailable(
                "the source exposes no audio formats",
                user_action="Check that the URL points to a playable audio track.",
                context={"external_id": metadata.external_id},
            )
        selected = select_best_audio(formats)
        thumbnails = tuple(
            Thumbnail(url=item.url, width=item.width, height=item.height)
            for item in metadata.thumbnails
            if item.url
        )
        candidate = Candidate(
            id=CandidateId(new_uuid7()),
            source_ref=ref,
            external_id=metadata.external_id,
            title=metadata.title,
            uploader=metadata.uploader,
            duration_seconds=metadata.duration_seconds,
            formats=formats,
            thumbnails=thumbnails,
            extra={
                "extractor": metadata.extractor or "",
                "extractor_key": _text(document.get("extractor_key")) or "",
                "webpage_url": metadata.webpage_url or sanitize_url(ref.url) or "",
                "selected_format_id": selected.format_id or "",
                "format_count": len(formats),
                "upload_date": metadata.upload_date or "",
                "channel": _text(document.get("channel")) or "",
                "has_drm": False,
            },
        )
        if any(item.drm for item in formats):
            warnings = (*warnings, "drm_formats_excluded")
        return CandidateList(source=ref, candidates=(candidate,), warnings=warnings)


def _raw_formats(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = document.get("formats")
    if isinstance(raw, (list, tuple)):
        return [item for item in raw if isinstance(item, Mapping)]
    if _text(document.get("url")) and _text(document.get("acodec")):
        return [document]
    return []


def _audio_formats(
    raw_formats: list[Mapping[str, Any]],
) -> tuple[tuple[Format, ...], tuple[str, ...]]:
    audio_only: list[Format] = []
    combined: list[Format] = []
    for item in raw_formats:
        if _raw_has_drm(item):
            continue
        container = (_text(item.get("ext")) or "").lower()
        if container in _VIDEO_CONTAINERS and (_text(item.get("vcodec")) not in {None, "none"}):
            continue
        vcodec = (_text(item.get("vcodec")) or "").lower()
        acodec = (_text(item.get("acodec")) or "").lower()
        if acodec in {"", "none"}:
            if vcodec not in {"", "none"} or container not in _AUDIO_CONTAINERS:
                continue
        if vcodec not in {"", "none"}:
            # A combined format is only a last-resort fallback.
            combined.append(_format(item))
            continue
        if container not in _AUDIO_CONTAINERS and container in _VIDEO_CONTAINERS:
            continue
        audio_only.append(_format(item))
        if len(audio_only) >= _MAX_AUDIO_FORMATS:
            break
    formats = tuple(audio_only)
    warnings: list[str] = []
    if not formats and combined:
        formats = tuple(sorted(combined, key=_format_score)[:1])
        warnings.append("combined_audio_video_fallback")
    return formats, tuple(warnings)


def _format(item: Mapping[str, Any]) -> Format:
    abr = _number(item.get("abr"))
    bitrate = int(round(abr * 1000)) if abr is not None and abr > 0 else None
    size = _integer(item.get("filesize")) or _integer(item.get("filesize_approx"))
    return Format(
        codec=_text(item.get("acodec")),
        bitrate=bitrate,
        sample_rate=_integer(item.get("asr")),
        channels=_integer(item.get("audio_channels")),
        language=_text(item.get("language")),
        size=size,
        url=None,
        drm=_raw_has_drm(item),
        container=(_text(item.get("ext")) or "").lower() or None,
        format_id=_text(item.get("format_id")),
    )


def _format_score(fmt: Format) -> tuple[int, int, int, int, str]:
    container = (fmt.container or "").lower()
    codec = (fmt.codec or "").lower()
    if container == "m4a":
        family = 0
    elif container in {"opus", "webm"} or codec in {"opus", "libopus"}:
        family = 1
    elif container in _AUDIO_CONTAINERS:
        family = 2
    else:
        family = 3
    return (
        family,
        -(fmt.bitrate or 0),
        -(fmt.sample_rate or 0),
        -(fmt.size or 0),
        fmt.format_id or "",
    )


def select_best_audio(formats: tuple[Format, ...] | list[Format]) -> Format:
    """Choose the best audio-only format deterministically."""

    audio = [fmt for fmt in formats if not fmt.drm]
    pool = audio or list(formats)
    if not pool:
        raise Unavailable("no audio format can be selected")
    return sorted(pool, key=_format_score)[0]


def _raw_has_drm(item: Mapping[str, Any]) -> bool:
    value = item.get("has_drm")
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes"}
    return bool(value)


def _is_playlist_document(document: Mapping[str, Any]) -> bool:
    kind = (_text(document.get("_type")) or "").lower()
    if kind in {"playlist", "multi_video"}:
        return True
    entries = document.get("entries")
    return isinstance(entries, (list, tuple)) and bool(entries)


def _looks_like_playlist_url(url: str) -> bool:
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    path = parts.path.casefold()
    if any(marker in path for marker in _PLAYLIST_PATHS):
        return True
    query = parse_qs(parts.query)
    return "list" in query or "album" in query


def _text(value: object) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number >= 0 else None


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def fallback_external_id(url: str) -> str:
    """Stable fallback identifier for pathological extractor output."""

    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


__all__ = [
    "YtDlpSource",
    "build_source_metadata",
    "fallback_external_id",
    "normalize_source_metadata",
    "select_best_audio",
]

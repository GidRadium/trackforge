"""SoundCloud site policy on top of the generic yt-dlp loader.

SoundCloud tracks are audio-only; sets are playlists.  Cookies are optional
and mostly needed for private tracks or age-restricted content.  Search
(``scsearch``) belongs to L3, so this loader advertises no search support.
"""

# pyright: standard

from __future__ import annotations

from urllib.parse import urlsplit

from ..config import SourceProfile
from ..models import (
    DownloadVariant,
    MediaItem,
    MediaKind,
    Recommendations,
    StreamRole,
    UrlHint,
)
from .base import LoaderCapabilities, LoaderContext
from .ytdlp import YtDlpLoader

_RESERVED_ROOTS = frozenset(
    {
        "charts",
        "creators",
        "discover",
        "download",
        "imprint",
        "jobs",
        "mobile",
        "pages",
        "search",
        "settings",
        "stream",
        "tags",
        "terms-of-use",
        "upload",
        "you",
    }
)


def _is_soundcloud_host(host: str) -> bool:
    return host == "soundcloud.com" or host.endswith(".soundcloud.com")


def _best_soundcloud_audio(
    variants: tuple[DownloadVariant, ...],
) -> DownloadVariant | None:
    """Pick the most original audio variant, never a transcoded container.

    yt-dlp exposes both progressive ``http_mp3_*`` formats and HLS
    ``hls_mp3_*``/``hls_opus_*`` renditions.  The progressive HTTP rendition
    is the closest thing to the source SoundCloud serves and does not need
    remuxing, so it wins over HLS even when the HLS bitrate is higher.
    """

    candidates: list[tuple[tuple[float, ...], DownloadVariant]] = []
    for variant in variants:
        if variant.kind is not MediaKind.AUDIO:
            continue
        if not variant.downloadable or variant.unavailable_reason is not None:
            continue
        audio = next(
            (stream for stream in variant.streams if stream.role is StreamRole.AUDIO),
            None,
        )
        if audio is None or audio.drm:
            continue
        protocol = (audio.protocol or "").lower()
        direct = 1.0 if protocol.startswith(("http://", "https://")) else 0.0
        hls = 1.0 if "m3u8" in protocol or "hls" in protocol else 0.0
        marker = " ".join(
            part
            for part in (audio.format_id, variant.id, variant.label)
            if part
        ).lower()
        original = 1.0 if "original" in marker or "download" in marker else 0.0
        ext = (audio.ext or variant.container or "").lower()
        compatible = 1.0 if ext in {"mp3", "flac", "wav", "m4a", "aac"} else 0.0
        score = (
            direct,
            1.0 - hls,
            original,
            compatible,
            float(audio.bitrate_kbps or 0.0),
            float(audio.filesize or audio.filesize_approx or 0),
        )
        candidates.append((score, variant))
    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair[0])[1]


class SoundCloudLoader(YtDlpLoader):
    """SoundCloud tracks and sets through ordinary yt-dlp."""

    name = "soundcloud"
    config_key = "soundcloud"
    priority = 20
    capabilities = LoaderCapabilities(
        search=False,
        playlists=True,
        cookies=True,
        proxy=True,
    )

    def supports_url(self, url: str) -> bool:
        try:
            parsed = urlsplit(url.strip())
        except ValueError:
            return False
        return (
            parsed.scheme.lower() in {"http", "https"}
            and _is_soundcloud_host((parsed.hostname or "").lower())
        )

    def classify_url(self, url: str) -> UrlHint:
        try:
            parsed = urlsplit(url.strip())
        except ValueError:
            return UrlHint.UNKNOWN
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not _is_soundcloud_host(host)
        ):
            return UrlHint.UNKNOWN
        # Short links hide whether they point at a track or a set.
        if host == "on.soundcloud.com":
            return UrlHint.UNKNOWN
        segments = [segment for segment in parsed.path.split("/") if segment]
        if not segments:
            return UrlHint.UNKNOWN
        lowered = [segment.lower() for segment in segments]
        if "sets" in lowered:
            index = lowered.index("sets")
            if index >= 1 and index + 1 < len(segments):
                return UrlHint.COLLECTION
            return UrlHint.UNKNOWN
        if lowered[0] in _RESERVED_ROOTS:
            return UrlHint.UNKNOWN
        if len(segments) >= 2:
            return UrlHint.SINGLE
        # A bare profile URL is not a single media item.
        return UrlHint.UNKNOWN

    def is_ready(self, profile: SourceProfile) -> bool:
        """SoundCloud works without cookies; the profile is optional."""
        del profile
        return True

    def recommend(
        self,
        item: MediaItem,
        *,
        ctx: LoaderContext,
    ) -> Recommendations:
        del ctx
        audio = _best_soundcloud_audio(item.variants)
        return Recommendations(audio=audio.id if audio is not None else None)


__all__ = ["SoundCloudLoader"]

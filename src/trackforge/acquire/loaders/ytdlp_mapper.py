"""Normalize yt-dlp ``info_dict`` values into acquisition models.

The mapper is deliberately tolerant: yt-dlp's payload is an extensible
dictionary and optional fields disappear between versions and extractors.
Unknown or source-specific values are copied to ``MediaItem.extras`` instead
of being promoted to the public model.
"""

# pyright: standard

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ...metadata.model import PartialDate
from ..config import SourceProfile
from ..errors import UnresolvedEntry, UnresolvedRedirect, UnsupportedUrl
from ..models import (
    DownloadVariant,
    LiveStatus,
    MediaItem,
    MediaKind,
    ResolutionState,
    SourceInfo,
    SourceKind,
    SourceRef,
    StreamInfo,
    StreamRole,
    SubtitleTrack,
    Thumbnail,
)
from .base import default_recommendations, select_best_thumbnail

_VIDEO_EXTENSIONS = frozenset({"mp4", "m4v", "mov", "webm", "mkv", "avi", "flv"})
_AUDIO_EXTENSIONS = frozenset({"m4a", "mp3", "opus", "ogg", "oga", "webm", "aac", "flac", "wav"})
_IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp", "gif", "avif"})
_STORYBOARD_EXTENSIONS = frozenset({"mhtml", "mht"})
_MP4_AUDIO_EXTENSIONS = frozenset({"m4a", "mp4", "aac"})
_WEBM_AUDIO_EXTENSIONS = frozenset({"webm", "opus", "ogg", "oga"})
_AUTH_AVAILABILITY = frozenset(
    {"private", "premium_only", "subscriber_only", "needs_auth", "members_only"}
)
_DRM_AVAILABILITY = frozenset({"drm", "drm_protected"})
_GENERIC_FORMAT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")

# Flat collections are intentionally bounded: yt-dlp's ``entries`` may be a
# lazy ``PagedList`` and materialising it would trigger hundreds of requests
# before the user gets a chance to choose anything.
DEFAULT_FLAT_LIMIT = 50
MAX_FLAT_LIMIT = 500
_COLLECTION_TYPES = frozenset({"playlist", "multi_video"})

_MAPPED_KEYS = frozenset(
    {
        "_type",
        "alt_title",
        "album",
        "album_artist",
        "album_artists",
        "artist",
        "artists",
        "channel",
        "creator",
        "description",
        "display_id",
        "duration",
        "formats",
        "id",
        "original_url",
        "publisher",
        "release_date",
        "release_timestamp",
        "release_year",
        "thumbnail",
        "thumbnails",
        "timestamp",
        "title",
        "track",
        "upload_date",
        "uploader",
        "url",
        "webpage_url",
    }
)
_NON_EXTRA_KEYS = frozenset(
    {
        "__post_extractor",
        "_format_sort_fields",
        "_old_archive_ids",
        "_version",
        "abr",
        "acodec",
        "aspect_ratio",
        "audio_channels",
        "automatic_captions",
        "container",
        "entries",
        "ext",
        "filesize",
        "filesize_approx",
        "format",
        "format_id",
        "format_note",
        "fps",
        "fragment_base_url",
        "fragments",
        "has_drm",
        "height",
        "http_headers",
        "language",
        "manifest_url",
        "protocol",
        "requested_downloads",
        "requested_entries",
        "requested_formats",
        "requested_subtitles",
        "resolution",
        "subtitles",
        "tbr",
        "vbr",
        "vcodec",
        "width",
    }
)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: object) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _text(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _integer(value: object) -> int | None:
    number = _number(value)
    if number is None:
        return None
    return int(number)


def _positive_int(value: object) -> int | None:
    number = _integer(value)
    if number is None or number <= 0:
        return None
    return number


def _str_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, Mapping):
        return ()
    result: list[str] = []
    for entry in _items(value):
        text = _text(entry)
        if text is not None and text not in result:
            result.append(text)
    return tuple(result)


def _first_text(*values: object) -> str | None:
    for value in values:
        text = _text(value)
        if text is not None:
            return text
    return None


def _url_identifier(url: str | None) -> str | None:
    """Derive a stable item id from a page URL when the extractor omits one."""
    if not url:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    query = parse_qs(parsed.query)
    for key in ("v", "id", "track_id"):
        value = (query.get(key) or [""])[0].strip()
        if value:
            return value
    path = parsed.path.rstrip("/")
    if not path:
        return None
    last = path.rsplit("/", 1)[-1]
    if last and last.lower() not in {"watch", "index", "sets", "playlist"}:
        return last
    return None


def _drm_flag(value: object) -> bool:
    if value is True or value == 1:
        return True
    return bool(
        isinstance(value, str)
        and value.strip().lower() in {"true", "yes", "1", "maybe"}
    )


def _format_id(fmt: Mapping[str, Any], index: int) -> str:
    identifier = _text(fmt.get("format_id"))
    if identifier is not None and _GENERIC_FORMAT_ID_RE.match(identifier):
        return identifier
    return f"fmt-{index}"


def _codec_present(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return value.strip().lower() not in {"", "none", "null"}


def _classify_format(fmt: Mapping[str, Any], *, direct: bool = False) -> MediaKind:
    """Classify one yt-dlp format without trusting optional codec fields."""
    vcodec = fmt.get("vcodec")
    acodec = fmt.get("acodec")
    has_video = _codec_present(vcodec)
    has_audio = _codec_present(acodec)
    ext = (_text(fmt.get("ext")) or "").lower()
    note = (_text(fmt.get("format_note")) or "").lower()
    if ext in _STORYBOARD_EXTENSIONS or "storyboard" in note:
        return MediaKind.UNKNOWN
    if ext in _IMAGE_EXTENSIONS:
        return MediaKind.IMAGE
    height = _number(fmt.get("height"))
    width = _number(fmt.get("width"))
    abr = _number(fmt.get("abr"))
    asr = _number(fmt.get("asr"))
    channels = _number(fmt.get("audio_channels"))
    has_audio_fields = (abr or asr or channels) is not None
    has_dimensions = (height or 0) > 0 or (width or 0) > 0

    if has_video and has_audio:
        return MediaKind.AUDIO_VIDEO
    if has_video:
        if has_dimensions and has_audio_fields:
            return MediaKind.AUDIO_VIDEO
        return MediaKind.VIDEO
    if has_audio:
        if has_dimensions:
            return MediaKind.AUDIO_VIDEO
        return MediaKind.AUDIO

    # Codecs are missing entirely (common for direct file URLs).
    if direct and ext in _AUDIO_EXTENSIONS and not has_dimensions:
        return MediaKind.AUDIO
    if direct:
        return MediaKind.AUDIO_VIDEO
    if has_dimensions:
        return MediaKind.AUDIO_VIDEO if has_audio_fields else MediaKind.VIDEO
    if abr is not None or asr is not None or channels is not None:
        return MediaKind.AUDIO
    if ext in _AUDIO_EXTENSIONS:
        return MediaKind.AUDIO
    if ext in _VIDEO_EXTENSIONS:
        return MediaKind.AUDIO_VIDEO
    return MediaKind.UNKNOWN


def _format_has_source(fmt: Mapping[str, Any]) -> bool:
    if _text(fmt.get("url")) is not None:
        return True
    return bool(_items(fmt.get("fragments")))


def _stream(
    fmt: Mapping[str, Any],
    role: StreamRole,
    *,
    index: int,
    item: Mapping[str, Any],
) -> StreamInfo:
    video = role is StreamRole.VIDEO
    bitrate = None
    if video:
        bitrate = _number(fmt.get("vbr")) or _number(fmt.get("tbr"))
    else:
        bitrate = _number(fmt.get("abr"))
        if bitrate is None and not _codec_present(fmt.get("vcodec")):
            # For progressive formats ``tbr`` is the total stream bitrate,
            # not the audio bitrate, so only use it for audio-only formats.
            bitrate = _number(fmt.get("tbr"))
    drm = _drm_flag(fmt.get("has_drm")) or _drm_flag(item.get("has_drm"))
    language = _text(fmt.get("language"))
    language_preference = _number(fmt.get("language_preference"))
    if not video and language_preference is None and language:
        item_language = _text(item.get("language"))
        if item_language:
            rank = _language_rank(language.lower(), item_language.lower())
            language_preference = 10.0 if rank == 0 else 9.0 if rank == 1 else -1.0
    filesize = _positive_int(fmt.get("filesize"))
    filesize_approx = _positive_int(fmt.get("filesize_approx"))
    duration = _number(item.get("duration"))
    if (
        filesize is None
        and filesize_approx is None
        and bitrate is not None
        and bitrate > 0
        and duration is not None
        and duration > 0
    ):
        # Approximate the stream size from its bitrate and duration.
        filesize_approx = max(1, int(bitrate * 1000 / 8 * duration))
    return StreamInfo(
        role=role,
        format_id=_format_id(fmt, index),
        codec=_text(fmt.get("vcodec") if video else fmt.get("acodec")),
        ext=_text(fmt.get("ext")) or _text(fmt.get("container")),
        url=_text(fmt.get("url")),
        width=_positive_int(fmt.get("width")) if video else None,
        height=_positive_int(fmt.get("height")) if video else None,
        fps=_number(fmt.get("fps")) if video else None,
        bitrate_kbps=bitrate,
        sample_rate=_positive_int(fmt.get("asr")) if not video else None,
        channels=_positive_int(fmt.get("audio_channels")) if not video else None,
        language=language,
        language_preference=language_preference,
        dynamic_range=_text(fmt.get("dynamic_range")) if video else None,
        filesize=filesize,
        filesize_approx=filesize_approx,
        protocol=_text(fmt.get("protocol")),
        drm=drm,
    )


def _container(fmt: Mapping[str, Any]) -> str | None:
    return _text(fmt.get("ext")) or _text(fmt.get("container"))


def _availability(fmt: Mapping[str, Any], item: Mapping[str, Any]) -> str:
    return (
        _text(fmt.get("availability"))
        or _text(item.get("availability"))
        or ""
    ).lower()


def _unavailable_reason(
    fmt: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    authenticated: bool,
) -> str | None:
    availability = _availability(fmt, item)
    if (
        _drm_flag(fmt.get("has_drm"))
        or _drm_flag(item.get("has_drm"))
        or availability in _DRM_AVAILABILITY
    ):
        return "DRM-protected"
    if not authenticated and (
        availability in _AUTH_AVAILABILITY or availability in _DRM_AVAILABILITY
    ):
        return f"requires authentication ({availability})"
    if not _format_has_source(fmt):
        return "no stream URL or fragments"
    return None


def _combined_size(streams: Sequence[StreamInfo]) -> tuple[int | None, int | None]:
    exact = 0
    approximate = 0
    all_exact = True
    all_known = True
    for stream in streams:
        if stream.filesize is not None:
            exact += stream.filesize
            approximate += stream.filesize
        elif stream.filesize_approx is not None:
            all_exact = False
            approximate += stream.filesize_approx
        else:
            all_known = False
    if not all_known:
        return None, None
    if all_exact:
        return exact, None
    return None, approximate


def _broad_codec(codec: str | None) -> str:
    if not codec:
        return ""
    stem = codec.split(".", 1)[0].strip().lower()
    aliases = {
        "avc1": "h264",
        "avc3": "h264",
        "h264": "h264",
        "hev1": "h265",
        "hvc1": "h265",
        "h265": "h265",
        "vp09": "vp9",
        "vp9": "vp9",
        "av01": "av1",
        "av1": "av1",
        "mp4a": "aac",
        "aac": "aac",
        "opus": "opus",
        "vorbis": "vorbis",
    }
    return aliases.get(stem, stem)


def _slug(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _video_height(fmt: Mapping[str, Any]) -> int | None:
    return _positive_int(fmt.get("height"))


def _video_bitrate(fmt: Mapping[str, Any]) -> float:
    return _number(fmt.get("tbr")) or _number(fmt.get("vbr")) or 0.0


def _audio_bitrate(fmt: Mapping[str, Any]) -> float:
    return _number(fmt.get("abr")) or _number(fmt.get("tbr")) or 0.0


def _language_key(value: object) -> str:
    return (_text(value) or "").lower()


def _language_display(value: object) -> str:
    return _text(value) or "und"


def _language_slug(value: object) -> str:
    return _slug(_language_display(value)) or "und"


def _language_matches(candidate: str, original: str | None) -> bool:
    if not original:
        return False
    if candidate == original:
        return True
    return candidate.split("-", 1)[0] == original.split("-", 1)[0]


def _language_rank(candidate: str, original: str | None) -> int:
    """0 = exact original, 1 = same base language, 2 = other/none."""
    if not original:
        return 1 if not candidate else 2
    if candidate == original:
        return 0
    if candidate and candidate.split("-", 1)[0] == original.split("-", 1)[0]:
        return 1
    return 2


def _language_groups(
    formats: Sequence[Mapping[str, Any]],
) -> list[tuple[str, list[Mapping[str, Any]]]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for fmt in formats:
        groups.setdefault(_language_key(fmt.get("language")), []).append(fmt)
    return list(groups.items())


def _has_multiple_languages(formats: Sequence[Mapping[str, Any]]) -> bool:
    return len({_language_key(fmt.get("language")) for fmt in formats}) > 1


def _video_quality(variant: DownloadVariant) -> tuple[float, ...]:
    video = next((s for s in variant.streams if s.role is StreamRole.VIDEO), None)
    audio = next((s for s in variant.streams if s.role is StreamRole.AUDIO), None)
    height = float(video.height or 0) if video else 0.0
    fps = float(video.fps or 0.0) if video else 0.0
    bitrate = float(video.bitrate_kbps or 0.0) if video else 0.0
    audio_preference = float(audio.language_preference or 0.0) if audio else 0.0
    audio_bitrate = float(audio.bitrate_kbps or 0.0) if audio else 0.0
    return (
        float(height),
        float(fps),
        float(bitrate),
        audio_preference,
        float(audio_bitrate),
    )


def _video_key(variant: DownloadVariant) -> tuple[object, ...]:
    video = next((s for s in variant.streams if s.role is StreamRole.VIDEO), None)
    if video is None:
        return (variant.id,)
    dynamic_range = (video.dynamic_range or "").lower()
    if dynamic_range in {"sdr", "unknown"}:
        dynamic_range = ""
    audio = next((s for s in variant.streams if s.role is StreamRole.AUDIO), None)
    return (
        video.height or 0,
        (variant.container or "").lower(),
        _broad_codec(video.codec),
        dynamic_range,
        _language_key(audio.language if audio else None),
    )


def _audio_key(variant: DownloadVariant) -> tuple[object, ...]:
    audio = next((s for s in variant.streams if s.role is StreamRole.AUDIO), None)
    if audio is None:
        return (variant.id,)
    return (
        _language_key(audio.language),
        (audio.ext or variant.container or "").lower(),
        _broad_codec(audio.codec),
        (audio.protocol or "").lower(),
    )


def _is_video_variant(variant: DownloadVariant) -> bool:
    return variant.kind in {MediaKind.AUDIO_VIDEO, MediaKind.VIDEO}


def _unique_id(base: str, used: set[str]) -> str:
    candidate = _slug(base) or "variant"
    if candidate not in used:
        used.add(candidate)
        return candidate
    index = 2
    while f"{candidate}-{index}" in used:
        index += 1
    result = f"{candidate}-{index}"
    used.add(result)
    return result


def _video_label(fmt: Mapping[str, Any], *, progressive: bool) -> str:
    height = _video_height(fmt)
    container = (_container(fmt) or "video").upper()
    base = f"{height}p {container}" if height else f"{container} video"
    if not progressive:
        base = f"{base} video-only"
    dynamic_range = (_text(fmt.get("dynamic_range")) or "").upper()
    if dynamic_range and dynamic_range not in {"SDR", "UNKNOWN"}:
        base = f"{base} {dynamic_range}"
    return base


def _video_selector(fmt: Mapping[str, Any]) -> str:
    identifier = _text(fmt.get("format_id"))
    height = _video_height(fmt)
    fallback = (
        f"bv*[height={height}]+ba/b[height={height}]"
        if height is not None
        else "bv*+ba/b"
    )
    return f"{identifier}/{fallback}" if identifier else fallback


def _audio_selector(fmt: Mapping[str, Any]) -> str:
    identifier = _text(fmt.get("format_id"))
    ext = (_container(fmt) or "").lower()
    fallback = f"ba[ext={ext}]/ba/b" if ext else "ba/b"
    return f"{identifier}/{fallback}" if identifier else fallback


def _legacy_audio_matches(video: Mapping[str, Any], audio: Mapping[str, Any]) -> bool:
    video_ext = (_container(video) or "").lower()
    audio_ext = (_container(audio) or "").lower()
    audio_codec = _broad_codec(_text(audio.get("acodec")))
    if video_ext in {"mp4", "m4v", "mov"}:
        return audio_ext in _MP4_AUDIO_EXTENSIONS or audio_codec in {"aac", "mp4a"}
    if video_ext in {"webm", "mkv"}:
        return audio_ext in _WEBM_AUDIO_EXTENSIONS or audio_codec in {"opus", "vorbis"}
    return False


def _audio_sort_key(
    video: Mapping[str, Any],
    audio: Mapping[str, Any],
    original_language: str | None,
) -> tuple[float, ...]:
    """Rank audio for one specific video stream (compatibility first)."""
    compatible = 1.0 if _legacy_audio_matches(video, audio) else 0.0
    language = (_text(audio.get("language")) or "").lower()
    original = 1.0 if (
        not language
        or (
            original_language is not None
            and (
                language == original_language
                or language.split("-", 1)[0] == original_language
            )
        )
    ) else 0.0
    preference = _number(audio.get("language_preference")) or 0.0
    return (
        compatible,
        original,
        preference,
        _audio_bitrate(audio),
        float(_positive_int(audio.get("filesize")) or 0),
    )


def pair_video_with_audio(
    video_only: Sequence[Mapping[str, Any]],
    audio_only: Sequence[Mapping[str, Any]],
    *,
    item: Mapping[str, Any] | None = None,
    authenticated: bool = False,
    used_ids: set[str] | None = None,
    multi_language: bool = False,
) -> list[DownloadVariant]:
    """Pair video-only formats with every available audio language."""
    item = item or {}
    used = used_ids if used_ids is not None else set()
    original_language = _language_key(item.get("language")) or None
    groups = _language_groups(audio_only)
    groups.sort(
        key=lambda pair: (
            _language_rank(pair[0], original_language),
            pair[0] or "~",
        )
    )
    variants: list[DownloadVariant] = []

    ordered_video = sorted(
        video_only,
        key=lambda fmt: (
            _video_height(fmt) or 0,
            _number(fmt.get("fps")) or 0.0,
            _video_bitrate(fmt),
        ),
        reverse=True,
    )
    for index, video in enumerate(ordered_video, start=1):
        video_stream = _stream(video, StreamRole.VIDEO, index=index, item=item)
        if not groups:
            reason = _unavailable_reason(video, item, authenticated=authenticated)
            height = _video_height(video)
            container = (_container(video) or "video").lower()
            base = (
                f"{height}p-{container}-video"
                if height is not None
                else f"video-{container}"
            )
            variants.append(
                DownloadVariant(
                    id=_unique_id(base, used),
                    label=_video_label(video, progressive=False),
                    kind=MediaKind.VIDEO,
                    container=_container(video),
                    streams=(video_stream,),
                    selector=_video_selector(video),
                    filesize=video_stream.filesize,
                    filesize_approx=video_stream.filesize_approx,
                    downloadable=reason is None,
                    unavailable_reason=reason,
                )
            )
            continue

        for language_key, group in groups:
            best_audio = max(
                group,
                key=lambda audio: _audio_sort_key(
                    video,
                    audio,
                    original_language,
                ),
            )
            audio_stream = _stream(
                best_audio,
                StreamRole.AUDIO,
                index=index,
                item=item,
            )
            language = audio_stream.language or language_key
            suffix = f"-{_language_slug(language)}" if multi_language else ""
            video_reason = _unavailable_reason(
                video,
                item,
                authenticated=authenticated,
            )
            audio_reason = _unavailable_reason(
                best_audio,
                item,
                authenticated=authenticated,
            )
            reason = video_reason or audio_reason
            filesize, filesize_approx = _combined_size(
                (video_stream, audio_stream)
            )
            height = _video_height(video)
            container = (_container(video) or "").lower() or None
            base = (
                f"{height}p-{container}{suffix}"
                if height is not None
                else f"video-{container or 'video'}{suffix}"
            )
            label = _video_label(video, progressive=True)
            if multi_language:
                label = f"{label} [{_language_display(language)}]"
            variants.append(
                DownloadVariant(
                    id=_unique_id(base, used),
                    label=label,
                    kind=MediaKind.AUDIO_VIDEO,
                    container=container,
                    streams=(video_stream, audio_stream),
                    selector=(
                        f"{video_stream.format_id}+{audio_stream.format_id}"
                        f"/bv*[height={height}]+ba/b[height={height}]"
                        if height is not None
                        else (
                            f"{video_stream.format_id}+"
                            f"{audio_stream.format_id}/bv*+ba/b"
                        )
                    ),
                    filesize=filesize,
                    filesize_approx=filesize_approx,
                    requires_merge=True,
                    requires_ffmpeg=True,
                    downloadable=reason is None,
                    unavailable_reason=reason,
                )
            )
    return variants


def map_progressive(
    progressive: Sequence[Mapping[str, Any]],
    *,
    item: Mapping[str, Any] | None = None,
    authenticated: bool = False,
    used_ids: set[str] | None = None,
    multi_language: bool = False,
) -> list[DownloadVariant]:
    """Map formats that already contain both video and audio."""
    item = item or {}
    used = used_ids if used_ids is not None else set()
    variants: list[DownloadVariant] = []
    for index, fmt in enumerate(progressive, start=1):
        video_stream = _stream(fmt, StreamRole.VIDEO, index=index, item=item)
        audio_stream = _stream(fmt, StreamRole.AUDIO, index=index, item=item)
        reason = _unavailable_reason(fmt, item, authenticated=authenticated)
        filesize = video_stream.filesize or audio_stream.filesize
        filesize_approx = video_stream.filesize_approx or audio_stream.filesize_approx
        height = _video_height(fmt)
        container = (_container(fmt) or "").lower() or None
        suffix = ""
        if multi_language and audio_stream.language:
            suffix = f"-{_language_slug(audio_stream.language)}"
        base = (
            f"{height}p-{container}{suffix}"
            if height is not None
            else f"progress-{container or 'video'}{suffix}"
        )
        label = _video_label(fmt, progressive=True)
        if multi_language and audio_stream.language:
            label = f"{label} [{_language_display(audio_stream.language)}]"
        variants.append(
            DownloadVariant(
                id=_unique_id(base, used),
                label=label,
                kind=MediaKind.AUDIO_VIDEO,
                container=container,
                streams=(video_stream, audio_stream),
                selector=_video_selector(fmt),
                filesize=filesize,
                filesize_approx=filesize_approx,
                downloadable=reason is None,
                unavailable_reason=reason,
            )
        )
    return variants


def map_audio(
    audio_only: Sequence[Mapping[str, Any]],
    *,
    item: Mapping[str, Any] | None = None,
    authenticated: bool = False,
    used_ids: set[str] | None = None,
    multi_language: bool = False,
) -> list[DownloadVariant]:
    """Map audio-only formats, keeping the best stream per language/codec."""
    item = item or {}
    used = used_ids if used_ids is not None else set()
    ordered = sorted(
        audio_only,
        key=lambda fmt: (
            _language_key(fmt.get("language")),
            -_audio_bitrate(fmt),
        ),
    )
    variants: list[DownloadVariant] = []
    seen: set[tuple[object, ...]] = set()
    for index, fmt in enumerate(ordered, start=1):
        audio_stream = _stream(fmt, StreamRole.AUDIO, index=index, item=item)
        language = _language_key(audio_stream.language)
        language_value = audio_stream.language or language
        key = (
            language,
            (audio_stream.ext or "").lower(),
            _broad_codec(audio_stream.codec),
            (audio_stream.protocol or "").lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        reason = _unavailable_reason(fmt, item, authenticated=authenticated)
        ext = (audio_stream.ext or "").lower()
        ext = ext or _broad_codec(audio_stream.codec) or "audio"
        abr = _integer(audio_stream.bitrate_kbps)
        suffix = (
            f"-{_language_slug(language_value)}" if multi_language else ""
        )
        base = f"audio-{ext}"
        if abr is not None:
            base = f"{base}-{abr}"
        base = f"{base}{suffix}"
        label_ext = ext.upper()
        label = f"Audio {label_ext}"
        if abr is not None:
            label = f"{label} {abr}k"
        if multi_language:
            label = f"{label} [{_language_display(language_value)}]"
        variants.append(
            DownloadVariant(
                id=_unique_id(base, used),
                label=label,
                kind=MediaKind.AUDIO,
                container=ext if ext != "audio" else None,
                streams=(audio_stream,),
                selector=_audio_selector(fmt),
                filesize=audio_stream.filesize,
                filesize_approx=audio_stream.filesize_approx,
                downloadable=reason is None,
                unavailable_reason=reason,
            )
        )
    return variants


def dedupe_and_sort(variants: Sequence[DownloadVariant]) -> list[DownloadVariant]:
    """Collapse duplicate video/audio variants and order them for display."""
    best_video: dict[tuple[object, ...], DownloadVariant] = {}
    audio: dict[tuple[object, ...], DownloadVariant] = {}
    order: list[tuple[str, tuple[object, ...]]] = []

    for variant in variants:
        if _is_video_variant(variant):
            key = _video_key(variant)
            previous = best_video.get(key)
            if previous is None:
                best_video[key] = variant
                order.append(("video", key))
            elif _video_quality(variant) > _video_quality(previous):
                best_video[key] = variant
        elif variant.kind is MediaKind.AUDIO:
            key = _audio_key(variant)
            previous = audio.get(key)
            if previous is None:
                audio[key] = variant
                order.append(("audio", key))
            elif _audio_bitrate_from_variant(variant) > _audio_bitrate_from_variant(previous):
                audio[key] = variant

    seen_ids: set[str] = set()
    unique: list[DownloadVariant] = []
    for group, key in order:
        variant = best_video.get(key) if group == "video" else audio.get(key)
        if variant is None or variant.id in seen_ids:
            continue
        seen_ids.add(variant.id)
        unique.append(variant)

    def sort_key(variant: DownloadVariant) -> tuple[object, ...]:
        if variant.kind is MediaKind.AUDIO_VIDEO:
            order_index = 0
        elif variant.kind is MediaKind.VIDEO:
            order_index = 1
        elif variant.kind is MediaKind.AUDIO:
            order_index = 2
        else:
            order_index = 3
        quality = _video_quality(variant)
        audio = next(
            (stream for stream in variant.streams if stream.role is StreamRole.AUDIO),
            None,
        )
        audio_preference = float(audio.language_preference or 0.0) if audio else 0.0
        audio_bitrate = _audio_bitrate_from_variant(variant)
        return (
            order_index,
            -quality[0],
            -quality[1],
            -quality[2],
            -quality[3],
            -audio_preference,
            -audio_bitrate,
            variant.label,
        )

    ordered = sorted(unique, key=sort_key)

    # Recompute canonical IDs after deduplication so the winning variant gets
    # the unsuffixed id (``1080p-mp4`` rather than ``1080p-mp4-2``).  Language
    # suffixes are preserved because they are part of the canonical base.
    used: set[str] = set()
    rebuilt: list[DownloadVariant] = []
    for variant in ordered:
        if _is_video_variant(variant):
            base = re.sub(r"-\d+$", "", variant.id) or "variant"
        else:
            base = variant.id
        identifier = _unique_id(base, used)
        rebuilt.append(
            variant if identifier == variant.id else replace(variant, id=identifier)
        )
    return rebuilt


def _audio_bitrate_from_variant(variant: DownloadVariant) -> float:
    audio = next((s for s in variant.streams if s.role is StreamRole.AUDIO), None)
    return float(audio.bitrate_kbps or 0.0) if audio else 0.0


def build_variants(
    item: Mapping[str, Any],
    authenticated: bool = False,
) -> list[DownloadVariant]:
    """Build user-facing variants from one yt-dlp item.

    ``formats`` is split into video-only, audio-only and progressive streams.
    Video-only streams are paired with a compatible audio stream; a lone
    ``url`` becomes one progressive variant.
    """
    raw_formats = _items(item.get("formats"))
    direct = not raw_formats and _text(item.get("url")) is not None
    if direct:
        raw_formats = [item]

    video_only: list[Mapping[str, Any]] = []
    audio_only: list[Mapping[str, Any]] = []
    progressive: list[Mapping[str, Any]] = []
    for fmt in raw_formats:
        if not isinstance(fmt, Mapping):
            continue
        kind = _classify_format(fmt, direct=direct)
        if kind is MediaKind.VIDEO:
            video_only.append(fmt)
        elif kind is MediaKind.AUDIO:
            audio_only.append(fmt)
        elif kind is MediaKind.AUDIO_VIDEO:
            progressive.append(fmt)

    language_candidates = [
        fmt
        for fmt in (*audio_only, *progressive)
        if _codec_present(fmt.get("acodec"))
    ]
    multi_language = _has_multiple_languages(language_candidates)

    used_ids: set[str] = set()
    variants: list[DownloadVariant] = []
    variants.extend(
        pair_video_with_audio(
            video_only,
            audio_only,
            item=item,
            authenticated=authenticated,
            used_ids=used_ids,
            multi_language=multi_language,
        )
    )
    variants.extend(
        map_progressive(
            progressive,
            item=item,
            authenticated=authenticated,
            used_ids=used_ids,
            multi_language=multi_language,
        )
    )
    variants.extend(
        map_audio(
            audio_only,
            item=item,
            authenticated=authenticated,
            used_ids=used_ids,
            multi_language=multi_language,
        )
    )
    return dedupe_and_sort(variants)


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


def youtube_thumbnail_size(url: str) -> tuple[int, int] | None:
    """Return known YouTube CDN dimensions instead of trusting bad metadata.

    YouTube reports ``maxresdefault`` as 1920x1080, but the CDN actually
    serves a 1280x720 JPEG/WebP.  The same normalization applies to the other
    well-known thumbnail names.
    """
    try:
        host = urlsplit(url).hostname or ""
        name = urlsplit(url).path.rsplit("/", 1)[-1].lower()
    except ValueError:
        return None
    if not (host == "i.ytimg.com" or host.endswith(".ytimg.com")):
        return None
    return _YOUTUBE_THUMBNAIL_SIZES.get(name)


def _thumbnail_ext(url: str) -> str | None:
    try:
        name = urlsplit(url).path.rsplit("/", 1)[-1].lower()
    except ValueError:
        return None
    if "." not in name:
        return None
    ext = name.rsplit(".", 1)[-1]
    return ext if ext in {"jpg", "jpeg", "png", "webp", "gif", "avif"} else None


def map_thumbnails(entry: Mapping[str, Any]) -> tuple[Thumbnail, ...]:
    """Normalize the ``thumbnails`` list and the legacy ``thumbnail`` field."""
    result: list[Thumbnail] = []
    by_url: dict[str, int] = {}

    def add(raw: object, *, source: str) -> None:
        if isinstance(raw, Mapping):
            url = _text(raw.get("url"))
            width = _positive_int(raw.get("width"))
            height = _positive_int(raw.get("height"))
            ext = _text(raw.get("ext"))
            mime = _text(raw.get("mime_type")) or _text(raw.get("mime"))
            filesize = _positive_int(raw.get("filesize"))
            preference = _integer(raw.get("preference")) or 0
            headers = raw.get("http_headers")
            source_name = _text(raw.get("source")) or source
        else:
            url = _text(raw)
            width = height = filesize = None
            ext = mime = None
            preference = 0
            headers = None
            source_name = source
        if url is None:
            return
        known_size = youtube_thumbnail_size(url)
        if known_size is not None:
            width, height = known_size
        if ext is None:
            ext = _thumbnail_ext(url)
        headers_map = {
            str(key): str(value)
            for key, value in _mapping(headers).items()
            if _text(key) is not None and _text(value) is not None
        }
        thumbnail = Thumbnail(
            url=url,
            width=width,
            height=height,
            ext=ext,
            mime=mime,
            filesize=filesize,
            preference=preference,
            source=source_name,
            http_headers=headers_map,
        )
        existing = by_url.get(url)
        if existing is None:
            by_url[url] = len(result)
            result.append(thumbnail)
            return
        previous = result[existing]
        # Keep the richer record when the same URL appears twice.
        if (width or 0) * (height or 0) > (previous.width or 0) * (previous.height or 0):
            result[existing] = replace(
                thumbnail,
                preference=max(preference, previous.preference),
                source=previous.source or source_name,
            )

    for raw in _items(entry.get("thumbnails")):
        add(raw, source="yt-dlp")
    legacy = entry.get("thumbnail")
    if isinstance(legacy, (list, tuple)):
        # Some extractors put a list of URLs in the legacy ``thumbnail``
        # field instead of using ``thumbnails``.
        for raw in legacy:
            add(raw, source="thumbnail")
    elif legacy is not None:
        add(legacy, source="thumbnail")
    return tuple(result)


_SUBTITLE_MIME = {
    "vtt": "text/vtt",
    "srt": "application/x-subrip",
    "ass": "text/x-ssa",
    "ssa": "text/x-ssa",
    "ttml": "application/ttml+xml",
    "dfxp": "application/ttml+xml",
    "json3": "application/json",
    "srv1": "text/xml",
    "srv2": "text/xml",
    "srv3": "text/xml",
}


def map_subtitles(entry: Mapping[str, Any]) -> tuple[SubtitleTrack, ...]:
    """Map both uploader subtitles and automatic captions, all formats."""
    result: list[SubtitleTrack] = []
    seen: set[tuple[object, ...]] = set()

    def add(
        language: str,
        raw: object,
        *,
        automatic: bool,
    ) -> None:
        if isinstance(raw, Mapping):
            url = _text(raw.get("url"))
            data = _text(raw.get("data"))
            ext = _text(raw.get("ext"))
            name = _text(raw.get("name"))
            headers = raw.get("http_headers")
        else:
            url = _text(raw)
            data = None
            ext = None
            name = None
            headers = None
        if url is None and data is None:
            return
        key = (language, ext, automatic, url, bool(data))
        if key in seen:
            return
        seen.add(key)
        ext_value = (ext or "").lower() or None
        mime = _text(raw.get("mime_type")) if isinstance(raw, Mapping) else None
        mime = mime or (_SUBTITLE_MIME.get(ext_value or "") if ext_value else None)
        result.append(
            SubtitleTrack(
                language=language or "und",
                url=url,
                ext=ext_value,
                name=name,
                mime=mime,
                is_automatic=automatic,
                data=data,
                http_headers={
                    str(key_name): str(value)
                    for key_name, value in _mapping(headers).items()
                    if _text(key_name) is not None and _text(value) is not None
                },
            )
        )

    for field, automatic in (("subtitles", False), ("automatic_captions", True)):
        for language, tracks in _mapping(entry.get(field)).items():
            language_text = _text(language) or "und"
            for raw in _items(tracks):
                add(language_text, raw, automatic=automatic)
    return tuple(result)


def parse_partial_date(value: object, *, fallback_year: int | None = None) -> PartialDate | None:
    """Parse yt-dlp's ``YYYYMMDD``/ISO/partial dates without raising."""
    year = fallback_year
    if value is None or isinstance(value, bool):
        return PartialDate(year) if year is not None else None
    if isinstance(value, (int, float)):
        text = str(int(value))
    else:
        text = str(value).strip()
    if not text:
        return PartialDate(year) if year is not None else None
    text = text.split("T", 1)[0].split(" ", 1)[0]

    match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", text)
    if match is None:
        match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match is not None:
        try:
            return PartialDate(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
            )
        except ValueError:
            return PartialDate(int(match.group(1)))

    match = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if match is not None:
        try:
            return PartialDate(int(match.group(1)), int(match.group(2)))
        except ValueError:
            return PartialDate(int(match.group(1)))

    match = re.fullmatch(r"(\d{4})", text)
    if match is not None:
        return PartialDate(int(match.group(1)))

    return PartialDate(year) if year is not None else None


def _release_date(info: Mapping[str, Any]) -> PartialDate | None:
    fallback_year = _positive_int(info.get("release_year"))
    for key in ("release_date", "upload_date"):
        parsed = parse_partial_date(info.get(key))
        if parsed is not None:
            return parsed
    for key in ("release_timestamp", "timestamp"):
        timestamp = _number(info.get(key))
        if timestamp is not None and timestamp > 0:
            try:
                moment = datetime.fromtimestamp(timestamp, tz=UTC)
            except (OverflowError, OSError, ValueError):
                continue
            return PartialDate(moment.year, moment.month, moment.day)
    return PartialDate(fallback_year) if fallback_year is not None else None


_LIVE_STATUS_VALUES = {status.value for status in LiveStatus}


def _live_status(info: Mapping[str, Any]) -> LiveStatus:
    raw = _text(info.get("live_status"))
    if raw in _LIVE_STATUS_VALUES:
        return LiveStatus(raw)
    if info.get("is_live") is True:
        return LiveStatus.LIVE
    if info.get("is_upcoming") is True:
        return LiveStatus.UPCOMING
    if info.get("was_live") is True:
        return LiveStatus.WAS_LIVE
    return LiveStatus.UNKNOWN


def _entry_kind(variants: Sequence[DownloadVariant]) -> MediaKind:
    kinds = {variant.kind for variant in variants}
    if MediaKind.AUDIO_VIDEO in kinds:
        return MediaKind.AUDIO_VIDEO
    if MediaKind.VIDEO in kinds:
        return MediaKind.VIDEO
    if MediaKind.AUDIO in kinds:
        return MediaKind.AUDIO
    if MediaKind.IMAGE in kinds:
        return MediaKind.IMAGE
    return MediaKind.UNKNOWN


def _extras(info: Mapping[str, Any]) -> dict[str, object]:
    extras: dict[str, object] = {}
    for key, value in info.items():
        name = str(key)
        if name.startswith("_") or name in _MAPPED_KEYS or name in _NON_EXTRA_KEYS:
            continue
        if value is None or value == "" or value == [] or value == {}:
            continue
        extras[name] = value
    return extras


class YtDlpMapper:
    """Translate one sanitized yt-dlp ``info_dict`` into a ``SourceInfo``."""

    def __init__(self, profile: SourceProfile | None = None) -> None:
        self.profile = profile or SourceProfile(name="yt_dlp")

    def map(
        self,
        info: Mapping[str, Any],
        ref: SourceRef,
        *,
        flat: bool | None = False,
        limit: int | None = None,
    ) -> SourceInfo:
        """Map one payload.

        ``flat=None`` is auto mode: collections stay flat (no formats) while
        a single item is resolved fully.  This lets the generic yt-dlp loader
        auto-detect the shape when ``classify_url`` cannot know it offline.
        """
        if not isinstance(info, Mapping):
            raise UnsupportedUrl(ref.url, source=ref.source)
        type_ = self._check_type(info, ref)
        is_collection = type_ in _COLLECTION_TYPES
        effective_flat = is_collection if flat is None else flat
        thumbnails = map_thumbnails(info)
        creator = self._creator(info)

        if is_collection:
            entries = self._entries(info, limit=limit)
            items = tuple(
                self._item(
                    entry,
                    ref,
                    flat=effective_flat,
                    position=position,
                    fallback_url=None,
                )
                for position, entry in enumerate(entries, start=1)
            )
            kind = SourceKind.COLLECTION
            title = _first_text(
                info.get("title"),
                info.get("playlist_title"),
                ref.url,
            )
        else:
            items = (
                self._item(
                    info,
                    ref,
                    flat=effective_flat,
                    position=1,
                    fallback_url=ref.url,
                ),
            )
            kind = SourceKind.SINGLE
            title = _first_text(info.get("title"), info.get("track"), ref.url)

        return SourceInfo(
            source=ref.source,
            url=ref.url,
            kind=kind,
            id=_text(info.get("id")) or ref.id,
            title=title,
            creator=creator,
            description=_text(info.get("description")),
            thumbnails=thumbnails,
            thumbnail=select_best_thumbnail(thumbnails),
            items=items,
            publisher=_text(info.get("publisher")),
        )

    def _check_type(self, info: Mapping[str, Any], ref: SourceRef) -> str:
        type_ = (_text(info.get("_type")) or "video").lower()
        if type_ in {"url", "url_transparent"}:
            raise UnresolvedRedirect(
                f"unresolved {type_} result for {ref.url}",
                url=ref.url,
            )
        return type_

    def _entries(
        self,
        info: Mapping[str, Any],
        *,
        limit: int | None,
    ) -> tuple[Mapping[str, Any], ...]:
        """Return at most ``limit`` entries without materialising the playlist.

        ``entries`` can be a lazy ``PagedList``.  We iterate only until the
        requested bound is reached so a 500-track playlist does not turn into
        hundreds of network requests during the flat stage.
        """
        raw = info.get("entries")
        if raw is None or isinstance(raw, (str, bytes, bytearray, Mapping)):
            return ()
        requested = limit if isinstance(limit, int) and limit > 0 else DEFAULT_FLAT_LIMIT
        cap = min(requested, MAX_FLAT_LIMIT)
        entries: list[Mapping[str, Any]] = []
        try:
            iterator = iter(raw)
        except TypeError:
            return ()
        for entry in iterator:
            if not isinstance(entry, Mapping):
                continue
            entries.append(entry)
            if len(entries) >= cap:
                break
        return tuple(entries)

    def _creator(self, info: Mapping[str, Any]) -> str | None:
        artists = _str_tuple(info.get("artists") or info.get("artist"))
        return _first_text(
            info.get("uploader"),
            info.get("channel"),
            info.get("creator"),
            artists[0] if artists else None,
        )

    def _item(
        self,
        info: Mapping[str, Any],
        ref: SourceRef,
        *,
        flat: bool,
        position: int,
        fallback_url: str | None,
    ) -> MediaItem:
        page_url = self._page_url(info, fallback_url=fallback_url)
        identifier = (
            _text(info.get("id"))
            or _url_identifier(page_url)
            or f"item-{position}"
        )
        title = _first_text(
            info.get("track"),
            info.get("title"),
            info.get("display_id"),
            identifier,
            fallback_url,
        ) or "item"
        creator = self._creator(info)
        artists = _str_tuple(info.get("artists") or info.get("artist"))
        if not artists and creator is not None:
            artists = (creator,)
        album_artists = _str_tuple(
            info.get("album_artists") or info.get("album_artist")
        )
        authenticated = bool(
            self.profile.cookies_file or self.profile.cookies_from_browser
        )
        variants = (
            ()
            if flat
            else tuple(build_variants(info, authenticated=authenticated))
        )
        if not artists and creator:
            artists = (creator,)
        suggested_stem = self._suggested_stem(creator, title)
        thumbnails = map_thumbnails(info)
        base = MediaItem(
            id=identifier,
            title=title,
            suggested_stem=suggested_stem,
            kind=_entry_kind(variants),
            creator=creator,
            artists=artists,
            album=_text(info.get("album")),
            album_artists=album_artists,
            duration=_number(info.get("duration")),
            live_status=_live_status(info),
            release_date=_release_date(info),
            page_url=page_url,
            thumbnails=thumbnails,
            thumbnail=select_best_thumbnail(thumbnails),
            subtitles=map_subtitles(info),
            variants=variants,
            resolution=ResolutionState.FLAT if flat else ResolutionState.FULL,
            position=position,
            group_id=_text(info.get("id")) or identifier,
            extras=_extras(info),
        )
        if flat:
            return base
        return replace(base, recommendations=default_recommendations(base))

    @staticmethod
    def _page_url(
        info: Mapping[str, Any],
        *,
        fallback_url: str | None,
    ) -> str:
        for key in ("webpage_url", "original_url", "url"):
            candidate = _text(info.get(key))
            if candidate is None:
                continue
            try:
                parsed = urlsplit(candidate)
            except ValueError:
                continue
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                return candidate
        if fallback_url:
            return fallback_url
        identifier = _text(info.get("id")) or _text(info.get("display_id"))
        suffix = f" (id {identifier})" if identifier else ""
        raise UnresolvedEntry(
            f"collection entry has no page URL{suffix}",
            entry_id=identifier,
        )

    @staticmethod
    def _suggested_stem(creator: str | None, title: str) -> str:
        if creator and title and not title.lower().startswith(f"{creator.lower()} - "):
            return f"{creator} - {title}"
        return title


def best_variant(
    variants: Sequence[DownloadVariant],
    *,
    kinds: Sequence[MediaKind] = (),
    prefer_container: str | None = None,
    prefer_codec: str | None = None,
    require_audio: bool = False,
) -> DownloadVariant | None:
    """Return the best usable variant matching optional source preferences."""
    kind_set = set(kinds)
    candidates: list[tuple[tuple[float, ...], DownloadVariant]] = []
    for variant in variants:
        if not variant.downloadable or variant.unavailable_reason is not None:
            continue
        if any(stream.drm for stream in variant.streams):
            continue
        if kind_set and variant.kind not in kind_set:
            continue
        video = next((s for s in variant.streams if s.role is StreamRole.VIDEO), None)
        audio = next((s for s in variant.streams if s.role is StreamRole.AUDIO), None)
        if require_audio and audio is None:
            continue
        container = (variant.container or "").lower()
        video_codec = _broad_codec(video.codec if video else None)
        audio_codec = _broad_codec(audio.codec if audio else None)
        container_match = 1.0 if prefer_container and container == prefer_container else 0.0
        preferred_codec = prefer_codec.lower() if prefer_codec else None
        selected_codec = video_codec if video is not None else audio_codec
        codec_match = 1.0 if preferred_codec and selected_codec == preferred_codec else 0.0
        audio_match = 1.0 if video is not None and audio_codec == "aac" else 0.0
        language_preference = (audio.language_preference or 0.0) if audio else 0.0
        height = (video.height or 0) if video else 0
        fps = (video.fps or 0.0) if video else 0.0
        bitrate = (video.bitrate_kbps or 0.0) if video else 0.0
        audio_bitrate = (audio.bitrate_kbps or 0.0) if audio else 0.0
        # Container/codec preferences dominate resolution for YouTube.
        score = (
            container_match,
            codec_match,
            audio_match,
            float(language_preference),
            float(height),
            float(fps),
            float(bitrate),
            float(audio_bitrate),
        )
        candidates.append((score, variant))
    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair[0])[1]


def best_video_variant(
    variants: Sequence[DownloadVariant],
    *,
    prefer_container: str | None = None,
    prefer_codec: str | None = None,
) -> DownloadVariant | None:
    return best_variant(
        variants,
        kinds=(MediaKind.AUDIO_VIDEO, MediaKind.VIDEO),
        prefer_container=prefer_container,
        prefer_codec=prefer_codec,
        require_audio=False,
    )


def best_audio_variant(variants: Sequence[DownloadVariant]) -> DownloadVariant | None:
    return best_variant(variants, kinds=(MediaKind.AUDIO,))


__all__ = [
    "DEFAULT_FLAT_LIMIT",
    "MAX_FLAT_LIMIT",
    "YtDlpMapper",
    "best_audio_variant",
    "best_variant",
    "best_video_variant",
    "build_variants",
    "dedupe_and_sort",
    "map_audio",
    "map_progressive",
    "map_subtitles",
    "map_thumbnails",
    "pair_video_with_audio",
    "parse_partial_date",
    "youtube_thumbnail_size",
]

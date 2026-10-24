"""Read-only initial tag observation backed by the optional mutagen extra.

The original file is opened read-only.  Embedded artwork is described by
metadata only; image/audio bytes never enter the artifact payload and the
source file is never rewritten.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from trackforge.application.dto.observations import (
    ArtworkInfo,
    InitialTagsPayload,
    NormalizedTags,
    RawTagInfo,
    TagReaderInfo,
)
from trackforge.application.dto.rendering import CoverArt, TagSet, is_synced_lyrics
from trackforge.application.ports.execution import (
    ArtifactDraft,
    StepContext,
    StepResult,
    StepWarning,
)
from trackforge.application.ports.observations import (
    INITIAL_TAGS_ARTIFACT,
    MEDIA_ARTIFACT,
    media_input,
)
from trackforge.domain.errors import DomainValidationError, ProbeFailed
from trackforge.domain.model.capabilities import (
    CapabilityKind,
    CapabilityManifest,
    Cost,
    Determinism,
    SideEffect,
)

_TAG_READER_MISSING = "tag_reader_missing"
_TAG_READER_FAILED = "tag_reader_failed"

_ARTIST_KEYS = ("artist", "TPE1", "©ART")
_ALBUM_ARTIST_KEYS = ("albumartist", "album_artist", "TPE2", "aART")
_ALBUM_KEYS = ("album", "TALB", "©alb")
_TITLE_KEYS = ("title", "TIT2", "©nam")
_DATE_KEYS = ("date", "year", "TDRC", "TYER", "©day")
_GENRE_KEYS = ("genre", "TCON", "©gen")
_BPM_KEYS = ("bpm", "TBPM", "tmpo")
_KEY_KEYS = ("key", "initialkey", "TKEY", "----:com.apple.iTunes:initialkey")
_TRACK_KEYS = ("tracknumber", "TRCK", "trkn")
_DISC_KEYS = ("discnumber", "TPOS", "disk")
_COMMENT_KEYS = ("comment", "description", "COMM", "TXXX:comment", "©cmt")
_LYRICS_KEYS = ("lyrics", "unsyncedlyrics", "USLT", "TXXX:USLT", "©lyr")

_MBID_KEYS: dict[str, tuple[str, ...]] = {
    "recording": ("musicbrainz_trackid", "musicbrainz_recordingid"),
    "release": ("musicbrainz_albumid", "musicbrainz_releaseid"),
    "release_group": ("musicbrainz_releasegroupid",),
    "artist": ("musicbrainz_artistid",),
    "work": ("musicbrainz_workid",),
    "release_track": ("musicbrainz_releasetrackid",),
}


def load_mutagen() -> Any | None:
    """Import mutagen lazily, returning ``None`` when the extra is absent."""

    try:
        return importlib.import_module("mutagen")
    except ImportError:
        return None


def _normalized_key(value: object) -> str:
    return str(value).strip().lower().replace(" ", "_").replace("-", "_")


def _lookup_values(tags: Any, aliases: Sequence[str]) -> list[Any]:
    if tags is None:
        return []
    try:
        keys = list(tags.keys())
    except (AttributeError, TypeError):
        return []

    for alias in aliases:
        if alias in keys:
            return [tags[alias]]
        for key in keys:
            if str(key).lower() == alias.lower():
                return [tags[key]]

    normalized = {(_normalized_key(key), key): key for key in keys}
    for alias in aliases:
        alias_key = _normalized_key(alias)
        for key_norm, key_original in normalized:
            if key_norm == alias_key:
                return [tags[key_original]]
        if alias_key.startswith("musicbrainz_"):
            for key_norm, key_original in normalized:
                if key_norm.endswith(alias_key):
                    return [tags[key_original]]
        if alias in {"USLT", "APIC", "TXXX"}:
            for key_norm, key_original in normalized:
                if key_norm.startswith(_normalized_key(alias)):
                    return [tags[key_original]]
    return []


def _texts(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace").strip()
        return [text] if text else []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for item in value:
            result.extend(_texts(item))
        return result
    text_attr = getattr(value, "text", None)
    if text_attr is not None:
        return _texts(text_attr)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [str(value)]
    return []


def _all_texts(tags: Any, aliases: Sequence[str]) -> list[str]:
    result: list[str] = []
    for raw in _lookup_values(tags, aliases):
        for text in _texts(raw):
            if text not in result:
                result.append(text)
    return result


def _first_text(tags: Any, aliases: Sequence[str]) -> str | None:
    values = _all_texts(tags, aliases)
    return values[0] if values else None


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        text = value.strip()
        return int(text) if text.isdigit() else None
    return None


def _pair(value: object) -> tuple[int | None, int | None]:
    if value is None:
        return None, None
    if isinstance(value, str):
        head, _, tail = value.partition("/")
        return _int(head), _int(tail) if tail else None
    if isinstance(value, bytes):
        return _pair(value.decode("utf-8", errors="replace"))
    if isinstance(value, (list, tuple)):
        if not value:
            return None, None
        if len(value) == 1:
            return _pair(value[0])
        if len(value) >= 2:
            return _int(value[0]), _int(value[1])
    text = getattr(value, "text", None)
    if text is not None:
        return _pair(text)
    return None, None


def _track_pair(tags: Any, aliases: Sequence[str]) -> tuple[int | None, int | None]:
    for raw in _lookup_values(tags, aliases):
        number, total = _pair(raw)
        if number is not None or total is not None:
            return number, total
    return None, None


def _mbids(tags: Any) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for field, aliases in _MBID_KEYS.items():
        values = _all_texts(tags, aliases)
        if values:
            result[field] = values
    return result


def _picture_type(value: object) -> str | None:
    if value is None:
        return None
    name = getattr(value, "name", None)
    return str(name) if name is not None else str(value)


def _artwork_info(
    *,
    mime_type: object = None,
    description: object = None,
    picture_type: object = None,
    width: object = None,
    height: object = None,
    data: object = None,
) -> ArtworkInfo:
    size = len(data) if isinstance(data, (bytes, bytearray)) else None
    return ArtworkInfo(
        mime_type=str(mime_type) if mime_type else None,
        description=str(description) if description else None,
        picture_type=_picture_type(picture_type),
        width=width if isinstance(width, int) and width >= 0 else None,
        height=height if isinstance(height, int) and height >= 0 else None,
        size_bytes=size,
    )


def _artwork(audio: Any, tags: Any, module: Any) -> list[ArtworkInfo]:
    result: list[ArtworkInfo] = []
    seen: set[tuple[str | None, str | None, int | None]] = set()

    def add(info: ArtworkInfo) -> None:
        key = (info.mime_type, info.description, info.size_bytes)
        if key not in seen:
            seen.add(key)
            result.append(info)

    for picture in getattr(audio, "pictures", None) or ():
        add(
            _artwork_info(
                mime_type=getattr(picture, "mime", None),
                description=getattr(picture, "desc", None),
                picture_type=getattr(picture, "type", None),
                width=getattr(picture, "width", None),
                height=getattr(picture, "height", None),
                data=getattr(picture, "data", None),
            )
        )

    if tags is not None:
        try:
            for apic in tags.getall("APIC"):
                add(
                    _artwork_info(
                        mime_type=getattr(apic, "mime", None),
                        description=getattr(apic, "desc", None),
                        picture_type=getattr(apic, "type", None),
                        data=getattr(apic, "data", None),
                    )
                )
        except (AttributeError, KeyError, TypeError):
            pass
        try:
            covers = tags.get("covr") or ()
            for cover in covers:
                image_format = str(getattr(getattr(cover, "imageformat", None), "name", ""))
                mime_type = None
                if "PNG" in image_format.upper():
                    mime_type = "image/png"
                elif "JPEG" in image_format.upper() or "JPG" in image_format.upper():
                    mime_type = "image/jpeg"
                add(
                    _artwork_info(
                        mime_type=mime_type,
                        data=getattr(cover, "data", None),
                    )
                )
        except (AttributeError, KeyError, TypeError):
            pass
        try:
            embedded = tags.get("metadata_block_picture") or ()
            if isinstance(embedded, str):
                embedded = [embedded]
            for encoded in embedded:
                if not isinstance(encoded, str):
                    continue
                raw = base64.b64decode(encoded, validate=False)
                picture = module.flac.Picture(raw)
                add(
                    _artwork_info(
                        mime_type=picture.mime,
                        description=picture.desc,
                        picture_type=picture.type,
                        width=picture.width,
                        height=picture.height,
                        data=picture.data,
                    )
                )
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
    return result


def _tag_keys(tags: Any) -> list[str]:
    if tags is None:
        return []
    try:
        return sorted({str(key) for key in tags.keys()})
    except (AttributeError, TypeError):
        return []


def read_initial_tags(path: Path, module: Any | None = None) -> InitialTagsPayload:
    """Read existing tags from ``path`` without modifying it."""

    mutagen = module if module is not None else load_mutagen()
    if mutagen is None:
        raise ProbeFailed(
            "mutagen is not installed",
            retryable=False,
            user_action="Install TrackForge with the 'tagging' extra.",
        )
    try:
        audio = mutagen.File(str(path))
    except Exception as exc:  # mutagen exposes format-specific exception types
        raise ProbeFailed(
            "mutagen could not open the media file",
            retryable=False,
            user_action="Check that the file is a valid, readable audio file.",
            context={"path": str(path), "error": str(exc)},
        ) from exc
    if audio is None:
        raise ProbeFailed(
            "mutagen did not recognize the media file",
            retryable=False,
            user_action="Check that the file is a supported audio format.",
            context={"path": str(path)},
        )

    tags = getattr(audio, "tags", None)
    track, track_total = _track_pair(tags, _TRACK_KEYS)
    disc, disc_total = _track_pair(tags, _DISC_KEYS)
    normalized = NormalizedTags(
        title=_first_text(tags, _TITLE_KEYS),
        artist=_all_texts(tags, _ARTIST_KEYS),
        album=_first_text(tags, _ALBUM_KEYS),
        albumartist=_all_texts(tags, _ALBUM_ARTIST_KEYS),
        date=_first_text(tags, _DATE_KEYS),
        genre=_all_texts(tags, _GENRE_KEYS),
        bpm=_positive_number(_first_text(tags, _BPM_KEYS)),
        key=_first_text(tags, _KEY_KEYS),
        track=track,
        track_total=track_total,
        disc=disc,
        disc_total=disc_total,
        comment=_first_text(tags, _COMMENT_KEYS),
        lyrics=_first_text(tags, _LYRICS_KEYS),
        mbids=_mbids(tags),
        artwork=_artwork(audio, tags, mutagen),
    )
    return InitialTagsPayload(
        tags=normalized,
        raw=RawTagInfo(
            format=type(audio).__name__,
            mime_types=[str(item) for item in (getattr(audio, "mime", None) or ())],
            tag_keys=_tag_keys(tags),
        ),
        reader=TagReaderInfo(id="mutagen", version=str(mutagen.version_string)),
    )


def _tag_skip(
    message: str,
    *,
    capability_id: str,
    user_action: str | None,
) -> StepResult:
    return StepResult.skipped(
        warnings=(
            StepWarning(
                code=_TAG_READER_FAILED,
                message=message,
                capability_id=capability_id,
                user_action=user_action,
            ),
        ),
        actual_cost=Cost(cpu_seconds=0.1),
    )


class InitialTagsObserver:
    """Expose the source file's existing tags as a weak observation."""

    manifest = CapabilityManifest(
        id="initial_tags",
        version="1",
        kind=CapabilityKind.OBSERVER,
        requires=(MEDIA_ARTIFACT,),
        provides=(INITIAL_TAGS_ARTIFACT,),
        cost=Cost(cpu_seconds=0.1, memory_mb=32.0),
        determinism=Determinism.DETERMINISTIC,
        cacheable=True,
        side_effects=SideEffect.NONE,
        license="GPL-2.0-or-later",
        description="Read existing tags with mutagen; never write to the source.",
    )

    async def run(self, ctx: StepContext) -> StepResult:
        media_step = ctx.inputs.get(MEDIA_ARTIFACT)
        if media_step is None:
            raise DomainValidationError("initial_tags requires a media input artifact")
        media = media_input(media_step)
        path = Path(media.cas_path)
        if not path.is_file():
            raise ProbeFailed(
                "media bytes are missing from the content-addressed store",
                user_action="Re-acquire the source file.",
                context={"path": str(path)},
            )

        module = load_mutagen()
        if module is None:
            return StepResult.skipped(
                warnings=(
                    StepWarning(
                        code=_TAG_READER_MISSING,
                        message="mutagen is not installed; existing tags were not read",
                        capability_id=self.manifest.id,
                        user_action="Install TrackForge with the 'tagging' extra.",
                    ),
                ),
                actual_cost=Cost(cpu_seconds=0.0),
            )

        try:
            payload = await asyncio.to_thread(read_initial_tags, path, module)
        except ProbeFailed as exc:
            return _tag_skip(
                exc.message, capability_id=self.manifest.id, user_action=exc.user_action
            )
        except Exception as exc:  # a broken tag cannot fail the observation run
            return _tag_skip(
                f"existing tags could not be read: {exc}",
                capability_id=self.manifest.id,
                user_action="Check that the file's tags are valid; the audio bytes are untouched.",
            )
        return StepResult(
            artifacts=(
                ArtifactDraft(
                    type=INITIAL_TAGS_ARTIFACT,
                    payload=payload.model_dump(mode="json"),
                ),
            ),
            actual_cost=Cost(cpu_seconds=0.1, memory_mb=32.0),
        )


# ---------------------------------------------------------------------------
# Canonical read-back
# ---------------------------------------------------------------------------

_MBID_READ_KEYS: dict[str, tuple[str, ...]] = {
    "mb_recording_id": (
        "MusicBrainz Track Id",
        "MusicBrainz Recording Id",
        "MUSICBRAINZ_TRACKID",
        "MUSICBRAINZ_RECORDINGID",
    ),
    "mb_release_id": ("MusicBrainz Album Id", "MusicBrainz Release Id", "MUSICBRAINZ_ALBUMID"),
    "mb_release_group_id": (
        "MusicBrainz Release Group Id",
        "MUSICBRAINZ_RELEASEGROUPID",
    ),
    "mb_artist_id": ("MusicBrainz Artist Id", "MUSICBRAINZ_ARTISTID"),
    "mb_albumartist_id": (
        "MusicBrainz Album Artist Id",
        "MusicBrainz Release Artist Id",
        "MUSICBRAINZ_ALBUMARTISTID",
    ),
    "acoustid": ("Acoustid Id", "ACOUSTID_ID"),
    "isrc": ("ISRC",),
}

_VORBIS_KEYS = {
    "title": ("TITLE",),
    "artists": ("ARTIST",),
    "albumartist": ("ALBUMARTIST", "ALBUM ARTIST"),
    "album": ("ALBUM",),
    "date": ("DATE",),
    "originaldate": ("ORIGINALDATE", "ORIGINAL DATE"),
    "track": ("TRACKNUMBER", "TRACK"),
    "tracktotal": ("TRACKTOTAL", "TOTALTRACKS"),
    "disc": ("DISCNUMBER", "DISC"),
    "disctotal": ("DISCTOTAL", "TOTALDISCS"),
    "genre": ("GENRE",),
    "bpm": ("BPM",),
    "key": ("KEY", "INITIALKEY"),
    "language": ("LANGUAGE",),
    "label": ("LABEL", "ORGANIZATION", "PUBLISHER"),
    "catalog_number": ("CATALOGNUMBER", "CATALOGNUMBER"),
    "barcode": ("BARCODE",),
    "isrc": ("ISRC",),
    "compilation": ("COMPILATION",),
    "comment": ("COMMENT",),
    "description": ("DESCRIPTION",),
    "lyrics": ("LYRICS", "UNSYNCEDLYRICS"),
    "copyright": ("COPYRIGHT",),
    "license": ("LICENSE",),
    "source_url": ("TRACKFORGE_SOURCE_URL", "SOURCE_URL"),
    "source_id": ("TRACKFORGE_SOURCE_ID", "SOURCE_ID"),
}


def _frame_values(tags: Any, frame_id: str) -> list[str]:
    values: list[str] = []
    try:
        frames = tags.getall(frame_id)
    except (AttributeError, KeyError, TypeError):
        return values
    for frame in frames:
        values.extend(_texts(getattr(frame, "text", None)))
    return values


def _frame_first(tags: Any, frame_id: str) -> str | None:
    values = _frame_values(tags, frame_id)
    return values[0] if values else None


def _id3_multi(values: list[str], version: tuple[int, ...]) -> list[str]:
    """Split pre-2.4 ID3 values, which mutagen serializes with ``/``."""

    if len(values) != 1 or version >= (2, 4, 0):
        return values
    text = values[0]
    separator = "/" if "/" in text else ("; " if "; " in text else None)
    if separator is None:
        return values
    return [part.strip() for part in text.split(separator) if part.strip()]


def _id3_txxx(tags: Any) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    try:
        frames = tags.getall("TXXX")
    except (AttributeError, KeyError, TypeError):
        return result
    for frame in frames:
        description = str(getattr(frame, "desc", "") or "")
        if description:
            result[description] = _texts(getattr(frame, "text", None))
    return result


def _txxx_lookup(txxx: dict[str, list[str]], aliases: Sequence[str]) -> str | None:
    lowered = {key.casefold(): values for key, values in txxx.items()}
    for alias in aliases:
        values = lowered.get(alias.casefold())
        if values:
            return values[0]
    return None


def _pair_values(value: str | None) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    head, _, tail = value.partition("/")
    number = _int(head)
    total = _int(tail) if tail else None
    return (
        number if number not in {None, 0} else None,
        total if total not in {None, 0} else None,
    )


def _read_id3(tags: Any, module: Any) -> TagSet:
    del module
    version = tuple(getattr(tags, "version", (2, 4, 0)))
    txxx = _id3_txxx(tags)
    track, tracktotal = _pair_values(_frame_first(tags, "TRCK"))
    disc, disctotal = _pair_values(_frame_first(tags, "TPOS"))
    date = _frame_first(tags, "TDRC")
    if date is None:
        year = _frame_first(tags, "TYER")
        day_month = _frame_first(tags, "TDAT")
        if year and day_month and len(day_month) == 4:
            date = f"{year}-{day_month[2:4]}-{day_month[0:2]}"
        elif year:
            date = year
    bpm_text = _frame_first(tags, "TBPM")
    mbids: dict[str, str | None] = {}
    for field_name, aliases in _MBID_READ_KEYS.items():
        if field_name == "isrc":
            continue
        mbids[field_name] = _txxx_lookup(txxx, aliases)
    trackforge = {
        key.upper(): values[0]
        for key, values in txxx.items()
        if key.upper().startswith("TRACKFORGE_") and values
    }
    comments = _frame_values(tags, "COMM")
    if not comments:
        fallback = _txxx_lookup(txxx, ("COMMENT", "comment"))
        if fallback:
            comments = [fallback]
    lyrics = _frame_values(tags, "USLT")
    compilation_text = _frame_first(tags, "TCMP")
    return TagSet(
        title=_frame_first(tags, "TIT2"),
        artists=tuple(_id3_multi(_frame_values(tags, "TPE1"), version)),
        albumartist=_frame_first(tags, "TPE2"),
        album=_frame_first(tags, "TALB"),
        date=date,
        originaldate=_frame_first(tags, "TDOR"),
        track=track,
        tracktotal=tracktotal,
        disc=disc,
        disctotal=disctotal,
        genre=tuple(_id3_multi(_frame_values(tags, "TCON"), version)),
        bpm=_positive_number(bpm_text),
        key=_frame_first(tags, "TKEY"),
        language=_frame_first(tags, "TLAN"),
        label=_frame_first(tags, "TPUB"),
        catalog_number=_txxx_lookup(txxx, ("CATALOGNUMBER",)),
        barcode=_txxx_lookup(txxx, ("BARCODE",)),
        isrc=_txxx_lookup(txxx, ("ISRC",)),
        compilation=(
            None
            if compilation_text is None
            else compilation_text.strip().lower() in {"1", "true", "yes"}
        ),
        comment=comments[0] if comments else None,
        description=_txxx_lookup(txxx, ("DESCRIPTION", "description")),
        lyrics=_plain_lyrics(lyrics[0] if lyrics else None),
        source_url=trackforge.get("TRACKFORGE_SOURCE_URL") or _txxx_lookup(txxx, ("SOURCE_URL",)),
        source_id=trackforge.get("TRACKFORGE_SOURCE_ID") or _txxx_lookup(txxx, ("SOURCE_ID",)),
        copyright=_frame_first(tags, "TCOP"),
        license=_txxx_lookup(txxx, ("LICENSE",)),
        mb_recording_id=mbids.get("mb_recording_id"),
        mb_release_id=mbids.get("mb_release_id"),
        mb_release_group_id=mbids.get("mb_release_group_id"),
        mb_artist_id=mbids.get("mb_artist_id"),
        mb_albumartist_id=mbids.get("mb_albumartist_id"),
        acoustid=mbids.get("acoustid"),
        trackforge=trackforge,
    )


def _mp4_values(tags: Any, key: str) -> list[str]:
    try:
        raw = tags.get(key, [])
    except (AttributeError, KeyError, TypeError):
        return []
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    result: list[str] = []
    for value in raw:
        if isinstance(value, (bytes, bytearray)):
            text = bytes(value).decode("utf-8", errors="replace").strip()
        elif isinstance(value, str):
            text = value.strip()
        elif isinstance(value, bool):
            continue
        else:
            data = getattr(value, "data", None)
            if isinstance(data, (bytes, bytearray)):
                text = bytes(data).decode("utf-8", errors="replace").strip()
            else:
                text = str(value).strip()
        if text:
            result.append(text)
    return result


def _mp4_freeform(tags: Any) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    try:
        keys = list(tags.keys())
    except (AttributeError, TypeError):
        return result
    for key in keys:
        text_key = str(key)
        if not text_key.startswith("----:"):
            continue
        descriptor = text_key.rsplit(":", 1)[-1]
        result[descriptor] = _mp4_values(tags, text_key)
    return result


def _freeform_lookup(freeform: dict[str, list[str]], aliases: Sequence[str]) -> str | None:
    lowered = {key.casefold(): values for key, values in freeform.items()}
    for alias in aliases:
        values = lowered.get(alias.casefold())
        if values:
            return values[0]
    return None


def _read_mp4(tags: Any) -> TagSet:
    freeform = _mp4_freeform(tags)
    track_values = []
    try:
        track_values = list(tags.get("trkn", []))
        disc_values = list(tags.get("disk", []))
    except (AttributeError, KeyError, TypeError):
        disc_values = []
    track, tracktotal = _pair_from_mp4(track_values[0]) if track_values else (None, None)
    disc, disctotal = _pair_from_mp4(disc_values[0]) if disc_values else (None, None)
    bpm_values = _mp4_values(tags, "tmpo")
    bpm_text = _freeform_lookup(freeform, ("BPM",))
    bpm = _positive_number(bpm_text)
    if bpm is None:
        bpm = _positive_number(bpm_values[0] if bpm_values else None)
    compilation = None
    try:
        raw_compilation = tags.get("cpil", [])
    except (AttributeError, KeyError, TypeError):
        raw_compilation = []
    if isinstance(raw_compilation, (list, tuple)):
        compilation = bool(raw_compilation[0]) if raw_compilation else None
    elif raw_compilation is not None:
        compilation = bool(raw_compilation)
    trackforge = {
        key.upper(): values[0]
        for key, values in freeform.items()
        if key.upper().startswith("TRACKFORGE_") and values
    }
    return TagSet(
        title=_first_mp4(tags, "©nam"),
        artists=tuple(_mp4_values(tags, "©ART")),
        albumartist=_first_mp4(tags, "aART"),
        album=_first_mp4(tags, "©alb"),
        date=_first_mp4(tags, "©day"),
        originaldate=_freeform_lookup(freeform, ("ORIGINALDATE", "ORIGINAL DATE")),
        track=track,
        tracktotal=tracktotal,
        disc=disc,
        disctotal=disctotal,
        genre=tuple(_mp4_values(tags, "©gen")),
        bpm=bpm,
        key=_freeform_lookup(freeform, ("KEY", "INITIALKEY")),
        language=_freeform_lookup(freeform, ("LANGUAGE",)),
        label=_freeform_lookup(freeform, ("LABEL",)),
        catalog_number=_freeform_lookup(freeform, ("CATALOGNUMBER",)),
        barcode=_freeform_lookup(freeform, ("BARCODE",)),
        isrc=_freeform_lookup(freeform, ("ISRC",)),
        compilation=compilation,
        comment=_first_mp4(tags, "©cmt"),
        description=_freeform_lookup(freeform, ("DESCRIPTION",)),
        lyrics=_plain_lyrics(_first_mp4(tags, "©lyr")),
        source_url=trackforge.get("TRACKFORGE_SOURCE_URL")
        or _freeform_lookup(freeform, ("SOURCE_URL",)),
        source_id=trackforge.get("TRACKFORGE_SOURCE_ID")
        or _freeform_lookup(freeform, ("SOURCE_ID",)),
        copyright=_first_mp4(tags, "cprt"),
        license=_freeform_lookup(freeform, ("LICENSE",)),
        mb_recording_id=_freeform_lookup(
            freeform,
            ("MusicBrainz Track Id", "MusicBrainz Recording Id"),
        ),
        mb_release_id=_freeform_lookup(
            freeform,
            ("MusicBrainz Album Id", "MusicBrainz Release Id"),
        ),
        mb_release_group_id=_freeform_lookup(freeform, ("MusicBrainz Release Group Id",)),
        mb_artist_id=_freeform_lookup(freeform, ("MusicBrainz Artist Id",)),
        mb_albumartist_id=_freeform_lookup(freeform, ("MusicBrainz Album Artist Id",)),
        acoustid=_freeform_lookup(freeform, ("Acoustid Id",)),
        trackforge=trackforge,
    )


def _first_mp4(tags: Any, key: str) -> str | None:
    values = _mp4_values(tags, key)
    return values[0] if values else None


def _pair_from_mp4(value: object) -> tuple[int | None, int | None]:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        number = _int(value[0])
        total = _int(value[1])
    else:
        number, total = _pair_values(str(value))
    return (
        number if number not in {None, 0} else None,
        total if total not in {None, 0} else None,
    )


def _vorbis_map(tags: Any) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    if tags is None:
        return result
    try:
        keys = list(tags.keys())
    except (AttributeError, TypeError):
        return result
    for key in keys:
        normalized = str(key).strip().upper()
        try:
            raw = tags.get(key)
        except (AttributeError, KeyError, TypeError):
            continue
        values = _texts(raw)
        if values:
            result.setdefault(normalized, []).extend(
                item for item in values if item not in result.get(normalized, [])
            )
    return result


def _vorbis_first(tags: dict[str, list[str]], aliases: Sequence[str]) -> str | None:
    for alias in aliases:
        values = tags.get(alias.upper())
        if values:
            return values[0]
    return None


def _read_vorbis(tags: Any) -> TagSet:
    values = _vorbis_map(tags)
    bpm_text = _vorbis_first(values, _VORBIS_KEYS["bpm"])
    compilation_text = _vorbis_first(values, _VORBIS_KEYS["compilation"])
    track, track_pair_total = _pair_values(_vorbis_first(values, _VORBIS_KEYS["track"]))
    tracktotal = _int(_vorbis_first(values, _VORBIS_KEYS["tracktotal"]))
    if tracktotal is None:
        tracktotal = track_pair_total
    disc, disc_pair_total = _pair_values(_vorbis_first(values, _VORBIS_KEYS["disc"]))
    disctotal = _int(_vorbis_first(values, _VORBIS_KEYS["disctotal"]))
    if disctotal is None:
        disctotal = disc_pair_total
    comment = _vorbis_first(values, _VORBIS_KEYS["comment"])
    description = _vorbis_first(values, _VORBIS_KEYS["description"])
    if comment is None:
        comment = description
    trackforge = {
        key: items[0] for key, items in values.items() if key.startswith("TRACKFORGE_") and items
    }

    def joined(field_name: str) -> str | None:
        artists = values.get(field_name)
        return ", ".join(artists) if artists else None

    return TagSet(
        title=_vorbis_first(values, _VORBIS_KEYS["title"]),
        artists=tuple(values.get("ARTIST", ())),
        albumartist=joined("ALBUMARTIST"),
        album=_vorbis_first(values, _VORBIS_KEYS["album"]),
        date=_vorbis_first(values, _VORBIS_KEYS["date"]),
        originaldate=_vorbis_first(values, _VORBIS_KEYS["originaldate"]),
        track=track,
        tracktotal=tracktotal,
        disc=disc,
        disctotal=disctotal,
        genre=tuple(values.get("GENRE", ())),
        bpm=_positive_number(bpm_text),
        key=_vorbis_first(values, _VORBIS_KEYS["key"]),
        language=_vorbis_first(values, _VORBIS_KEYS["language"]),
        label=_vorbis_first(values, _VORBIS_KEYS["label"]),
        catalog_number=_vorbis_first(values, _VORBIS_KEYS["catalog_number"]),
        barcode=_vorbis_first(values, _VORBIS_KEYS["barcode"]),
        isrc=_vorbis_first(values, _VORBIS_KEYS["isrc"]),
        compilation=(
            None
            if compilation_text is None
            else compilation_text.strip().lower() in {"1", "true", "yes"}
        ),
        comment=comment,
        description=description,
        lyrics=_plain_lyrics(_vorbis_first(values, _VORBIS_KEYS["lyrics"])),
        source_url=_vorbis_first(values, _VORBIS_KEYS["source_url"]),
        source_id=_vorbis_first(values, _VORBIS_KEYS["source_id"]),
        copyright=_vorbis_first(values, _VORBIS_KEYS["copyright"]),
        license=_vorbis_first(values, _VORBIS_KEYS["license"]),
        mb_recording_id=_vorbis_first(
            values,
            ("MUSICBRAINZ_TRACKID", "MUSICBRAINZ_RECORDINGID"),
        ),
        mb_release_id=_vorbis_first(
            values,
            ("MUSICBRAINZ_ALBUMID", "MUSICBRAINZ_RELEASEID"),
        ),
        mb_release_group_id=_vorbis_first(values, ("MUSICBRAINZ_RELEASEGROUPID",)),
        mb_artist_id=_vorbis_first(values, ("MUSICBRAINZ_ARTISTID",)),
        mb_albumartist_id=_vorbis_first(values, ("MUSICBRAINZ_ALBUMARTISTID",)),
        acoustid=_vorbis_first(values, ("ACOUSTID_ID", "ACOUSTID")),
        trackforge=trackforge,
    )


def _plain_lyrics(value: str | None) -> str | None:
    """Never surface LRC timecodes as canonical plain lyrics."""

    return None if is_synced_lyrics(value) else value


def _is_number(value: str) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _positive_number(value: str | None) -> float | None:
    if value is None or not _is_number(value):
        return None
    number = float(value)
    return number if number > 0 and number < float("inf") else None


def _read_cover_from_audio(audio: Any, module: Any) -> CoverArt | None:
    class_name = type(audio).__name__
    tags = getattr(audio, "tags", None)
    if class_name == "MP3":
        if tags is not None:
            try:
                pictures = tags.getall("APIC")
            except (AttributeError, KeyError, TypeError):
                pictures = []
            for picture in pictures:
                if getattr(picture, "type", None) == 3 or picture is pictures[0]:
                    try:
                        return CoverArt(
                            data=getattr(picture, "data", b""),
                            mime_type=str(getattr(picture, "mime", "")),
                            description=str(getattr(picture, "desc", "") or ""),
                        )
                    except Exception:  # noqa: S112 - skip malformed cover frames
                        continue
        return None
    if class_name == "MP4":
        if tags is not None:
            try:
                covers = tags.get("covr", [])
            except (AttributeError, KeyError, TypeError):
                covers = []
            for cover in covers or []:
                image_format = getattr(cover, "imageformat", None)
                mime = "image/png" if int(image_format or 0) == 14 else "image/jpeg"
                try:
                    return CoverArt(data=bytes(cover), mime_type=mime)
                except Exception:  # noqa: S112 - skip malformed cover frames
                    continue
        return None
    if class_name == "FLAC":
        for picture in getattr(audio, "pictures", None) or ():
            if getattr(picture, "type", None) == 3:
                try:
                    width = getattr(picture, "width", None)
                    height = getattr(picture, "height", None)
                    return CoverArt(
                        data=getattr(picture, "data", b""),
                        mime_type=str(getattr(picture, "mime", "")),
                        description=str(getattr(picture, "desc", "") or ""),
                        width=width if isinstance(width, int) and width > 0 else None,
                        height=height if isinstance(height, int) and height > 0 else None,
                    )
                except Exception:
                    return None
        return None
    if class_name in {"OggVorbis", "OggOpus"} and tags is not None:
        try:
            encoded_values = tags.get("metadata_block_picture") or []
        except (AttributeError, KeyError, TypeError):
            encoded_values = []
        if isinstance(encoded_values, str):
            encoded_values = [encoded_values]
        for encoded in encoded_values:
            try:
                raw = base64.b64decode(str(encoded), validate=False)
                picture = module.flac.Picture(raw)
                return CoverArt(
                    data=picture.data,
                    mime_type=str(picture.mime),
                    description=str(picture.desc or ""),
                    width=picture.width if picture.width > 0 else None,
                    height=picture.height if picture.height > 0 else None,
                )
            except Exception:  # noqa: S112 - skip malformed picture blocks
                continue
    return None


class MutagenTagReader:
    """Read canonical tags and cover art with mutagen."""

    def __init__(self, module: Any | None = None) -> None:
        self._module = module

    def _mutagen(self) -> Any:
        module = self._module if self._module is not None else load_mutagen()
        if module is None:
            raise ProbeFailed(
                "mutagen is not installed",
                retryable=False,
                user_action="Install TrackForge with the 'tagging' extra.",
            )
        return module

    def _open(self, path: Path) -> Any:
        module = self._mutagen()
        try:
            audio = module.File(str(path))
        except Exception as exc:  # mutagen raises format-specific errors
            raise ProbeFailed(
                "mutagen could not open the media file",
                retryable=False,
                user_action="Check that the file is a valid, readable audio file.",
                context={"path": str(path), "error": str(exc)},
            ) from exc
        if audio is None:
            raise ProbeFailed(
                "mutagen did not recognize the media file",
                retryable=False,
                user_action="Check that the file is a supported audio format.",
                context={"path": str(path)},
            )
        return audio

    def read(self, path: Path) -> TagSet:
        """Read canonical values without modifying the file."""

        audio = self._open(Path(path))
        class_name = type(audio).__name__
        tags = getattr(audio, "tags", None)
        module = self._mutagen()
        try:
            if class_name == "MP3":
                return _read_id3(tags, module) if tags is not None else TagSet()
            if class_name == "MP4":
                return _read_mp4(tags) if tags is not None else TagSet()
            if class_name == "FLAC":
                return _read_vorbis(getattr(audio, "tags", None))
            if class_name in {"OggVorbis", "OggOpus"}:
                return _read_vorbis(getattr(audio, "tags", None))
        except DomainValidationError:
            raise
        except Exception as exc:
            raise ProbeFailed(
                "canonical tags could not be decoded",
                retryable=False,
                user_action="Check that the file's tags are valid.",
                context={"path": str(path), "error": str(exc)},
            ) from exc
        raise ProbeFailed(
            "the tag reader does not support this file format",
            retryable=False,
            user_action="Use MP3, M4A, FLAC, OGG, or Opus output.",
            context={"path": str(path), "format": class_name},
        )

    def read_cover(self, path: Path) -> CoverArt | None:
        """Read the embedded front cover, if one exists."""

        audio = self._open(Path(path))
        return _read_cover_from_audio(audio, self._mutagen())


__all__ = [
    "INITIAL_TAGS_ARTIFACT",
    "InitialTagsObserver",
    "MutagenTagReader",
    "load_mutagen",
    "read_initial_tags",
]

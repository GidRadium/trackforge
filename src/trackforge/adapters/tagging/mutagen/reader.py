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


__all__ = [
    "INITIAL_TAGS_ARTIFACT",
    "InitialTagsObserver",
    "load_mutagen",
    "read_initial_tags",
]

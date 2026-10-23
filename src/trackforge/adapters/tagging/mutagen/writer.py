"""mutagen-backed tag writer for the five MVP output formats.

The writer always works on a temporary copy in the destination directory and
commits it with ``fsync`` + ``rename``.  It never touches the CAS source.  The
canonical ``TagSet`` is mapped per format; LRC/ELRC is rejected by ``TagSet``
and therefore can never end up in a plain-lyrics field.
"""

from __future__ import annotations

import base64
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from trackforge.adapters.tagging.mutagen.reader import MutagenTagReader, load_mutagen
from trackforge.application.dto.rendering import (
    CoverArt,
    RenderOptions,
    TagMismatch,
    TagSet,
)
from trackforge.domain.errors import ProbeFailed, RenderFailed

_ID3_MANAGED_FRAMES = frozenset(
    {
        "TIT2",
        "TPE1",
        "TPE2",
        "TALB",
        "TDRC",
        "TYER",
        "TDAT",
        "TDOR",
        "TRCK",
        "TPOS",
        "TCON",
        "TBPM",
        "TKEY",
        "TLAN",
        "TPUB",
        "TCMP",
        "TCOP",
        "COMM",
        "USLT",
        "APIC",
    }
)

_ID3_MANAGED_TXXX = frozenset(
    {
        "CATALOGNUMBER",
        "BARCODE",
        "ISRC",
        "ACOUSTID ID",
        "MUSICBRAINZ TRACK ID",
        "MUSICBRAINZ RECORDING ID",
        "MUSICBRAINZ ALBUM ID",
        "MUSICBRAINZ RELEASE ID",
        "MUSICBRAINZ RELEASE GROUP ID",
        "MUSICBRAINZ ARTIST ID",
        "MUSICBRAINZ ALBUM ARTIST ID",
        "DESCRIPTION",
        "LICENSE",
        "SOURCE_URL",
        "SOURCE_ID",
    }
)

_MP4_MANAGED_ATOMS = frozenset(
    {
        "©nam",
        "©ART",
        "aART",
        "©alb",
        "©day",
        "trkn",
        "disk",
        "©gen",
        "tmpo",
        "©lyr",
        "©cmt",
        "covr",
        "cpil",
        "cprt",
    }
)

_MP4_MANAGED_FREEFORM = frozenset(
    {
        "MUSICBRAINZ TRACK ID",
        "MUSICBRAINZ RECORDING ID",
        "MUSICBRAINZ ALBUM ID",
        "MUSICBRAINZ RELEASE ID",
        "MUSICBRAINZ RELEASE GROUP ID",
        "MUSICBRAINZ ARTIST ID",
        "MUSICBRAINZ ALBUM ARTIST ID",
        "ACOUSTID ID",
        "ISRC",
        "KEY",
        "LANGUAGE",
        "BPM",
        "LABEL",
        "CATALOGNUMBER",
        "BARCODE",
        "ORIGINALDATE",
        "DESCRIPTION",
        "LICENSE",
        "SOURCE_URL",
        "SOURCE_ID",
    }
)

_VORBIS_MANAGED = frozenset(
    {
        "TITLE",
        "ARTIST",
        "ALBUMARTIST",
        "ALBUM",
        "DATE",
        "ORIGINALDATE",
        "TRACKNUMBER",
        "TRACKTOTAL",
        "DISCNUMBER",
        "DISCTOTAL",
        "GENRE",
        "BPM",
        "KEY",
        "LANGUAGE",
        "LABEL",
        "CATALOGNUMBER",
        "BARCODE",
        "ISRC",
        "COMPILATION",
        "COMMENT",
        "DESCRIPTION",
        "LYRICS",
        "UNSYNCEDLYRICS",
        "COPYRIGHT",
        "LICENSE",
        "MUSICBRAINZ_TRACKID",
        "MUSICBRAINZ_RECORDINGID",
        "MUSICBRAINZ_ALBUMID",
        "MUSICBRAINZ_RELEASEID",
        "MUSICBRAINZ_RELEASEGROUPID",
        "MUSICBRAINZ_ARTISTID",
        "MUSICBRAINZ_ALBUMARTISTID",
        "ACOUSTID_ID",
        "METADATA_BLOCK_PICTURE",
    }
)


class MutagenTagWriter:
    """Write, replace, and verify canonical tags with mutagen."""

    def __init__(
        self,
        module: Any | None = None,
        *,
        reader: MutagenTagReader | None = None,
    ) -> None:
        self._module = module
        self._reader = reader or MutagenTagReader(module)

    def _mutagen(self) -> Any:
        module = self._module if self._module is not None else load_mutagen()
        if module is None:
            raise RenderFailed(
                "mutagen is not installed",
                user_action="Install TrackForge with the 'tagging' extra.",
            )
        return module

    def write(
        self,
        path: Path,
        tagset: TagSet,
        cover: CoverArt | None,
        options: RenderOptions,
    ) -> None:
        """Replace canonical fields in ``path`` atomically."""

        if not isinstance(tagset, TagSet):
            raise RenderFailed("tag writer requires a TagSet")
        target = Path(path)
        if not target.is_file():
            raise RenderFailed(
                "audio file to tag does not exist",
                context={"path": str(target)},
            )
        temporary = target.with_name(f".{target.stem}-{uuid.uuid4().hex}.tagtmp{target.suffix}")
        try:
            shutil.copy2(target, temporary)
            audio = self._open(temporary)
            self._apply(audio, tagset, cover, options)
            self._save(audio, options)
            _fsync_file(temporary)
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        except RenderFailed:
            temporary.unlink(missing_ok=True)
            raise
        except Exception as exc:  # mutagen raises format-specific exceptions
            temporary.unlink(missing_ok=True)
            raise RenderFailed(
                "tags could not be written",
                user_action="Check that the audio file is valid and writable.",
                context={"path": str(target), "error": str(exc)},
            ) from exc

    def verify(
        self,
        path: Path,
        tagset: TagSet,
        cover: CoverArt | None,
    ) -> list[TagMismatch]:
        """Read the file back and report every canonical mismatch."""

        target = Path(path)
        try:
            actual = self._reader.read(target)
            actual_cover = self._reader.read_cover(target)
        except ProbeFailed as exc:
            raise RenderFailed(
                "rendered file could not be read back",
                user_action=exc.user_action,
                context={"path": str(target), "error": exc.message},
            ) from exc
        mismatches = _compare_tagset(tagset, actual)
        if cover is not None:
            if actual_cover is None:
                mismatches.append(
                    TagMismatch(
                        field="cover",
                        expected=f"{cover.mime_type}:{len(cover.data)} bytes",
                        actual=None,
                        detail="embedded cover is missing",
                    )
                )
            elif actual_cover.mime_type != cover.mime_type:
                mismatches.append(
                    TagMismatch(
                        field="cover_mime",
                        expected=cover.mime_type,
                        actual=actual_cover.mime_type,
                    )
                )
            elif actual_cover.data != cover.data:
                mismatches.append(
                    TagMismatch(
                        field="cover",
                        expected=f"{len(cover.data)} bytes",
                        actual=f"{len(actual_cover.data)} bytes",
                        detail="embedded cover bytes differ",
                    )
                )
        return mismatches

    def _open(self, path: Path) -> Any:
        module = self._mutagen()
        try:
            audio = module.File(str(path))
        except Exception as exc:
            raise RenderFailed(
                "mutagen could not open the rendered file",
                user_action="Check that the source audio produced a valid output file.",
                context={"path": str(path), "error": str(exc)},
            ) from exc
        if audio is None:
            raise RenderFailed(
                "mutagen did not recognize the rendered file",
                user_action="Use a source format supported by the MVP renderer.",
                context={"path": str(path)},
            )
        return audio

    def _save(self, audio: Any, options: RenderOptions) -> None:
        class_name = type(audio).__name__
        if class_name == "MP3":
            audio.save(v2_version=options.id3_version, v1=0)
            return
        if class_name in {"MP4", "FLAC", "OggVorbis", "OggOpus"}:
            audio.save()
            return
        raise RenderFailed(
            "the tag writer does not support this file format",
            user_action="Use MP3, M4A, FLAC, OGG, or Opus output.",
            context={"format": class_name},
        )

    def _apply(
        self,
        audio: Any,
        tagset: TagSet,
        cover: CoverArt | None,
        options: RenderOptions,
    ) -> None:
        class_name = type(audio).__name__
        if class_name == "MP3":
            self._apply_id3(audio, tagset, cover, options)
        elif class_name == "MP4":
            self._apply_mp4(audio, tagset, cover, options)
        elif class_name == "FLAC":
            self._apply_vorbis(audio, tagset, cover, options, flac=True)
        elif class_name in {"OggVorbis", "OggOpus"}:
            self._apply_vorbis(audio, tagset, cover, options, flac=False)
        else:
            raise RenderFailed(
                "the tag writer does not support this file format",
                user_action="Use MP3, M4A, FLAC, OGG, or Opus output.",
                context={"format": class_name},
            )

    # -- ID3 -------------------------------------------------------------

    def _apply_id3(
        self,
        audio: Any,
        tagset: TagSet,
        cover: CoverArt | None,
        options: RenderOptions,
    ) -> None:
        module = self._mutagen()
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        if not options.preserve_unknown_tags:
            tags.clear()
        else:
            _remove_managed_id3(tags)

        frames: list[tuple[str, list[str]]] = [
            ("TIT2", _one(tagset.title)),
            ("TPE1", list(tagset.artists)),
            ("TPE2", _one(tagset.albumartist)),
            ("TALB", _one(tagset.album)),
            ("TDRC", _one(tagset.date)),
            ("TDOR", _one(tagset.originaldate)),
            ("TRCK", _track_value(tagset.track, tagset.tracktotal)),
            ("TPOS", _track_value(tagset.disc, tagset.disctotal)),
            ("TCON", list(tagset.genre)),
            ("TBPM", _one(_number_text(tagset.bpm))),
            ("TKEY", _one(tagset.key)),
            ("TLAN", _one(tagset.language)),
            ("TPUB", _one(tagset.label)),
            ("TCOP", _one(tagset.copyright)),
        ]
        for frame_id, values in frames:
            if values:
                tags.add(_id3_text_frame(module, frame_id, values))

        if tagset.compilation is not None:
            tags.add(
                module.id3.TCMP(
                    encoding=3,
                    text=["1" if tagset.compilation else "0"],
                )
            )
        if tagset.comment:
            tags.add(module.id3.COMM(encoding=3, lang="eng", desc="", text=[tagset.comment]))
        if tagset.lyrics:
            tags.add(module.id3.USLT(encoding=3, lang="eng", desc="", text=tagset.lyrics))

        for description, value in _txxx_values(tagset).items():
            tags.add(module.id3.TXXX(encoding=3, desc=description, text=[value]))
        if cover is not None:
            tags.add(
                module.id3.APIC(
                    encoding=3,
                    mime=cover.mime_type,
                    type=3,
                    desc=cover.description,
                    data=cover.data,
                )
            )

    # -- MP4 -------------------------------------------------------------

    def _apply_mp4(
        self,
        audio: Any,
        tagset: TagSet,
        cover: CoverArt | None,
        options: RenderOptions,
    ) -> None:
        module = self._mutagen()
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        if not options.preserve_unknown_tags:
            tags.clear()
        else:
            _remove_managed_mp4(tags)

        text_atoms = {
            "©nam": tagset.title,
            "aART": tagset.albumartist,
            "©alb": tagset.album,
            "©day": tagset.date,
            "©cmt": tagset.comment,
            "©lyr": tagset.lyrics,
            "cprt": tagset.copyright,
        }
        for atom, value in text_atoms.items():
            if value:
                tags[atom] = [value]
        if tagset.artists:
            tags["©ART"] = list(tagset.artists)
        if tagset.genre:
            tags["©gen"] = list(tagset.genre)
        if tagset.track is not None:
            tags["trkn"] = [(tagset.track, tagset.tracktotal or 0)]
        if tagset.disc is not None:
            tags["disk"] = [(tagset.disc, tagset.disctotal or 0)]
        if tagset.bpm is not None:
            tags["tmpo"] = [int(round(tagset.bpm))]
        if tagset.compilation is not None:
            tags["cpil"] = tagset.compilation
        if cover is not None:
            image_format = (
                module.mp4.MP4Cover.FORMAT_PNG
                if cover.mime_type == "image/png"
                else module.mp4.MP4Cover.FORMAT_JPEG
            )
            tags["covr"] = [module.mp4.MP4Cover(cover.data, imageformat=image_format)]
        for description, value in _freeform_values(tagset).items():
            key = f"----:com.apple.iTunes:{description}"
            tags[key] = [module.mp4.MP4FreeForm(value.encode("utf-8"))]

    # -- FLAC / Ogg / Opus ----------------------------------------------

    def _apply_vorbis(
        self,
        audio: Any,
        tagset: TagSet,
        cover: CoverArt | None,
        options: RenderOptions,
        *,
        flac: bool,
    ) -> None:
        module = self._mutagen()
        tags = getattr(audio, "tags", None)
        if tags is None:
            audio.add_tags()
            tags = audio.tags
        if not options.preserve_unknown_tags:
            tags.clear()
        else:
            _remove_managed_vorbis(tags)

        values = _vorbis_values(tagset)
        for key, items in values.items():
            tags[key] = list(items)

        if flac:
            audio.clear_pictures()
            if cover is not None:
                picture = module.flac.Picture()
                picture.type = 3
                picture.mime = cover.mime_type
                picture.desc = cover.description
                picture.width = cover.width or 0
                picture.height = cover.height or 0
                picture.depth = 24
                picture.data = cover.data
                audio.add_picture(picture)
        else:
            _delete_vorbis_key(tags, "METADATA_BLOCK_PICTURE")
            if cover is not None:
                picture = module.flac.Picture()
                picture.type = 3
                picture.mime = cover.mime_type
                picture.desc = cover.description
                picture.width = cover.width or 0
                picture.height = cover.height or 0
                picture.depth = 24
                picture.data = cover.data
                tags["METADATA_BLOCK_PICTURE"] = [base64.b64encode(picture.write()).decode("ascii")]


def _id3_text_frame(module: Any, frame_id: str, values: list[str]) -> Any:
    frame_type = getattr(module.id3, frame_id, None)
    if frame_type is None:
        raise RenderFailed(
            "unknown ID3 frame",
            context={"frame": frame_id},
        )
    return frame_type(encoding=3, text=values)


def _one(value: str | None) -> list[str]:
    return [value] if value else []


def _track_value(number: int | None, total: int | None) -> list[str]:
    if number is None:
        return []
    return [f"{number}/{total}" if total is not None else str(number)]


def _number_text(value: float | None) -> str | None:
    if value is None:
        return None
    return str(int(value)) if value.is_integer() else f"{value:g}"


def _txxx_values(tagset: TagSet) -> dict[str, str]:
    result: dict[str, str] = {}
    if tagset.catalog_number:
        result["CATALOGNUMBER"] = tagset.catalog_number
    if tagset.barcode:
        result["BARCODE"] = tagset.barcode
    if tagset.isrc:
        result["ISRC"] = tagset.isrc
    if tagset.acoustid:
        result["Acoustid Id"] = tagset.acoustid
    if tagset.description:
        result["DESCRIPTION"] = tagset.description
    if tagset.license:
        result["LICENSE"] = tagset.license
    if tagset.mb_recording_id:
        result["MusicBrainz Track Id"] = tagset.mb_recording_id
    if tagset.mb_release_id:
        result["MusicBrainz Album Id"] = tagset.mb_release_id
    if tagset.mb_release_group_id:
        result["MusicBrainz Release Group Id"] = tagset.mb_release_group_id
    if tagset.mb_artist_id:
        result["MusicBrainz Artist Id"] = tagset.mb_artist_id
    if tagset.mb_albumartist_id:
        result["MusicBrainz Album Artist Id"] = tagset.mb_albumartist_id
    if tagset.source_url:
        result["TRACKFORGE_SOURCE_URL"] = tagset.source_url
    if tagset.source_id:
        result["TRACKFORGE_SOURCE_ID"] = tagset.source_id
    for key, value in tagset.trackforge.items():
        result[key] = value
    return result


def _freeform_values(tagset: TagSet) -> dict[str, str]:
    result: dict[str, str] = {}
    if tagset.bpm is not None:
        result["BPM"] = _number_text(tagset.bpm) or ""
    if tagset.key:
        result["KEY"] = tagset.key
    if tagset.language:
        result["LANGUAGE"] = tagset.language
    if tagset.label:
        result["LABEL"] = tagset.label
    if tagset.catalog_number:
        result["CATALOGNUMBER"] = tagset.catalog_number
    if tagset.barcode:
        result["BARCODE"] = tagset.barcode
    if tagset.isrc:
        result["ISRC"] = tagset.isrc
    if tagset.originaldate:
        result["ORIGINALDATE"] = tagset.originaldate
    if tagset.description:
        result["DESCRIPTION"] = tagset.description
    if tagset.license:
        result["LICENSE"] = tagset.license
    if tagset.mb_recording_id:
        result["MusicBrainz Track Id"] = tagset.mb_recording_id
    if tagset.mb_release_id:
        result["MusicBrainz Album Id"] = tagset.mb_release_id
    if tagset.mb_release_group_id:
        result["MusicBrainz Release Group Id"] = tagset.mb_release_group_id
    if tagset.mb_artist_id:
        result["MusicBrainz Artist Id"] = tagset.mb_artist_id
    if tagset.mb_albumartist_id:
        result["MusicBrainz Album Artist Id"] = tagset.mb_albumartist_id
    if tagset.acoustid:
        result["Acoustid Id"] = tagset.acoustid
    if tagset.source_url:
        result["TRACKFORGE_SOURCE_URL"] = tagset.source_url
    if tagset.source_id:
        result["TRACKFORGE_SOURCE_ID"] = tagset.source_id
    for key, value in tagset.trackforge.items():
        result[key] = value
    return result


def _vorbis_values(tagset: TagSet) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    if tagset.title:
        result["TITLE"] = [tagset.title]
    if tagset.artists:
        result["ARTIST"] = list(tagset.artists)
    if tagset.albumartist:
        result["ALBUMARTIST"] = [tagset.albumartist]
    if tagset.album:
        result["ALBUM"] = [tagset.album]
    if tagset.date:
        result["DATE"] = [tagset.date]
    if tagset.originaldate:
        result["ORIGINALDATE"] = [tagset.originaldate]
    if tagset.track is not None:
        result["TRACKNUMBER"] = [str(tagset.track)]
    if tagset.tracktotal is not None:
        result["TRACKTOTAL"] = [str(tagset.tracktotal)]
    if tagset.disc is not None:
        result["DISCNUMBER"] = [str(tagset.disc)]
    if tagset.disctotal is not None:
        result["DISCTOTAL"] = [str(tagset.disctotal)]
    if tagset.genre:
        result["GENRE"] = list(tagset.genre)
    if tagset.bpm is not None:
        result["BPM"] = [_number_text(tagset.bpm) or ""]
    if tagset.key:
        result["KEY"] = [tagset.key]
    if tagset.language:
        result["LANGUAGE"] = [tagset.language]
    if tagset.label:
        result["LABEL"] = [tagset.label]
    if tagset.catalog_number:
        result["CATALOGNUMBER"] = [tagset.catalog_number]
    if tagset.barcode:
        result["BARCODE"] = [tagset.barcode]
    if tagset.isrc:
        result["ISRC"] = [tagset.isrc]
    if tagset.compilation is not None:
        result["COMPILATION"] = ["1" if tagset.compilation else "0"]
    if tagset.comment:
        result["COMMENT"] = [tagset.comment]
    if tagset.description:
        result["DESCRIPTION"] = [tagset.description]
    if tagset.lyrics:
        result["LYRICS"] = [tagset.lyrics]
    if tagset.copyright:
        result["COPYRIGHT"] = [tagset.copyright]
    if tagset.license:
        result["LICENSE"] = [tagset.license]
    if tagset.mb_recording_id:
        result["MUSICBRAINZ_TRACKID"] = [tagset.mb_recording_id]
    if tagset.mb_release_id:
        result["MUSICBRAINZ_ALBUMID"] = [tagset.mb_release_id]
    if tagset.mb_release_group_id:
        result["MUSICBRAINZ_RELEASEGROUPID"] = [tagset.mb_release_group_id]
    if tagset.mb_artist_id:
        result["MUSICBRAINZ_ARTISTID"] = [tagset.mb_artist_id]
    if tagset.mb_albumartist_id:
        result["MUSICBRAINZ_ALBUMARTISTID"] = [tagset.mb_albumartist_id]
    if tagset.acoustid:
        result["ACOUSTID_ID"] = [tagset.acoustid]
    if tagset.source_url:
        result["TRACKFORGE_SOURCE_URL"] = [tagset.source_url]
    if tagset.source_id:
        result["TRACKFORGE_SOURCE_ID"] = [tagset.source_id]
    for key, value in tagset.trackforge.items():
        result[key] = [value]
    return result


def _remove_managed_id3(tags: Any) -> None:
    for frame_id in list(tags.keys()):
        base = str(frame_id).split(":", 1)[0]
        if base in _ID3_MANAGED_FRAMES:
            del tags[frame_id]
        elif base == "TXXX":
            description = str(frame_id).split(":", 1)[1] if ":" in str(frame_id) else ""
            if description.upper() in _ID3_MANAGED_TXXX or description.upper().startswith(
                "TRACKFORGE_"
            ):
                del tags[frame_id]


def _remove_managed_mp4(tags: Any) -> None:
    for key in list(tags.keys()):
        text_key = str(key)
        if text_key in _MP4_MANAGED_ATOMS:
            del tags[key]
            continue
        if text_key.startswith("----:"):
            descriptor = text_key.rsplit(":", 1)[-1]
            if descriptor.upper() in _MP4_MANAGED_FREEFORM or descriptor.upper().startswith(
                "TRACKFORGE_"
            ):
                del tags[key]


def _remove_managed_vorbis(tags: Any) -> None:
    for key in list(tags.keys()):
        upper = str(key).upper()
        if upper in _VORBIS_MANAGED or upper.startswith("TRACKFORGE_"):
            del tags[key]


def _delete_vorbis_key(tags: Any, key: str) -> bool:
    for existing in list(tags.keys()):
        if str(existing).upper() == key.upper():
            del tags[existing]
            return True
    return False


def _compare_tagset(expected: TagSet, actual: TagSet) -> list[TagMismatch]:
    mismatches: list[TagMismatch] = []

    def check(field_name: str, expected_value: object, actual_value: object) -> None:
        if expected_value is None:
            return
        if isinstance(expected_value, (tuple, list)):
            if not expected_value:
                return
            actual_list = list(actual_value) if isinstance(actual_value, (tuple, list)) else []
            if list(expected_value) != actual_list:
                mismatches.append(TagMismatch(field_name, expected_value, actual_value))
            return
        if isinstance(expected_value, float):
            if (
                not isinstance(actual_value, (int, float))
                or abs(float(actual_value) - expected_value) > 1e-6
            ):
                mismatches.append(TagMismatch(field_name, expected_value, actual_value))
            return
        if expected_value != actual_value:
            mismatches.append(TagMismatch(field_name, expected_value, actual_value))

    for field_name in (
        "title",
        "albumartist",
        "album",
        "date",
        "originaldate",
        "track",
        "tracktotal",
        "disc",
        "disctotal",
        "key",
        "language",
        "label",
        "catalog_number",
        "barcode",
        "isrc",
        "compilation",
        "comment",
        "description",
        "lyrics",
        "source_url",
        "source_id",
        "copyright",
        "license",
        "mb_recording_id",
        "mb_release_id",
        "mb_release_group_id",
        "mb_artist_id",
        "mb_albumartist_id",
        "acoustid",
    ):
        check(field_name, getattr(expected, field_name), getattr(actual, field_name))
    check("artists", expected.artists, actual.artists)
    check("genre", expected.genre, actual.genre)
    check("bpm", expected.bpm, actual.bpm)
    for key, value in expected.trackforge.items():
        if actual.trackforge.get(key) != value:
            mismatches.append(TagMismatch(f"trackforge.{key}", value, actual.trackforge.get(key)))
    return mismatches


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


__all__ = ["MutagenTagWriter"]

"""Write normalized metadata through mutagen.

Only the P0 containers are writable for now: MP3/AAC (ID3), FLAC and Ogg
(Vorbis comments) and MP4/M4A (iTunes atoms).  Other containers stay read-only
until a backend that can write them safely is added.

The writer is intentionally non-destructive: ``None``/empty values mean
"leave this field alone" unless :class:`WritePolicy` asks to remove missing
fields.  Unknown frames are preserved because mutagen edits the existing tag
object in place.
"""

from __future__ import annotations

import base64
import os
import shutil
import struct
import tempfile
from pathlib import Path
from typing import Any

import mutagen
from mutagen import MutagenError
from mutagen.asf import ASF, ASFByteArrayAttribute
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, COMM, TDRC, TDAT, TIPL, TXXX, TYER, USLT, Encoding, ID3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm, MP4Tags

from ..capabilities import WritePolicy, WriteReport
from ..errors import (
    MetadataWriteError,
    UnsupportedFormatError,
    WriteVerificationError,
)
from ..formats import ContainerFormat, DetectedFormat
from ..model import Artwork, ArtworkKind, Lyrics, MediaMetadata
from ..normalize import ID3_GENRES
from .mutagen import WRITABLE_CONTAINERS, open_audio_file

_ID3_PICTURE_TYPE = {
    ArtworkKind.FRONT: 3,
    ArtworkKind.BACK: 4,
    ArtworkKind.ARTIST: 8,
    ArtworkKind.LOGO: 19,
    ArtworkKind.OTHER: 0,
}

_ID3_CONTAINERS = {
    ContainerFormat.MP3,
    ContainerFormat.AAC,
    ContainerFormat.WAV,
    ContainerFormat.AIFF,
    ContainerFormat.DSF,
    ContainerFormat.DFF,
}

_APE_CONTAINERS = {
    ContainerFormat.APE,
    ContainerFormat.WAVPACK,
    ContainerFormat.MUSEPACK,
    ContainerFormat.TTA,
}

_MB_ID3_DESCRIPTIONS = {
    "track": "MusicBrainz Track Id",
    "album": "MusicBrainz Album Id",
    "artist": "MusicBrainz Artist Id",
    "album_artist": "MusicBrainz Album Artist Id",
    "release_group": "MusicBrainz Release Group Id",
    "release_track": "MusicBrainz Release Track Id",
    "work": "MusicBrainz Work Id",
}

_RG_ID3_DESCRIPTIONS = {
    "track_gain": "ReplayGain Track Gain",
    "album_gain": "ReplayGain Album Gain",
    "track_peak": "ReplayGain Track Peak",
    "album_peak": "ReplayGain Album Peak",
}

_MB_VORBIS_KEYS = {
    "track": "MUSICBRAINZ_TRACKID",
    "album": "MUSICBRAINZ_ALBUMID",
    "artist": "MUSICBRAINZ_ARTISTID",
    "album_artist": "MUSICBRAINZ_ALBUMARTISTID",
    "release_group": "MUSICBRAINZ_RELEASEGROUPID",
    "release_track": "MUSICBRAINZ_RELEASETRACKID",
    "work": "MUSICBRAINZ_WORKID",
}

_RG_VORBIS_KEYS = {
    "track_gain": "REPLAYGAIN_TRACK_GAIN",
    "album_gain": "REPLAYGAIN_ALBUM_GAIN",
    "track_peak": "REPLAYGAIN_TRACK_PEAK",
    "album_peak": "REPLAYGAIN_ALBUM_PEAK",
}

_ASF_MB_KEYS = {
    "track": "MusicBrainz/Track Id",
    "album": "MusicBrainz/Album Id",
    "artist": "MusicBrainz/Artist Id",
    "album_artist": "MusicBrainz/Album Artist Id",
    "release_group": "MusicBrainz/Release Group Id",
    "release_track": "MusicBrainz/Release Track Id",
    "work": "MusicBrainz/Work Id",
}

_MB_MP4_NAMES = {
    "track": "MusicBrainz Track Id",
    "album": "MusicBrainz Album Id",
    "artist": "MusicBrainz Artist Id",
    "album_artist": "MusicBrainz Album Artist Id",
    "release_group": "MusicBrainz Release Group Id",
    "release_track": "MusicBrainz Release Track Id",
    "work": "MusicBrainz Work Id",
}


def write_file(
    backend: Any,
    path: Path,
    detected: DetectedFormat,
    metadata: MediaMetadata,
    *,
    policy: WritePolicy,
) -> WriteReport:
    """Write ``metadata`` to ``path`` according to ``policy``."""
    if detected.container not in WRITABLE_CONTAINERS:
        raise UnsupportedFormatError(str(path))

    report = WriteReport(backend=backend.name, container=detected.container)
    before = path.stat().st_size

    if policy.atomic:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        os.close(descriptor)
        temp_path = Path(temp_name)
        try:
            shutil.copy2(path, temp_path)
            _write_in_place(temp_path, detected, metadata, policy, report)
            if policy.verify:
                _verify(backend, temp_path, detected, metadata, report)
            _fsync_file(temp_path)
            os.replace(temp_path, path)
            _fsync_dir(path.parent)
        except WriteVerificationError:
            raise
        except (MutagenError, OSError, ValueError) as exc:
            raise MetadataWriteError(str(path), str(exc)) from exc
        finally:
            temp_path.unlink(missing_ok=True)
    else:
        try:
            _write_in_place(path, detected, metadata, policy, report)
            if policy.verify:
                _verify(backend, path, detected, metadata, report)
        except WriteVerificationError:
            raise
        except (MutagenError, OSError, ValueError) as exc:
            raise MetadataWriteError(str(path), str(exc)) from exc

    report.bytes_before = before
    report.bytes_after = path.stat().st_size
    return report


def _write_in_place(
    path: Path,
    detected: DetectedFormat,
    metadata: MediaMetadata,
    policy: WritePolicy,
    report: WriteReport,
) -> None:
    audio_file = open_audio_file(path, detected)
    if detected.container in _ID3_CONTAINERS:
        _write_id3(audio_file, metadata, policy, report)
        if policy.id3_version == 3:
            _downgrade_id3(audio_file.tags, metadata)
        try:
            audio_file.save(v2_version=policy.id3_version)
        except TypeError:
            audio_file.save()
        if policy.write_legacy and detected.container in {
            ContainerFormat.MP3,
            ContainerFormat.AAC,
        }:
            _write_id3v1(path, metadata, report)
    elif detected.container == ContainerFormat.FLAC:
        _write_flac(audio_file, metadata, policy, report)
        audio_file.save()
    elif detected.container == ContainerFormat.OGG:
        _write_ogg(audio_file, metadata, policy, report)
        audio_file.save()
    elif detected.container == ContainerFormat.MP4:
        _write_mp4(audio_file, metadata, policy, report)
        audio_file.save()
    elif detected.container in _APE_CONTAINERS:
        _write_ape(audio_file, metadata, policy, report)
        audio_file.save()
    elif detected.container == ContainerFormat.ASF:
        _write_asf(audio_file, metadata, policy, report)
        audio_file.save()
    else:
        raise UnsupportedFormatError(str(path))


# ----------------------------------------------------------------------
# ID3
# ----------------------------------------------------------------------


def _write_id3(
    audio_file: Any,
    metadata: MediaMetadata,
    policy: WritePolicy,
    report: WriteReport,
) -> None:
    if audio_file.tags is None:
        audio_file.add_tags()
    tags = audio_file.tags
    assert isinstance(tags, ID3)

    _id3_text(tags, "TIT2", metadata.title, "title", policy, report)
    _id3_text(tags, "TSOT", metadata.title_sort, "title_sort", policy, report)
    artists = metadata.artists or ([metadata.artist] if metadata.artist else [])
    _id3_texts(tags, "TPE1", artists, "artists", policy, report)
    _id3_text(tags, "TSOP", metadata.artist_sort, "artist_sort", policy, report)
    if metadata.date is not None:
        _id3_texts(tags, "TDRC", [str(metadata.date)], "date", policy, report)
    elif policy.remove_missing:
        tags.delall("TDRC")
        report.written.append("date")
    _id3_text(
        tags,
        "TDOR",
        str(metadata.original_date) if metadata.original_date else None,
        "original_date",
        policy,
        report,
    )
    _id3_text(
        tags,
        "TDRL",
        str(metadata.release_date) if metadata.release_date else None,
        "release_date",
        policy,
        report,
    )
    _id3_text(tags, "TMOO", metadata.mood, "mood", policy, report)
    _id3_texts(tags, "TCON", metadata.genre, "genre", policy, report)
    _id3_texts(tags, "TCOM", metadata.composer, "composer", policy, report)
    _id3_texts(tags, "TEXT", metadata.lyricist, "lyricist", policy, report)
    _id3_texts(tags, "TPE3", metadata.conductor, "conductor", policy, report)
    _id3_text(tags, "TCOP", metadata.copyright, "copyright", policy, report)
    _id3_text(tags, "TLAN", metadata.language, "language", policy, report)
    _id3_text(tags, "GRP1", metadata.grouping, "grouping", policy, report)

    if metadata.comment is not None or policy.remove_missing:
        _replace_id3_comment(tags, metadata.comment, report)

    if metadata.lyrics is not None or policy.remove_missing:
        _replace_id3_lyrics(tags, metadata.lyrics, report)

    _id3_producer(tags, metadata, policy, report)
    _id3_musicbrainz(tags, metadata, policy, report)
    _id3_replaygain(tags, metadata, policy, report)
    _id3_artwork(tags, _artwork(metadata), policy, report)

    audio = metadata.audio
    if audio is not None:
        _id3_text(tags, "TALB", audio.album, "audio.album", policy, report)
        _id3_text(tags, "TSOA", audio.album_sort, "audio.album_sort", policy, report)
        _id3_text(
            tags,
            "TPE2",
            audio.album_artist,
            "audio.album_artist",
            policy,
            report,
        )
        _id3_text(
            tags,
            "TSO2",
            audio.album_artist_sort,
            "audio.album_artist_sort",
            policy,
            report,
        )
        _id3_text(
            tags,
            "TRCK",
            _number_pair(audio.track_number, audio.track_total),
            "audio.track",
            policy,
            report,
        )
        _id3_text(
            tags,
            "TPOS",
            _number_pair(audio.disc_number, audio.disc_total),
            "audio.disc",
            policy,
            report,
        )
        if audio.compilation is not None:
            _id3_texts(
                tags,
                "TCMP",
                ["1" if audio.compilation else "0"],
                "audio.compilation",
                policy,
                report,
            )
        _id3_text(tags, "TPUB", audio.label, "audio.label", policy, report)
        _id3_text(tags, "TSRC", audio.isrc, "audio.isrc", policy, report)
        if audio.bpm is not None:
            _id3_texts(
                tags,
                "TBPM",
                [str(audio.bpm)],
                "audio.bpm",
                policy,
                report,
            )
        _id3_text(tags, "TKEY", audio.key, "audio.key", policy, report)


def _id3_text(
    tags: ID3,
    frame_id: str,
    value: str | None,
    field: str,
    policy: WritePolicy,
    report: WriteReport,
) -> None:
    if value is not None:
        _id3_texts(tags, frame_id, [value], field, policy, report)
    elif policy.remove_missing:
        tags.delall(frame_id)
        report.written.append(field)


def _id3_texts(
    tags: ID3,
    frame_id: str,
    values: list[str] | None,
    field: str,
    policy: WritePolicy,
    report: WriteReport,
) -> None:
    if values:
        tags.delall(frame_id)
        frame_class = mutagen.id3.Frames[frame_id]
        tags.add(frame_class(encoding=3, text=list(values)))
        report.written.append(field)
    elif policy.remove_missing:
        tags.delall(frame_id)
        report.written.append(field)


def _downgrade_id3(tags: ID3 | None, metadata: MediaMetadata) -> None:
    """Make ID3 frames safe for a v2.3 writer.

    ID3v2.3 has no UTF-8 text encoding and no TDRC frame, so UTF-8 frames are
    converted to UTF-16 and the date is moved to TYER/TDAT.
    """
    if tags is None:
        return
    for frame in tags.values():
        if getattr(frame, "encoding", None) == Encoding.UTF8:
            frame.encoding = Encoding.UTF16

    tags.delall("TDRC")
    tags.delall("TYER")
    tags.delall("TDAT")
    if metadata.date is None:
        return
    date = metadata.date
    tags.add(TYER(encoding=Encoding.UTF16, text=[f"{date.year:04d}"]))
    if date.month is not None and date.day is not None:
        tags.add(TDAT(encoding=Encoding.UTF16, text=[f"{date.day:02d}{date.month:02d}"]))


def _replace_id3_comment(tags: ID3, value: str | None, report: WriteReport) -> None:
    """Replace only the default English/empty-description comment."""
    keys = [
        frame.HashKey
        for frame in tags.getall("COMM")
        if frame.lang == "eng" and not (frame.desc or "")
    ]
    for key in keys:
        tags.delall(key)
    if value is not None:
        tags.add(COMM(encoding=3, lang="eng", desc="", text=[value]))
    if value is not None or keys:
        report.written.append("comment")


def _replace_id3_lyrics(tags: ID3, lyrics: Lyrics | None, report: WriteReport) -> None:
    """Replace only the matching language/empty-description lyrics frame."""
    language = (lyrics.language if lyrics is not None else None) or "eng"
    keys = [
        frame.HashKey
        for frame in tags.getall("USLT")
        if frame.lang == language and not (frame.desc or "")
    ]
    for key in keys:
        tags.delall(key)
    if lyrics is not None:
        tags.add(USLT(encoding=3, lang=language, desc="", text=lyrics.text))
    if lyrics is not None or keys:
        report.written.append("lyrics")


def _id3_producer(
    tags: ID3,
    metadata: MediaMetadata,
    policy: WritePolicy,
    report: WriteReport,
) -> None:
    if not metadata.producer and not policy.remove_missing:
        return
    frame = tags.get("TIPL")
    people = list(getattr(frame, "people", []) or [])
    people = [
        (role, person)
        for role, person in people
        if str(role).lower() != "producer"
    ]
    people.extend(("producer", person) for person in metadata.producer)
    if people:
        tags.delall("TIPL")
        tags.add(TIPL(encoding=3, people=people))
    else:
        tags.delall("TIPL")
    report.written.append("producer")


def _id3_musicbrainz(
    tags: ID3,
    metadata: MediaMetadata,
    policy: WritePolicy,
    report: WriteReport,
) -> None:
    for name, description in _MB_ID3_DESCRIPTIONS.items():
        value = metadata.musicbrainz_ids.get(name)
        if value is not None:
            _id3_txxx(tags, description, value, f"musicbrainz_ids.{name}", report)
        elif policy.remove_missing:
            tags.delall(f"TXXX:{description}")


def _id3_replaygain(
    tags: ID3,
    metadata: MediaMetadata,
    policy: WritePolicy,
    report: WriteReport,
) -> None:
    replaygain = metadata.audio.replaygain if metadata.audio is not None else {}
    for name, description in _RG_ID3_DESCRIPTIONS.items():
        value = replaygain.get(name)
        if value is not None:
            _id3_txxx(
                tags,
                description,
                f"{value:.2f} dB",
                f"audio.replaygain.{name}",
                report,
            )
        elif policy.remove_missing:
            tags.delall(f"TXXX:{description}")


def _id3_txxx(tags: ID3, description: str, value: str, field: str, report: WriteReport) -> None:
    tags.delall(f"TXXX:{description}")
    tags.add(TXXX(encoding=3, desc=description, text=[value]))
    report.written.append(field)


def _id3_artwork(
    tags: ID3,
    artwork,
    policy: WritePolicy,
    report: WriteReport,
) -> None:
    images = list(artwork)
    if not images and not policy.remove_missing:
        return
    tags.delall("APIC")
    for image in images:
        tags.add(
            APIC(
                encoding=3,
                mime=image.mime,
                type=_picture_type(image.kind),
                desc=image.description or "",
                data=image.data,
            )
        )
    report.written.append("artwork")


def _write_id3v1(path: Path, metadata: MediaMetadata, report: WriteReport) -> None:
    """Append a lossy ID3v1 compatibility block."""
    block = _id3v1_block(metadata)
    with path.open("r+b") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        if size >= 128:
            handle.seek(-128, 2)
            tail = handle.read(128)
            if tail[:3] == b"TAG":
                handle.truncate(size - 128)
        handle.seek(0, 2)
        handle.write(block)
    report.coerced.append(("legacy.id3v1", "30-byte fields; lossy"))


def _id3v1_block(metadata: MediaMetadata) -> bytes:
    def field(value: str | None, length: int) -> bytes:
        raw = (value or "").encode("latin-1", "replace")
        return raw[:length].ljust(length, b"\x00")

    year = str(metadata.date.year) if metadata.date is not None else ""
    comment = field(metadata.comment, 28)
    track = metadata.audio.track_number if metadata.audio is not None else None
    track_byte = bytes([track]) if track is not None and 0 < track < 256 else b"\x00"
    genre = 255
    if metadata.genre:
        try:
            genre = ID3_GENRES.index(metadata.genre[0])
        except ValueError:
            genre = 255
    return (
        b"TAG"
        + field(metadata.title, 30)
        + field(metadata.artist or (metadata.artists[0] if metadata.artists else None), 30)
        + field(metadata.audio.album if metadata.audio is not None else None, 30)
        + field(year, 4)
        + comment
        + b"\x00"
        + track_byte
        + bytes([genre])
    )


# ----------------------------------------------------------------------
# Vorbis comments (FLAC and Ogg)
# ----------------------------------------------------------------------


def _write_flac(
    audio_file: FLAC,
    metadata: MediaMetadata,
    policy: WritePolicy,
    report: WriteReport,
) -> None:
    tags = audio_file.tags
    if tags is None:
        audio_file.add_tags()
        tags = audio_file.tags
    _write_vorbis_common(tags, metadata, policy, report)
    _write_vorbis_audio(tags, metadata, policy, report)

    if _artwork(metadata) or policy.remove_missing:
        audio_file.clear_pictures()
        for image in _artwork(metadata):
            audio_file.add_picture(_flac_picture(image))
        report.written.append("artwork")


def _write_ogg(
    audio_file: Any,
    metadata: MediaMetadata,
    policy: WritePolicy,
    report: WriteReport,
) -> None:
    tags = audio_file.tags
    if tags is None:
        audio_file.add_tags()
        tags = audio_file.tags
    _write_vorbis_common(tags, metadata, policy, report)
    _write_vorbis_audio(tags, metadata, policy, report)

    if _artwork(metadata) or policy.remove_missing:
        _delete_tag(tags, "METADATA_BLOCK_PICTURE")
        if _artwork(metadata):
            tags["METADATA_BLOCK_PICTURE"] = [
                base64.b64encode(_flac_picture_bytes(image)).decode("ascii")
                for image in _artwork(metadata)
            ]
        report.written.append("artwork")


def _write_vorbis_common(
    tags,
    metadata: MediaMetadata,
    policy: WritePolicy,
    report: WriteReport,
) -> None:
    _vorbis_text(tags, "TITLE", metadata.title, "title", policy, report)
    _vorbis_text(tags, "TITLESORT", metadata.title_sort, "title_sort", policy, report)
    artists = metadata.artists or ([metadata.artist] if metadata.artist else [])
    _vorbis_values(tags, "ARTIST", artists, "artists", policy, report)
    _vorbis_text(tags, "ARTISTSORT", metadata.artist_sort, "artist_sort", policy, report)
    _vorbis_text(tags, "DATE", str(metadata.date) if metadata.date else None, "date", policy, report)
    _vorbis_text(
        tags,
        "ORIGINALDATE",
        str(metadata.original_date) if metadata.original_date else None,
        "original_date",
        policy,
        report,
    )
    _vorbis_text(
        tags,
        "RELEASEDATE",
        str(metadata.release_date) if metadata.release_date else None,
        "release_date",
        policy,
        report,
    )
    _vorbis_text(tags, "MOOD", metadata.mood, "mood", policy, report)
    _vorbis_values(tags, "GENRE", metadata.genre, "genre", policy, report)
    _vorbis_text(tags, "COMMENT", metadata.comment, "comment", policy, report)
    _vorbis_values(tags, "COMPOSER", metadata.composer, "composer", policy, report)
    _vorbis_values(tags, "LYRICIST", metadata.lyricist, "lyricist", policy, report)
    _vorbis_values(tags, "CONDUCTOR", metadata.conductor, "conductor", policy, report)
    _vorbis_text(tags, "COPYRIGHT", metadata.copyright, "copyright", policy, report)
    _vorbis_text(tags, "LANGUAGE", metadata.language, "language", policy, report)
    _vorbis_text(tags, "GROUPING", metadata.grouping, "grouping", policy, report)
    if metadata.lyrics is not None:
        _vorbis_text(tags, "LYRICS", metadata.lyrics.text, "lyrics", policy, report)
    elif policy.remove_missing:
        _delete_tag(tags, "LYRICS")
        report.written.append("lyrics")

    for name, key in _MB_VORBIS_KEYS.items():
        value = metadata.musicbrainz_ids.get(name)
        _vorbis_text(
            tags,
            key,
            value,
            f"musicbrainz_ids.{name}",
            policy,
            report,
        )


def _write_vorbis_audio(
    tags,
    metadata: MediaMetadata,
    policy: WritePolicy,
    report: WriteReport,
) -> None:
    audio = metadata.audio
    if audio is None:
        return
    _vorbis_text(tags, "ALBUM", audio.album, "audio.album", policy, report)
    _vorbis_text(tags, "ALBUMSORT", audio.album_sort, "audio.album_sort", policy, report)
    _vorbis_text(
        tags,
        "ALBUMARTIST",
        audio.album_artist,
        "audio.album_artist",
        policy,
        report,
    )
    _vorbis_text(
        tags,
        "ALBUMARTISTSORT",
        audio.album_artist_sort,
        "audio.album_artist_sort",
        policy,
        report,
    )
    if audio.track_number is not None:
        _vorbis_text(
            tags,
            "TRACKNUMBER",
            str(audio.track_number),
            "audio.track_number",
            policy,
            report,
        )
    elif policy.remove_missing:
        _delete_tag(tags, "TRACKNUMBER")
    if audio.track_total is not None:
        _vorbis_text(
            tags,
            "TRACKTOTAL",
            str(audio.track_total),
            "audio.track_total",
            policy,
            report,
        )
    elif policy.remove_missing:
        _delete_tag(tags, "TRACKTOTAL")
    if audio.disc_number is not None:
        _vorbis_text(
            tags,
            "DISCNUMBER",
            str(audio.disc_number),
            "audio.disc_number",
            policy,
            report,
        )
    elif policy.remove_missing:
        _delete_tag(tags, "DISCNUMBER")
    if audio.disc_total is not None:
        _vorbis_text(
            tags,
            "DISCTOTAL",
            str(audio.disc_total),
            "audio.disc_total",
            policy,
            report,
        )
    elif policy.remove_missing:
        _delete_tag(tags, "DISCTOTAL")
    if audio.compilation is not None:
        _vorbis_text(
            tags,
            "COMPILATION",
            "1" if audio.compilation else "0",
            "audio.compilation",
            policy,
            report,
        )
    _vorbis_text(tags, "LABEL", audio.label, "audio.label", policy, report)
    _vorbis_text(tags, "ISRC", audio.isrc, "audio.isrc", policy, report)
    if audio.bpm is not None:
        _vorbis_text(tags, "BPM", str(audio.bpm), "audio.bpm", policy, report)
    _vorbis_text(tags, "INITIALKEY", audio.key, "audio.key", policy, report)

    for name, key in _RG_VORBIS_KEYS.items():
        value = audio.replaygain.get(name)
        _vorbis_text(
            tags,
            key,
            f"{value:.2f} dB" if value is not None else None,
            f"audio.replaygain.{name}",
            policy,
            report,
        )


def _delete_tag(tags, key: str) -> None:
    if key in tags:
        del tags[key]


def _vorbis_text(
    tags,
    key: str,
    value: str | None,
    field: str,
    policy: WritePolicy,
    report: WriteReport,
) -> None:
    if value is not None:
        tags[key] = [value]
        report.written.append(field)
    elif policy.remove_missing:
        _delete_tag(tags, key)
        report.written.append(field)


def _vorbis_values(
    tags,
    key: str,
    values: list[str],
    field: str,
    policy: WritePolicy,
    report: WriteReport,
) -> None:
    if values:
        tags[key] = list(values)
        report.written.append(field)
    elif policy.remove_missing:
        _delete_tag(tags, key)
        report.written.append(field)


# ----------------------------------------------------------------------
# MP4 atoms
# ----------------------------------------------------------------------


def _write_mp4(
    audio_file: MP4,
    metadata: MediaMetadata,
    policy: WritePolicy,
    report: WriteReport,
) -> None:
    if audio_file.tags is None:
        audio_file.add_tags()
    tags = audio_file.tags
    assert isinstance(tags, MP4Tags)

    _mp4_text(tags, "©nam", metadata.title, "title", policy, report)
    _mp4_text(tags, "sonm", metadata.title_sort, "title_sort", policy, report)
    artists = metadata.artists or ([metadata.artist] if metadata.artist else [])
    _mp4_values(tags, "©ART", artists, "artists", policy, report)
    _mp4_text(tags, "soar", metadata.artist_sort, "artist_sort", policy, report)
    _mp4_text(tags, "©day", str(metadata.date) if metadata.date else None, "date", policy, report)
    _mp4_freeform(
        tags,
        "ORIGINALDATE",
        str(metadata.original_date) if metadata.original_date else None,
        "original_date",
        policy,
        report,
    )
    _mp4_freeform(
        tags,
        "RELEASEDATE",
        str(metadata.release_date) if metadata.release_date else None,
        "release_date",
        policy,
        report,
    )
    _mp4_freeform(tags, "MOOD", metadata.mood, "mood", policy, report)
    _mp4_values(tags, "©gen", metadata.genre, "genre", policy, report)
    _mp4_text(tags, "©cmt", metadata.comment, "comment", policy, report)
    _mp4_values(tags, "©wrt", metadata.composer, "composer", policy, report)
    _mp4_text(tags, "©lyr", metadata.lyrics.text if metadata.lyrics else None, "lyrics", policy, report)
    _mp4_text(tags, "cprt", metadata.copyright, "copyright", policy, report)
    _mp4_text(tags, "©grp", metadata.grouping, "grouping", policy, report)
    _mp4_freeform(tags, "LANGUAGE", metadata.language, "language", policy, report)

    audio = metadata.audio
    if audio is not None:
        _mp4_text(tags, "©alb", audio.album, "audio.album", policy, report)
        _mp4_text(tags, "soal", audio.album_sort, "audio.album_sort", policy, report)
        _mp4_text(tags, "aART", audio.album_artist, "audio.album_artist", policy, report)
        _mp4_text(
            tags,
            "soaa",
            audio.album_artist_sort,
            "audio.album_artist_sort",
            policy,
            report,
        )
        if audio.track_number is not None or audio.track_total is not None:
            tags["trkn"] = [(audio.track_number or 0, audio.track_total or 0)]
            report.written.append("audio.track")
        elif policy.remove_missing:
            tags.pop("trkn", None)
        if audio.disc_number is not None or audio.disc_total is not None:
            tags["disk"] = [(audio.disc_number or 0, audio.disc_total or 0)]
            report.written.append("audio.disc")
        elif policy.remove_missing:
            tags.pop("disk", None)
        if audio.compilation is not None:
            tags["cpil"] = bool(audio.compilation)
            report.written.append("audio.compilation")
        if audio.bpm is not None:
            tags["tmpo"] = [int(audio.bpm)]
            report.written.append("audio.bpm")
        _mp4_freeform(tags, "ISRC", audio.isrc, "audio.isrc", policy, report)
        _mp4_freeform(tags, "LABEL", audio.label, "audio.label", policy, report)
        _mp4_freeform(tags, "CONDUCTOR", _join(metadata.conductor), "conductor", policy, report)
        _mp4_freeform(tags, "LYRICIST", _join(metadata.lyricist), "lyricist", policy, report)
        for name, value in audio.replaygain.items():
            description = _RG_ID3_DESCRIPTIONS.get(name)
            if description:
                _mp4_freeform(
                    tags,
                    description,
                    f"{value:.2f} dB",
                    f"audio.replaygain.{name}",
                    policy,
                    report,
                )

    video = metadata.video
    if video is not None:
        _mp4_text(tags, "tvsh", video.show, "video.show", policy, report)
        if video.season is not None:
            tags["tvsn"] = [video.season]
            report.written.append("video.season")
        if video.episode is not None:
            tags["tves"] = [video.episode]
            report.written.append("video.episode")
        _mp4_text(tags, "tven", video.episode_id, "video.episode_id", policy, report)
        _mp4_text(tags, "tvnn", video.network, "video.network", policy, report)
        _mp4_text(tags, "ldes", video.synopsis, "video.synopsis", policy, report)
        _mp4_values(tags, "keyw", video.keywords, "video.keywords", policy, report)
        _mp4_freeform(tags, "DIRECTOR", _join(video.director), "video.director", policy, report)
        if video.writer:
            _mp4_values(tags, "©wrt", video.writer, "video.writer", policy, report)

    for name, description in _MB_MP4_NAMES.items():
        value = metadata.musicbrainz_ids.get(name)
        _mp4_freeform(tags, description, value, f"musicbrainz_ids.{name}", policy, report)

    if _artwork(metadata) or policy.remove_missing:
        tags.pop("covr", None)
        if _artwork(metadata):
            tags["covr"] = [
                MP4Cover(
                    image.data,
                    imageformat=(
                        MP4Cover.FORMAT_PNG
                        if image.mime == "image/png"
                        else MP4Cover.FORMAT_JPEG
                    ),
                )
                for image in _artwork(metadata)
            ]
        report.written.append("artwork")


def _mp4_text(
    tags: MP4Tags,
    key: str,
    value: str | None,
    field: str,
    policy: WritePolicy,
    report: WriteReport,
) -> None:
    if value is not None:
        tags[key] = [value]
        report.written.append(field)
    elif policy.remove_missing:
        _delete_tag(tags, key)
        report.written.append(field)


def _mp4_values(
    tags: MP4Tags,
    key: str,
    values: list[str],
    field: str,
    policy: WritePolicy,
    report: WriteReport,
) -> None:
    if values:
        tags[key] = list(values)
        report.written.append(field)
    elif policy.remove_missing:
        _delete_tag(tags, key)
        report.written.append(field)


def _mp4_freeform(
    tags: MP4Tags,
    name: str,
    value: str | None,
    field: str,
    policy: WritePolicy,
    report: WriteReport,
) -> None:
    key = f"----:com.apple.iTunes:{name}"
    if value is not None:
        tags[key] = [MP4FreeForm(value.encode("utf-8"))]
        report.written.append(field)
    elif policy.remove_missing:
        _delete_tag(tags, key)
        report.written.append(field)


# ----------------------------------------------------------------------
# APEv2 (APE, WavPack, Musepack, TrueAudio)
# ----------------------------------------------------------------------


def _write_ape(audio_file, metadata: MediaMetadata, policy: WritePolicy, report: WriteReport) -> None:
    if audio_file.tags is None:
        audio_file.add_tags()
    tags = audio_file.tags
    if tags is None:
        raise UnsupportedFormatError("APEv2 tag could not be created")

    def add(key: str, value: str | None, field: str) -> None:
        if value is not None:
            tags[key] = str(value)
            report.written.append(field)
        elif policy.remove_missing:
            _delete_tag(tags, key)

    artists = metadata.artists or ([metadata.artist] if metadata.artist else [])
    add("Title", metadata.title, "title")
    add("Artist", _join(artists), "artists")
    add("Album", metadata.audio.album if metadata.audio else None, "audio.album")
    add("Album Artist", metadata.audio.album_artist if metadata.audio else None, "audio.album_artist")
    add("Track", _number_pair(metadata.audio.track_number, metadata.audio.track_total) if metadata.audio else None, "audio.track")
    add("Disc", _number_pair(metadata.audio.disc_number, metadata.audio.disc_total) if metadata.audio else None, "audio.disc")
    add("Year", str(metadata.date) if metadata.date else None, "date")
    add("Original Date", str(metadata.original_date) if metadata.original_date else None, "original_date")
    add("Release Date", str(metadata.release_date) if metadata.release_date else None, "release_date")
    add("Mood", metadata.mood, "mood")
    add("Genre", _join(metadata.genre), "genre")
    add("Comment", metadata.comment, "comment")
    add("Composer", _join(metadata.composer), "composer")
    add("Lyricist", _join(metadata.lyricist), "lyricist")
    add("Conductor", _join(metadata.conductor), "conductor")
    add("Lyrics", metadata.lyrics.text if metadata.lyrics else None, "lyrics")
    add("Producer", _join(metadata.producer), "producer")
    add("Copyright", metadata.copyright, "copyright")
    add("Language", metadata.language, "language")
    add("Grouping", metadata.grouping, "grouping")
    add("Label", metadata.audio.label if metadata.audio else None, "audio.label")
    add("ISRC", metadata.audio.isrc if metadata.audio else None, "audio.isrc")
    add("BPM", str(metadata.audio.bpm) if metadata.audio and metadata.audio.bpm is not None else None, "audio.bpm")
    add("Initial Key", metadata.audio.key if metadata.audio else None, "audio.key")
    if metadata.audio:
        add(
            "Compilation",
            "1" if metadata.audio.compilation else "0"
            if metadata.audio.compilation is not None
            else None,
            "audio.compilation",
        )
        for name, key in _RG_VORBIS_KEYS.items():
            value = metadata.audio.replaygain.get(name)
            add(key, f"{value:.2f} dB" if value is not None else None, f"audio.replaygain.{name}")
    for name, key in _MB_VORBIS_KEYS.items():
        add(key, metadata.musicbrainz_ids.get(name), f"musicbrainz_ids.{name}")

    if _artwork(metadata) or policy.remove_missing:
        _delete_tag(tags, "Cover Art (Front)")
        image = next(iter(_artwork(metadata)), None)
        if image is not None:
            suffix = ".png" if image.mime == "image/png" else ".jpg"
            tags["Cover Art (Front)"] = (
                f"cover{suffix}".encode("utf-8") + b"\x00" + image.data
            )
        report.written.append("artwork")


# ----------------------------------------------------------------------
# ASF (WMA/WMV)
# ----------------------------------------------------------------------


def _write_asf(audio_file: ASF, metadata: MediaMetadata, policy: WritePolicy, report: WriteReport) -> None:
    if audio_file.tags is None:
        audio_file.add_tags()
    tags = audio_file.tags
    if tags is None:
        raise UnsupportedFormatError("ASF tag could not be created")

    def add(key: str, value: str | None, field: str) -> None:
        if value is not None:
            tags[key] = str(value)
            report.written.append(field)
        elif policy.remove_missing:
            _delete_tag(tags, key)

    artists = metadata.artists or ([metadata.artist] if metadata.artist else [])
    add("Title", metadata.title, "title")
    add("Author", _join(artists), "artists")
    add("Copyright", metadata.copyright, "copyright")
    add("Description", metadata.comment, "comment")
    add("WM/AlbumTitle", metadata.audio.album if metadata.audio else None, "audio.album")
    add("WM/AlbumArtist", metadata.audio.album_artist if metadata.audio else None, "audio.album_artist")
    add("WM/TrackNumber", _number_pair(metadata.audio.track_number, metadata.audio.track_total) if metadata.audio else None, "audio.track")
    add("WM/PartOfSet", _number_pair(metadata.audio.disc_number, metadata.audio.disc_total) if metadata.audio else None, "audio.disc")
    add("WM/Year", str(metadata.date) if metadata.date else None, "date")
    add("WM/OriginalReleaseYear", str(metadata.original_date) if metadata.original_date else None, "original_date")
    add("WM/ReleaseDate", str(metadata.release_date) if metadata.release_date else None, "release_date")
    add("WM/Mood", metadata.mood, "mood")
    add("WM/Genre", _join(metadata.genre), "genre")
    add("WM/Composer", _join(metadata.composer), "composer")
    add("WM/Conductor", _join(metadata.conductor), "conductor")
    add("WM/Writer", _join(metadata.lyricist), "lyricist")
    add("WM/Lyrics", metadata.lyrics.text if metadata.lyrics else None, "lyrics")
    add("WM/Publisher", metadata.audio.label if metadata.audio else None, "audio.label")
    add("WM/ISRC", metadata.audio.isrc if metadata.audio else None, "audio.isrc")
    add("WM/BeatsPerMinute", str(metadata.audio.bpm) if metadata.audio and metadata.audio.bpm is not None else None, "audio.bpm")
    add("WM/Language", metadata.language, "language")
    if metadata.audio and metadata.audio.compilation is not None:
        add("WM/IsCompilation", "1" if metadata.audio.compilation else "0", "audio.compilation")
    if metadata.video is not None:
        add("WM/Director", _join(metadata.video.director), "video.director")
        add("WM/Description", metadata.video.synopsis, "video.synopsis")
    for name, key in _ASF_MB_KEYS.items():
        add(key, metadata.musicbrainz_ids.get(name), f"musicbrainz_ids.{name}")

    if _artwork(metadata) or policy.remove_missing:
        _delete_tag(tags, "WM/Picture")
        image = next(iter(_artwork(metadata)), None)
        if image is not None:
            tags["WM/Picture"] = ASFByteArrayAttribute(_asf_picture_bytes(image))
        report.written.append("artwork")


def _asf_picture_bytes(image: Artwork) -> bytes:
    mime = image.mime.encode("utf-16-le")
    description = (image.description or "").encode("utf-16-le")
    return b"".join(
        [
            bytes([_picture_type(image.kind)]),
            struct.pack("<I", len(image.data)),
            struct.pack("<I", len(mime)),
            mime,
            b"\x00\x00",
            description,
            b"\x00\x00",
            image.data,
        ]
    )


# ----------------------------------------------------------------------
# Artwork and verification
# ----------------------------------------------------------------------


def _flac_picture_bytes(image: Artwork) -> bytes:
    mime = image.mime.encode("utf-8")
    description = (image.description or "").encode("utf-8")
    return b"".join(
        [
            struct.pack(">I", _picture_type(image.kind)),
            struct.pack(">I", len(mime)),
            mime,
            struct.pack(">I", len(description)),
            description,
            struct.pack(
                ">IIII",
                image.width or 0,
                image.height or 0,
                0,
                0,
            ),
            struct.pack(">I", len(image.data)),
            image.data,
        ]
    )


def _flac_picture(image: Artwork) -> Picture:
    picture = Picture()
    picture.type = _picture_type(image.kind)
    picture.mime = image.mime
    picture.desc = image.description or ""
    picture.width = image.width or 0
    picture.height = image.height or 0
    picture.data = image.data
    return picture


def _picture_type(kind: ArtworkKind | str) -> int:
    if isinstance(kind, str):
        try:
            kind = ArtworkKind(kind)
        except ValueError:
            return 0
    return _ID3_PICTURE_TYPE.get(kind, 0)


def _number_pair(number: int | None, total: int | None) -> str | None:
    if number is None:
        return None
    if total is None:
        return str(number)
    return f"{number}/{total}"


def _join(values: list[str]) -> str | None:
    return "; ".join(values) if values else None


def _verify(
    backend: Any,
    path: Path,
    detected: DetectedFormat,
    expected: MediaMetadata,
    report: WriteReport,
) -> None:
    result = backend.read(path, detected)
    actual = result.metadata
    mismatches: list[str] = []
    for field in report.written:
        if field == "artwork":
            if _artwork_signature(getattr(expected, "artwork", ())) != _artwork_signature(result.artwork):
                mismatches.append("artwork")
            continue
        expected_value = _field_value(expected, field)
        actual_value = _field_value(actual, field)
        if field == "lyrics" or field.endswith(".lyrics"):
            expected_value = expected_value.text if expected_value else None
            actual_value = actual_value.text if actual_value else None
        if expected_value != actual_value:
            mismatches.append(f"{field}: {expected_value!r} != {actual_value!r}")
    if mismatches:
        raise WriteVerificationError(
            "metadata verification failed: " + "; ".join(mismatches)
        )


def _field_value(metadata: MediaMetadata, field: str):
    if field == "audio.track":
        return (
            (metadata.audio.track_number, metadata.audio.track_total)
            if metadata.audio
            else None
        )
    if field == "audio.disc":
        return (
            (metadata.audio.disc_number, metadata.audio.disc_total)
            if metadata.audio
            else None
        )

    if field.startswith("audio."):
        target: Any = metadata.audio
        parts = field.split(".")[1:]
    elif field.startswith("video."):
        target = metadata.video
        parts = field.split(".")[1:]
    else:
        target = metadata
        parts = field.split(".")

    for part in parts:
        if target is None:
            return None
        if isinstance(target, dict):
            target = target.get(part)
        else:
            target = getattr(target, part)
    return target


def _artwork(metadata: MediaMetadata):
    return getattr(metadata, "artwork", ())


def _artwork_signature(artwork) -> list[tuple[str, str, bytes]]:
    return sorted(
        (str(item.kind), item.mime, item.data)
        for item in artwork
    )


def _fsync_file(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    except OSError:
        pass


def _fsync_dir(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)

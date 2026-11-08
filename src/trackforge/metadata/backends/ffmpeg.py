"""Read and write metadata through ffprobe/ffmpeg.

This backend exists for containers mutagen does not handle: Matroska/WebM,
AVI, FLV and MPEG-TS/PS.  It shells out to ``ffprobe`` and ``ffmpeg``; the
library itself never requires them until one of these formats is opened.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..capabilities import (
    Capabilities,
    UnsupportedPolicy,
    WritePolicy,
    WriteReport,
    standard_capabilities,
)
from ..errors import (
    MetadataReadError,
    MetadataWriteError,
    UnsupportedFieldError,
    UnsupportedFormatError,
    WriteVerificationError,
)
from ..formats import ContainerFormat, DetectedFormat, TagSystem
from ..model import (
    Artwork,
    AudioMetadata,
    Lyrics,
    MediaMetadata,
    VideoMetadata,
)
from ..normalize import (
    clean_text,
    first_text,
    parse_bool,
    parse_gain,
    parse_genres,
    parse_int,
    parse_number_pair,
    parse_partial_date,
    split_values,
)
from .base import ReadResult

_WRITABLE = frozenset(
    {
        ContainerFormat.MATROSKA,
        ContainerFormat.AVI,
        ContainerFormat.FLV,
    }
)

_MUXERS = {
    ContainerFormat.MATROSKA: "matroska",
    ContainerFormat.AVI: "avi",
    ContainerFormat.FLV: "flv",
}

_AVI_FIELDS = {
    "title",
    "artists",
    "audio.album",
    "date",
    "genre",
    "comment",
    "audio.track",
}

_AVI_CAPABILITY_FIELDS = {
    "title",
    "artists",
    "album",
    "date",
    "genre",
    "comment",
    "track_number",
    "track_total",
}

_MB_KEYS = {
    "track": "musicbrainz_trackid",
    "album": "musicbrainz_albumid",
    "artist": "musicbrainz_artistid",
    "album_artist": "musicbrainz_albumartistid",
    "release_group": "musicbrainz_releasegroupid",
    "release_track": "musicbrainz_releasetrackid",
    "work": "musicbrainz_workid",
}

_RG_KEYS = {
    "track_gain": "replaygain_track_gain",
    "album_gain": "replaygain_album_gain",
    "track_peak": "replaygain_track_peak",
    "album_peak": "replaygain_album_peak",
}

_MB_READ_ALIASES = {value: key for key, value in _MB_KEYS.items()}


class FfmpegBackend:
    """Read/write metadata for containers handled best by ffmpeg."""

    name = "ffmpeg"
    containers = frozenset(
        {
            ContainerFormat.MATROSKA,
            ContainerFormat.AVI,
            ContainerFormat.FLV,
            ContainerFormat.MPEG_TS,
            ContainerFormat.MPEG_PS,
        }
    )

    def __init__(
        self,
        *,
        ffprobe: str | None = None,
        ffmpeg: str | None = None,
    ) -> None:
        self.ffprobe = ffprobe if ffprobe is not None else shutil.which("ffprobe")
        self.ffmpeg = ffmpeg if ffmpeg is not None else shutil.which("ffmpeg")

    @property
    def available(self) -> bool:
        return self.ffprobe is not None

    def read(self, path: Path, detected: DetectedFormat) -> ReadResult:
        data = self._probe(path)
        metadata = MediaMetadata()
        streams = data.get("streams", [])
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        video_streams = [
            s
            for s in streams
            if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")
        ]

        if audio_streams:
            metadata.audio = AudioMetadata()
        if video_streams:
            metadata.video = VideoMetadata()

        tags = _lower_tags(data.get("format", {}).get("tags"))
        self._apply_tags(tags, metadata)
        if audio_streams and not metadata.language:
            metadata.language = first_text(
                (audio_streams[0].get("tags") or {}).get("language")
            )

        return ReadResult(metadata=metadata, artwork=[], warnings=[])

    def capabilities(self, container: ContainerFormat) -> Capabilities:
        tag_system = (
            TagSystem.MATROSKA
            if container is ContainerFormat.MATROSKA
            else TagSystem.RIFF_INFO
            if container is ContainerFormat.AVI
            else TagSystem.NONE
        )
        capabilities = standard_capabilities(
            container,
            tag_system,
            audio=True,
            video=True,
            arbitrary_keys=container is ContainerFormat.MATROSKA,
            artwork=False,
            writable=container in _WRITABLE,
        )
        if container is ContainerFormat.AVI:
            fields = {
                name: capability
                if name in _AVI_CAPABILITY_FIELDS
                else replace(capability, supported=False)
                for name, capability in capabilities.fields.items()
            }
            capabilities = replace(capabilities, fields=fields)
        return capabilities

    def write(
        self,
        path: Path,
        detected: DetectedFormat,
        metadata: MediaMetadata,
        *,
        policy: WritePolicy,
    ) -> WriteReport:
        if detected.container not in _WRITABLE or self.ffmpeg is None:
            raise UnsupportedFormatError(str(path))

        report = WriteReport(backend=self.name, container=detected.container)
        args = self._metadata_args(metadata, detected.container, policy, report)
        if policy.remove_missing:
            report.warnings.append("ffmpeg backend cannot delete metadata fields")

        if policy.on_unsupported is UnsupportedPolicy.ERROR and report.skipped:
            field, _reason = report.skipped[0]
            raise UnsupportedFieldError(field, detected.container.value)

        before = path.stat().st_size
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=path.suffix,
            dir=path.parent,
        )
        os.close(descriptor)
        temp_path = Path(temp_name)
        try:
            command = [
                self.ffmpeg,
                "-v",
                "error",
                "-y",
                "-i",
                str(path),
                "-map",
                "0",
                "-c",
                "copy",
                "-map_metadata",
                "0",
                *args,
                "-f",
                _MUXERS[detected.container],
                str(temp_path),
            ]
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise MetadataWriteError(
                    str(path),
                    completed.stderr.strip() or "ffmpeg failed",
                )
            if policy.verify:
                self._verify(temp_path, detected, metadata, report)
            os.replace(temp_path, path)
        except WriteVerificationError:
            raise
        except (OSError, ValueError) as exc:
            raise MetadataWriteError(str(path), str(exc)) from exc
        finally:
            temp_path.unlink(missing_ok=True)

        report.bytes_before = before
        report.bytes_after = path.stat().st_size
        return report

    def _probe(self, path: Path) -> dict[str, Any]:
        if self.ffprobe is None:
            raise UnsupportedFormatError(str(path))
        command = [
            self.ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise MetadataReadError(str(path), str(exc)) from exc
        if completed.returncode != 0:
            raise MetadataReadError(
                str(path),
                completed.stderr.strip() or "ffprobe failed",
            )
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise MetadataReadError(str(path), "invalid ffprobe JSON") from exc

    @staticmethod
    def _apply_tags(tags: dict[str, str], metadata: MediaMetadata) -> None:
        def first(*keys: str) -> str | None:
            for key in keys:
                value = tags.get(key.lower())
                if value:
                    return clean_text(str(value))
            return None

        def values(*keys: str) -> list[str]:
            for key in keys:
                value = tags.get(key.lower())
                if value:
                    return split_values(str(value))
            return []

        metadata.title = first("title")
        metadata.title_sort = first("titlesort", "title_sort")
        metadata.artists = _semicolon_values(tags, "artist", "artists")
        metadata.artist = metadata.artists[0] if metadata.artists else None
        metadata.artist_sort = first("artistsort", "artist_sort")
        metadata.date = parse_partial_date(first("date", "year", "date_released"))
        metadata.genre = parse_genres(values("genre"))
        metadata.comment = first("comment")
        metadata.composer = _semicolon_values(tags, "composer")
        metadata.lyricist = _semicolon_values(tags, "lyricist")
        metadata.conductor = _semicolon_values(tags, "conductor")
        lyrics_text = first("lyrics", "unsyncedlyrics")
        if lyrics_text is not None:
            metadata.lyrics = Lyrics(text=lyrics_text, language=first("language"))
        metadata.producer = _semicolon_values(tags, "producer")
        metadata.copyright = first("copyright")
        metadata.language = first("language")
        metadata.grouping = first("grouping")

        for source, name in _MB_READ_ALIASES.items():
            value = first(source)
            if value is not None:
                metadata.musicbrainz_ids[name] = value

        if metadata.audio is not None:
            audio = metadata.audio
            audio.album = first("album", "product")
            audio.album_sort = first("albumsort", "album_sort")
            audio.album_artist = first("album_artist", "albumartist")
            audio.album_artist_sort = first("albumartistsort", "album_artist_sort")
            audio.track_number, audio.track_total = parse_number_pair(
                first("track", "tracknumber", "part_number")
            )
            audio.disc_number, audio.disc_total = parse_number_pair(
                first("disc", "discnumber")
            )
            audio.compilation = parse_bool(first("compilation"))
            audio.label = first("label", "publisher")
            audio.isrc = first("isrc")
            audio.bpm = parse_int(first("bpm", "beatsperminute"))
            audio.key = first("initial_key", "initialkey", "key")
            for name, key in _RG_KEYS.items():
                value = first(key)
                if value is not None:
                    gain = parse_gain(value)
                    if gain is not None:
                        audio.replaygain[name] = gain

        if metadata.video is not None:
            video = metadata.video
            video.show = first("show", "tvshow")
            video.season = parse_int(first("season_number", "season"))
            video.episode = parse_int(first("episode_sort", "episode"))
            video.episode_id = first("episode_id")
            video.network = first("network")
            video.director = _semicolon_values(tags, "director")
            video.writer = _semicolon_values(tags, "writer", "screenplay_by")
            video.synopsis = first("synopsis", "summary", "description")
            video.rating = first("rating", "law_rating")
            video.keywords = _split_list(first("keywords"))
            video.location = first("location")

        known = {
            "title", "titlesort", "title_sort", "artist", "artists", "artistsort",
            "artist_sort", "date", "year", "date_released", "genre", "comment",
            "composer", "lyricist", "conductor", "lyrics", "unsyncedlyrics",
            "producer", "copyright", "language", "grouping", "album", "product",
            "albumsort", "album_sort", "album_artist", "albumartist",
            "albumartistsort", "album_artist_sort", "track", "tracknumber",
            "part_number", "disc", "discnumber", "compilation", "label", "publisher",
            "isrc", "bpm", "beatsperminute", "initial_key", "initialkey", "key",
            "show", "tvshow", "season_number", "season", "episode_sort", "episode",
            "episode_id", "network", "director", "writer", "screenplay_by", "synopsis",
            "summary", "description", "rating", "law_rating", "keywords", "location",
            "encoder", "duration", "creation_time", "handler_name", "vendor_id",
            "major_brand", "minor_version", "compatible_brands", "media_type",
            *_MB_READ_ALIASES,
            *_RG_KEYS.values(),
        }
        for key, value in tags.items():
            if key not in known:
                metadata.extra[key] = split_values(str(value))

    @staticmethod
    def _metadata_args(
        metadata: MediaMetadata,
        container: ContainerFormat,
        policy: WritePolicy,
        report: WriteReport,
    ) -> list[str]:
        args: list[str] = []

        def add(key: str, value: str | None, field: str) -> None:
            if value is None or value == "":
                return
            if container is ContainerFormat.AVI and field not in _AVI_FIELDS:
                report.skipped.append((field, "AVI stores only RIFF INFO fields"))
                return
            args.extend(["-metadata", f"{key}={value}"])
            report.written.append(field)

        add("title", metadata.title, "title")
        add("titlesort", metadata.title_sort, "title_sort")
        add("artist", _join(metadata.artists) or metadata.artist, "artists")
        add("artistsort", metadata.artist_sort, "artist_sort")
        add("date", str(metadata.date) if metadata.date else None, "date")
        add("genre", _join(metadata.genre), "genre")
        add("comment", metadata.comment, "comment")
        add("composer", _join(metadata.composer), "composer")
        add("lyricist", _join(metadata.lyricist), "lyricist")
        add("conductor", _join(metadata.conductor), "conductor")
        add("lyrics", metadata.lyrics.text if metadata.lyrics else None, "lyrics")
        add("producer", _join(metadata.producer), "producer")
        add("copyright", metadata.copyright, "copyright")
        add("language", metadata.language, "language")
        add("grouping", metadata.grouping, "grouping")

        audio = metadata.audio
        if audio is not None:
            add("album", audio.album, "audio.album")
            add("albumsort", audio.album_sort, "audio.album_sort")
            add("album_artist", audio.album_artist, "audio.album_artist")
            add("albumartistsort", audio.album_artist_sort, "audio.album_artist_sort")
            add("track", _number_pair(audio.track_number, audio.track_total), "audio.track")
            add("disc", _number_pair(audio.disc_number, audio.disc_total), "audio.disc")
            if audio.compilation is not None:
                add(
                    "compilation",
                    "1" if audio.compilation else "0",
                    "audio.compilation",
                )
            add("label", audio.label, "audio.label")
            add("isrc", audio.isrc, "audio.isrc")
            add("bpm", str(audio.bpm) if audio.bpm is not None else None, "audio.bpm")
            add("initial_key", audio.key, "audio.key")
            for name, key in _RG_KEYS.items():
                value = audio.replaygain.get(name)
                add(key, f"{value:.2f} dB" if value is not None else None, f"audio.replaygain.{name}")

        video = metadata.video
        if video is not None:
            add("show", video.show, "video.show")
            add("season_number", str(video.season) if video.season is not None else None, "video.season")
            add("episode_sort", str(video.episode) if video.episode is not None else None, "video.episode")
            add("episode_id", video.episode_id, "video.episode_id")
            add("network", video.network, "video.network")
            add("director", _join(video.director), "video.director")
            add("writer", _join(video.writer), "video.writer")
            add("synopsis", video.synopsis, "video.synopsis")
            add("rating", video.rating, "video.rating")
            add("keywords", _join(video.keywords), "video.keywords")
            add("location", video.location, "video.location")

        for name, key in _MB_KEYS.items():
            add(key, metadata.musicbrainz_ids.get(name), f"musicbrainz_ids.{name}")

        if getattr(metadata, "artwork", None):
            report.skipped.append(("artwork", "ffmpeg backend does not write artwork yet"))

        return args

    def _verify(
        self,
        path: Path,
        detected: DetectedFormat,
        expected: MediaMetadata,
        report: WriteReport,
    ) -> None:
        result = self.read(path, detected)
        actual = result.metadata
        mismatches: list[str] = []
        for field in report.written:
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


def _lower_tags(tags: dict[str, Any] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in (tags or {}).items():
        if value is not None:
            result[str(key).lower()] = str(value)
    return result


def _join(values: list[str]) -> str | None:
    return "; ".join(values) if values else None


def _semicolon_values(tags: dict[str, str], *keys: str) -> list[str]:
    for key in keys:
        value = tags.get(key.lower())
        if value:
            return [part for part in (clean_text(item) for item in str(value).split(";")) if part]
    return []


def _split_list(value: str | None) -> list[str]:
    if value is None:
        return []
    parts: list[str] = []
    for item in value.replace(";", ",").split(","):
        cleaned = clean_text(item)
        if cleaned is not None:
            parts.append(cleaned)
    return parts


def _number_pair(number: int | None, total: int | None) -> str | None:
    if number is None:
        return None
    if total is None:
        return str(number)
    return f"{number}/{total}"


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

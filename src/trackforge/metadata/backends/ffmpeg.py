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

_SKIP_EXTRA = {
    "encoder",
    "encodedby",
    "encodersettings",
    "software",
    "duration",
    "creation_time",
    "handler_name",
    "vendor_id",
    "major_brand",
    "minor_version",
    "compatible_brands",
    "media_type",
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
        timeout: float = 30.0,
    ) -> None:
        self.ffprobe = ffprobe if ffprobe is not None else shutil.which("ffprobe")
        self.ffmpeg = ffmpeg if ffmpeg is not None else shutil.which("ffmpeg")
        self.timeout = timeout

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

        artwork = self._read_artwork(path, detected, streams)
        return ReadResult(metadata=metadata, artwork=artwork, warnings=[])

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
            artwork=container is ContainerFormat.MATROSKA,
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
        if not policy.atomic:
            report.warnings.append(
                "ffmpeg backend always writes through a temporary file"
            )
        args = self._metadata_args(metadata, detected.container, policy, report)
        artwork_args, artwork_files = self._artwork_args(
            path,
            detected,
            getattr(metadata, "artwork", ()),
            policy,
            report,
        )
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
                *artwork_args,
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
                timeout=self.timeout,
            )
            if completed.returncode != 0:
                raise MetadataWriteError(
                    str(path),
                    completed.stderr.strip() or "ffmpeg failed",
                )
            if policy.verify:
                self._verify(temp_path, detected, metadata, report)
            _fsync_file(temp_path)
            os.replace(temp_path, path)
            _fsync_dir(path.parent)
        except WriteVerificationError:
            raise
        except subprocess.TimeoutExpired as exc:
            raise MetadataWriteError(str(path), "ffmpeg timed out") from exc
        except (OSError, ValueError) as exc:
            raise MetadataWriteError(str(path), str(exc)) from exc
        finally:
            temp_path.unlink(missing_ok=True)
            for artwork_path in artwork_files:
                artwork_path.unlink(missing_ok=True)

        report.bytes_before = before
        report.bytes_after = path.stat().st_size
        return report

    def _read_artwork(
        self,
        path: Path,
        detected: DetectedFormat,
        streams: list[dict[str, Any]],
    ) -> list[Artwork]:
        if detected.container is not ContainerFormat.MATROSKA or self.ffmpeg is None:
            return []
        result: list[Artwork] = []
        for stream in streams:
            disposition = stream.get("disposition") or {}
            if stream.get("codec_type") != "video" or not disposition.get("attached_pic"):
                continue
            tags = _lower_tags(stream.get("tags"))
            mime = tags.get("mimetype") or "application/octet-stream"
            suffix = _image_suffix(mime)
            if suffix is None:
                continue
            index = stream.get("index")
            if index is None:
                continue
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
                temp_path = Path(handle.name)
            try:
                completed = subprocess.run(
                    [
                        self.ffmpeg,
                        "-v",
                        "error",
                        "-y",
                        "-i",
                        str(path),
                        "-map",
                        f"0:{index}",
                        "-c",
                        "copy",
                        str(temp_path),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=self.timeout,
                )
                if completed.returncode != 0 or temp_path.stat().st_size == 0:
                    continue
                filename = tags.get("filename") or temp_path.name
                result.append(
                    Artwork(
                        data=temp_path.read_bytes(),
                        mime=mime,
                        kind=_artwork_kind(filename),
                        description=filename,
                    )
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            finally:
                temp_path.unlink(missing_ok=True)
        return result

    def _artwork_args(
        self,
        path: Path,
        detected: DetectedFormat,
        artwork,
        policy: WritePolicy,
        report: WriteReport,
    ) -> tuple[list[str], list[Path]]:
        images = list(artwork)
        if detected.container is not ContainerFormat.MATROSKA:
            if images:
                report.skipped.append(
                    ("artwork", "ffmpeg artwork is implemented for Matroska only")
                )
            return [], []
        if not images and not policy.remove_missing:
            return [], []

        valid_images: list[tuple[Artwork, str]] = []
        for image in images:
            suffix = _image_suffix(image.mime)
            if suffix is None:
                report.skipped.append(
                    ("artwork", f"unsupported artwork MIME type: {image.mime}")
                )
                continue
            valid_images.append((image, suffix))
        if not valid_images and not policy.remove_missing:
            return [], []

        args: list[str] = []
        try:
            streams = self._probe(path).get("streams", [])
        except MetadataReadError:
            streams = []
        for stream in streams:
            disposition = stream.get("disposition") or {}
            if stream.get("codec_type") == "video" and disposition.get("attached_pic"):
                index = stream.get("index")
                if index is not None:
                    args.extend(["-map", f"-0:{index}"])

        temp_files: list[Path] = []
        attachment_index = 0
        for image, suffix in valid_images:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
                handle.write(image.data)
                temp_path = Path(handle.name)
            temp_files.append(temp_path)
            filename = f"cover{attachment_index}{suffix}"
            args.extend(
                [
                    "-attach",
                    str(temp_path),
                    f"-metadata:s:t:{attachment_index}",
                    f"mimetype={image.mime}",
                    f"-metadata:s:t:{attachment_index}",
                    f"filename={filename}",
                ]
            )
            attachment_index += 1
        if attachment_index or policy.remove_missing:
            report.written.append("artwork")
        return args, temp_files

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
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise MetadataReadError(str(path), "ffprobe timed out") from exc
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
        metadata.original_date = parse_partial_date(
            first("original_date", "originaldate")
        )
        metadata.release_date = parse_partial_date(
            first("release_date", "releasedate")
        )
        metadata.mood = first("mood")
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
            "artist_sort", "date", "year", "date_released", "original_date",
            "originaldate", "release_date", "releasedate", "mood", "genre", "comment",
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
        add(
            "original_date",
            str(metadata.original_date) if metadata.original_date else None,
            "original_date",
        )
        add(
            "release_date",
            str(metadata.release_date) if metadata.release_date else None,
            "release_date",
        )
        add("mood", metadata.mood, "mood")
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

        if policy.write_extra:
            for key, values in metadata.extra.items():
                if values and key.lower() not in _SKIP_EXTRA:
                    add(key, "; ".join(values), f"extra.{key}")

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
            if field == "artwork":
                expected_art = sorted(
                    (item.mime, item.data) for item in getattr(expected, "artwork", ())
                )
                actual_art = sorted((item.mime, item.data) for item in result.artwork)
                if expected_art != actual_art:
                    mismatches.append("artwork")
                continue
            expected_value = _field_value(expected, field)
            actual_value = _field_value(actual, field)
            if field == "lyrics" or field.endswith(".lyrics"):
                expected_value = expected_value.text if expected_value else None
                actual_value = actual_value.text if actual_value else None
            if field.startswith("extra.") and isinstance(expected_value, list) and isinstance(actual_value, list):
                if len(actual_value) == 1 and "; ".join(expected_value) == actual_value[0]:
                    continue
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


def _image_suffix(mime: str) -> str | None:
    if mime in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    if mime == "image/png":
        return ".png"
    return None


def _artwork_kind(filename: str) -> str:
    lower = filename.lower()
    if "back" in lower:
        return "back"
    if "artist" in lower:
        return "artist"
    if "logo" in lower:
        return "logo"
    if "cover" in lower or "front" in lower:
        return "front"
    return "other"

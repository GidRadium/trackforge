"""Read metadata through mutagen.

Mutagen covers the audio containers in the P0/P1 set.  Matroska, AVI, FLV and
MPEG-TS are deliberately absent: they need the ffmpeg backend and should not
be faked here.
"""

from __future__ import annotations

import base64
import mimetypes
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mutagen import MutagenError
from mutagen._vorbis import VCommentDict
from mutagen.aac import AAC
from mutagen.aiff import AIFF
from mutagen.apev2 import APEv2
from mutagen.asf import ASF, ASFTags
from mutagen.dsdiff import DSDIFF
from mutagen.dsf import DSF
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.monkeysaudio import MonkeysAudio
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm, MP4Tags
from mutagen.musepack import Musepack
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggspeex import OggSpeex
from mutagen.oggtheora import OggTheora
from mutagen.oggvorbis import OggVorbis
from mutagen.trueaudio import TrueAudio
from mutagen.wave import WAVE
from mutagen.wavpack import WavPack

from ..capabilities import (
    Capabilities,
    WritePolicy,
    WriteReport,
    standard_capabilities,
)
from ..errors import MetadataReadError, UnsupportedFormatError
from ..formats import ContainerFormat, DetectedFormat, TagSystem
from ..model import (
    Artwork,
    AudioMetadata,
    Lyrics,
    MediaMetadata,
    VideoMetadata,
)
from ..normalize import (
    ID3_GENRES,
    clean_text,
    first_text,
    parse_bool,
    parse_flac_picture,
    parse_gain,
    parse_genres,
    parse_int,
    parse_number_pair,
    parse_partial_date,
    picture_kind,
    split_values,
)
from .base import ReadResult

_FILE_CLASSES: dict[ContainerFormat, type[Any]] = {
    ContainerFormat.MP3: MP3,
    ContainerFormat.AAC: AAC,
    ContainerFormat.FLAC: FLAC,
    ContainerFormat.MP4: MP4,
    ContainerFormat.WAV: WAVE,
    ContainerFormat.AIFF: AIFF,
    ContainerFormat.ASF: ASF,
    ContainerFormat.APE: MonkeysAudio,
    ContainerFormat.WAVPACK: WavPack,
    ContainerFormat.MUSEPACK: Musepack,
    ContainerFormat.TTA: TrueAudio,
    ContainerFormat.DSF: DSF,
    ContainerFormat.DFF: DSDIFF,
}

_OGG_CLASSES: dict[str, type[Any]] = {
    "vorbis": OggVorbis,
    "opus": OggOpus,
    "flac": OggFLAC,
    "speex": OggSpeex,
    "theora": OggTheora,
}

WRITABLE_CONTAINERS = frozenset(
    {
        ContainerFormat.MP3,
        ContainerFormat.AAC,
        ContainerFormat.FLAC,
        ContainerFormat.OGG,
        ContainerFormat.MP4,
        ContainerFormat.WAV,
        ContainerFormat.AIFF,
        ContainerFormat.DSF,
        ContainerFormat.DFF,
    }
)

_TAG_SYSTEMS: dict[ContainerFormat, TagSystem] = {
    ContainerFormat.MP3: TagSystem.ID3V2,
    ContainerFormat.AAC: TagSystem.ID3V2,
    ContainerFormat.FLAC: TagSystem.VORBIS_COMMENT,
    ContainerFormat.OGG: TagSystem.VORBIS_COMMENT,
    ContainerFormat.MP4: TagSystem.MP4_ILST,
    ContainerFormat.WAV: TagSystem.ID3V2,
    ContainerFormat.AIFF: TagSystem.ID3V2,
    ContainerFormat.ASF: TagSystem.ASF,
    ContainerFormat.APE: TagSystem.APEV2,
    ContainerFormat.WAVPACK: TagSystem.APEV2,
    ContainerFormat.MUSEPACK: TagSystem.APEV2,
    ContainerFormat.TTA: TagSystem.APEV2,
    ContainerFormat.DSF: TagSystem.ID3V2,
    ContainerFormat.DFF: TagSystem.ID3V2,
}

_MB_TXXX = {    "musicbrainz track id": "track",
    "musicbrainz album id": "album",
    "musicbrainz artist id": "artist",
    "musicbrainz album artist id": "album_artist",
    "musicbrainz release group id": "release_group",
    "musicbrainz release track id": "release_track",
    "musicbrainz work id": "work",
}

_RG_TXXX = {
    "replaygain_track_gain": "track_gain",
    "replaygain_album_gain": "album_gain",
    "replaygain_track_peak": "track_peak",
    "replaygain_album_peak": "album_peak",
}

_MB_VORBIS = {
    "musicbrainz_trackid": "track",
    "musicbrainz_albumid": "album",
    "musicbrainz_artistid": "artist",
    "musicbrainz_albumartistid": "album_artist",
    "musicbrainz_releasegroupid": "release_group",
    "musicbrainz_releasetrackid": "release_track",
    "musicbrainz_workid": "work",
}

_RG_VORBIS = {
    "replaygain_track_gain": "track_gain",
    "replaygain_album_gain": "album_gain",
    "replaygain_track_peak": "track_peak",
    "replaygain_album_peak": "album_peak",
}


def open_audio_file(path: Path, detected: DetectedFormat) -> Any:
    """Open ``path`` with the mutagen class selected by ``detected``."""
    if detected.container == ContainerFormat.OGG:
        file_class = _OGG_CLASSES.get(detected.codec or "")
        if file_class is None:
            raise UnsupportedFormatError(str(path))
    else:
        file_class = _FILE_CLASSES.get(detected.container)
        if file_class is None:
            raise UnsupportedFormatError(str(path))

    try:
        return file_class(path)
    except (MutagenError, OSError, ValueError) as exc:
        raise MetadataReadError(str(path), str(exc)) from exc


class MutagenBackend:
    """Read tags from audio containers supported by mutagen."""

    name = "mutagen"
    containers = frozenset({*_FILE_CLASSES, ContainerFormat.OGG})

    def read(self, path: Path, detected: DetectedFormat) -> ReadResult:
        audio_file = self._open(path, detected)
        metadata = MediaMetadata()
        self._init_facets(metadata, detected)
        artwork: list[Artwork] = []
        warnings: list[str] = []

        tags = audio_file.tags
        fallback_found = False
        if tags:
            if isinstance(tags, ID3):
                self._read_id3(tags, metadata, artwork)
            elif isinstance(tags, APEv2):
                self._read_ape(tags, metadata, artwork)
            elif isinstance(tags, VCommentDict):
                self._read_vorbis(tags, audio_file, metadata, artwork)
            elif isinstance(tags, MP4Tags):
                self._read_mp4(tags, metadata, artwork)
            elif isinstance(tags, ASFTags):
                self._read_asf(tags, metadata, artwork)
            else:
                warnings.append(f"unsupported tag container: {type(tags).__name__}")

        if detected.container in {ContainerFormat.MP3, ContainerFormat.AAC}:
            fallback_found = self._read_legacy(path, metadata, artwork)

        if not tags and not fallback_found:
            warnings.append("no tags found")

        return ReadResult(metadata=metadata, artwork=artwork, warnings=warnings)

    def capabilities(self, container: ContainerFormat) -> Capabilities:
        tag_system = _TAG_SYSTEMS.get(container, TagSystem.NONE)
        return standard_capabilities(
            container,
            tag_system,
            audio=True,
            video=container in {ContainerFormat.MP4, ContainerFormat.OGG, ContainerFormat.ASF},
            arbitrary_keys=tag_system
            in {TagSystem.ID3V2, TagSystem.VORBIS_COMMENT, TagSystem.APEV2, TagSystem.MP4_ILST},
            artwork=True,
            writable=container in WRITABLE_CONTAINERS,
        )

    def write(
        self,
        path: Path,
        detected: DetectedFormat,
        metadata: MediaMetadata,
        *,
        policy: WritePolicy,
    ) -> WriteReport:
        from .mutagen_write import write_file

        return write_file(self, path, detected, metadata, policy=policy)

    def _open(self, path: Path, detected: DetectedFormat) -> Any:
        return open_audio_file(path, detected)

    def _read_legacy(
        self,
        path: Path,
        metadata: MediaMetadata,
        artwork: list[Artwork],
    ) -> bool:
        """Read APEv2/ID3v1 fallbacks for MP3/AAC files."""
        found = False

        try:
            ape_tags = APEv2(path)
        except (MutagenError, OSError, ValueError):
            ape_tags = None
        if ape_tags:
            temp = MediaMetadata()
            temp.audio = AudioMetadata()
            temp_artwork: list[Artwork] = []
            self._read_ape(ape_tags, temp, temp_artwork)
            _merge_missing(metadata, temp)
            if not artwork:
                artwork.extend(temp_artwork)
            found = True

        id3v1 = _read_id3v1(path)
        if id3v1 is not None:
            _merge_missing(metadata, id3v1)
            found = True
        return found

    @staticmethod
    def _init_facets(metadata: MediaMetadata, detected: DetectedFormat) -> None:
        container = detected.container
        extension = (detected.extension or "").lower()
        variant = detected.variant

        if container == ContainerFormat.MP4:
            if variant in {"m4a", "m4b"} or extension in {"m4a", "m4b"}:
                metadata.audio = AudioMetadata()
            elif variant in {"m4v", "mov"} or extension in {"m4v", "mov"}:
                metadata.video = VideoMetadata()
                metadata.audio = AudioMetadata()
            else:
                metadata.audio = AudioMetadata()
                metadata.video = VideoMetadata()
        elif container == ContainerFormat.OGG and detected.codec == "theora":
            metadata.audio = AudioMetadata()
            metadata.video = VideoMetadata()
        elif container == ContainerFormat.ASF:
            if extension == "wma":
                metadata.audio = AudioMetadata()
            elif extension in {"wmv", "asf"}:
                metadata.video = VideoMetadata()
                metadata.audio = AudioMetadata()
            else:
                metadata.audio = AudioMetadata()
                metadata.video = VideoMetadata()
        else:
            metadata.audio = AudioMetadata()

    # ------------------------------------------------------------------
    # ID3 (MP3, AAC, WAVE, AIFF, DSF, DFF)
    # ------------------------------------------------------------------

    @staticmethod
    def _read_id3(tags: ID3, metadata: MediaMetadata, artwork: list[Artwork]) -> None:
        audio = metadata.audio
        if audio is None:
            audio = AudioMetadata()
            metadata.audio = audio

        metadata.title = _id3_text(tags, "TIT2")
        metadata.title_sort = _id3_text(tags, "TSOT")
        metadata.artist = _id3_text(tags, "TPE1")
        metadata.artists = _id3_texts(tags, "TPE1")
        metadata.artist_sort = _id3_text(tags, "TSOP")

        date_text = _id3_text(tags, "TDRC")
        if date_text is None:
            date_text = _id3_fallback_date(tags)
        metadata.date = parse_partial_date(date_text)

        metadata.genre = parse_genres(_id3_texts(tags, "TCON"))
        metadata.comment = _id3_comment(tags)
        metadata.composer = _id3_texts(tags, "TCOM")
        metadata.lyricist = _id3_texts(tags, "TEXT")
        metadata.lyrics = _id3_lyrics(tags)
        metadata.copyright = _id3_text(tags, "TCOP")
        metadata.language = _id3_text(tags, "TLAN")
        metadata.grouping = _id3_text(tags, "GRP1")

        audio.album = _id3_text(tags, "TALB")
        audio.album_sort = _id3_text(tags, "TSOA")
        audio.album_artist = _id3_text(tags, "TPE2")
        audio.album_artist_sort = _id3_text(tags, "TSO2")
        audio.track_number, audio.track_total = parse_number_pair(_id3_text(tags, "TRCK"))
        audio.disc_number, audio.disc_total = parse_number_pair(_id3_text(tags, "TPOS"))
        audio.compilation = parse_bool(_id3_text(tags, "TCMP"))
        audio.label = _id3_text(tags, "TPUB")
        audio.isrc = _id3_text(tags, "TSRC")
        audio.bpm = parse_int(_id3_text(tags, "TBPM"))
        audio.key = _id3_text(tags, "TKEY")

        conductor = _id3_texts(tags, "TPE3")
        if conductor:
            metadata.conductor.extend(conductor)

        tipl = tags.get("TIPL")
        if tipl is not None:
            for role, person in getattr(tipl, "people", []) or []:
                if str(role).lower() == "producer" and clean_text(person):
                    metadata.producer.append(person)

        for frame in tags.getall("TXXX"):
            desc = clean_text(str(getattr(frame, "desc", "") or "")) or ""
            key = desc.lower()
            values = split_values(getattr(frame, "text", None))
            if not values:
                continue
            if key in _MB_TXXX:
                metadata.musicbrainz_ids[_MB_TXXX[key]] = values[0]
            elif key.replace(" ", "_") in _RG_TXXX:
                gain = parse_gain(values[0])
                if gain is not None:
                    audio.replaygain[_RG_TXXX[key.replace(" ", "_")]] = gain
            elif key in {"isrc", "label", "language", "conductor", "lyricist", "producer"}:
                _apply_txxx(metadata, audio, key, values)
            else:
                metadata.extra.setdefault(desc or key, []).extend(values)

        for frame in tags.getall("APIC"):
            data = bytes(frame.data)
            if not data:
                continue
            artwork.append(
                Artwork(
                    data=data,
                    mime=frame.mime or "application/octet-stream",
                    kind=picture_kind(frame.type),
                    description=clean_text(frame.desc),
                )
            )

    # ------------------------------------------------------------------
    # Vorbis comments (FLAC, Ogg Vorbis/Opus/FLAC/Speex/Theora)
    # ------------------------------------------------------------------

    def _read_vorbis(
        self,
        tags: VCommentDict,
        audio_file: Any,
        metadata: MediaMetadata,
        artwork: list[Artwork],
    ) -> None:
        items = {str(key).lower(): value for key, value in tags.items()}

        def texts(key: str) -> list[str]:
            return split_values(items.get(key))

        def text(key: str) -> str | None:
            return first_text(items.get(key))

        audio = metadata.audio
        if audio is None:
            audio = AudioMetadata()
            metadata.audio = audio

        metadata.title = text("title")
        metadata.title_sort = text("titlesort")
        metadata.artists = texts("artist")
        metadata.artist = metadata.artists[0] if metadata.artists else None
        metadata.artist_sort = text("artistsort")
        date_text = text("date") or text("year")
        metadata.date = parse_partial_date(date_text)
        metadata.genre = parse_genres(texts("genre"))
        metadata.comment = text("comment")
        metadata.composer = texts("composer")
        metadata.lyricist = texts("lyricist")
        metadata.conductor = texts("conductor")
        lyrics_text = text("lyrics") or text("unsyncedlyrics")
        if lyrics_text is not None:
            metadata.lyrics = Lyrics(text=lyrics_text, language=text("language"))
        metadata.producer = texts("producer")
        metadata.copyright = text("copyright")
        metadata.language = text("language")
        metadata.grouping = text("grouping")

        audio.album = text("album")
        audio.album_sort = text("albumsort")
        audio.album_artist = text("albumartist")
        audio.album_artist_sort = text("albumartistsort")
        audio.track_number = parse_int(text("tracknumber"))
        audio.track_total = parse_int(text("tracktotal")) or parse_int(text("totaltracks"))
        audio.disc_number = parse_int(text("discnumber"))
        audio.disc_total = parse_int(text("disctotal")) or parse_int(text("totaldiscs"))
        audio.compilation = parse_bool(text("compilation"))
        audio.label = text("label") or text("organization")
        audio.isrc = text("isrc")
        audio.bpm = parse_int(text("bpm"))
        audio.key = text("initialkey")

        for key, name in _MB_VORBIS.items():
            value = text(key)
            if value is not None:
                metadata.musicbrainz_ids[name] = value
        for key, name in _RG_VORBIS.items():
            value = parse_gain(text(key))
            if value is not None:
                audio.replaygain[name] = value

        for picture in getattr(audio_file, "pictures", []) or []:
            data = bytes(picture.data)
            if not data:
                continue
            artwork.append(
                Artwork(
                    data=data,
                    mime=picture.mime or "application/octet-stream",
                    kind=picture_kind(picture.type),
                    description=clean_text(picture.desc),
                    width=picture.width or None,
                    height=picture.height or None,
                )
            )

        for encoded in texts("metadata_block_picture"):
            try:
                picture = parse_flac_picture(base64.b64decode(encoded))
            except (ValueError, TypeError):
                continue
            if picture is not None:
                artwork.append(picture)

        cover_mimes = texts("coverartmime")
        for index, encoded in enumerate(texts("coverart")):
            try:
                data = base64.b64decode(encoded)
            except (ValueError, TypeError):
                continue
            if not data:
                continue
            mime = cover_mimes[index] if index < len(cover_mimes) else "image/jpeg"
            artwork.append(Artwork(data=data, mime=mime))

        known = {
            "title", "titlesort", "artist", "artists", "artistsort", "date", "year",
            "genre", "comment", "composer", "lyricist", "conductor", "lyrics", "unsyncedlyrics",
            "producer", "copyright", "language", "grouping", "album", "albumsort",
            "albumartist", "albumartistsort", "tracknumber", "tracktotal",
            "totaltracks", "discnumber", "disctotal", "totaldiscs", "compilation",
            "label", "organization", "isrc", "bpm", "initialkey",
            "metadata_block_picture", "coverart", "coverartmime",
            *_MB_VORBIS,
            *_RG_VORBIS,
        }
        for key, values in items.items():
            if key not in known:
                metadata.extra[key] = split_values(values)

    # ------------------------------------------------------------------
    # MP4 atoms
    # ------------------------------------------------------------------

    @staticmethod
    def _read_mp4(tags: MP4Tags, metadata: MediaMetadata, artwork: list[Artwork]) -> None:
        metadata.title = _mp4_text(tags, "©nam")
        metadata.title_sort = _mp4_text(tags, "sonm")
        metadata.artists = _mp4_texts(tags, "©ART")
        metadata.artist = metadata.artists[0] if metadata.artists else None
        metadata.artist_sort = _mp4_text(tags, "soar")
        metadata.date = parse_partial_date(_mp4_text(tags, "©day"))
        metadata.genre = parse_genres(_mp4_texts(tags, "©gen"))
        metadata.comment = _mp4_text(tags, "©cmt")
        metadata.composer = _mp4_texts(tags, "©wrt")
        metadata.lyrics = _lyrics_from_text(_mp4_text(tags, "©lyr"))
        metadata.copyright = _mp4_text(tags, "cprt") or _mp4_text(tags, "©cpy")
        metadata.language = _mp4_freeform_text(tags, "LANGUAGE")
        metadata.grouping = _mp4_text(tags, "©grp")

        audio = metadata.audio
        if audio is not None:
            audio.album = _mp4_text(tags, "©alb")
            audio.album_sort = _mp4_text(tags, "soal")
            audio.album_artist = _mp4_text(tags, "aART")
            audio.album_artist_sort = _mp4_text(tags, "soaa")
            audio.track_number, audio.track_total = _mp4_pair(tags, "trkn")
            audio.disc_number, audio.disc_total = _mp4_pair(tags, "disk")
            audio.compilation = parse_bool(_mp4_first(tags, "cpil"))
            audio.bpm = parse_int(_mp4_first(tags, "tmpo"))
            audio.isrc = _mp4_freeform_text(tags, "ISRC")
            audio.label = _mp4_freeform_text(tags, "LABEL")

        video = metadata.video
        if video is not None:
            video.show = _mp4_text(tags, "tvsh")
            video.season = parse_int(_mp4_first(tags, "tvsn"))
            video.episode = parse_int(_mp4_first(tags, "tves"))
            video.episode_id = _mp4_text(tags, "tven")
            video.network = _mp4_text(tags, "tvnn")
            video.synopsis = _mp4_text(tags, "ldes") or _mp4_text(tags, "desc")
            video.keywords = _mp4_texts(tags, "keyw")
            video.director = _mp4_freeform_texts(tags, "DIRECTOR")
            video.writer = _mp4_texts(tags, "©wrt")
            if audio is not None:
                audio.album = audio.album or _mp4_text(tags, "©alb")

        for cover in tags.get("covr") or []:
            data = bytes(cover)
            if not data:
                continue
            artwork.append(
                Artwork(
                    data=data,
                    mime=_mp4_cover_mime(cover),
                    kind="front",
                )
            )

        for key in tags.keys():
            if not key.startswith("----:"):
                continue
            parts = key.split(":", 2)
            name = parts[2] if len(parts) == 3 else key
            values = _mp4_freeform_values(tags, key)
            if not values:
                continue
            lower = name.lower()
            rg_key = lower.replace(" ", "_")
            if lower in _MB_TXXX:
                metadata.musicbrainz_ids[_MB_TXXX[lower]] = values[0]
            elif rg_key in _RG_TXXX and audio is not None:
                gain = parse_gain(values[0])
                if gain is not None:
                    audio.replaygain[_RG_TXXX[rg_key]] = gain
            elif lower == "artists":
                metadata.artists.extend(values)
            elif lower == "conductor":
                metadata.conductor.extend(values)
            elif lower == "lyricist":
                metadata.lyricist.extend(values)
            elif lower == "producer":
                metadata.producer.extend(values)
            elif lower in {"isrc", "label", "language"}:
                _apply_freeform(metadata, audio, lower, values)
            else:
                metadata.extra.setdefault(name, []).extend(values)

        for key, name in {
            "©too": "ENCODER",
            "purl": "PURL",
            "egid": "EGID",
            "catg": "CATEGORY",
            "stik": "MEDIA_KIND",
        }.items():
            value = _mp4_text(tags, key)
            if value is not None:
                metadata.extra.setdefault(name, []).append(value)

    # ------------------------------------------------------------------
    # APEv2 (APE, WavPack, Musepack, TrueAudio)
    # ------------------------------------------------------------------

    @staticmethod
    def _read_ape(tags: APEv2, metadata: MediaMetadata, artwork: list[Artwork]) -> None:
        items = {str(key).lower(): value for key, value in tags.items()}

        audio = metadata.audio
        if audio is None:
            audio = AudioMetadata()
            metadata.audio = audio

        def text(key: str) -> str | None:
            value = items.get(key)
            if value is None:
                return None
            raw = getattr(value, "value", value)
            if isinstance(raw, bytes):
                return None
            return clean_text(str(raw))

        def texts(key: str) -> list[str]:
            value = text(key)
            return split_values(value) if value else []

        metadata.title = text("title")
        metadata.artist = text("artist")
        metadata.artists = texts("artist")
        metadata.title_sort = text("titlesort")
        metadata.artist_sort = text("artistsort")
        metadata.date = parse_partial_date(text("year") or text("date"))
        metadata.genre = parse_genres(texts("genre"))
        metadata.comment = text("comment")
        metadata.composer = texts("composer")
        metadata.lyricist = texts("lyricist")
        metadata.conductor = texts("conductor")
        lyrics_text = text("lyrics")
        if lyrics_text is not None:
            metadata.lyrics = Lyrics(text=lyrics_text)
        metadata.copyright = text("copyright")
        metadata.language = text("language")

        audio.album = text("album")
        audio.album_sort = text("albumsort")
        audio.album_artist = text("album artist") or text("albumartist")
        audio.album_artist_sort = text("albumartistsort")
        audio.track_number, audio.track_total = parse_number_pair(text("track"))
        audio.disc_number, audio.disc_total = parse_number_pair(text("disc"))
        audio.compilation = parse_bool(text("compilation"))
        audio.label = text("label")
        audio.isrc = text("isrc")
        audio.bpm = parse_int(text("bpm"))
        audio.key = text("initial key") or text("key")

        for key, value in items.items():
            if key.startswith("musicbrainz"):
                normalized = key.replace(" ", "").replace("_", "")
                for marker, name in _MB_VORBIS.items():
                    if normalized == marker.replace("_", ""):
                        raw = getattr(value, "value", value)
                        if not isinstance(raw, bytes):
                            metadata.musicbrainz_ids[name] = str(raw)
                        break

        cover = items.get("cover art (front)")
        if cover is not None:
            raw = getattr(cover, "value", cover)
            if isinstance(raw, bytes) and b"\x00" in raw:
                filename, data = raw.split(b"\x00", 1)
                if data:
                    mime, _ = mimetypes.guess_type(filename.decode("utf-8", "replace"))
                    artwork.append(
                        Artwork(
                            data=data,
                            mime=mime or "image/jpeg",
                            kind="front",
                        )
                    )

        known = {
            "title", "artist", "titlesort", "artistsort", "year", "date", "genre",
            "comment", "composer", "lyricist", "conductor", "lyrics", "copyright", "language",
            "album", "albumsort", "album artist", "albumartist", "albumartistsort",
            "track", "disc", "compilation", "label", "isrc", "bpm", "initial key",
            "key", "cover art (front)",
        }
        for key, value in items.items():
            if key in known or key.startswith("musicbrainz") or key.startswith("replaygain"):
                continue
            raw = getattr(value, "value", value)
            if not isinstance(raw, bytes):
                metadata.extra[key] = split_values(str(raw))

    # ------------------------------------------------------------------
    # ASF (WMA/WMV)
    # ------------------------------------------------------------------

    @staticmethod
    def _read_asf(tags: ASFTags, metadata: MediaMetadata, artwork: list[Artwork]) -> None:
        items = {str(key).lower(): value for key, value in tags.items()}

        def values(key: str) -> list[str]:
            raw_values: Iterable[Any] = items.get(key, [])
            result: list[str] = []
            for item in raw_values:
                value = getattr(item, "value", item)
                if isinstance(value, bytes):
                    continue
                result.extend(split_values(str(value)))
            return result

        def text(key: str) -> str | None:
            result = values(key)
            return result[0] if result else None

        audio = metadata.audio
        if audio is None:
            audio = AudioMetadata()
            metadata.audio = audio

        metadata.title = text("title")
        metadata.artist = text("author")
        metadata.artists = [metadata.artist] if metadata.artist else []
        metadata.date = parse_partial_date(text("wm/year"))
        metadata.genre = parse_genres(values("wm/genre"))
        metadata.comment = text("description")
        metadata.composer = values("wm/composer")
        metadata.lyricist = values("wm/writer")
        metadata.conductor = values("wm/conductor")
        lyrics_text = text("wm/lyrics")
        if lyrics_text is not None:
            metadata.lyrics = Lyrics(text=lyrics_text, language=text("wm/language"))
        metadata.copyright = text("copyright")
        metadata.language = text("wm/language")

        audio.album = text("wm/albumtitle")
        audio.album_artist = text("wm/albumartist")
        audio.track_number, audio.track_total = parse_number_pair(text("wm/tracknumber"))
        audio.disc_number, audio.disc_total = parse_number_pair(text("wm/partofset"))
        audio.compilation = parse_bool(text("wm/iscompilation"))
        audio.label = text("wm/publisher")
        audio.isrc = text("wm/isrc")
        audio.bpm = parse_int(text("wm/beatsperminute"))

        for key, value in items.items():
            if not key.startswith("musicbrainz/"):
                continue
            raw = getattr(value[0], "value", value[0]) if value else None
            if isinstance(raw, str):
                metadata.musicbrainz_ids[key.split("/", 1)[1]] = raw

        video = metadata.video
        if video is not None:
            video.director = values("wm/director")
            video.writer = values("wm/writer")

        asf_picture = items.get("wm/picture")
        if asf_picture:
            for item in asf_picture:
                raw = getattr(item, "value", item)
                if isinstance(raw, bytes):
                    picture = _parse_asf_picture(raw)
                    if picture is not None:
                        artwork.append(picture)


# ----------------------------------------------------------------------
# Small mapping helpers
# ----------------------------------------------------------------------


def _merge_missing(target: MediaMetadata, source: MediaMetadata) -> None:
    """Fill only empty fields of ``target`` from a lower-priority tag system."""
    common_scalars = (
        "title", "title_sort", "artist", "artist_sort", "date", "comment",
        "copyright", "language", "grouping",
    )
    for name in common_scalars:
        if getattr(target, name) is None and getattr(source, name) is not None:
            setattr(target, name, getattr(source, name))

    common_lists = ("artists", "genre", "composer", "lyricist", "conductor", "producer")
    for name in common_lists:
        if not getattr(target, name) and getattr(source, name):
            setattr(target, name, list(getattr(source, name)))

    if target.lyrics is None:
        target.lyrics = source.lyrics
    for name in ("musicbrainz_ids", "extra"):
        target_value = getattr(target, name)
        for key, value in getattr(source, name).items():
            target_value.setdefault(key, value)

    if target.audio is None:
        target.audio = source.audio
    elif source.audio is not None:
        audio_scalars = (
            "album", "album_sort", "album_artist", "album_artist_sort",
            "track_number", "track_total", "disc_number", "disc_total",
            "compilation", "label", "isrc", "bpm", "key",
        )
        for name in audio_scalars:
            if getattr(target.audio, name) is None and getattr(source.audio, name) is not None:
                setattr(target.audio, name, getattr(source.audio, name))
        for key, value in source.audio.replaygain.items():
            target.audio.replaygain.setdefault(key, value)


def _read_id3v1(path: Path) -> MediaMetadata | None:
    try:
        with path.open("rb") as handle:
            handle.seek(-128, 2)
            data = handle.read(128)
    except (OSError, ValueError):
        return None
    if len(data) != 128 or data[:3] != b"TAG":
        return None

    def field(start: int, end: int) -> str | None:
        return clean_text(data[start:end].decode("latin-1"))

    comment_bytes = data[97:127]
    track: int | None = None
    if comment_bytes[28] == 0:
        track = comment_bytes[29] or None
        comment_bytes = comment_bytes[:28]

    metadata = MediaMetadata()
    metadata.title = field(3, 33)
    metadata.artist = field(33, 63)
    if metadata.artist:
        metadata.artists = [metadata.artist]
    metadata.date = parse_partial_date(field(93, 97))
    metadata.comment = clean_text(comment_bytes.decode("latin-1"))

    genre_index = data[127]
    if 0 <= genre_index < len(ID3_GENRES):
        metadata.genre = [ID3_GENRES[genre_index]]

    metadata.audio = AudioMetadata(
        album=field(63, 93),
        track_number=track,
    )
    return metadata


def _id3_text(tags: ID3, frame_id: str) -> str | None:
    frame = tags.get(frame_id)
    if frame is None:
        return None
    return first_text(getattr(frame, "text", None))


def _id3_texts(tags: ID3, frame_id: str) -> list[str]:
    frame = tags.get(frame_id)
    if frame is None:
        return []
    return split_values(getattr(frame, "text", None))


def _id3_fallback_date(tags: ID3) -> str | None:
    year = _id3_text(tags, "TYER")
    if year is None:
        return None
    day_month = _id3_text(tags, "TDAT")
    if day_month and len(day_month) == 4:
        return f"{year}-{day_month[2:4]}-{day_month[0:2]}"
    return year


def _id3_comment(tags: ID3) -> str | None:
    frames = tags.getall("COMM")
    preferred = [frame for frame in frames if frame.lang == "eng" and not clean_text(frame.desc)]
    for frame in preferred or frames:
        value = first_text(getattr(frame, "text", None))
        if value is not None:
            return value
    return None


def _id3_lyrics(tags: ID3) -> Lyrics | None:
    frames = tags.getall("USLT")
    if not frames:
        return None
    preferred = [frame for frame in frames if frame.lang == "eng" and not clean_text(frame.desc)]
    frame = (preferred or frames)[0]
    value = first_text(getattr(frame, "text", None))
    if value is None:
        return None
    return Lyrics(text=value, language=clean_text(frame.lang), synced=False)


def _apply_txxx(
    metadata: MediaMetadata,
    audio: AudioMetadata,
    key: str,
    values: list[str],
) -> None:
    if key == "isrc":
        audio.isrc = values[0]
    elif key == "label":
        audio.label = values[0]
    elif key == "language":
        metadata.language = values[0]
    elif key == "conductor":
        metadata.conductor.extend(values)
    elif key == "lyricist":
        metadata.lyricist.extend(values)
    elif key == "producer":
        metadata.producer.extend(values)


def _lyrics_from_text(value: str | None) -> Lyrics | None:
    if value is None:
        return None
    return Lyrics(text=value, synced=False)


def _mp4_values(tags: MP4Tags, key: str) -> list[Any]:
    value = tags.get(key)
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _mp4_first(tags: MP4Tags, key: str) -> Any:
    values = _mp4_values(tags, key)
    return values[0] if values else None


def _mp4_text(tags: MP4Tags, key: str) -> str | None:
    values = _mp4_values(tags, key)
    return first_text([str(item) for item in values if item is not None])


def _mp4_texts(tags: MP4Tags, key: str) -> list[str]:
    return split_values([str(item) for item in _mp4_values(tags, key) if item is not None])


def _mp4_pair(tags: MP4Tags, key: str) -> tuple[int | None, int | None]:
    value = _mp4_first(tags, key)
    if isinstance(value, tuple) and len(value) == 2:
        return parse_number_pair(value)
    return parse_number_pair(value)


def _mp4_freeform_values(tags: MP4Tags, key: str) -> list[str]:
    result: list[str] = []
    for value in _mp4_values(tags, key):
        if isinstance(value, MP4FreeForm):
            text = bytes(value).decode("utf-8", "replace")
        elif isinstance(value, bytes):
            text = value.decode("utf-8", "replace")
        else:
            text = str(value)
        cleaned = clean_text(text)
        if cleaned is not None:
            result.append(cleaned)
    return result


def _mp4_freeform_text(tags: MP4Tags, name: str) -> str | None:
    values = _mp4_freeform_texts(tags, name)
    return values[0] if values else None


def _mp4_freeform_texts(tags: MP4Tags, name: str) -> list[str]:
    key = f"----:com.apple.iTunes:{name}"
    return _mp4_freeform_values(tags, key)


def _mp4_cover_mime(cover: MP4Cover) -> str:
    if cover.imageformat == MP4Cover.FORMAT_PNG:
        return "image/png"
    if cover.imageformat == MP4Cover.FORMAT_JPEG:
        return "image/jpeg"
    return "application/octet-stream"


def _apply_freeform(
    metadata: MediaMetadata,
    audio: AudioMetadata | None,
    key: str,
    values: list[str],
) -> None:
    if key == "isrc" and audio is not None:
        audio.isrc = values[0]
    elif key == "label" and audio is not None:
        audio.label = values[0]
    elif key == "language":
        metadata.language = values[0]


def _parse_asf_picture(data: bytes) -> Artwork | None:
    """Parse the ASF ``WM/Picture`` byte-array structure."""
    if len(data) < 9:
        return None
    picture_type = data[0]
    data_length = int.from_bytes(data[1:5], "little")
    mime_length = int.from_bytes(data[5:9], "little")
    offset = 9
    if offset + mime_length + 2 > len(data):
        return None
    mime = data[offset : offset + mime_length].decode("utf-16-le", "replace").rstrip("\x00")
    offset += mime_length + 2
    description_end = offset
    while description_end + 1 < len(data) and data[description_end : description_end + 2] != b"\x00\x00":
        description_end += 2
    description = data[offset:description_end].decode("utf-16-le", "replace")
    offset = description_end + 2
    image = data[offset : offset + data_length]
    if not image:
        return None
    return Artwork(
        data=image,
        mime=mime or "application/octet-stream",
        kind=picture_kind(picture_type),
        description=clean_text(description),
    )

"""Integration tests for MediaFile and the mutagen backend.

The fixtures create real files with ffmpeg and tag them with mutagen.  If
ffmpeg is unavailable, the fixture is skipped; model/normalization tests still
run.
"""

from __future__ import annotations

import shutil
import wave
from pathlib import Path

import pytest

from trackforge.metadata import (
    Artwork,
    MediaFile,
    MediaKind,
    MetadataReadError,
    MetadataWriteError,
    NoAudioStreamError,
    NoVideoStreamError,
    PartialDate,
    UnsupportedFormatError,
    VideoMetadata,
    WritePolicy,
)


def test_mp3(media_dir: Path) -> None:
    media = MediaFile.open(media_dir / "sample.mp3")

    assert media.kind is MediaKind.AUDIO
    assert media.title == "MP3 Title"
    assert media.artist == "Artist A"
    assert media.artists == ["Artist A", "Artist B"]
    assert media.date == PartialDate(2024, 5, 17)
    assert media.genre == ["Rock"]
    assert media.comment == "MP3 Comment"
    assert media.composer == ["Composer"]
    assert media.lyricist == ["Lyricist"]
    assert media.conductor == ["Conductor"]
    assert media.producer == ["Producer"]
    assert media.lyrics is not None
    assert media.lyrics.text == "MP3 Lyrics"
    assert media.copyright == "(c) 2024"
    assert media.language == "eng"
    assert media.grouping == "Group"
    assert media.musicbrainz_ids["track"] == "mbid-track"

    audio = media.require_audio()
    assert audio.album == "MP3 Album"
    assert audio.album_artist == "Album Artist"
    assert (audio.track_number, audio.track_total) == (3, 12)
    assert (audio.disc_number, audio.disc_total) == (1, 2)
    assert audio.compilation is True
    assert audio.label == "Label"
    assert audio.isrc == "ISRC123"
    assert audio.bpm == 120
    assert audio.key == "Am"
    assert audio.replaygain["track_gain"] == -3.5

    assert media.artwork.front is not None
    assert media.artwork.front.mime == "image/jpeg"
    assert media.artwork.front.data == b"jpegdata"


def test_flac(media_dir: Path) -> None:
    media = MediaFile.open(media_dir / "sample.flac")

    assert media.title == "FLAC Title"
    assert media.artists == ["Artist A", "Artist B"]
    assert media.date == PartialDate(2023, 4)
    assert media.genre == ["Rock", "Pop"]
    assert media.comment == "FLAC Comment"
    assert media.conductor == ["Conductor"]
    assert media.lyrics is not None
    assert media.lyrics.text == "FLAC Lyrics"
    assert media.musicbrainz_ids["track"] == "mbid-flac"

    audio = media.require_audio()
    assert audio.album == "FLAC Album"
    assert audio.album_artist == "Album Artist"
    assert (audio.track_number, audio.track_total) == (4, 10)
    assert (audio.disc_number, audio.disc_total) == (1, 2)
    assert audio.label == "Label"
    assert audio.isrc == "ISRC456"
    assert audio.bpm == 100
    assert audio.key == "C"
    assert audio.replaygain["track_gain"] == -2.5

    assert media.artwork.front is not None
    assert media.artwork.front.mime == "image/png"
    assert (media.artwork.front.width, media.artwork.front.height) == (32, 32)


def test_ogg_vorbis(media_dir: Path) -> None:
    media = MediaFile.open(media_dir / "sample.ogg")

    assert media.kind is MediaKind.AUDIO
    assert media.title == "OGG Title"
    assert media.artist == "OGG Artist"
    assert media.date == PartialDate(2022)
    assert media.genre == ["Jazz"]
    assert media.lyrics is not None
    assert media.lyrics.text == "OGG Lyrics"
    assert media.require_audio().album == "OGG Album"
    assert media.require_audio().track_number == 5


def test_ogg_opus(media_dir: Path) -> None:
    media = MediaFile.open(media_dir / "sample.opus")

    assert media.title == "Opus Title"
    assert media.artist == "Opus Artist"
    assert media.require_audio().album == "Opus Album"


def test_m4a(media_dir: Path) -> None:
    media = MediaFile.open(media_dir / "sample.m4a")

    assert media.kind is MediaKind.AUDIO
    assert media.title == "M4A Title"
    assert media.artists == ["Artist A", "Artist B"]
    assert media.date == PartialDate(2021, 9, 30)
    assert media.genre == ["Electronic"]
    assert media.comment == "M4A Comment"
    assert media.composer == ["Composer"]
    assert media.lyrics is not None
    assert media.lyrics.text == "M4A Lyrics"
    assert media.musicbrainz_ids["track"] == "mbid-m4a"

    audio = media.require_audio()
    assert audio.album == "M4A Album"
    assert audio.album_artist == "Album Artist"
    assert audio.album_artist_sort == "Album Artist, The"
    assert (audio.track_number, audio.track_total) == (6, 14)
    assert (audio.disc_number, audio.disc_total) == (1, 2)
    assert audio.compilation is True
    assert audio.bpm == 128
    assert audio.isrc == "ISRC789"
    assert audio.label == "Label"
    assert media.artwork.front is not None
    assert media.artwork.front.mime == "image/jpeg"


def test_mp4_video(media_dir: Path) -> None:
    media = MediaFile.open(media_dir / "sample.mp4")

    assert media.kind is MediaKind.AUDIO_VIDEO
    assert media.title == "Video Title"
    assert media.date == PartialDate(2020)
    assert media.genre == ["Drama"]

    video = media.require_video()
    assert video.show == "Show Name"
    assert (video.season, video.episode) == (2, 3)
    assert video.episode_id == "S02E03"
    assert video.network == "Network"
    assert video.director == ["Director"]
    assert video.writer == ["Writer"]
    assert video.synopsis == "Long synopsis"
    assert video.keywords == ["one", "two"]
    assert media.audio is not None


def test_capabilities(media_dir: Path) -> None:
    media = MediaFile.open(media_dir / "sample.mp3")
    capabilities = media.capabilities

    assert capabilities is not None
    assert capabilities.writable is True
    assert capabilities.supports("title")
    assert capabilities.supports("album")
    assert not capabilities.supports("season")


def test_untagged_wav(tmp_path: Path) -> None:
    path = tmp_path / "silence.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 8)

    media = MediaFile.open(path)

    assert media.kind is MediaKind.AUDIO
    assert media.title is None
    assert media.audio is not None
    assert "no tags found" in media.warnings


def test_require_streams() -> None:
    audio_only = MediaFile(path=Path("audio.mp3"))
    with pytest.raises(NoVideoStreamError):
        audio_only.require_video()

    video_only = MediaFile(path=Path("video.mp4"), video=VideoMetadata())
    with pytest.raises(NoAudioStreamError):
        video_only.require_audio()


def test_unsupported_container(tmp_path: Path) -> None:
    path = tmp_path / "video.mkv"
    path.write_bytes(b"\x1aE\xdf\xa3" + b"matroska")

    with pytest.raises(UnsupportedFormatError):
        MediaFile.open(path)


def test_write_mp3(tmp_path: Path, media_dir: Path) -> None:
    target = tmp_path / "copy.mp3"
    shutil.copy2(media_dir / "sample.mp3", target)
    media = MediaFile.open(target)
    media.title = "Written Title"
    media.comment = "Written Comment"
    media.require_audio().album = "Written Album"
    media.require_audio().track_number = 9
    media.require_audio().track_total = 9

    report = media.save()

    assert report.backend == "mutagen"
    assert not report.lossy
    assert "title" in report.written
    assert "audio.album" in report.written
    assert report.bytes_before > 0

    updated = MediaFile.open(target)
    assert updated.title == "Written Title"
    assert updated.comment == "Written Comment"
    assert updated.require_audio().album == "Written Album"
    assert (updated.audio.track_number, updated.audio.track_total) == (9, 9)
    assert updated.artwork.front is not None


def test_write_flac(tmp_path: Path, media_dir: Path) -> None:
    target = tmp_path / "copy.flac"
    shutil.copy2(media_dir / "sample.flac", target)
    media = MediaFile.open(target)
    media.title = "Written FLAC"
    media.require_audio().album = "Written FLAC Album"
    media.require_audio().track_number = 8
    media.date = PartialDate(2030, 1, 2)

    report = media.save()

    assert "title" in report.written
    assert "audio.album" in report.written
    updated = MediaFile.open(target)
    assert updated.title == "Written FLAC"
    assert updated.require_audio().album == "Written FLAC Album"
    assert updated.require_audio().track_number == 8
    assert updated.date == PartialDate(2030, 1, 2)
    assert updated.artwork.front is not None


def test_write_ogg(tmp_path: Path, media_dir: Path) -> None:
    target = tmp_path / "copy.ogg"
    shutil.copy2(media_dir / "sample.ogg", target)
    media = MediaFile.open(target)
    media.title = "Written OGG"
    media.artists = ["Written Artist"]
    media.lyrics = None

    report = media.save()

    assert "title" in report.written
    updated = MediaFile.open(target)
    assert updated.title == "Written OGG"
    assert updated.artists == ["Written Artist"]


def test_write_ogg_artwork(tmp_path: Path, media_dir: Path) -> None:
    target = tmp_path / "copy-art.ogg"
    shutil.copy2(media_dir / "sample.ogg", target)
    media = MediaFile.open(target)
    media.artwork.add(Artwork(data=b"pngdata", mime="image/png", width=1, height=1))

    report = media.save()

    assert "artwork" in report.written
    updated = MediaFile.open(target)
    assert updated.artwork.front is not None
    assert updated.artwork.front.mime == "image/png"
    assert updated.artwork.front.data == b"pngdata"


def test_write_m4a(tmp_path: Path, media_dir: Path) -> None:
    target = tmp_path / "copy.m4a"
    shutil.copy2(media_dir / "sample.m4a", target)
    media = MediaFile.open(target)
    media.title = "Written M4A"
    media.require_audio().album = "Written M4A Album"
    media.require_audio().bpm = 99
    media.require_audio().compilation = False

    report = media.save()

    assert "title" in report.written
    assert "audio.bpm" in report.written
    updated = MediaFile.open(target)
    assert updated.title == "Written M4A"
    assert updated.require_audio().album == "Written M4A Album"
    assert updated.require_audio().bpm == 99
    assert updated.require_audio().compilation is False


def test_write_mp4_video(tmp_path: Path, media_dir: Path) -> None:
    target = tmp_path / "copy.mp4"
    shutil.copy2(media_dir / "sample.mp4", target)
    media = MediaFile.open(target)
    media.require_video().show = "Written Show"
    media.require_video().season = 4
    media.require_video().episode = 5
    media.require_video().director = ["New Director"]

    report = media.save()

    assert "video.show" in report.written
    updated = MediaFile.open(target)
    assert updated.require_video().show == "Written Show"
    assert (updated.video.season, updated.video.episode) == (4, 5)
    assert updated.video.director == ["New Director"]


def test_write_in_place(tmp_path: Path, media_dir: Path) -> None:
    target = tmp_path / "copy-in-place.mp3"
    shutil.copy2(media_dir / "sample.mp3", target)
    media = MediaFile.open(target)
    media.title = "In-place Title"

    report = media.save(policy=WritePolicy(atomic=False))

    assert "title" in report.written
    assert MediaFile.open(target).title == "In-place Title"


def test_remove_missing_field(tmp_path: Path, media_dir: Path) -> None:
    target = tmp_path / "copy-remove.mp3"
    shutil.copy2(media_dir / "sample.mp3", target)
    media = MediaFile.open(target)
    media.comment = None

    report = media.save(policy=WritePolicy(remove_missing=True))

    assert "comment" in report.written
    updated = MediaFile.open(target)
    assert updated.comment is None


def test_remove_missing_vorbis_field(tmp_path: Path, media_dir: Path) -> None:
    target = tmp_path / "copy-remove.flac"
    shutil.copy2(media_dir / "sample.flac", target)
    media = MediaFile.open(target)
    media.comment = None

    report = media.save(policy=WritePolicy(remove_missing=True))

    assert "comment" in report.written
    assert MediaFile.open(target).comment is None


def test_save_without_source() -> None:
    media = MediaFile()
    with pytest.raises(MetadataWriteError):
        media.save()


def test_read_only_wav_cannot_save(tmp_path: Path) -> None:
    path = tmp_path / "silence.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 8)

    media = MediaFile.open(path)
    with pytest.raises(UnsupportedFormatError):
        media.save()


def test_corrupt_flac(tmp_path: Path) -> None:
    path = tmp_path / "broken.flac"
    path.write_bytes(b"fLaC" + b"\x00" * 4)

    with pytest.raises(MetadataReadError):
        MediaFile.open(path)

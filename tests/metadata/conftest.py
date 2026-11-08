"""Shared media fixtures.

The library itself does not depend on ffmpeg.  These fixtures skip when the
binary is unavailable; the unit tests for model/normalization still run.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,
    COMM,
    TALB,
    TBPM,
    TCMP,
    TCOM,
    TCON,
    TCOP,
    TEXT,
    TIT2,
    TKEY,
    TLAN,
    TPE1,
    TPE2,
    TPE3,
    TIPL,
    TPOS,
    TPUB,
    TRCK,
    TDRC,
    TSRC,
    TSO2,
    TSOT,
    TXXX,
    USLT,
    GRP1,
    ID3,
)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis


@pytest.fixture(scope="session")
def ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    if executable is None:
        pytest.skip("ffmpeg is not installed")
    return executable


@pytest.fixture(scope="session")
def media_dir(ffmpeg: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("media")

    def make(name: str, *args: str) -> None:
        command = [ffmpeg, "-v", "error", "-y", *args, str(directory / name)]
        try:
            subprocess.run(command, check=True, capture_output=True)
        except subprocess.CalledProcessError as exc:
            pytest.skip(f"ffmpeg cannot create {name}: {exc.stderr.decode('utf-8', 'replace')}")

    audio = "sine=frequency=440:duration=0.2"
    video = "color=c=black:s=32x32:d=0.2"

    make("sample.mp3", "-f", "lavfi", "-i", audio, "-c:a", "libmp3lame")
    make("sample.flac", "-f", "lavfi", "-i", audio, "-c:a", "flac")
    make("sample.ogg", "-f", "lavfi", "-i", audio, "-c:a", "libvorbis")
    make("sample.opus", "-f", "lavfi", "-i", audio, "-c:a", "libopus")
    make("sample.m4a", "-f", "lavfi", "-i", audio, "-c:a", "aac")
    make("sample.wma", "-f", "lavfi", "-i", audio, "-c:a", "wmav2")
    make("sample.wv", "-f", "lavfi", "-i", audio, "-c:a", "wavpack")
    make("sample.wav", "-f", "lavfi", "-i", audio, "-c:a", "pcm_s16le")
    make("sample.aiff", "-f", "lavfi", "-i", audio, "-c:a", "pcm_s16be")
    make(
        "sample.mp4",
        "-f", "lavfi", "-i", video,
        "-f", "lavfi", "-i", audio,
        "-c:v", "mpeg4", "-c:a", "aac", "-shortest",
    )
    make(
        "sample-video-only.mp4",
        "-f", "lavfi", "-i", video,
        "-c:v", "mpeg4", "-an",
    )
    make(
        "sample.mkv",
        "-f", "lavfi", "-i", video,
        "-f", "lavfi", "-i", audio,
        "-c:v", "mpeg4", "-c:a", "aac", "-shortest",
    )
    make(
        "sample.avi",
        "-f", "lavfi", "-i", video,
        "-f", "lavfi", "-i", audio,
        "-c:v", "mpeg4", "-c:a", "libmp3lame", "-shortest",
    )
    make(
        "sample.flv",
        "-f", "lavfi", "-i", video,
        "-f", "lavfi", "-i", audio,
        "-c:v", "flv", "-c:a", "libmp3lame", "-shortest",
    )
    make(
        "sample.ts",
        "-f", "lavfi", "-i", video,
        "-f", "lavfi", "-i", audio,
        "-c:v", "mpeg2video", "-c:a", "mp2", "-shortest", "-f", "mpegts",
    )
    make(
        "sample.mpg",
        "-f", "lavfi", "-i", video,
        "-f", "lavfi", "-i", audio,
        "-c:v", "mpeg2video", "-c:a", "mp2", "-shortest", "-f", "vob",
    )

    _tag_mp3(directory / "sample.mp3")
    _tag_flac(directory / "sample.flac")
    _tag_ogg(directory / "sample.ogg")
    _tag_opus(directory / "sample.opus")
    _tag_m4a(directory / "sample.m4a")
    _tag_mp4_video(directory / "sample.mp4")
    return directory


def _tag_mp3(path: Path) -> None:
    audio = MP3(path, ID3=ID3)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    assert tags is not None
    tags.add(TIT2(encoding=3, text=["MP3 Title"]))
    tags.add(TPE1(encoding=3, text=["Artist A", "Artist B"]))
    tags.add(TALB(encoding=3, text=["MP3 Album"]))
    tags.add(TPE2(encoding=3, text=["Album Artist"]))
    tags.add(TRCK(encoding=3, text=["3/12"]))
    tags.add(TPOS(encoding=3, text=["1/2"]))
    tags.add(TDRC(encoding=3, text=["2024-05-17"]))
    tags.add(TCON(encoding=3, text=["(17)"]))
    tags.add(COMM(encoding=3, lang="eng", desc="", text=["MP3 Comment"]))
    tags.add(TCOM(encoding=3, text=["Composer"]))
    tags.add(TEXT(encoding=3, text=["Lyricist"]))
    tags.add(TPE3(encoding=3, text=["Conductor"]))
    tags.add(USLT(encoding=3, lang="eng", desc="", text="MP3 Lyrics"))
    tags.add(TSOT(encoding=3, text=["Title, MP3"]))
    tags.add(TSO2(encoding=3, text=["Album Artist, The"]))
    tags.add(TCMP(encoding=3, text=["1"]))
    tags.add(TBPM(encoding=3, text=["120"]))
    tags.add(TSRC(encoding=3, text=["ISRC123"]))
    tags.add(TKEY(encoding=3, text=["Am"]))
    tags.add(TLAN(encoding=3, text=["eng"]))
    tags.add(GRP1(encoding=3, text=["Group"]))
    tags.add(TCOP(encoding=3, text=["(c) 2024"]))
    tags.add(TPUB(encoding=3, text=["Label"]))
    tags.add(TIPL(encoding=3, people=[("producer", "Producer")]))
    tags.add(TXXX(encoding=3, desc="MusicBrainz Track Id", text=["mbid-track"]))
    tags.add(TXXX(encoding=3, desc="ReplayGain Track Gain", text=["-3.5 dB"]))
    tags.add(
        APIC(
            encoding=3,
            mime="image/jpeg",
            type=3,
            desc="front",
            data=b"jpegdata",
        )
    )
    audio.save()


def _tag_flac(path: Path) -> None:
    audio = FLAC(path)
    audio["TITLE"] = "FLAC Title"
    audio["ARTIST"] = ["Artist A", "Artist B"]
    audio["ALBUM"] = "FLAC Album"
    audio["ALBUMARTIST"] = "Album Artist"
    audio["TRACKNUMBER"] = "4"
    audio["TRACKTOTAL"] = "10"
    audio["DISCNUMBER"] = "1"
    audio["DISCTOTAL"] = "2"
    audio["DATE"] = "2023-04"
    audio["GENRE"] = ["Rock", "Pop"]
    audio["COMMENT"] = "FLAC Comment"
    audio["COMPOSER"] = "Composer"
    audio["LYRICIST"] = "Lyricist"
    audio["CONDUCTOR"] = "Conductor"
    audio["LYRICS"] = "FLAC Lyrics"
    audio["LABEL"] = "Label"
    audio["ISRC"] = "ISRC456"
    audio["BPM"] = "100"
    audio["INITIALKEY"] = "C"
    audio["MUSICBRAINZ_TRACKID"] = "mbid-flac"
    audio["REPLAYGAIN_TRACK_GAIN"] = "-2.5 dB"
    picture = Picture()
    picture.type = 3
    picture.mime = "image/png"
    picture.desc = "cover"
    picture.width = 32
    picture.height = 32
    picture.data = b"pngdata"
    audio.add_picture(picture)
    audio.save()


def _tag_ogg(path: Path) -> None:
    audio = OggVorbis(path)
    audio["TITLE"] = "OGG Title"
    audio["ARTIST"] = ["OGG Artist"]
    audio["ALBUM"] = "OGG Album"
    audio["TRACKNUMBER"] = "5"
    audio["DATE"] = "2022"
    audio["GENRE"] = "Jazz"
    audio["LYRICS"] = "OGG Lyrics"
    audio.save()


def _tag_opus(path: Path) -> None:
    audio = OggOpus(path)
    audio["TITLE"] = "Opus Title"
    audio["ARTIST"] = "Opus Artist"
    audio["ALBUM"] = "Opus Album"
    audio.save()


def _tag_m4a(path: Path) -> None:
    audio = MP4(path)
    audio["©nam"] = ["M4A Title"]
    audio["©ART"] = ["Artist A", "Artist B"]
    audio["©alb"] = ["M4A Album"]
    audio["aART"] = ["Album Artist"]
    audio["trkn"] = [(6, 14)]
    audio["disk"] = [(1, 2)]
    audio["©day"] = ["2021-09-30"]
    audio["©gen"] = ["Electronic"]
    audio["©cmt"] = ["M4A Comment"]
    audio["©wrt"] = ["Composer"]
    audio["©lyr"] = ["M4A Lyrics"]
    audio["tmpo"] = [128]
    audio["cpil"] = True
    audio["soaa"] = ["Album Artist, The"]
    audio["covr"] = [MP4Cover(b"jpegdata", imageformat=MP4Cover.FORMAT_JPEG)]
    audio["----:com.apple.iTunes:MusicBrainz Track Id"] = [MP4FreeForm(b"mbid-m4a")]
    audio["----:com.apple.iTunes:ISRC"] = [MP4FreeForm(b"ISRC789")]
    audio["----:com.apple.iTunes:LABEL"] = [MP4FreeForm(b"Label")]
    audio.save()


def _tag_mp4_video(path: Path) -> None:
    video = MP4(path)
    video["©nam"] = ["Video Title"]
    video["©ART"] = ["Studio"]
    video["©day"] = ["2020"]
    video["©gen"] = ["Drama"]
    video["©wrt"] = ["Writer"]
    video["tvsh"] = ["Show Name"]
    video["tvsn"] = [2]
    video["tven"] = ["S02E03"]
    video["tves"] = [3]
    video["tvnn"] = ["Network"]
    video["ldes"] = ["Long synopsis"]
    video["desc"] = ["Short description"]
    video["keyw"] = ["one", "two"]
    video["----:com.apple.iTunes:DIRECTOR"] = [MP4FreeForm(b"Director")]
    video.save()

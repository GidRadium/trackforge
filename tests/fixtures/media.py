"""Generate small real audio files for integration tests with ffmpeg.

The helper never downloads anything: input is a lavfi sine/silence source and
the requested tags are written by the local ffmpeg binary.  Tests skip with a
clear reason when ffmpeg/ffprobe are unavailable.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

CONTAINER_CODECS: dict[str, tuple[str, ...]] = {
    "mp3": ("-c:a", "libmp3lame", "-b:a", "64k"),
    "m4a": ("-c:a", "aac", "-b:a", "64k"),
    "flac": ("-c:a", "flac"),
    "ogg": ("-c:a", "libvorbis", "-q:a", "2"),
    "opus": ("-c:a", "libopus", "-b:a", "32k"),
}

DEFAULT_TAGS: dict[str, str] = {
    "title": "Fixture Song",
    "artist": "Fixture Artist",
    "album": "Fixture Album",
    "album_artist": "Fixture Album Artist",
    "date": "2024",
    "genre": "Test Genre",
    "track": "3/12",
    "disc": "1/2",
    "comment": "fixture comment",
    "lyrics": "la la la",
    "musicbrainz_trackid": "11111111-1111-1111-1111-111111111111",
}

MBID_RECORDING = DEFAULT_TAGS["musicbrainz_trackid"]

LRC_TEXT = """[00:01.00]First line
[00:03.50]Second line
[00:06.25]Third line
"""


@dataclass(frozen=True, slots=True)
class MediaFixture:
    """One generated media file and the tags requested for it."""

    path: Path
    container: str
    duration_seconds: float
    channels: int
    tags: Mapping[str, str]


def media_tools_available() -> bool:
    """Return whether both ffmpeg and ffprobe are installed."""

    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def skip_without_media_tools() -> None:
    """Skip the current test when ffmpeg/ffprobe are unavailable."""

    if not media_tools_available():
        pytest.skip("ffmpeg and ffprobe are required for media integration tests")


def generate_media(
    directory: Path,
    container: str,
    *,
    name: str | None = None,
    duration: float = 0.75,
    frequency: float = 440.0,
    channels: int = 1,
    with_tags: bool = True,
    tags: Mapping[str, str] | None = None,
) -> MediaFixture:
    """Generate one tiny sine/silence file and return its metadata."""

    if container not in CONTAINER_CODECS:
        raise ValueError(f"unsupported fixture container: {container}")
    if duration <= 0:
        raise ValueError("duration must be positive")
    if channels < 1:
        raise ValueError("channels must be positive")
    skip_without_media_tools()

    directory.mkdir(parents=True, exist_ok=True)
    filename = name or f"fixture-{container}-{channels}ch.{container}"
    path = directory / filename
    requested_tags = dict(tags) if tags is not None else (DEFAULT_TAGS if with_tags else {})

    command = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={frequency:g}:duration={duration:g}",
        "-ac",
        str(channels),
        "-map_metadata",
        "-1",
    ]
    for key, value in requested_tags.items():
        command.extend(["-metadata", f"{key}={value}"])
    command.extend(CONTAINER_CODECS[container])
    command.append(str(path))
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ffmpeg failed for {container}: {exc.stderr}") from exc
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg did not produce a non-empty {container} file")
    return MediaFixture(
        path=path,
        container=container,
        duration_seconds=duration,
        channels=channels,
        tags=requested_tags,
    )


def generate_png(
    directory: Path,
    *,
    name: str = "cover.png",
    width: int = 100,
    height: int = 100,
) -> Path:
    """Create a small valid RGB PNG without external image libraries."""

    if width < 1 or height < 1:
        raise ValueError("PNG dimensions must be positive")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    raw = b"".join(b"\x00" + b"\xff\x40\x20" * width for _ in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)
    return path


def generate_all_formats(
    directory: Path,
    *,
    with_tags: bool = True,
    duration: float = 0.75,
) -> dict[str, MediaFixture]:
    """Generate one fixture for every MVP container."""

    return {
        container: generate_media(
            directory,
            container,
            with_tags=with_tags,
            duration=duration,
        )
        for container in CONTAINER_CODECS
    }


__all__ = [
    "CONTAINER_CODECS",
    "DEFAULT_TAGS",
    "LRC_TEXT",
    "MBID_RECORDING",
    "MediaFixture",
    "generate_all_formats",
    "generate_media",
    "generate_png",
    "media_tools_available",
    "skip_without_media_tools",
]

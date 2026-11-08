"""Value parsing and field-clearing helpers for the CLI."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trackforge.metadata import MediaFile

CLEARABLE = {
    "title", "artist", "album", "album_artist", "date", "original_date",
    "release_date", "genre", "comment", "composer", "lyricist", "lyrics",
    "track", "disc", "isrc", "bpm", "show", "season", "episode", "director",
    "synopsis", "mood", "work", "subtitle", "media", "release_country",
    "release_status", "release_type", "movement_name", "movement",
    "movement_total",
}


def clear_field(media: MediaFile, field: str) -> None:
    if field not in CLEARABLE:
        raise ValueError(f"field cannot be cleared: {field!r}")
    if field == "artist":
        media.artist = None
        media.artists = []
    elif field == "genre":
        media.genre = []
    elif field == "lyrics":
        media.lyrics = None
    elif field == "track":
        if media.audio is None:
            raise ValueError("file has no audio stream")
        media.audio.track_number = None
        media.audio.track_total = None
    elif field == "disc":
        if media.audio is None:
            raise ValueError("file has no audio stream")
        media.audio.disc_number = None
        media.audio.disc_total = None
    elif field in {"album", "album_artist", "isrc", "bpm", "movement", "movement_total"}:
        if media.audio is None:
            raise ValueError("file has no audio stream")
        attr = {
            "album": "album",
            "album_artist": "album_artist",
            "isrc": "isrc",
            "bpm": "bpm",
            "movement": "movement_number",
            "movement_total": "movement_total",
        }[field]
        setattr(media.audio, attr, None)
    elif field == "movement_name":
        if media.audio is None:
            raise ValueError("file has no audio stream")
        media.audio.movement_name = None
    elif field in {"show", "season", "episode", "director", "synopsis"}:
        if media.video is None:
            raise ValueError("file has no video stream")
        attr = "episode" if field == "episode" else field
        setattr(media.video, attr, None)
        if field == "director":
            media.video.director = []
    else:
        setattr(media, field, None)


def image_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    raise ValueError(f"unsupported cover format: {suffix or '<none>'}")

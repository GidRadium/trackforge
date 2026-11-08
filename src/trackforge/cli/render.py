"""Rendering helpers for CLI commands."""

from __future__ import annotations

from dataclasses import asdict


def to_dict(media) -> dict[str, object]:
    audio = media.audio
    video = media.video
    return {
        "path": str(media.path),
        "container": media.container.value if media.container else None,
        "mime_type": media.mime_type,
        "kind": media.kind.value,
        "title": media.title,
        "artist": media.artist,
        "artists": media.artists,
        "date": str(media.date) if media.date else None,
        "genre": media.genre,
        "comment": media.comment,
        "composer": media.composer,
        "lyricist": media.lyricist,
        "conductor": media.conductor,
        "lyrics": media.lyrics.text if media.lyrics else None,
        "musicbrainz_ids": media.musicbrainz_ids,
        "audio": asdict(audio) if audio is not None else None,
        "video": asdict(video) if video is not None else None,
        "artwork": [
            {
                "mime": item.mime,
                "kind": str(item.kind),
                "bytes": len(item.data),
            }
            for item in media.artwork
        ],
        "warnings": media.warnings,
    }


def container(media) -> str:
    format_ = media.container
    if format_ is None:
        return "unknown"
    return f"{format_.value} ({media.mime_type})"


def or_dash(value: str | None) -> str:
    return value if value else "—"


def number(value: int | None, total: int | None) -> str:
    if value is None:
        return "—"
    if total is None:
        return str(value)
    return f"{value}/{total}"

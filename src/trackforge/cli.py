"""Command-line entry point for trackforge."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import typer

app = typer.Typer(
    name="tfg",
    help="Collect, verify, and write metadata for audio and video files.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def root() -> None:
    """Collect, verify, and write metadata for audio and video files."""


@app.command()
def info(
    path: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Media file to inspect.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Print machine-readable JSON.",
    ),
) -> None:
    """Show normalized metadata for a media file."""
    from trackforge.metadata import MediaFile, TrackForgeError

    try:
        media = MediaFile.open(path)
    except (TrackForgeError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(json.dumps(_to_dict(media), ensure_ascii=False, indent=2))
        return

    typer.echo(f"Path:      {media.path}")
    typer.echo(f"Container: {_container(media)}")
    typer.echo(f"Kind:      {media.kind.value}")
    typer.echo(f"Title:     {_or_dash(media.title)}")
    typer.echo(f"Artist:    {_or_dash(media.artist)}")
    typer.echo(f"Date:      {_or_dash(str(media.date) if media.date else None)}")
    typer.echo(f"Genre:     {', '.join(media.genre) if media.genre else '—'}")

    if media.audio is not None:
        audio = media.audio
        typer.echo(f"Album:     {_or_dash(audio.album)}")
        typer.echo(f"Album artist: {_or_dash(audio.album_artist)}")
        typer.echo(f"Track:     {_number(audio.track_number, audio.track_total)}")
        typer.echo(f"Disc:      {_number(audio.disc_number, audio.disc_total)}")
        typer.echo(f"ISRC:      {_or_dash(audio.isrc)}")

    if media.video is not None:
        video = media.video
        typer.echo(f"Show:      {_or_dash(video.show)}")
        typer.echo(
            "Episode:   "
            + (
                f"S{video.season:02d}E{video.episode:02d}"
                if video.season is not None and video.episode is not None
                else "—"
            )
        )
        typer.echo(f"Director:  {', '.join(video.director) if video.director else '—'}")

    if media.artwork:
        kinds = ", ".join(str(item.kind) for item in media.artwork)
        typer.echo(f"Artwork:   {len(media.artwork)} ({kinds})")
    else:
        typer.echo("Artwork:   —")

    for warning in media.warnings:
        typer.echo(f"Warning:   {warning}")


def _to_dict(media) -> dict[str, object]:
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


def _container(media) -> str:
    container = media.container
    if container is None:
        return "unknown"
    return f"{container.value} ({media.mime_type})"


def _or_dash(value: str | None) -> str:
    return value if value else "—"


def _number(number: int | None, total: int | None) -> str:
    if number is None:
        return "—"
    if total is None:
        return str(number)
    return f"{number}/{total}"


def main() -> None:
    """Run the ``tfg`` command-line interface."""
    app()


if __name__ == "__main__":
    main()

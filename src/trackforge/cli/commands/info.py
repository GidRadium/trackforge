"""``tfg info`` command."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ..render import container, number, or_dash, to_dict


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
        typer.echo(json.dumps(to_dict(media), ensure_ascii=False, indent=2))
        return

    typer.echo(f"Path:      {media.path}")
    typer.echo(f"Container: {container(media)}")
    typer.echo(f"Kind:      {media.kind.value}")
    typer.echo(f"Title:     {or_dash(media.title)}")
    typer.echo(f"Artist:    {or_dash(media.artist)}")
    typer.echo(f"Date:      {or_dash(str(media.date) if media.date else None)}")
    typer.echo(f"Genre:     {', '.join(media.genre) if media.genre else '—'}")

    if media.audio is not None:
        audio = media.audio
        typer.echo(f"Album:     {or_dash(audio.album)}")
        typer.echo(f"Album artist: {or_dash(audio.album_artist)}")
        typer.echo(f"Track:     {number(audio.track_number, audio.track_total)}")
        typer.echo(f"Disc:      {number(audio.disc_number, audio.disc_total)}")
        typer.echo(f"ISRC:      {or_dash(audio.isrc)}")

    if media.video is not None:
        video = media.video
        typer.echo(f"Show:      {or_dash(video.show)}")
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

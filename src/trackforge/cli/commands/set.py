"""``tfg set`` command."""

# Typer uses call expressions in argument defaults by design.
# pyright: standard

from __future__ import annotations

from pathlib import Path

import typer

from ..values import clear_field, image_mime


def set_metadata(
    path: Path = typer.Argument(  # noqa: B008
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Media file to update.",
    ),
    title: str | None = typer.Option(None, "--title"),
    artist: str | None = typer.Option(None, "--artist"),
    album: str | None = typer.Option(None, "--album"),
    album_artist: str | None = typer.Option(None, "--album-artist"),
    date_value: str | None = typer.Option(None, "--date", help="YYYY, YYYY-MM or YYYY-MM-DD."),
    original_date_value: str | None = typer.Option(None, "--original-date"),
    release_date_value: str | None = typer.Option(None, "--release-date"),
    mood: str | None = typer.Option(None, "--mood"),
    work: str | None = typer.Option(None, "--work"),
    subtitle: str | None = typer.Option(None, "--subtitle"),
    media_value: str | None = typer.Option(None, "--media"),
    release_country: str | None = typer.Option(None, "--release-country"),
    release_status: str | None = typer.Option(None, "--release-status"),
    release_type: str | None = typer.Option(None, "--release-type"),
    genre: str | None = typer.Option(None, "--genre", help="Separate multiple genres with ';'."),
    comment: str | None = typer.Option(None, "--comment"),
    composer: str | None = typer.Option(None, "--composer", help="Separate multiple names with ';'."),
    lyrics: str | None = typer.Option(None, "--lyrics"),
    cover: Path | None = typer.Option(  # noqa: B008
        None,
        "--cover",
        exists=True,
        dir_okay=False,
        readable=True,
        help="JPEG or PNG file to use as the front cover.",
    ),
    remove_cover: bool = typer.Option(False, "--remove-cover"),
    clear: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--clear",
        help="Remove a field; repeat for multiple fields.",
    ),
    track: str | None = typer.Option(None, "--track", help="N or N/TOTAL."),
    disc: str | None = typer.Option(None, "--disc", help="N or N/TOTAL."),
    isrc: str | None = typer.Option(None, "--isrc"),
    bpm: int | None = typer.Option(None, "--bpm"),
    movement_name: str | None = typer.Option(None, "--movement-name"),
    movement: int | None = typer.Option(None, "--movement"),
    movement_total: int | None = typer.Option(None, "--movement-total"),
    rating: int | None = typer.Option(None, "--rating", min=0, max=5),
    show: str | None = typer.Option(None, "--show"),
    season: int | None = typer.Option(None, "--season"),
    episode: int | None = typer.Option(None, "--episode"),
    director: str | None = typer.Option(None, "--director", help="Separate multiple names with ';'."),
    synopsis: str | None = typer.Option(None, "--synopsis"),
    strict: bool = typer.Option(False, "--strict", help="Fail if a field cannot be stored."),
    legacy: bool = typer.Option(False, "--legacy", help="Also write an ID3v1 block for MP3/AAC."),
    id3v23: bool = typer.Option(False, "--id3v23", help="Write ID3v2.3 instead of v2.4."),
) -> None:
    """Update metadata fields and save the file."""
    from trackforge.metadata import (
        Artwork,
        Lyrics,
        MediaFile,
        TrackForgeError,
        UnsupportedPolicy,
        WritePolicy,
    )
    from trackforge.metadata.normalize import parse_number_pair, parse_partial_date

    try:
        media = MediaFile.open(path)
        changes = 0

        if title is not None:
            media.title = title
            changes += 1
        if artist is not None:
            media.artist = artist
            media.artists = [artist]
            changes += 1
        if date_value is not None:
            parsed_date = parse_partial_date(date_value)
            if parsed_date is None:
                raise ValueError(f"invalid date: {date_value!r}")
            media.date = parsed_date
            changes += 1
        if original_date_value is not None:
            parsed_date = parse_partial_date(original_date_value)
            if parsed_date is None:
                raise ValueError(f"invalid original date: {original_date_value!r}")
            media.original_date = parsed_date
            changes += 1
        if release_date_value is not None:
            parsed_date = parse_partial_date(release_date_value)
            if parsed_date is None:
                raise ValueError(f"invalid release date: {release_date_value!r}")
            media.release_date = parsed_date
            changes += 1
        if mood is not None:
            media.mood = mood
            changes += 1
        if work is not None:
            media.work = work
            changes += 1
        if subtitle is not None:
            media.subtitle = subtitle
            changes += 1
        if media_value is not None:
            media.media = media_value
            changes += 1
        if release_country is not None:
            media.release_country = release_country
            changes += 1
        if release_status is not None:
            media.release_status = release_status
            changes += 1
        if release_type is not None:
            media.release_type = release_type
            changes += 1
        if genre is not None:
            media.genre = [part.strip() for part in genre.split(";") if part.strip()]
            changes += 1
        if comment is not None:
            media.comment = comment
            changes += 1
        if composer is not None:
            media.composer = [part.strip() for part in composer.split(";") if part.strip()]
            changes += 1
        if lyrics is not None:
            language = media.lyrics.language if media.lyrics is not None else "eng"
            media.lyrics = Lyrics(text=lyrics, language=language)
            changes += 1
        if cover is not None:
            mime = image_mime(cover)
            data = cover.read_bytes()
            if not data:
                raise ValueError(f"cover file is empty: {cover}")
            media.artwork.replace_front(Artwork(data=data, mime=mime))
            changes += 1
        if remove_cover:
            if not media.artwork.remove_front():
                raise ValueError("no front cover to remove")
            changes += 1
        for field in clear or []:
            clear_field(media, field)
            changes += 1

        audio = media.audio
        needs_audio = any(
            value is not None
            for value in (
                album,
                album_artist,
                track,
                disc,
                isrc,
                bpm,
                movement_name,
                movement,
                movement_total,
                rating,
            )
        )
        if needs_audio and audio is None:
            raise ValueError("file has no audio stream")
        if audio is not None:
            if album is not None:
                audio.album = album
                changes += 1
            if album_artist is not None:
                audio.album_artist = album_artist
                changes += 1
            if track is not None:
                number, total = parse_number_pair(track)
                if number is None and total is None:
                    raise ValueError(f"invalid track: {track!r}")
                audio.track_number = number
                audio.track_total = total
                changes += 1
            if disc is not None:
                number, total = parse_number_pair(disc)
                if number is None and total is None:
                    raise ValueError(f"invalid disc: {disc!r}")
                audio.disc_number = number
                audio.disc_total = total
                changes += 1
            if isrc is not None:
                audio.isrc = isrc
                changes += 1
            if bpm is not None:
                audio.bpm = bpm
                changes += 1
            if movement_name is not None:
                audio.movement_name = movement_name
                changes += 1
            if movement is not None:
                audio.movement_number = movement
                changes += 1
            if movement_total is not None:
                audio.movement_total = movement_total
                changes += 1
            if rating is not None:
                audio.rating = rating
                changes += 1

        video = media.video
        needs_video = any(
            value is not None for value in (show, season, episode, director, synopsis)
        )
        if needs_video and video is None:
            raise ValueError("file has no video stream")
        if video is not None:
            if show is not None:
                video.show = show
                changes += 1
            if season is not None:
                video.season = season
                changes += 1
            if episode is not None:
                video.episode = episode
                changes += 1
            if director is not None:
                video.director = [part.strip() for part in director.split(";") if part.strip()]
                changes += 1
            if synopsis is not None:
                video.synopsis = synopsis
                changes += 1

        if changes == 0:
            typer.echo("error: no fields to change", err=True)
            raise typer.Exit(code=1)

        policy = WritePolicy(
            on_unsupported=UnsupportedPolicy.ERROR if strict else UnsupportedPolicy.SKIP,
            write_legacy=legacy,
            id3_version=3 if id3v23 else 4,
            remove_artwork=remove_cover,
            remove_missing=bool(clear),
        )
        report = media.save(policy=policy)
    except (TrackForgeError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if report.written:
        typer.echo("written: " + ", ".join(report.written))
    for field, reason in report.skipped:
        typer.echo(f"skipped: {field} ({reason})")
    for field, reason in report.coerced:
        typer.echo(f"coerced: {field} ({reason})")
    for warning in report.warnings:
        typer.echo(f"warning: {warning}")

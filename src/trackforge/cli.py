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


@app.command("set")
def set_metadata(
    path: Path = typer.Argument(
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
    cover: Path | None = typer.Option(
        None,
        "--cover",
        exists=True,
        dir_okay=False,
        readable=True,
        help="JPEG or PNG file to use as the front cover.",
    ),
    remove_cover: bool = typer.Option(False, "--remove-cover"),
    clear: list[str] | None = typer.Option(
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
            mime = _image_mime(cover)
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
            _clear_field(media, field)
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


@app.command("scan")
def scan_command(
    paths: list[Path] = typer.Argument(
        ...,
        exists=True,
        readable=True,
        help="Files or directories to scan.",
    ),
    workers: int = typer.Option(4, "--workers", min=1),
    progress: bool = typer.Option(False, "--progress"),
    limit: int | None = typer.Option(None, "--limit", min=0),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Scan files or directories and report per-file results."""
    from trackforge.metadata import discover_media_files, scan

    files = discover_media_files(paths)
    if not files:
        typer.echo("error: no media files found", err=True)
        raise typer.Exit(code=1)

    progress_callback = None
    if progress:
        def progress_callback(done: int, total: int, _result) -> None:
            typer.echo(f"[{done}/{total}]", err=True)

    results = scan(
        files,
        workers=workers,
        on_progress=progress_callback,
        limit=limit,
    )
    if json_output:
        payload = [
            {
                "path": str(result.path),
                "ok": result.ok,
                "kind": result.media.kind.value if result.media else None,
                "title": result.media.title if result.media else None,
                "error": str(result.error) if result.error else None,
            }
            for result in results
        ]
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for result in results:
            if result.ok and result.media is not None:
                typer.echo(
                    f"ok    {result.path}  {result.media.kind.value}  "
                    f"{_or_dash(result.media.title)}"
                )
            else:
                typer.echo(f"error {result.path}  {result.error}", err=True)

    if any(result.error is not None for result in results):
        raise typer.Exit(code=1)


_CLEARABLE = {
    "title", "artist", "album", "album_artist", "date", "original_date",
    "release_date", "genre", "comment", "composer", "lyricist", "lyrics",
    "track", "disc", "isrc", "bpm", "show", "season", "episode", "director",
    "synopsis", "mood", "work", "subtitle", "media", "release_country",
    "release_status", "release_type", "movement_name", "movement",
    "movement_total",
}


def _clear_field(media, field: str) -> None:
    if field not in _CLEARABLE:
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


def _image_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    raise ValueError(f"unsupported cover format: {suffix or '<none>'}")


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

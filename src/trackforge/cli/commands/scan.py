"""``tfg scan`` command."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from ..render import or_dash


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
                    f"{or_dash(result.media.title)}"
                )
            else:
                typer.echo(f"error {result.path}  {result.error}", err=True)

    if any(result.error is not None for result in results):
        raise typer.Exit(code=1)

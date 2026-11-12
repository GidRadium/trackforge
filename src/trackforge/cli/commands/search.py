"""``tfg search`` and the shared text-search stub.

Text search is intentionally not implemented in this checkpoint.  The command
exists so the CLI surface is stable, but it explains the limitation instead of
silently picking a result or pretending that a query was a URL.
"""

# pyright: standard

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import typer

from ..common import EXIT_ERROR


def search_not_implemented() -> NoReturn:
    """Print the text-search stub message and exit with code 1."""
    typer.echo("error: text search is not implemented yet", err=True)
    typer.echo(
        "hint: pass a direct URL for now; text search will arrive later",
        err=True,
    )
    raise typer.Exit(code=EXIT_ERROR)


def search_command(
    query: str = typer.Argument(
        ...,
        help="Text query (for example 'Artist - Track').",
    ),
    limit: int = typer.Option(
        10,
        "--limit",
        min=1,
        help="Maximum number of search candidates.",
    ),
    source: str | None = typer.Option(
        None,
        "--source",
        "-s",
        help="Restrict search to one loader.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        help="Explicit config file; user config still has lower precedence.",
    ),
    cookies: Path | None = typer.Option(  # noqa: B008
        None,
        "--cookies",
        help="One-shot cookies file override for this run.",
    ),
    proxy: str | None = typer.Option(
        None,
        "--proxy",
        help="One-shot proxy override (URL, 'env' or 'none').",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Print machine-readable results.",
    ),
) -> None:
    """Search for media by text (not implemented yet)."""
    del query, limit, source, config_path, cookies, proxy, json_output
    search_not_implemented()


__all__ = ["search_command", "search_not_implemented"]

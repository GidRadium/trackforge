"""Thin Typer command-line adapter for TrackForge."""

from __future__ import annotations

from typing import Annotated

import typer

from trackforge import __version__
from trackforge.interfaces.cli.commands import register_commands
from trackforge.interfaces.cli.commands.config import config_app
from trackforge.interfaces.cli.common import (  # noqa: F401 - public for CLI tests
    build_config,
    build_run_options,
    execute,
    exit_code_for,
)

app = typer.Typer(
    name="trackforge",
    help="Local-first audio metadata and library tooling.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
)
app.add_typer(config_app, name="config")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"trackforge {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Run a TrackForge command."""


register_commands(app)


__all__ = [
    "app",
    "build_config",
    "build_run_options",
    "execute",
    "exit_code_for",
    "main",
]

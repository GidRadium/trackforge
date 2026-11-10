"""Typer application wiring."""

# Typer uses call expressions in argument defaults by design.
# pyright: standard

from __future__ import annotations

import typer

from .commands.doctor import doctor
from .commands.info import info
from .commands.scan import scan_command
from .commands.set import set_metadata

app = typer.Typer(
    name="tfg",
    help="Collect, verify, and write metadata for audio and video files.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def root() -> None:
    """Collect, verify, and write metadata for audio and video files."""


app.command("info")(info)
app.command("set")(set_metadata)
app.command("scan")(scan_command)
app.command("doctor")(doctor)


def main() -> None:
    """Run the ``tfg`` command-line interface."""
    app()


if __name__ == "__main__":
    main()

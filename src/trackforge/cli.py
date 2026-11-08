"""Command-line entry point for trackforge."""

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


def main() -> None:
    """Run the ``tfg`` command-line interface."""
    app()


if __name__ == "__main__":
    main()

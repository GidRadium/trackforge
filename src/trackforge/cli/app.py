"""Typer application wiring."""

# Typer uses call expressions in argument defaults by design.
# pyright: standard

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import typer

from .commands.doctor import doctor
from .commands.info import info
from .commands.input import run_input
from .commands.inspect import inspect_url
from .commands.scan import scan_command
from .commands.search import search_command
from .commands.set import set_metadata
from .commands.sources import sources_command


class TfgGroup(typer.core.TyperGroup):
    """Typer group with a default ``input`` command.

    Registered subcommands always win, so ``tfg info``/``set``/``scan`` keep
    their meaning.  Any other first argument (a URL or a text query) is routed
    to the same command as ``tfg input``/``tfg i``.
    """

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        # Let ``tfg --json URL`` and friends reach the default command too.
        # Root ``--help`` keeps its normal group help.
        if (
            args
            and args[0].startswith("-")
            and args[0] not in {"--help", "-h"}
            and not ctx.resilient_parsing
        ):
            setattr(ctx, "_protected_args", ["input"])
            ctx.args = list(args)
            return ctx.args
        return super().parse_args(ctx, args)

    def resolve_command(
        self,
        ctx: Any,
        args: Sequence[str],
    ) -> tuple[Any, Any, list[str]]:
        if args:
            first = args[0]
            if first not in self.commands and not first.startswith("-"):
                fallback = self.commands.get("input")
                if fallback is not None:
                    return "input", fallback, list(args)
        return super().resolve_command(ctx, args)


app = typer.Typer(
    name="tfg",
    help="Collect, verify, and write metadata for audio and video files.",
    no_args_is_help=True,
    add_completion=False,
    cls=TfgGroup,
)


@app.callback()
def root() -> None:
    """Collect, verify, and write metadata for audio and video files."""


app.command("info")(info)
app.command("set")(set_metadata)
app.command("scan")(scan_command)
app.command("doctor")(doctor)
app.command("inspect")(inspect_url)
app.command("input")(run_input)
app.command("i", hidden=True)(run_input)
app.command("search")(search_command)
app.command("sources")(sources_command)


def main() -> None:
    """Run the ``tfg`` command-line interface."""
    app()


if __name__ == "__main__":
    main()

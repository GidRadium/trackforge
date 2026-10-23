"""``trackforge resolve`` — resolve a persisted item."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from trackforge.api import AsyncTrackForge
from trackforge.application.dto.views import ResolveView
from trackforge.interfaces.cli.common import build_config, build_run_options, execute
from trackforge.interfaces.cli.presenters import emit_json


def resolve_command(
    item_id: Annotated[str, typer.Argument(help="Item id from a previous run.")],
    mode: Annotated[str | None, typer.Option("--mode")] = None,
    target_format: Annotated[
        str | None,
        typer.Option(
            "--target-format", help="Accepted for command parity; resolve does not render."
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    data_dir: Annotated[str | None, typer.Option("--data-dir")] = None,
    library_dir: Annotated[str | None, typer.Option("--library-dir")] = None,
) -> None:
    """Resolve one item's claims without running acquire/observe again."""

    def body() -> None:
        config = build_config(
            data_dir=data_dir,
            library_dir=library_dir,
            mode=mode,
        )
        options = build_run_options(config, mode=mode, auto=True, target_format=target_format)

        async def operation() -> ResolveView:
            async with AsyncTrackForge(config) as facade:
                return await facade.resolve(item_id, options)

        result = asyncio.run(operation())
        if as_json:
            emit_json(result)
        else:
            fields = getattr(result, "fields", {})
            for field, value in fields.items():
                typer.echo(f"{field}: {value}")
            for warning in getattr(result, "warnings", ()):
                typer.echo(f"warning: {warning}", err=True)

    execute(as_json, body)


__all__ = ["resolve_command"]

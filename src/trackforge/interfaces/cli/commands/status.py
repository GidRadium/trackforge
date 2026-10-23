"""``trackforge status`` — inspect a persisted run."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from trackforge.api import AsyncTrackForge
from trackforge.application.dto.results import StatusView
from trackforge.interfaces.cli.common import build_config, execute
from trackforge.interfaces.cli.presenters import emit_json, print_status


def status_command(
    run_id: Annotated[str, typer.Argument(help="Run id.")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
    data_dir: Annotated[str | None, typer.Option("--data-dir")] = None,
    library_dir: Annotated[str | None, typer.Option("--library-dir")] = None,
) -> None:
    """Show run, step, and item status from the catalog."""

    def body() -> None:
        config = build_config(data_dir=data_dir, library_dir=library_dir)

        async def operation() -> StatusView:
            async with AsyncTrackForge(config) as facade:
                return await facade.status(run_id)

        result = asyncio.run(operation())
        if as_json:
            emit_json(result)
        else:
            print_status(result)

    execute(as_json, body)


__all__ = ["status_command"]

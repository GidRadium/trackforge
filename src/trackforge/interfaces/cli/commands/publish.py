"""``trackforge publish`` — copy the latest bundle into the managed library."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from trackforge.api import AsyncTrackForge
from trackforge.application.dto.results import PublishResult
from trackforge.interfaces.cli.common import build_config, build_run_options, execute
from trackforge.interfaces.cli.presenters import emit_json, print_publish


def publish_command(
    item_id: Annotated[str, typer.Argument(help="Item id from a previous run.")],
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    mode: Annotated[str | None, typer.Option("--mode")] = None,
    target_format: Annotated[
        str | None,
        typer.Option(
            "--target-format", help="Accepted for command parity; publish uses the render."
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    data_dir: Annotated[str | None, typer.Option("--data-dir")] = None,
    library_dir: Annotated[str | None, typer.Option("--library-dir")] = None,
) -> None:
    """Publish an already rendered item without re-running the pipeline."""

    def body() -> None:
        config = build_config(
            data_dir=data_dir,
            library_dir=library_dir,
            profile=profile,
            mode=mode,
        )
        options = build_run_options(
            config, profile=profile, mode=mode, auto=True, target_format=target_format
        )

        async def operation() -> PublishResult:
            async with AsyncTrackForge(config) as facade:
                return await facade.publish(item_id, options)

        result = asyncio.run(operation())
        if as_json:
            emit_json(result)
        else:
            print_publish(result)

    execute(as_json, body)


__all__ = ["publish_command"]

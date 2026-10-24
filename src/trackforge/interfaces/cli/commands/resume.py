"""``trackforge resume`` — continue a failed or paused run."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from trackforge.api import AsyncTrackForge
from trackforge.application.dto.results import RunResult
from trackforge.interfaces.cli.common import build_config, build_run_options, execute
from trackforge.interfaces.cli.presenters import emit_json, print_run


def resume_command(
    run_id: Annotated[str, typer.Argument(help="Run id to resume.")],
    enable: Annotated[
        str | None,
        typer.Option(
            "--enable",
            help="Comma-separated capabilities to enable (for example asr, stems).",
        ),
    ] = None,
    disable: Annotated[
        str | None,
        typer.Option(
            "--disable",
            help="Comma-separated capabilities to disable (for example asr, stems).",
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    data_dir: Annotated[str | None, typer.Option("--data-dir")] = None,
    library_dir: Annotated[str | None, typer.Option("--library-dir")] = None,
) -> None:
    """Continue a run from its last successful checkpoint."""

    def body() -> None:
        config = build_config(data_dir=data_dir, library_dir=library_dir)
        options = build_run_options(
            config,
            auto=True,
            enable=enable,
            disable=disable,
        )

        async def operation() -> RunResult:
            async with AsyncTrackForge(config) as facade:
                return await facade.resume(run_id, options)

        result = asyncio.run(operation())
        if as_json:
            emit_json(result)
        else:
            print_run(result)

    execute(as_json, body)


__all__ = ["resume_command"]

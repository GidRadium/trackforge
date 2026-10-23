"""``trackforge render`` — materialize the current decisions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from trackforge.api import AsyncTrackForge
from trackforge.application.dto.results import RenderView
from trackforge.interfaces.cli.common import build_config, build_run_options, execute
from trackforge.interfaces.cli.presenters import emit_json


def render_command(
    item_id: Annotated[str, typer.Argument(help="Item id from a previous run.")],
    target_format: Annotated[
        str | None,
        typer.Option("--target-format", help="Output format: mp3, m4a, flac, ogg, opus."),
    ] = None,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    mode: Annotated[str | None, typer.Option("--mode")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    data_dir: Annotated[str | None, typer.Option("--data-dir")] = None,
    library_dir: Annotated[str | None, typer.Option("--library-dir")] = None,
) -> None:
    """Render and verify a new output bundle for one item."""

    def body() -> None:
        config = build_config(
            data_dir=data_dir,
            library_dir=library_dir,
            profile=profile,
            mode=mode,
        )
        options = build_run_options(
            config,
            profile=profile,
            mode=mode,
            auto=True,
            target_format=target_format,
        )

        async def operation() -> RenderView:
            async with AsyncTrackForge(config) as facade:
                return await facade.render(item_id, options)

        result = asyncio.run(operation())
        if as_json:
            emit_json(result)
        else:
            typer.echo(f"Rendered: {result.audio_path}")
            typer.echo(f"Bundle: {result.bundle_id} ({result.bundle_hash})")
            typer.echo(f"Directory: {Path(result.directory)}")
            for warning in result.warnings:
                typer.echo(f"warning: {warning}", err=True)

    execute(as_json, body)


__all__ = ["render_command"]

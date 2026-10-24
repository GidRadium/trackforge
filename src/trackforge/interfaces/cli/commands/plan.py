"""``trackforge plan`` — side-effect-free local planning."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from trackforge.api import AsyncTrackForge
from trackforge.application.dto.plan import PlanView
from trackforge.interfaces.cli.common import build_config, build_run_options, execute
from trackforge.interfaces.cli.presenters import emit_json, print_plan


def plan_command(
    source: Annotated[str, typer.Argument(help="Local audio file to plan.")],
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    mode: Annotated[str | None, typer.Option("--mode")] = None,
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
    target_format: Annotated[
        str | None,
        typer.Option("--target-format", help="Accepted for command parity; plan does not render."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    offline: Annotated[bool, typer.Option("--offline")] = False,
    data_dir: Annotated[str | None, typer.Option("--data-dir")] = None,
    library_dir: Annotated[str | None, typer.Option("--library-dir")] = None,
) -> None:
    """Describe candidates and steps without writing anything."""

    def body() -> None:
        config = build_config(
            data_dir=data_dir,
            library_dir=library_dir,
            profile=profile,
            mode=mode,
            offline=offline,
        )
        options = build_run_options(
            config,
            profile=profile,
            mode=mode,
            dry_run=True,
            enable=enable,
            disable=disable,
            offline=offline,
            target_format=target_format,
        )

        async def operation() -> PlanView:
            facade = AsyncTrackForge(config)
            try:
                return await facade.plan(source, options)
            finally:
                await facade.close()

        plan = asyncio.run(operation())
        if as_json:
            emit_json(plan)
        else:
            print_plan(plan)
        if plan.blocked_reason is not None:
            raise typer.Exit(2)

    execute(as_json, body)


__all__ = ["plan_command"]

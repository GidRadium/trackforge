"""``trackforge plan`` — side-effect-free local planning."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Annotated

import typer

from trackforge.api import AsyncTrackForge
from trackforge.application.dto.collections import CollectionPlanView
from trackforge.application.dto.plan import PlanView
from trackforge.interfaces.cli.commands.collection import (
    is_collection_input,
    make_collection_options,
)
from trackforge.interfaces.cli.common import build_config, build_run_options, execute
from trackforge.interfaces.cli.presenters import (
    emit_json,
    print_collection_plan,
    print_plan,
)


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
    playlist_start: Annotated[
        int | None,
        typer.Option("--playlist-start", help="First collection position to list."),
    ] = None,
    playlist_end: Annotated[
        int | None,
        typer.Option("--playlist-end", help="Last collection position to list."),
    ] = None,
    playlist_items: Annotated[
        str | None,
        typer.Option("--playlist-items", help="Collection positions, such as 1,3-5."),
    ] = None,
    skip: Annotated[
        str | None,
        typer.Option("--skip", help="Collection positions to skip."),
    ] = None,
    tag_basis: Annotated[
        str | None,
        typer.Option("--tag-basis", help="album_first, playlist_first, or ask."),
    ] = None,
    auto: Annotated[bool, typer.Option("--auto", help="Never prompt.")] = False,
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
        collection_source = is_collection_input(source)

        async def operation() -> PlanView | CollectionPlanView:
            facade = AsyncTrackForge(config)
            try:
                if collection_source:
                    collection_options, warnings = make_collection_options(
                        config,
                        start=playlist_start,
                        end=playlist_end,
                        items=playlist_items,
                        skip=skip,
                        tag_basis=tag_basis,
                        auto=auto,
                        as_json=as_json,
                    )
                    plan = await facade.collection_plan(source, collection_options)
                    return replace(
                        plan,
                        warnings=tuple(dict.fromkeys((*warnings, *plan.warnings))),
                    )
                return await facade.plan(source, options)
            finally:
                await facade.close()

        plan = asyncio.run(operation())
        if as_json:
            emit_json(plan)
        elif isinstance(plan, CollectionPlanView):
            print_collection_plan(plan)
        else:
            print_plan(plan)
        if plan.blocked_reason is not None:
            raise typer.Exit(2)

    execute(as_json, body)


__all__ = ["plan_command"]

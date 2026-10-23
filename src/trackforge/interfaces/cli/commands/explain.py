"""``trackforge explain`` — explain one field decision."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from trackforge.api import AsyncTrackForge
from trackforge.application.dto.decisions import ExplanationView
from trackforge.interfaces.cli.common import build_config, execute
from trackforge.interfaces.cli.presenters import emit_json, print_explanation


def explain_command(
    item_id: Annotated[str, typer.Argument(help="Item id from a previous run.")],
    field: Annotated[str, typer.Option("--field", help="Field to explain.")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
    data_dir: Annotated[str | None, typer.Option("--data-dir")] = None,
    library_dir: Annotated[str | None, typer.Option("--library-dir")] = None,
) -> None:
    """Show the chosen claim, alternatives, policy, and rationale."""

    def body() -> None:
        config = build_config(data_dir=data_dir, library_dir=library_dir)

        async def operation() -> ExplanationView:
            async with AsyncTrackForge(config) as facade:
                return await facade.explain(item_id, field)

        result = asyncio.run(operation())
        if as_json:
            emit_json(result)
        else:
            print_explanation(result)

    execute(as_json, body)


__all__ = ["explain_command"]

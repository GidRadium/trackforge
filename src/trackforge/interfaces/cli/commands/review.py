"""``trackforge review`` — inspect and correct one item's decisions."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from trackforge.api import AsyncTrackForge
from trackforge.application.dto.views import ReviewView
from trackforge.interfaces.cli.common import (
    build_config,
    build_run_options,
    execute,
    fail,
)
from trackforge.interfaces.cli.presenters import emit_json, print_review


def review_command(
    item_id: Annotated[str, typer.Argument(help="Item id from a previous run.")],
    field: Annotated[str | None, typer.Option("--field", help="Field to review.")] = None,
    set_value: Annotated[
        str | None,
        typer.Option("--set", help="New value, or field=value when --field is omitted."),
    ] = None,
    lock: Annotated[bool, typer.Option("--lock", help="Lock the field value.")] = False,
    unlock: Annotated[bool, typer.Option("--unlock", help="Remove a field lock.")] = False,
    unknown: Annotated[
        bool,
        typer.Option("--unknown", help="Mark the field as unknown."),
    ] = False,
    why: Annotated[bool, typer.Option("--why", help="Explain the current decision.")] = False,
    skip: Annotated[bool, typer.Option("--skip", help="Keep the current value.")] = False,
    accept: Annotated[bool, typer.Option("--accept", help="Accept current decisions.")] = False,
    auto: Annotated[bool, typer.Option("--auto", help="Accept all current decisions.")] = False,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    mode: Annotated[str | None, typer.Option("--mode")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    data_dir: Annotated[str | None, typer.Option("--data-dir")] = None,
    library_dir: Annotated[str | None, typer.Option("--library-dir")] = None,
) -> None:
    """Show, accept, edit, lock, unlock, or explain review fields."""

    def body() -> None:
        selected_actions = [unlock, unknown, why, skip, accept]
        if sum(bool(value) for value in selected_actions) > 1:
            fail("choose only one review action", as_json=as_json)
        field_name = field.strip() if isinstance(field, str) and field.strip() else None
        raw_value: object = set_value
        if set_value is not None and field_name is None:
            if "=" not in set_value:
                fail(
                    "use --set field=value when --field is omitted",
                    as_json=as_json,
                )
            field_name, raw_value = set_value.split("=", 1)
            field_name = field_name.strip()
        action = "show"
        if unlock:
            action = "unlock"
        elif unknown:
            action = "unknown"
        elif why:
            action = "why"
        elif skip:
            action = "skip"
        elif accept:
            action = "accept"
        elif set_value is not None:
            action = "edit"
        elif lock:
            action = "lock"
        if action in {"edit", "lock", "unlock", "unknown", "why"} and not field_name:
            fail(f"--{action} requires --field", as_json=as_json)

        config = build_config(
            data_dir=data_dir,
            library_dir=library_dir,
            mode=mode,
        )
        options = build_run_options(config, mode=mode, auto=True)

        async def operation() -> ReviewView:
            async with AsyncTrackForge(config) as facade:
                return await facade.review(
                    item_id,
                    action=action,
                    field=field_name,
                    value=raw_value,
                    lock=lock and action == "edit",
                    reason=reason,
                    mode=options.mode,
                    auto=auto,
                )

        result = asyncio.run(operation())
        if as_json:
            emit_json(result)
        else:
            print_review(result)

    execute(as_json, body)


__all__ = ["review_command"]

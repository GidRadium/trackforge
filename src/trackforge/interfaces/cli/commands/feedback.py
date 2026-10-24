"""``trackforge feedback`` — export and aggregate review feedback locally."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from trackforge.api import AsyncTrackForge
from trackforge.application.dto.feedback import FeedbackExportResult, FeedbackStatsReport
from trackforge.interfaces.cli.common import build_config, execute, fail
from trackforge.interfaces.cli.presenters import emit_json

feedback_app = typer.Typer(
    name="feedback",
    help="Export review feedback and calculate correction statistics.",
    no_args_is_help=True,
)


@feedback_app.command("export")
def export_command(
    format: Annotated[
        str,
        typer.Option("--format", "-f", help="Export format: jsonl or csv."),
    ] = "jsonl",
    output: Annotated[
        str | None,
        typer.Option("--output", "-o", help="Output file or directory."),
    ] = None,
    item: Annotated[str | None, typer.Option("--item", help="Only this item id.")] = None,
    field: Annotated[str | None, typer.Option("--field", help="Only this field.")] = None,
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only events on/after this ISO date or timestamp."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit one JSON object.")] = False,
    data_dir: Annotated[str | None, typer.Option("--data-dir")] = None,
    library_dir: Annotated[str | None, typer.Option("--library-dir")] = None,
) -> None:
    """Write a redacted JSONL/CSV export; nothing is sent to the network."""

    def body() -> None:
        target_format = format.strip().lower()
        if target_format not in {"jsonl", "csv"}:
            fail("--format must be jsonl or csv", as_json=as_json)
        config = build_config(data_dir=data_dir, library_dir=library_dir)

        async def operation() -> FeedbackExportResult:
            async with AsyncTrackForge(config) as facade:
                return await facade.feedback_export(
                    format=target_format,
                    output=output,
                    item_id=item,
                    field=field,
                    since=since,
                )

        result = asyncio.run(operation())
        if as_json:
            emit_json(result)
            return
        typer.echo(
            f"Exported {result.event_count} event(s) as {result.format}: {result.output_path}"
        )
        if result.correction_count:
            typer.echo(f"Corrections: {result.correction_count}")
        for warning in result.warnings:
            typer.echo(f"warning: {warning}", err=True)

    execute(as_json, body)


@feedback_app.command("stats")
def stats_command(
    group_by: Annotated[
        str,
        typer.Option("--group-by", help="Group by field, source, or method."),
    ] = "field",
    item: Annotated[str | None, typer.Option("--item", help="Only this item id.")] = None,
    field: Annotated[str | None, typer.Option("--field", help="Only this field.")] = None,
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only events on/after this ISO date or timestamp."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit one JSON object.")] = False,
    data_dir: Annotated[str | None, typer.Option("--data-dir")] = None,
    library_dir: Annotated[str | None, typer.Option("--library-dir")] = None,
) -> None:
    """Show correction, review, and unknown rates without claiming accuracy."""

    def body() -> None:
        config = build_config(data_dir=data_dir, library_dir=library_dir)

        async def operation() -> FeedbackStatsReport:
            async with AsyncTrackForge(config) as facade:
                return await facade.feedback_stats(
                    group_by=group_by,
                    item_id=item,
                    field=field,
                    since=since,
                )

        result = asyncio.run(operation())
        if as_json:
            emit_json(result)
            return
        typer.echo(f"Events: {result.total_events}")
        typer.echo(f"Decisions: {result.total_decisions}")
        typer.echo(f"Reviewed items: {result.reviewed_items}/{result.total_items}")
        typer.echo(f"Corrections: {result.corrections}")
        typer.echo(f"Correction rate: {result.correction_rate:.3f}")
        typer.echo(f"Review rate: {result.review_rate:.3f}")
        typer.echo(f"Unknown rate: {result.unknown_rate:.3f}")
        typer.echo(f"Grouped by: {result.group_by}")
        typer.echo(f"{'Group':<24} {'Reviews':>8} {'Correct':>8} {'Rate':>8}")
        for key, group in sorted(result.groups.items()):
            typer.echo(
                f"{key:<24} {group.reviews:>8} {group.corrections:>8} {group.correction_rate:>8.3f}"
            )
        if not result.groups:
            typer.echo("No feedback events yet.")
        for warning in result.warnings:
            typer.echo(f"warning: {warning}", err=True)

    execute(as_json, body)


__all__ = ["feedback_app"]

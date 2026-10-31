"""``trackforge import`` — the primary local end-to-end command."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from trackforge.api import AsyncTrackForge
from trackforge.application.dto.plan import PlanView
from trackforge.application.dto.results import RunResult
from trackforge.interfaces.cli.common import build_config, build_run_options, execute
from trackforge.interfaces.cli.presenters import (
    emit_json,
    print_handler_reports,
    print_plan,
    print_run,
    report_labels,
)
from trackforge.interfaces.cli.progress import (
    ProgressReporter,
    clamp_progress_interval,
    resolve_progress_mode,
    resolve_reports_mode,
)


def import_command(
    path: Annotated[str, typer.Argument(help="Local audio file to import.")],
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="Analysis profile: fast, balanced, full, custom."),
    ] = None,
    mode: Annotated[
        str | None,
        typer.Option("--mode", help="Resolution mode (policy in this build)."),
    ] = None,
    auto: Annotated[
        bool,
        typer.Option("--auto", help="Run without interactive review."),
    ] = False,
    review: Annotated[
        bool,
        typer.Option("--review", help="Show resolved decisions before publishing."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Only print the plan; write nothing."),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit one JSON object for scripts."),
    ] = False,
    force: Annotated[
        str | None,
        typer.Option("--force", help="Comma-separated step names to recompute."),
    ] = None,
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
        typer.Option("--target-format", help="Output format: mp3, m4a, flac, ogg, opus."),
    ] = None,
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Disable network access."),
    ] = False,
    refresh: Annotated[
        bool,
        typer.Option("--refresh", help="Refresh external metadata despite its TTL."),
    ] = False,
    progress: Annotated[
        str | None,
        typer.Option("--progress", help="auto, rich, plain, or off."),
    ] = None,
    progress_interval: Annotated[
        float | None,
        typer.Option("--progress-interval", help="Seconds between live updates."),
    ] = None,
    reports: Annotated[
        str | None,
        typer.Option("--reports", help="full, summary, or off."),
    ] = None,
    sidecar: Annotated[
        bool | None,
        typer.Option("--sidecar/--no-sidecar", help="Publish the JSON sidecar file."),
    ] = None,
    data_dir: Annotated[
        str | None,
        typer.Option("--data-dir", help="Override the data directory."),
    ] = None,
    library_dir: Annotated[
        str | None,
        typer.Option("--library-dir", help="Override the managed library directory."),
    ] = None,
) -> None:
    """Import a local file through plan → acquire → … → publish."""

    def body() -> None:
        config = build_config(
            data_dir=data_dir,
            library_dir=library_dir,
            profile=profile,
            mode=mode,
            offline=offline,
        )
        effective_auto = auto or (config.download_auto and not review)
        effective_sidecar = sidecar if sidecar is not None else config.download_sidecar
        options = build_run_options(
            config,
            profile=profile,
            mode=mode,
            auto=effective_auto or not review,
            review=review,
            dry_run=dry_run,
            force=force,
            enable=enable,
            disable=disable,
            target_format=target_format,
            offline=offline,
            refresh=refresh,
            sidecar=effective_sidecar,
        )

        progress_mode = resolve_progress_mode(
            progress if progress is not None else config.download_progress,
            as_json=as_json,
        )
        report_mode = resolve_reports_mode(
            reports if reports is not None else config.download_reports,
            as_json=as_json,
        )
        interval = clamp_progress_interval(
            progress_interval
            if progress_interval is not None
            else config.download_progress_interval
        )
        facade = AsyncTrackForge(config)
        reporter = ProgressReporter(
            facade.progress_board,
            mode=progress_mode,
            interval=interval,
            as_json=as_json,
        )

        async def operation() -> RunResult | PlanView:
            try:
                return await facade.import_path(path, options)
            finally:
                await facade.close()

        reporter.start()
        try:
            result = asyncio.run(operation())
        except KeyboardInterrupt:
            reporter.stop()
            snapshot = reporter.snapshot()
            if snapshot is not None and not as_json:
                print_handler_reports(
                    facade.progress_board.reports(snapshot.collection_id),
                    mode=report_mode,
                    labels=report_labels(snapshot),
                    total=snapshot.total,
                )
            raise
        finally:
            reporter.stop()
        if as_json:
            emit_json(result)
        elif isinstance(result, PlanView):
            print_plan(result)
        else:
            print_run(result)
            snapshot = reporter.snapshot()
            print_handler_reports(
                result.handler_reports,
                mode=report_mode,
                labels=report_labels(snapshot) if snapshot is not None else {},
                total=snapshot.total if snapshot is not None else 1,
            )

    execute(as_json, body)


__all__ = ["import_command"]

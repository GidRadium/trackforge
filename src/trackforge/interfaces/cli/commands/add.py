"""``trackforge add`` — plan and run one URL or local path."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Annotated

import typer

from trackforge.api import AsyncTrackForge
from trackforge.application.dto.collections import (
    CollectionPlanOptions,
    CollectionPlanView,
    CollectionRunResult,
)
from trackforge.application.dto.plan import PlanView
from trackforge.application.dto.results import RunResult
from trackforge.domain.errors import CandidateNotFound, DomainValidationError
from trackforge.interfaces.cli.commands.collection import (
    is_collection_input,
    make_collection_options,
)
from trackforge.interfaces.cli.common import build_config, build_run_options, execute
from trackforge.interfaces.cli.presenters import (
    emit_json,
    print_collection_plan,
    print_collection_run,
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


def add_command(
    source: Annotated[str, typer.Argument(help="URL or local audio file to add.")],
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
        typer.Option("--auto", help="Pick the best audio format without prompting."),
    ] = False,
    review: Annotated[
        bool,
        typer.Option("--review", help="Show resolved decisions before publishing."),
    ] = False,
    format_id: Annotated[
        str | None,
        typer.Option("--format", help="yt-dlp format id selected by plan."),
    ] = None,
    playlist_start: Annotated[
        int | None,
        typer.Option("--playlist-start", help="First collection position to process."),
    ] = None,
    playlist_end: Annotated[
        int | None,
        typer.Option("--playlist-end", help="Last collection position to process."),
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
    min_sleep: Annotated[
        float | None,
        typer.Option("--min-sleep", help="Seconds to pause between collection items."),
    ] = None,
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Continue an existing collection for this source."),
    ] = False,
    fail_fast: Annotated[
        bool,
        typer.Option("--fail-fast", help="Stop a collection after the first failed item."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Only print the plan; download nothing."),
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
    """Plan and run a URL (or import a local file)."""

    def body() -> None:
        config = build_config(
            data_dir=data_dir,
            library_dir=library_dir,
            profile=profile,
            mode=mode,
            offline=offline,
        )
        effective_auto = auto or (config.download_auto and not review)
        effective_format = format_id or (config.download_format_id or None)
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
            format_id=effective_format,
            offline=offline,
            refresh=refresh,
            sidecar=effective_sidecar,
        )
        is_url = source.strip().casefold().startswith(("http://", "https://"))
        collection_source = is_collection_input(source)
        collection_options: CollectionPlanOptions | None = None
        collection_warnings: tuple[str, ...] = ()
        if collection_source:
            collection_options, collection_warnings = make_collection_options(
                config,
                start=(
                    playlist_start if playlist_start is not None else config.download_playlist_start
                ),
                end=(playlist_end if playlist_end is not None else config.download_playlist_end),
                items=(
                    playlist_items
                    if playlist_items is not None
                    else (config.download_playlist_items or None)
                ),
                skip=skip if skip is not None else (config.download_skip or None),
                tag_basis=tag_basis,
                auto=effective_auto,
                as_json=as_json,
                prompt=not dry_run,
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

        async def operation() -> RunResult | PlanView | CollectionPlanView | CollectionRunResult:
            try:
                if collection_source and collection_options is not None:
                    result = await facade.collection_add(
                        source,
                        options,
                        collection_options=collection_options,
                        resume=resume,
                        fail_fast=fail_fast,
                        min_sleep=(config.collection_min_sleep if min_sleep is None else min_sleep),
                    )
                    if collection_warnings:
                        result = replace(
                            result,
                            warnings=tuple(dict.fromkeys((*collection_warnings, *result.warnings))),
                        )
                    return result
                if dry_run:
                    return await facade.add(source, options)
                if not is_url or effective_format is not None or effective_auto or review:
                    return await facade.add(source, options)
                plan = await facade.plan(source, options)
                if not isinstance(plan, PlanView):
                    return await facade.add(source, options)
                if plan.blocked_reason is not None or not plan.candidates:
                    raise CandidateNotFound(
                        plan.blocked_reason or "source resolved to zero candidates",
                        context={"warnings": list(plan.warnings)},
                    )
                chosen = _choose_format(plan, as_json=as_json)
                return await facade.add(source, replace(options, format_id=chosen))
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
        elif isinstance(result, CollectionPlanView):
            print_collection_plan(result)
        elif isinstance(result, CollectionRunResult):
            print_collection_run(result)
            snapshot = reporter.snapshot()
            print_handler_reports(
                result.handler_reports,
                mode=report_mode,
                labels=report_labels(snapshot) if snapshot is not None else {},
                total=snapshot.total if snapshot is not None else len(result.items),
                aggregate=True,
            )
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


def _choose_format(plan: PlanView, *, as_json: bool = False) -> str:
    candidate = plan.candidates[0]
    typer.echo(f"Source: {candidate.title}", err=as_json)
    if candidate.uploader:
        typer.echo(f"Uploader: {candidate.uploader}", err=as_json)
    formats = [fmt for fmt in candidate.formats if not fmt.drm]
    if not formats:
        raise CandidateNotFound("no selectable audio format is available")
    typer.echo("Formats:", err=as_json)
    for index, fmt in enumerate(formats, start=1):
        bitrate = f"{fmt.bitrate // 1000} kbps" if fmt.bitrate else "bitrate unknown"
        typer.echo(
            f"  {index}. {fmt.format_id or 'unknown'} "
            f"[{fmt.container or '?'} {fmt.codec or '?'}, {bitrate}]",
            err=as_json,
        )
    try:
        choice = typer.prompt("Choose format", default="1", err=as_json)
    except (EOFError, typer.Abort):
        raise DomainValidationError(
            "interactive format selection requires input",
            user_action="Pass --auto or --format <id> for non-interactive use.",
        ) from None
    try:
        index = int(choice)
    except ValueError:
        raise DomainValidationError(
            "format choice must be a number",
            user_action="Pass --auto or --format <id> for non-interactive use.",
        ) from None
    if index < 1 or index > len(formats):
        raise DomainValidationError(
            f"format choice must be between 1 and {len(formats)}",
            user_action="Run plan to list the available formats.",
        )
    selected = formats[index - 1].format_id
    if not selected:
        raise CandidateNotFound("selected format has no id")
    return selected


__all__ = ["add_command"]

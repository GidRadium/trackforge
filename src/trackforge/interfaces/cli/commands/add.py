"""``trackforge add`` — plan and run one URL or local path."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Annotated

import typer

from trackforge.api import AsyncTrackForge
from trackforge.application.dto.plan import PlanView
from trackforge.application.dto.results import RunResult
from trackforge.domain.errors import CandidateNotFound, DomainValidationError
from trackforge.interfaces.cli.common import build_config, build_run_options, execute
from trackforge.interfaces.cli.presenters import emit_json, print_plan, print_run


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
        options = build_run_options(
            config,
            profile=profile,
            mode=mode,
            auto=auto or not review,
            review=review,
            dry_run=dry_run,
            force=force,
            enable=enable,
            disable=disable,
            target_format=target_format,
            format_id=format_id,
            offline=offline,
            refresh=refresh,
        )
        is_url = source.strip().casefold().startswith(("http://", "https://"))

        async def operation() -> RunResult | PlanView:
            facade = AsyncTrackForge(config)
            if dry_run:
                return await facade.add(source, options)
            async with facade:
                if not is_url or format_id is not None or auto or review:
                    return await facade.add(source, options)
                plan = await facade.plan(source, options)
                if plan.blocked_reason is not None or not plan.candidates:
                    raise CandidateNotFound(
                        plan.blocked_reason or "source resolved to zero candidates",
                        context={"warnings": list(plan.warnings)},
                    )
                chosen = _choose_format(plan, as_json=as_json)
                return await facade.add(source, replace(options, format_id=chosen))

        result = asyncio.run(operation())
        if as_json:
            emit_json(result)
        elif isinstance(result, PlanView):
            print_plan(result)
        else:
            print_run(result)

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

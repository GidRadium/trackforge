"""``trackforge collection`` commands and shared collection CLI helpers."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, urlsplit

import typer

from trackforge.api import AsyncTrackForge
from trackforge.application.dto.collections import (
    CollectionPlanOptions,
    CollectionPlanView,
    CollectionRunResult,
)
from trackforge.application.dto.results import RunOptions
from trackforge.application.services.tag_basis import choose_tag_basis
from trackforge.config import TrackForgeConfig
from trackforge.domain.model.collection import Collection
from trackforge.interfaces.cli.common import build_config, build_run_options, execute
from trackforge.interfaces.cli.presenters import (
    emit_json,
    print_collection,
    print_collection_plan,
    print_collection_run,
    print_handler_reports,
    report_labels,
)
from trackforge.interfaces.cli.progress import (
    ProgressReporter,
    clamp_progress_interval,
    resolve_progress_mode,
    resolve_reports_mode,
)

collection_app = typer.Typer(
    name="collection",
    help="Plan, add, resume, and inspect playlist/folder collections.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
)


def is_collection_input(source: str) -> bool:
    """Cheap pre-routing check before invoking source adapters."""

    text = source.strip()
    if not text:
        return False
    if text.startswith(("http://", "https://")):
        try:
            parts = urlsplit(text)
        except ValueError:
            return False
        path = parts.path.casefold()
        if any(
            marker in path for marker in ("/playlist", "/sets/", "/album/", "/channel/", "/user/")
        ):
            return "/track/" not in path
        query = parse_qs(parts.query)
        return "list" in query or "album" in query
    return Path(text).expanduser().is_dir()


def make_collection_options(
    config: TrackForgeConfig,
    *,
    start: int | None,
    end: int | None,
    items: str | None,
    skip: str | None,
    tag_basis: str | None,
    auto: bool,
    as_json: bool,
    prompt: bool = True,
) -> tuple[CollectionPlanOptions, tuple[str, ...]]:
    """Resolve ``ask`` and build validated planning options."""

    choice = choose_tag_basis(
        tag_basis or config.tag_basis,
        default=config.tag_basis,
        auto=auto or as_json or not prompt,
        json_output=as_json,
        prompt=_prompt_for_basis if prompt and not auto and not as_json else None,
    )
    options = CollectionPlanOptions(
        start=start,
        end=end,
        items=items or (),
        skip=skip or (),
        tag_basis=choice.basis,
        offline=config.offline,
    )
    return options, choice.warnings


def _prompt_for_basis(message: str) -> str:
    try:
        return str(typer.prompt(message, default="1"))
    except (EOFError, typer.Abort):
        return "1"


def collection_mode_options(
    config: TrackForgeConfig,
    *,
    profile: str | None,
    mode: str | None,
    auto: bool,
    review: bool,
    dry_run: bool,
    force: str | None,
    enable: str | None,
    disable: str | None,
    target_format: str | None,
    offline: bool,
    sidecar: bool = True,
) -> RunOptions:
    return build_run_options(
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
        offline=offline,
        sidecar=sidecar,
    )


@collection_app.command("plan")
def collection_plan_command(
    source: Annotated[str, typer.Argument(help="Playlist/album URL or local folder.")],
    playlist_start: Annotated[int | None, typer.Option("--playlist-start")] = None,
    playlist_end: Annotated[int | None, typer.Option("--playlist-end")] = None,
    playlist_items: Annotated[
        str | None,
        typer.Option("--playlist-items", help="Positions such as 1,3-5."),
    ] = None,
    skip: Annotated[
        str | None,
        typer.Option("--skip", help="Positions to skip, such as 2,8."),
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
    """List a collection and its selected positions without writing anything."""

    def body() -> None:
        config = build_config(
            data_dir=data_dir,
            library_dir=library_dir,
            offline=offline,
        )
        options, warnings = make_collection_options(
            config,
            start=playlist_start,
            end=playlist_end,
            items=playlist_items,
            skip=skip,
            tag_basis=tag_basis,
            auto=auto,
            as_json=as_json,
        )

        async def operation() -> CollectionPlanView:
            facade = AsyncTrackForge(config)
            try:
                plan = await facade.collection_plan(source, options)
            finally:
                await facade.close()
            if warnings:
                plan = replace(
                    plan,
                    warnings=tuple(dict.fromkeys((*warnings, *plan.warnings))),
                )
            return plan

        plan = asyncio.run(operation())
        if as_json:
            emit_json(plan)
        else:
            print_collection_plan(plan)
        if plan.blocked_reason is not None:
            raise typer.Exit(2)

    execute(as_json, body)


@collection_app.command("add")
def collection_add_command(
    source: Annotated[str, typer.Argument(help="Playlist/album URL or local folder.")],
    playlist_start: Annotated[int | None, typer.Option("--playlist-start")] = None,
    playlist_end: Annotated[int | None, typer.Option("--playlist-end")] = None,
    playlist_items: Annotated[str | None, typer.Option("--playlist-items")] = None,
    skip: Annotated[str | None, typer.Option("--skip")] = None,
    tag_basis: Annotated[str | None, typer.Option("--tag-basis")] = None,
    min_sleep: Annotated[
        float | None,
        typer.Option("--min-sleep", help="Seconds to pause between items."),
    ] = None,
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Continue an existing collection for this source."),
    ] = False,
    fail_fast: Annotated[
        bool,
        typer.Option("--fail-fast", help="Stop after the first failed item."),
    ] = False,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    mode: Annotated[str | None, typer.Option("--mode")] = None,
    auto: Annotated[bool, typer.Option("--auto")] = False,
    review: Annotated[bool, typer.Option("--review")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    force: Annotated[str | None, typer.Option("--force")] = None,
    enable: Annotated[str | None, typer.Option("--enable")] = None,
    disable: Annotated[str | None, typer.Option("--disable")] = None,
    target_format: Annotated[str | None, typer.Option("--target-format")] = None,
    offline: Annotated[bool, typer.Option("--offline")] = False,
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
    data_dir: Annotated[str | None, typer.Option("--data-dir")] = None,
    library_dir: Annotated[str | None, typer.Option("--library-dir")] = None,
) -> None:
    """Plan and process a collection sequentially."""

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
        options = collection_mode_options(
            config,
            profile=profile,
            mode=mode,
            auto=effective_auto,
            review=review,
            dry_run=dry_run,
            force=force,
            enable=enable,
            disable=disable,
            target_format=target_format,
            offline=offline,
            sidecar=effective_sidecar,
        )
        plan_options, warnings = make_collection_options(
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

        async def operation() -> CollectionPlanView | CollectionRunResult:
            try:
                result = await facade.collection_add(
                    source,
                    options,
                    collection_options=plan_options,
                    resume=resume,
                    fail_fast=fail_fast,
                    min_sleep=(config.collection_min_sleep if min_sleep is None else min_sleep),
                )
            finally:
                await facade.close()
            if warnings:
                result = replace(
                    result,
                    warnings=tuple(dict.fromkeys((*warnings, *result.warnings))),
                )
            return result

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
        else:
            print_collection_run(result)
            snapshot = reporter.snapshot()
            print_handler_reports(
                result.handler_reports,
                mode=report_mode,
                labels=report_labels(snapshot) if snapshot is not None else {},
                total=snapshot.total if snapshot is not None else len(result.items),
                aggregate=True,
            )

    execute(as_json, body)


@collection_app.command("resume")
def collection_resume_command(
    collection_id: Annotated[str, typer.Argument(help="Collection id to resume.")],
    min_sleep: Annotated[float | None, typer.Option("--min-sleep")] = None,
    fail_fast: Annotated[bool, typer.Option("--fail-fast")] = False,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    mode: Annotated[str | None, typer.Option("--mode")] = None,
    auto: Annotated[bool, typer.Option("--auto")] = False,
    review: Annotated[bool, typer.Option("--review")] = False,
    force: Annotated[str | None, typer.Option("--force")] = None,
    enable: Annotated[str | None, typer.Option("--enable")] = None,
    disable: Annotated[str | None, typer.Option("--disable")] = None,
    target_format: Annotated[str | None, typer.Option("--target-format")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    offline: Annotated[bool, typer.Option("--offline")] = False,
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
    data_dir: Annotated[str | None, typer.Option("--data-dir")] = None,
    library_dir: Annotated[str | None, typer.Option("--library-dir")] = None,
) -> None:
    """Continue planned/acquired items; processed items are never repeated."""

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
        options = collection_mode_options(
            config,
            profile=profile,
            mode=mode,
            auto=effective_auto,
            review=review,
            dry_run=False,
            force=force,
            enable=enable,
            disable=disable,
            target_format=target_format,
            offline=offline,
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
            scope_id=str(collection_id),
        )

        async def operation() -> CollectionRunResult:
            try:
                return await facade.collection_resume(
                    collection_id,
                    options,
                    fail_fast=fail_fast,
                    min_sleep=(config.collection_min_sleep if min_sleep is None else min_sleep),
                )
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
        else:
            print_collection_run(result)
            snapshot = reporter.snapshot()
            print_handler_reports(
                result.handler_reports,
                mode=report_mode,
                labels=report_labels(snapshot) if snapshot is not None else {},
                total=snapshot.total if snapshot is not None else len(result.items),
                aggregate=True,
            )

    execute(as_json, body)


@collection_app.command("list")
def collection_list_command(
    as_json: Annotated[bool, typer.Option("--json")] = False,
    data_dir: Annotated[str | None, typer.Option("--data-dir")] = None,
    library_dir: Annotated[str | None, typer.Option("--library-dir")] = None,
) -> None:
    """List persisted collections."""

    def body() -> None:
        config = build_config(data_dir=data_dir, library_dir=library_dir)

        async def operation() -> tuple[Collection, ...]:
            async with AsyncTrackForge(config) as facade:
                return await facade.list_collections()

        collections = asyncio.run(operation())
        if as_json:
            emit_json(collections)
            return
        if not collections:
            typer.echo("No collections.")
            return
        for collection in collections:
            typer.echo(
                f"{collection.id}  {collection.kind.value:<8} "
                f"{collection.title}  ({len(collection.items)} items, "
                f"{collection.tag_basis.value})"
            )

    execute(as_json, body)


@collection_app.command("show")
def collection_show_command(
    collection_id: Annotated[str, typer.Argument(help="Collection id.")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
    data_dir: Annotated[str | None, typer.Option("--data-dir")] = None,
    library_dir: Annotated[str | None, typer.Option("--library-dir")] = None,
) -> None:
    """Show a persisted collection and per-item statuses."""

    def body() -> None:
        config = build_config(data_dir=data_dir, library_dir=library_dir)

        async def operation() -> Collection:
            async with AsyncTrackForge(config) as facade:
                return await facade.get_collection(collection_id)

        collection = asyncio.run(operation())
        if as_json:
            emit_json(collection)
        else:
            print_collection(collection)

    execute(as_json, body)


__all__ = [
    "collection_app",
    "collection_mode_options",
    "is_collection_input",
    "make_collection_options",
]

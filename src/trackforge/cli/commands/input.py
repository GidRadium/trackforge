"""Default ``tfg URL`` / ``tfg "query"`` command.

The command inspects a URL, asks the user to pick collection items and one
download variant per item, and then downloads into the L2 staging area.  Text
input is wired to a deliberately explicit stub until search is implemented.
"""

# Typer uses call expressions in argument defaults by design.
# pyright: standard

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import typer

from trackforge.acquire.config import AppConfig, load_config
from trackforge.acquire.errors import AcquireError, CancelledError
from trackforge.acquire.models import (
    DownloadOptions,
    DownloadRequest,
    DownloadResult,
    ItemSelection,
    MediaItem,
    SourceInfo,
    SourceKind,
)
from trackforge.acquire.planner import DownloadPlan, YtDlpPlanner
from trackforge.acquire.progress import (
    CancellationToken,
    NullProgressSink,
    ProgressSink,
)
from trackforge.acquire.redaction import redact_text
from trackforge.acquire.registry import default_registry
from trackforge.acquire.serialization import source_info_to_dict
from trackforge.acquire.service import AcquisitionService

from ..common import (
    EXIT_CANCELLED,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    build_overrides,
    print_error,
    stdin_is_tty,
    usage_error,
)
from ..progress import HumanProgressSink, JsonlProgressSink, StatusLine
from ..selection import (
    SelectionError,
    SelectionRequired,
    SelectionSpec,
    confirm_playlist,
    render_item_table,
    render_variant_table,
    select_items,
    select_variant,
    selection_needs_resolution,
)
from .search import search_not_implemented

_LOGGER = logging.getLogger("trackforge.acquire")

INPUT_RESULT_SCHEMA_VERSION = 1


def run_input(
    target: str = typer.Argument(
        ...,
        help="URL to inspect and download, or text to search for.",
    ),
    variant: str | None = typer.Option(
        None,
        "--variant",
        help=(
            "Variant id or alias: recommended, recommended:audio, "
            "recommended:video, best, or a resolution such as 1080p."
        ),
    ),
    audio: str | None = typer.Option(
        None,
        "--audio",
        help="Audio shortcut: 'recommended' or an explicit variant id.",
    ),
    video: str | None = typer.Option(
        None,
        "--video",
        help="Video shortcut: 'recommended' or an explicit variant id.",
    ),
    pick: int | None = typer.Option(
        None,
        "--pick",
        min=1,
        help="Text-search result index (reserved for the search checkpoint).",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Use loader recommendations instead of asking.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Print the final result as JSON on stdout.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Resolve and plan selections without downloading.",
    ),
    events: str | None = typer.Option(
        None,
        "--events",
        help="Progress event stream; the only supported value is 'jsonl'.",
    ),
    source: str | None = typer.Option(
        None,
        "--source",
        "-s",
        help="Force a loader by name or profile key (youtube, yt_dlp, ...).",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        help="Explicit config file; user config still has lower precedence.",
    ),
    cookies: Path | None = typer.Option(  # noqa: B008
        None,
        "--cookies",
        help="One-shot cookies file override for this run.",
    ),
    proxy: str | None = typer.Option(
        None,
        "--proxy",
        help="One-shot proxy override (URL, 'env' or 'none').",
    ),
    flat: bool = typer.Option(
        False,
        "--flat/--no-flat",
        help="List collection entries without resolving their formats.",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        min=1,
        help="Maximum number of collection items to list.",
    ),
    out: Path | None = typer.Option(  # noqa: B008
        None,
        "--out",
        "--output-dir",
        help=(
            "Staging root. Defaults to general.output_dir from the config, "
            "or the current directory."
        ),
    ),
) -> None:
    """Inspect a URL (or query), choose items/variants, then download."""
    raw = target.strip()
    if not raw:
        usage_error("input must not be empty")
    _validate_options(
        raw=raw,
        variant=variant,
        audio=audio,
        video=video,
        pick=pick,
        events=events,
    )

    if not _looks_like_url(raw):
        if _has_uri_scheme(raw):
            usage_error("expected an absolute http(s) URL")
        search_not_implemented()

    url = _validate_url(raw)
    spec = SelectionSpec(
        variant=variant,
        audio=audio,
        video=video,
        yes=yes,
        pick=pick,
    )
    status = (
        None
        if json_output or events == "jsonl"
        else StatusLine()
    )
    if status is not None:
        status.update("[resolve] inspecting source...")

    try:
        config = _load_config(
            config_path,
            cookies=cookies,
            proxy=proxy,
            source=source,
        )
        registry = default_registry(config)
        service = AcquisitionService(
            config=config,
            registry=registry,
            logger=_LOGGER,
        )
        source_info = service.inspect(
            url,
            flat=flat,
            limit=limit,
            source=source,
        )
    except BrokenPipeError:
        if status is not None:
            status.clear()
        raise typer.Exit(code=EXIT_OK) from None
    except (typer.Abort, KeyboardInterrupt):
        if status is not None:
            status.clear()
        typer.echo("cancelled", err=True)
        raise typer.Exit(code=EXIT_CANCELLED) from None
    except (AcquireError, OSError, ValueError) as exc:
        if status is not None:
            status.finish("[resolve] failed")
        print_error(exc)
        raise typer.Exit(code=EXIT_ERROR) from exc

    if status is not None:
        status.clear()
    interactive = stdin_is_tty()
    def ask(prompt: str) -> str:
        return _ask(prompt, err=json_output)

    def echo(message: str) -> None:
        _echo(message, err=json_output)

    try:
        source_info, selected = _select(
            service,
            source_info,
            spec,
            interactive=interactive,
            ask=ask,
            echo=echo,
            status=status,
        )
        options = _download_options(config, out)
        chosen: list[ItemSelection] = []
        for index, item in enumerate(selected, start=1):
            if len(selected) > 1:
                echo(f"\nItem {index}/{len(selected)}:")
            variant = _choose_one(
                item,
                spec,
                interactive,
                ask=ask,
                echo=echo,
            )
            chosen.append(
                ItemSelection(item_id=item.id, variant_id=variant.id)
            )
        selections = tuple(chosen)
        if (
            source_info.kind is SourceKind.COLLECTION
            and len(selected) > 1
            and interactive
            and not spec.yes
        ):
            confirm_playlist(len(selected), ask=ask, echo=echo)
        request = DownloadRequest(
            source_info=source_info,
            selections=selections,
            options=options,
        )
        plan = YtDlpPlanner().plan(
            request,
            job_id="dry-run" if dry_run else "cli",
        )
    except SelectionRequired as exc:
        if status is not None:
            status.clear()
        _report_selection_required(exc, json_output=json_output)
        raise typer.Exit(code=EXIT_USAGE) from exc
    except SelectionError as exc:
        if status is not None:
            status.finish()
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=EXIT_USAGE) from exc
    except (typer.Abort, KeyboardInterrupt):
        if status is not None:
            status.clear()
        typer.echo("cancelled", err=True)
        raise typer.Exit(code=EXIT_CANCELLED) from None
    except CancelledError:
        if status is not None:
            status.clear()
        typer.echo("cancelled", err=True)
        raise typer.Exit(code=EXIT_CANCELLED) from None
    except (AcquireError, OSError, ValueError) as exc:
        if status is not None:
            status.finish()
        print_error(exc)
        raise typer.Exit(code=EXIT_ERROR) from exc

    if dry_run:
        if json_output:
            typer.echo(
                json.dumps(
                    _result_document(
                        raw=raw,
                        source=source_info,
                        selections=selections,
                        plan=plan,
                        result=None,
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            _render_plan(plan)
        return

    if status is not None:
        status.finish("[download] starting...")
    progress = _progress_sink(
        json_output=json_output,
        events=events,
        status=status,
    )
    cancel = CancellationToken()
    try:
        result = service.download(
            request,
            progress=progress,
            cancel=cancel,
        )
    except BrokenPipeError:
        if status is not None:
            status.clear()
        raise typer.Exit(code=EXIT_OK) from None
    except CancelledError:
        if status is not None:
            status.clear()
        typer.echo("cancelled", err=True)
        raise typer.Exit(code=EXIT_CANCELLED) from None
    except (typer.Abort, KeyboardInterrupt):
        cancel.cancel()
        if status is not None:
            status.clear()
        typer.echo("cancelled", err=True)
        raise typer.Exit(code=EXIT_CANCELLED) from None
    except (AcquireError, OSError, ValueError) as exc:
        if status is not None:
            status.finish()
        print_error(exc)
        raise typer.Exit(code=EXIT_ERROR) from exc

    if status is not None:
        status.finish()
    if json_output:
        typer.echo(
            json.dumps(
                _result_document(
                    raw=raw,
                    source=source_info,
                    selections=selections,
                    plan=plan,
                    result=result,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _render_result(result)
    if result.failures:
        raise typer.Exit(code=EXIT_ERROR)


def _select(
    service: AcquisitionService,
    source_info: SourceInfo,
    spec: SelectionSpec,
    *,
    interactive: bool,
    ask,
    echo,
    status: StatusLine | None = None,
) -> tuple[SourceInfo, tuple[MediaItem, ...]]:
    selected = select_items(
        source_info,
        spec,
        interactive=interactive,
        ask=ask,
        echo=echo,
    )
    source_info, selected = _resolve_selected(
        service,
        source_info,
        selected,
        status=status,
    )
    return source_info, selected


def _choose_one(
    item: MediaItem,
    spec: SelectionSpec,
    interactive: bool,
    *,
    ask,
    echo,
):
    return select_variant(
        item,
        spec,
        interactive=interactive,
        ask=ask,
        echo=echo,
    )


def _resolve_selected(
    service: AcquisitionService,
    source_info: SourceInfo,
    selected: Sequence[MediaItem],
    *,
    status: StatusLine | None = None,
) -> tuple[SourceInfo, tuple[MediaItem, ...]]:
    """Resolve flat collection entries before selecting their variants."""
    by_id = {item.id: item for item in source_info.items}
    for item in selected:
        if not selection_needs_resolution(item):
            continue
        if not item.page_url:
            raise SelectionError(
                f"item {item.id!r} has no page URL to resolve"
            )
        if status is not None:
            status.update(f"[resolve] item {item.title or item.id}...")
        resolved = service.inspect(
            item.page_url,
            flat=False,
            source=source_info.source,
        )
        if status is not None:
            status.clear()
        if not resolved.items:
            raise SelectionError(
                f"item {item.id!r} could not be resolved"
            )
        by_id[item.id] = resolved.items[0]
    source = replace(
        source_info,
        items=tuple(by_id[item.id] for item in source_info.items),
    )
    return source, tuple(by_id[item.id] for item in selected)


def _download_options(config: AppConfig, out: Path | None) -> DownloadOptions:
    target_dir = out or config.general.output_dir or Path.cwd()
    return DownloadOptions(
        target_dir=target_dir,
        write_thumbnail=config.download.write_thumbnail,
        write_info_json=config.download.write_info_json,
        collision=config.download.collision,
    )


def _progress_sink(
    *,
    json_output: bool,
    events: str | None,
    status: StatusLine | None = None,
) -> ProgressSink:
    if events == "jsonl":
        return JsonlProgressSink()
    if json_output:
        return NullProgressSink()
    return HumanProgressSink(status)


def _result_document(
    *,
    raw: str,
    source: SourceInfo,
    selections: Iterable[ItemSelection],
    plan: DownloadPlan,
    result: DownloadResult | None,
) -> dict[str, object]:
    return {
        "schema_version": INPUT_RESULT_SCHEMA_VERSION,
        "input": {
            "kind": "url",
            "raw": redact_text(raw),
        },
        "dry_run": result is None,
        "source": source_info_to_dict(source),
        "selections": [
            {
                "item_id": selection.item_id,
                "variant_id": selection.variant_id,
            }
            for selection in selections
        ],
        "plan": [
            {
                "item_id": item_plan.item.id,
                "variant_id": item_plan.variant.id,
                "selector": item_plan.selector,
                "output_stem": item_plan.output_stem,
            }
            for item_plan in plan.items
        ],
        "result": None if result is None else _download_result(result),
    }


def _download_result(result: DownloadResult) -> dict[str, object]:
    return {
        "files": [
            {
                "item_id": downloaded.item_id,
                "path": str(downloaded.path),
                "sidecars": [str(path) for path in downloaded.sidecars],
                "warnings": [
                    redact_text(warning) for warning in downloaded.warnings
                ],
            }
            for downloaded in result.files
        ],
        "failures": [
            {
                "item_id": failure.item_id,
                "message": redact_text(failure.message),
                "retryable": failure.retryable,
            }
            for failure in result.failures
        ],
        "warnings": [redact_text(warning) for warning in result.warnings],
    }


def _render_plan(plan: DownloadPlan) -> None:
    typer.echo("Dry run: no files will be downloaded.")
    for item_plan in plan.items:
        typer.echo(
            f"  - {item_plan.item.id}: {item_plan.variant.id} "
            f"-> {item_plan.output_stem} [{item_plan.selector}]"
        )


def _render_result(result: DownloadResult) -> None:
    if result.files:
        typer.echo(f"Downloaded {len(result.files)} file(s):")
        for downloaded in result.files:
            typer.echo(f"  - {downloaded.path} ({downloaded.item_id})")
            for sidecar in downloaded.sidecars:
                typer.echo(f"      sidecar: {sidecar}")
            for warning in downloaded.warnings:
                typer.echo(f"      warning: {redact_text(warning)}")
    else:
        typer.echo("No files downloaded.")
    if result.failures:
        typer.echo("Failures:")
        for failure in result.failures:
            retry = " (retryable)" if failure.retryable else ""
            typer.echo(
                f"  - {failure.item_id}: {redact_text(failure.message)}{retry}"
            )
    for warning in result.warnings:
        typer.echo(f"Warning: {redact_text(warning)}")


def _report_selection_required(
    exc: SelectionRequired,
    *,
    json_output: bool,
) -> None:
    to_stderr = json_output
    typer.echo(f"selection required: {exc}", err=to_stderr)

    def echo(line: str) -> None:
        typer.echo(line, err=to_stderr)

    if (
        exc.source.kind is SourceKind.COLLECTION
        and len(exc.source.items) > 1
    ):
        render_item_table(exc.source, echo)
        return
    items = exc.items or exc.source.items
    for item in items:
        render_variant_table(item, echo)


def _ask(prompt: str, *, err: bool = False) -> str:
    try:
        return str(
            typer.prompt(
                prompt,
                default="",
                prompt_suffix="",
                show_default=False,
                err=err,
            )
        )
    except EOFError as exc:
        raise CancelledError("input closed") from exc


def _echo(message: str, *, err: bool = False) -> None:
    typer.echo(message, err=err)


def _looks_like_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _has_uri_scheme(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return bool(parsed.scheme) and "://" in value


def _validate_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        usage_error(f"invalid URL: {exc}")
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        usage_error("expected an absolute http(s) URL")
    return value


def _validate_options(
    *,
    raw: str,
    variant: str | None,
    audio: str | None,
    video: str | None,
    pick: int | None,
    events: str | None,
) -> None:
    if events is not None and events != "jsonl":
        usage_error("--events only supports 'jsonl'")
    selected = [
        name
        for name, value in (
            ("--variant", variant),
            ("--audio", audio),
            ("--video", video),
        )
        if value is not None
    ]
    if len(selected) > 1:
        usage_error(
            f"use only one of {', '.join(selected)}"
        )
    if pick is not None and _looks_like_url(raw):
        usage_error("--pick is only valid for text search, not for a URL")


def _load_config(
    config_path: Path | None,
    *,
    cookies: Path | None,
    proxy: str | None,
    source: str | None,
) -> AppConfig:
    base_config = load_config(config_path)
    overrides = build_overrides(
        cookies=cookies,
        proxy=proxy,
        source=source,
        source_names=base_config.sources,
    )
    return (
        load_config(config_path, overrides=overrides)
        if overrides
        else base_config
    )


__all__ = ["INPUT_RESULT_SCHEMA_VERSION", "run_input"]

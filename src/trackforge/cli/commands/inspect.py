"""Public ``tfg inspect`` command.

The command only resolves metadata: it never asks questions and never
downloads media.  ``--json`` emits the frozen ``SourceInfo`` v1 schema; the
default output is a compact human-readable summary.  Exit codes are stable:
``0`` success, ``1`` acquisition/configuration failure, ``2`` invalid
arguments.
"""

# Typer uses call expressions in argument defaults by design.
# pyright: standard

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlsplit

import typer

from trackforge.acquire.config import load_config
from trackforge.acquire.errors import AcquireError
from trackforge.acquire.models import (
    DownloadVariant,
    MediaItem,
    SourceInfo,
    StreamRole,
)
from trackforge.acquire.redaction import redact_text
from trackforge.acquire.registry import default_registry
from trackforge.acquire.serialization import source_info_to_json
from trackforge.acquire.service import AcquisitionService

from ..common import (
    EXIT_ERROR,
    EXIT_OK,
    build_overrides,
    print_error,
    usage_error,
)

_LOGGER = logging.getLogger("trackforge.acquire")

# Kept as module-level aliases for compatibility with existing CLI tests.
_overrides = build_overrides
_print_error = print_error
_usage_error = usage_error


def inspect_url(
    url: str = typer.Argument(
        ...,
        help="Absolute http(s) URL to inspect.",
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
        help="Maximum number of collection items to return.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Print the frozen SourceInfo v1 JSON object.",
    ),
) -> None:
    """Inspect URL metadata without downloading media."""
    target = _validate_url(url)

    try:
        # Load once to discover configured source sections, then apply the
        # one-shot CLI overrides to every section.  A specific section (for
        # example ``sources.soundcloud.proxy``) must not silently win over an
        # explicit ``--proxy``/``--cookies`` from the command line.
        base_config = load_config(config_path)
        overrides = _overrides(
            cookies=cookies,
            proxy=proxy,
            source=source,
            source_names=base_config.sources,
        )
        config = (
            load_config(config_path, overrides=overrides)
            if overrides
            else base_config
        )
        registry = default_registry(config)
        service = AcquisitionService(
            config=config,
            registry=registry,
            logger=_LOGGER,
        )
        source_info = service.inspect(
            target,
            flat=flat,
            limit=limit,
            source=source,
        )
    except BrokenPipeError:
        raise typer.Exit(code=EXIT_OK) from None
    except (AcquireError, OSError, ValueError) as exc:
        _print_error(exc)
        raise typer.Exit(code=EXIT_ERROR) from exc

    try:
        if json_output:
            typer.echo(source_info_to_json(source_info))
        else:
            _render_source(source_info)
    except BrokenPipeError:
        raise typer.Exit(code=EXIT_OK) from None


def _validate_url(value: str) -> str:
    text = value.strip()
    if not text:
        _usage_error("URL must not be empty")
    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        _usage_error(f"invalid URL: {exc}")
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        _usage_error("expected an absolute http(s) URL")
    return text


def _render_source(source: SourceInfo) -> None:
    thumbnail = source.thumbnail
    typer.echo(f"Source:        {source.source}")
    typer.echo(f"Kind:          {source.kind.value}")
    typer.echo(f"Title:         {_dash(source.title)}")
    typer.echo(f"Creator:       {_dash(source.creator)}")
    typer.echo(
        "Thumbnail:     "
        + _thumbnail(
            thumbnail.url if thumbnail else None,
            thumbnail.width if thumbnail else None,
            thumbnail.height if thumbnail else None,
        )
    )
    typer.echo(f"URL:           {redact_text(source.url)}")
    typer.echo(f"Items:         {len(source.items)}")

    for item in source.items:
        typer.echo("")
        _render_item(item)

    if source.warnings:
        typer.echo("")
        for warning in source.warnings:
            typer.echo(f"Warning:       {redact_text(warning)}")


def _render_item(item: MediaItem) -> None:
    position = item.position if item.position is not None else "?"
    thumbnail = item.thumbnail
    typer.echo(f"Item {position}:       {item.id}")
    typer.echo(f"  title:         {_dash(item.title)}")
    page_url = redact_text(item.page_url) if item.page_url else None
    typer.echo(f"  page_url:      {_dash(page_url)}")
    typer.echo(f"  resolution:    {item.resolution.value}")
    typer.echo(
        "  thumbnail:     "
        + _thumbnail(
            thumbnail.url if thumbnail else None,
            thumbnail.width if thumbnail else None,
            thumbnail.height if thumbnail else None,
        )
    )
    typer.echo(
        "  recommendations: "
        f"audio={_dash(item.recommendations.audio)} "
        f"video={_dash(item.recommendations.video)}"
    )
    typer.echo(f"  variants:      {len(item.variants)}")
    for variant in item.variants:
        _render_variant(variant)


def _render_variant(variant: DownloadVariant) -> None:
    height = _variant_height(variant)
    height_text = f"{height}p" if height is not None else "—"
    size_text = _size_label(variant)
    merge = " merge" if variant.requires_merge else ""
    typer.echo(
        f"    - {variant.id}: {variant.label} "
        f"[{variant.kind.value}, {variant.container or '—'}, {height_text}, "
        f"{size_text}{merge}]"
    )


def _variant_height(variant: DownloadVariant) -> int | None:
    heights = [
        stream.height
        for stream in variant.streams
        if stream.height is not None and stream.role is StreamRole.VIDEO
    ]
    return max(heights) if heights else None


def _size_label(variant: DownloadVariant) -> str:
    size = variant.filesize if variant.filesize is not None else variant.filesize_approx
    if size is None:
        return "size ?"
    prefix = "" if variant.filesize is not None else "~"
    return f"{prefix}{size / 1_000_000:.1f} MB"


def _thumbnail(
    url: str | None,
    width: int | None,
    height: int | None,
) -> str:
    if not url:
        return "—"
    dimensions = f"{width}x{height}" if width and height else "?x?"
    return f"{url} ({dimensions})"


def _dash(value: object) -> str:
    if value is None or value == "":
        return "—"
    return str(value)


__all__ = ["inspect_url"]

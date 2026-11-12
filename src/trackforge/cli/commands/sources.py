"""``tfg sources`` — list loaders and their offline capabilities."""

# pyright: standard

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import typer

from trackforge.acquire.config import AppConfig, load_config
from trackforge.acquire.errors import AcquireError
from trackforge.acquire.loaders.base import MediaLoader
from trackforge.acquire.registry import default_registry

from ..common import EXIT_ERROR, print_error

SOURCES_SCHEMA_VERSION = 1


def sources_command(
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        help="Explicit config file; user config still has lower precedence.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Print the loader list as JSON.",
    ),
) -> None:
    """List loaders, their config profiles and their capabilities."""
    try:
        config = load_config(config_path)
        registry = default_registry(config)
        rows = [
            _source_row(loader, config)
            for loader in registry.loaders
        ]
    except (AcquireError, OSError, ValueError) as exc:
        print_error(exc)
        raise typer.Exit(code=EXIT_ERROR) from exc

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "schema_version": SOURCES_SCHEMA_VERSION,
                    "sources": rows,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    typer.echo(f"{'name':<14} {'profile':<14} {'prio':>4}  {'on':<3} {'ready':<5}")
    for row in rows:
        caps = row["capabilities"]
        assert isinstance(caps, dict)
        enabled = "yes" if row["enabled"] else "no"
        ready = "yes" if row["ready"] else "no"
        capabilities = ",".join(
            name for name, available in caps.items() if available
        )
        typer.echo(
            f"{row['name']:<14} {row['profile']:<14} "
            f"{row['priority']:>4}  {enabled:<3} {ready:<5} {capabilities}"
        )


def _source_row(loader: MediaLoader, config: AppConfig) -> dict[str, object]:
    profile = config.profile_for(loader.profile_name())
    capabilities = asdict(loader.capabilities)
    return {
        "name": loader.name,
        "profile": loader.profile_name(),
        "priority": loader.priority,
        "enabled": bool(profile.enabled),
        "ready": bool(loader.is_ready(profile)),
        "capabilities": {
            str(name): bool(value) for name, value in capabilities.items()
        },
    }


__all__ = ["SOURCES_SCHEMA_VERSION", "sources_command"]

"""``trackforge config show|validate``."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from trackforge.config import TrackForgeConfig
from trackforge.interfaces.cli.common import build_config, execute
from trackforge.interfaces.cli.presenters import emit_json

config_app = typer.Typer(
    name="config",
    help="Inspect and validate configuration.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
)


@config_app.command("show")
def config_show(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit one JSON object for scripts."),
    ] = False,
    data_dir: Annotated[str | None, typer.Option("--data-dir")] = None,
    library_dir: Annotated[str | None, typer.Option("--library-dir")] = None,
) -> None:
    """Show the effective configuration."""

    def body() -> None:
        config = build_config(data_dir=data_dir, library_dir=library_dir)
        payload: dict[str, Any] = config.model_dump(mode="json")
        for key in (
            "ytdlp_binary",
            "deno_binary",
            "ytdlp_timeout_metadata",
            "ytdlp_timeout_download",
        ):
            payload.pop(key, None)
        if as_json:
            emit_json(payload)
            return
        for key, value in payload.items():
            typer.echo(f"{key}: {value}")

    execute(as_json, body)


@config_app.command("validate")
def config_validate(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit one JSON object for scripts."),
    ] = False,
    data_dir: Annotated[str | None, typer.Option("--data-dir")] = None,
    library_dir: Annotated[str | None, typer.Option("--library-dir")] = None,
) -> None:
    """Validate the effective configuration without creating directories."""

    def body() -> None:
        build_config(data_dir=data_dir, library_dir=library_dir)
        if as_json:
            emit_json({"valid": True})
            return
        typer.echo("Configuration is valid.")

    execute(as_json, body)


def validate(config: TrackForgeConfig) -> None:
    """Compatibility helper for callers that already loaded configuration."""

    _ = config


__all__ = ["config_app", "config_show", "config_validate"]

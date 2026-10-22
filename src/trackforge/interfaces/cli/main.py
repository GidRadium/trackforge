"""Thin Typer command-line adapter for TrackForge."""

from __future__ import annotations

import json
from typing import Annotated, Any, NoReturn

import typer

from trackforge import __version__
from trackforge.bootstrap.container import AppContext, build_context
from trackforge.config import ConfigError, TrackForgeConfig

app = typer.Typer(
    name="trackforge",
    help="Local-first audio metadata and library tooling.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
)
config_app = typer.Typer(
    name="config",
    help="Inspect and validate configuration.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
)
app.add_typer(config_app, name="config")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"trackforge {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Run a TrackForge command."""


def _load_context() -> AppContext:
    return build_context(TrackForgeConfig.from_file())


def _fail(message: str, exit_code: int) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(exit_code)


def _load_context_or_exit() -> AppContext:
    try:
        return _load_context()
    except ConfigError as exc:
        _fail(str(exc), 2)
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        _fail(f"internal error: {exc}", 5)


def _config_payload(context: AppContext) -> dict[str, Any]:
    return context.config.model_dump(mode="json")


@config_app.command("show")
def config_show(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit one JSON object for scripts."),
    ] = False,
) -> None:
    """Show the effective configuration."""

    context = _load_context_or_exit()
    payload = _config_payload(context)

    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return

    for key, value in payload.items():
        typer.echo(f"{key}: {value}")


@config_app.command("validate")
def config_validate(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit one JSON object for scripts."),
    ] = False,
) -> None:
    """Validate the effective configuration without creating directories."""

    _load_context_or_exit()

    if as_json:
        typer.echo(json.dumps({"valid": True}, sort_keys=True))
        return

    typer.echo("Configuration is valid.")

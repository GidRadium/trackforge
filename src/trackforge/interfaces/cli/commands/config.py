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
            "metadata_enabled",
            "acoustid_api_key",
            "musicbrainz_user_agent",
        ):
            payload.pop(key, None)
        for key in ("ytdlp_proxy", "metadata_proxy", "llm_proxy", "models_proxy"):
            if payload.get(key):
                payload[key] = "<configured>"
        site_proxies = payload.get("ytdlp_site_proxies")
        if isinstance(site_proxies, dict):
            payload["ytdlp_site_proxies"] = {
                str(host): ("<configured>" if proxy else "") for host, proxy in site_proxies.items()
            }
        if as_json:
            emit_json(payload)
            return
        for key, value in payload.items():
            if key == "ytdlp_site_proxies":
                if isinstance(value, dict) and value:
                    for host, proxy in value.items():
                        typer.echo(f"ytdlp_site_proxies.{host}: {proxy or '(direct)'}")
                else:
                    typer.echo("ytdlp_site_proxies: (none)")
                continue
            if key == "ytdlp_cookie_files":
                if isinstance(value, dict) and value:
                    for host, path in value.items():
                        typer.echo(f"ytdlp_cookie_files.{host}: {path or '(disabled)'}")
                else:
                    typer.echo("ytdlp_cookie_files: (none)")
                continue
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

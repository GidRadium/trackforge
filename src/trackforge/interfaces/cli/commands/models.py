"""``trackforge models`` — inspect and explicitly download local models.

Listing never performs network I/O.  ``download`` is the only command that may
fetch weights, and it verifies the configured license/checksum afterwards.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

import typer

from trackforge.application.ports.models import ModelInfo
from trackforge.bootstrap.container import build_catalog_context, build_model_manager
from trackforge.domain.errors import ModelUnavailable
from trackforge.interfaces.cli.common import build_config, execute
from trackforge.interfaces.cli.presenters import emit_json

models_app = typer.Typer(
    name="models",
    help="Inspect local model availability or download configured ASR/Demucs weights.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
)


@models_app.command("list")
def models_list(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit one JSON object for scripts."),
    ] = False,
    data_dir: Annotated[str | None, typer.Option("--data-dir")] = None,
    library_dir: Annotated[str | None, typer.Option("--library-dir")] = None,
) -> None:
    """List configured/registered models, their license, and local status."""

    def body() -> None:
        config = build_config(data_dir=data_dir, library_dir=library_dir)
        models: tuple[ModelInfo, ...] = ()
        try:
            with build_catalog_context(config) as context:
                models = _list_and_persist(context, config)
        except Exception:
            # Listing config-declared models must work before the first
            # catalog write; the registry is an index, not the source of truth.
            manager = build_model_manager(config)
            models = manager.list_models()
        if as_json:
            emit_json({"models": [model.as_dict() for model in models]})
            return
        if not models:
            typer.echo("No local models configured; lexical ranker is always available.")
            return
        for model in models:
            typer.echo(
                f"{model.id}\t{model.version or '-'}\t"
                f"{model.license or 'unlicensed'}\t{model.status.value}"
            )

    execute(as_json, body)


@models_app.command("download")
def models_download(
    target: Annotated[
        str,
        typer.Argument(help="Model kind (asr, demucs) or a configured model id."),
    ] = "asr",
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace an existing unusable download."),
    ] = False,
    offline: Annotated[
        bool,
        typer.Option("--offline", help="Refuse network access (default for analysis)."),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit one JSON object for scripts."),
    ] = False,
    data_dir: Annotated[str | None, typer.Option("--data-dir")] = None,
    library_dir: Annotated[str | None, typer.Option("--library-dir")] = None,
) -> None:
    """Download a configured ASR or Demucs model (explicit network command)."""

    def body() -> None:
        config = build_config(
            data_dir=data_dir,
            library_dir=library_dir,
            offline=offline,
        )
        manager = build_model_manager(config, for_download=True)
        specs = manager.list_models()
        requested = target.strip().casefold()
        selected = next(
            (model for model in specs if model.id.casefold() == requested),
            None,
        )
        if selected is None:
            selected = next(
                (model for model in specs if model.kind.value == requested),
                None,
            )
        if selected is None:
            raise ModelUnavailable(
                f"no configured model matches {target!r}",
                user_action=(
                    "Configure [models.asr] or [models.stems] name/path/license "
                    "in config.toml before downloading."
                ),
                context={"target": target},
            )
        info = manager.download(selected.id, force=force)
        if as_json:
            emit_json({"model": info.as_dict()})
            return
        typer.echo(
            f"Model: {info.id}\nPath: {info.path}\n"
            f"License: {info.license}\nStatus: {info.status.value}"
        )
        if info.status.value == "runtime_missing":
            extra = "stems" if info.kind.value == "demucs" else "asr"
            typer.echo(
                f"Runtime: install the [{extra}] extra to use this model",
                err=True,
            )

    execute(as_json, body)


def _list_and_persist(context: Any, config: Any) -> tuple[ModelInfo, ...]:
    async def _load() -> tuple[ModelInfo, ...]:
        async with context.unit_of_work() as uow:
            registered = tuple(await uow.models.list_models())
            manager = build_model_manager(config, registered=registered)
            current = manager.list_models()
            for model in current:
                await uow.models.upsert_model(model)
            return current

    return asyncio.run(_load())


__all__ = ["models_app"]

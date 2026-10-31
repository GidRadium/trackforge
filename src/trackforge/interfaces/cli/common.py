"""Shared CLI plumbing: configuration overrides, errors, and exit codes."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn, TypeVar

import typer

from trackforge import __version__
from trackforge.application.dto.results import RunOptions
from trackforge.application.ports.publishing import PublishOptions
from trackforge.config import ConfigError, TrackForgeConfig
from trackforge.domain.errors import (
    AcquisitionFailed,
    BudgetExceeded,
    CandidateNotFound,
    CatalogError,
    CollectionTooLarge,
    DomainValidationError,
    InvalidStateTransition,
    MigrationError,
    ModelUnavailable,
    ObservationFailed,
    PluginError,
    ProviderUnavailable,
    PublishFailed,
    RateLimited,
    RecordNotFound,
    RenderFailed,
    ReviewRequired,
    StorageError,
    TrackForgeError,
    UnsupportedMode,
    UnsupportedScope,
    UnsupportedSource,
)
from trackforge.interfaces.cli.presenters import (
    emit_error,
    emit_internal_error,
    emit_json,
)

T = TypeVar("T")

_USER_ERRORS = (
    ConfigError,
    DomainValidationError,
    UnsupportedSource,
    UnsupportedMode,
    UnsupportedScope,
    CandidateNotFound,
    RecordNotFound,
    InvalidStateTransition,
    ReviewRequired,
    BudgetExceeded,
    ModelUnavailable,
    CollectionTooLarge,
)
_PROVIDER_ERRORS = (ProviderUnavailable, RateLimited, AcquisitionFailed)
_INTERNAL_ERRORS = (
    RenderFailed,
    PublishFailed,
    ObservationFailed,
    StorageError,
    CatalogError,
    MigrationError,
    PluginError,
)


def build_config(
    *,
    data_dir: str | Path | None = None,
    library_dir: str | Path | None = None,
    profile: str | None = None,
    mode: str | None = None,
    offline: bool = False,
) -> TrackForgeConfig:
    """Load TOML/env configuration and apply explicit CLI overrides."""

    config = TrackForgeConfig.from_file()
    updates: dict[str, Any] = {}
    if data_dir is not None:
        root = Path(data_dir).expanduser()
        updates["data_dir"] = root
        # An explicit data directory isolates data/cache/state, but not the
        # configuration directory: config.toml, cookies, and .env stay in the
        # user's config dir unless TRACKFORGE_CONFIG_DIR explicitly overrides it.
        if not os.environ.get("TRACKFORGE_CACHE_DIR"):
            updates["cache_dir"] = root / "cache"
        if not os.environ.get("TRACKFORGE_STATE_DIR"):
            updates["state_dir"] = root / "state"
    if library_dir is not None:
        updates["library_dir"] = Path(library_dir).expanduser()
    if profile is not None:
        updates["profile"] = profile
    if mode is not None:
        updates["mode"] = mode
    if offline:
        updates["offline"] = True
    if not updates:
        return config
    return TrackForgeConfig(**{**config.model_dump(), **updates})


def build_run_options(
    config: TrackForgeConfig,
    *,
    profile: str | None = None,
    mode: str | None = None,
    auto: bool = False,
    review: bool = False,
    dry_run: bool = False,
    force: str | None = None,
    enable: str | None = None,
    disable: str | None = None,
    target_format: str | None = None,
    format_id: str | None = None,
    offline: bool = False,
    refresh: bool = False,
    sidecar: bool | None = None,
) -> RunOptions:
    """Translate CLI flags into a validated ``RunOptions`` value."""

    force_steps = tuple(part.strip() for part in (force or "").split(",") if part.strip())
    if auto and review:
        raise DomainValidationError(
            "--auto and --review are mutually exclusive",
            user_action="Choose one review mode.",
        )
    publish = PublishOptions()
    if sidecar is not None:
        publish = replace(publish, sidecar=sidecar)
    return RunOptions(
        profile=profile or config.profile,
        mode=mode or config.mode,
        auto=auto or not review,
        review=review,
        force_steps=force_steps,
        enabled=enable or (),
        disabled=disable or (),
        offline=offline or config.offline,
        refresh=refresh,
        dry_run=dry_run,
        target_format=target_format,  # type: ignore[arg-type]
        format_id=format_id,
        publish=publish,
    )


def execute[T](as_json: bool, operation: Callable[[], T]) -> T:
    """Run a command body and map expected failures to CLI exit codes."""

    try:
        return operation()
    except typer.Exit:
        raise
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except ConfigError as exc:
        payload = {
            "type": "ConfigError",
            "message": str(exc),
            "retryable": False,
            "user_action": "Check the TrackForge configuration.",
            "context": {"key": exc.key} if exc.key else {},
        }
        if as_json:
            emit_json_error(payload)
        else:
            typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    except TrackForgeError as exc:
        emit_error(exc, as_json=as_json)
        raise typer.Exit(exit_code_for(exc)) from None
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        emit_internal_error(str(exc) or type(exc).__name__, as_json=as_json)
        raise typer.Exit(5) from None


def exit_code_for(exc: TrackForgeError) -> int:
    if isinstance(exc, _PROVIDER_ERRORS):
        return 3
    if isinstance(exc, _USER_ERRORS):
        return 2
    if isinstance(exc, _INTERNAL_ERRORS):
        return 5
    return 5


def fail(message: str, *, as_json: bool, exit_code: int = 2) -> NoReturn:
    """Emit a user error without constructing a typed exception."""

    if as_json:
        payload = {
            "type": "UsageError",
            "message": message,
            "retryable": False,
            "user_action": None,
            "context": {},
        }
        emit_json_error(payload)
    else:
        typer.echo(f"error: {message}", err=True)
    raise typer.Exit(exit_code)


def emit_json_error(payload: dict[str, Any]) -> None:
    import json

    typer.echo(
        json.dumps({"ok": False, **payload, "error": payload}, ensure_ascii=False, sort_keys=True),
        err=True,
    )


def version_string() -> str:
    return f"trackforge {__version__}"


__all__ = [
    "build_config",
    "build_run_options",
    "emit_json",
    "execute",
    "exit_code_for",
    "fail",
    "version_string",
]

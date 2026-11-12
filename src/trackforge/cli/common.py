"""Shared helpers for the ``tfg`` command-line interface."""

# pyright: standard

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path
from typing import NoReturn, TextIO

import typer

from trackforge.acquire.redaction import redact_text

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CANCELLED = 130


def usage_error(message: str) -> NoReturn:
    """Print a usage error and exit with the stable usage code."""
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code=EXIT_USAGE)


def print_error(exc: BaseException) -> None:
    """Print a redacted, typed acquisition/operational error."""
    message = redact_text(str(exc))
    typer.echo(f"error: {type(exc).__name__}: {message}", err=True)
    hint = getattr(exc, "hint", None)
    if isinstance(hint, str) and hint:
        typer.echo(f"hint: {redact_text(hint)}", err=True)


def build_overrides(
    *,
    cookies: Path | None,
    proxy: str | None,
    source: str | None,
    source_names: Iterable[str] | None = None,
) -> dict[str, object]:
    """Build one-shot config overrides for every configured source."""
    payload: dict[str, object] = {}
    if cookies is not None:
        payload["cookies"] = str(cookies)
    if proxy is not None:
        payload["proxy"] = proxy
    if not payload:
        return {}

    names = {"default", *(source_names or ())}
    if source is not None:
        profile_key = source
        if profile_key in {"yt-dlp", "yt_dlp"}:
            profile_key = "yt_dlp"
        names.add(profile_key)
    return {"sources": {name: dict(payload) for name in sorted(names)}}


def stdin_is_tty(stream: TextIO | None = None) -> bool:
    """Return whether the current stdin can be used for interactive prompts."""
    candidate = stream if stream is not None else sys.stdin
    isatty = getattr(candidate, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except (OSError, ValueError):
        return False


__all__ = [
    "EXIT_CANCELLED",
    "EXIT_ERROR",
    "EXIT_OK",
    "EXIT_USAGE",
    "build_overrides",
    "print_error",
    "stdin_is_tty",
    "usage_error",
]

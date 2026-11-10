"""``tfg doctor`` command."""

# Typer uses call expressions in argument defaults by design.
# pyright: standard

from __future__ import annotations

import json
from pathlib import Path

import typer

from trackforge.acquire.config import load_config
from trackforge.acquire.doctor import DoctorReport, run_doctor
from trackforge.acquire.errors import AcquireError


def doctor(
    source: str = typer.Option(
        "youtube",
        "--source",
        "-s",
        help="Source profile to inspect (for example youtube, yandex_music).",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        help="Explicit config file; user config still has lower precedence.",
    ),
    cookies: Path | None = typer.Option(  # noqa: B008
        None,
        "--cookies",
        help="One-shot cookies file override for this source.",
    ),
    proxy: str | None = typer.Option(
        None,
        "--proxy",
        help="One-shot proxy override (URL, 'env' or 'none').",
    ),
    check_network: bool = typer.Option(
        False,
        "--check-network",
        help="Perform an opt-in HTTP check against YouTube.",
    ),
    timeout: float = typer.Option(
        10.0,
        "--timeout",
        min=0.1,
        help="Network timeout in seconds.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Print a machine-readable report (secrets are never included).",
    ),
) -> None:
    """Check cookies, proxy, yt-dlp, Deno/EJS and ffmpeg without network by default."""
    overrides: dict[str, object] = {}
    source_override: dict[str, object] = {}
    if cookies is not None:
        source_override["cookies"] = str(cookies)
    if proxy is not None:
        source_override["proxy"] = proxy
    if source_override:
        overrides["sources"] = {source: source_override}

    try:
        config = load_config(config_path, overrides=overrides or None)
        report = run_doctor(
            config,
            source,
            check_network=check_network,
            timeout=timeout,
        )
    except (AcquireError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        _render_report(report)
    if not report.ok:
        raise typer.Exit(code=1)


def _render_report(report: DoctorReport) -> None:
    typer.echo(f"TrackForge doctor — source: {report.source}")
    for check in report.checks:
        label = check.status.value.upper()
        typer.echo(f"[{label:<4}] {check.name}: {check.message}")
    if report.ok:
        suffix = ""
        if report.warnings:
            suffix = f" ({len(report.warnings)} warning(s))"
        typer.echo(f"\nResult: OK{suffix}")
    else:
        typer.echo(f"\nResult: FAILED ({len(report.failed)} failure(s))")

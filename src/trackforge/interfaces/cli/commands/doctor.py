"""``trackforge doctor`` — environment and permission checks."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from trackforge.api import AsyncTrackForge
from trackforge.application.dto.results import DoctorReport
from trackforge.interfaces.cli.common import build_config, execute
from trackforge.interfaces.cli.presenters import emit_json, print_doctor


def doctor_command(
    as_json: Annotated[bool, typer.Option("--json")] = False,
    data_dir: Annotated[str | None, typer.Option("--data-dir")] = None,
    library_dir: Annotated[str | None, typer.Option("--library-dir")] = None,
) -> None:
    """Check Python, ffmpeg/ffprobe, mutagen, permissions, schema, and settings."""

    def body() -> None:
        config = build_config(data_dir=data_dir, library_dir=library_dir)

        async def operation() -> DoctorReport:
            facade = AsyncTrackForge(config)
            try:
                return await facade.doctor()
            finally:
                await facade.close()

        report = asyncio.run(operation())
        if as_json:
            emit_json(report)
        else:
            print_doctor(report)
        if report.critical:
            raise typer.Exit(2)

    execute(as_json, body)


__all__ = ["doctor_command"]

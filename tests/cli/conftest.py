"""Fixtures and helpers for CLI tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import Result
from typer.testing import CliRunner

from trackforge.interfaces.cli.main import app

runner = CliRunner()


@pytest.fixture
def cli_dirs(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "data"
    library = tmp_path / "library"
    return data, library


def invoke(*args: str) -> Result:
    return runner.invoke(app, list(args))


def cli_paths(data: Path, library: Path) -> tuple[str, ...]:
    return ("--data-dir", str(data), "--library-dir", str(library))


def parse_json(result: Result, stream: str = "stdout") -> dict[str, Any]:
    text = result.stdout if stream == "stdout" else result.stderr
    payload = json.loads(text)
    assert isinstance(payload, dict)
    return payload


__all__ = ["cli_paths", "invoke", "parse_json", "runner"]

"""Tests for the ``tfg info`` command."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from trackforge.cli import app

runner = CliRunner()


def test_info_human(media_dir: Path) -> None:
    result = runner.invoke(app, ["info", str(media_dir / "sample.mp3")])

    assert result.exit_code == 0
    assert "MP3 Title" in result.output
    assert "MP3 Album" in result.output
    assert "audio" in result.output


def test_info_json(media_dir: Path) -> None:
    result = runner.invoke(app, ["info", "--json", str(media_dir / "sample.mp3")])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["title"] == "MP3 Title"
    assert payload["audio"]["album"] == "MP3 Album"
    assert payload["audio"]["track_number"] == 3
    assert payload["artwork"][0]["mime"] == "image/jpeg"


def test_info_video(media_dir: Path) -> None:
    result = runner.invoke(app, ["info", "--json", str(media_dir / "sample.mp4")])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["video"]["show"] == "Show Name"
    assert payload["video"]["season"] == 2


def test_info_unsupported(tmp_path: Path) -> None:
    path = tmp_path / "video.mkv"
    path.write_bytes(b"\x1aE\xdf\xa3" + b"matroska")

    result = runner.invoke(app, ["info", str(path)])

    assert result.exit_code == 1
    assert "error:" in result.output


def test_info_help() -> None:
    result = runner.invoke(app, ["info", "--help"])

    assert result.exit_code == 0
    assert "--json" in result.output

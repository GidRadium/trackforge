"""Tests for the ``tfg info`` command."""

from __future__ import annotations

import json
import shutil
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


def test_set_mp3(tmp_path: Path, media_dir: Path) -> None:
    target = tmp_path / "set.mp3"
    shutil.copy2(media_dir / "sample.mp3", target)

    result = runner.invoke(
        app,
        [
            "set",
            str(target),
            "--title",
            "CLI Title",
            "--artist",
            "CLI Artist",
            "--album",
            "CLI Album",
            "--track",
            "5/10",
            "--original-date",
            "2010",
            "--mood",
            "Energetic",
            "--work",
            "CLI Work",
            "--release-country",
            "US",
        ],
    )

    assert result.exit_code == 0, result.output
    from trackforge.metadata import MediaFile

    media = MediaFile.open(target)
    assert media.title == "CLI Title"
    assert media.artist == "CLI Artist"
    assert media.audio.album == "CLI Album"
    assert (media.audio.track_number, media.audio.track_total) == (5, 10)
    assert str(media.original_date) == "2010"
    assert media.mood == "Energetic"
    assert media.work == "CLI Work"
    assert media.release_country == "US"


def test_set_id3v23(tmp_path: Path, media_dir: Path) -> None:
    target = tmp_path / "set-v23.mp3"
    shutil.copy2(media_dir / "sample.mp3", target)

    result = runner.invoke(
        app,
        ["set", str(target), "--title", "CLI v2.3", "--id3v23"],
    )

    assert result.exit_code == 0, result.output
    assert target.read_bytes()[3] == 3
    from trackforge.metadata import MediaFile

    assert MediaFile.open(target).title == "CLI v2.3"


def test_set_video(tmp_path: Path, media_dir: Path) -> None:
    target = tmp_path / "set.mp4"
    shutil.copy2(media_dir / "sample.mp4", target)

    result = runner.invoke(
        app,
        [
            "set",
            str(target),
            "--show",
            "CLI Show",
            "--season",
            "2",
            "--episode",
            "3",
        ],
    )

    assert result.exit_code == 0, result.output
    from trackforge.metadata import MediaFile

    media = MediaFile.open(target)
    assert media.video.show == "CLI Show"
    assert (media.video.season, media.video.episode) == (2, 3)


def test_set_strict_avi_rejects_composer(tmp_path: Path, media_dir: Path) -> None:
    target = tmp_path / "set.avi"
    shutil.copy2(media_dir / "sample.avi", target)

    result = runner.invoke(
        app,
        ["set", str(target), "--composer", "Unsupported", "--strict"],
    )

    assert result.exit_code == 1
    assert "error:" in result.output


def test_set_without_fields(tmp_path: Path, media_dir: Path) -> None:
    target = tmp_path / "set-empty.mp3"
    shutil.copy2(media_dir / "sample.mp3", target)

    result = runner.invoke(app, ["set", str(target)])

    assert result.exit_code == 1
    assert "no fields" in result.output


def test_set_help() -> None:
    result = runner.invoke(app, ["set", "--help"])

    assert result.exit_code == 0
    assert "--id3v23" in result.output
    assert "--strict" in result.output


def test_scan_json(media_dir: Path) -> None:
    result = runner.invoke(
        app,
        ["scan", str(media_dir), "--json", "--workers", "2"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    mp3 = next(item for item in payload if item["path"].endswith("sample.mp3"))
    assert mp3["ok"] is True
    assert mp3["title"] == "MP3 Title"
    assert any(item["path"].endswith("sample.mkv") for item in payload)


def test_scan_empty_directory(tmp_path: Path) -> None:
    result = runner.invoke(app, ["scan", str(tmp_path)])

    assert result.exit_code == 1
    assert "no media files" in result.output

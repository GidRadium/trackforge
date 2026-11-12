"""Variant choice and download request construction for ``tfg input``."""

# pyright: standard

from __future__ import annotations

from pathlib import Path

import pytest
from cli_fakes import (
    URL,
    DownloadingFakeLoader,
    install_input,
    make_video_source,
)
from fakes import make_source_info
from typer.testing import CliRunner

from trackforge.acquire.config import AppConfig, DownloadConfig
from trackforge.acquire.models import CollisionPolicy
from trackforge.cli import app

runner = CliRunner()


def invoke(args: list[str]):
    return runner.invoke(app, ["input", *args])


def test_variant_id_reaches_download_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = DownloadingFakeLoader(info=make_video_source())
    install_input(monkeypatch, loader)

    result = invoke(
        [
            URL,
            "--variant",
            "audio-best",
            "--yes",
            "--out",
            str(tmp_path),
        ]
    )

    assert result.exit_code == 0, result.output
    request = loader.download_calls[0]
    assert request.selections[0].variant_id == "audio-best"
    assert request.source_info.items[0].recommendations.audio == "audio-best"


@pytest.mark.parametrize(
    ("option", "expected"),
    [
        ("--audio", "audio-best"),
        ("--video", "1080p-mp4"),
    ],
)
def test_audio_video_recommended_shortcuts(
    option: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = DownloadingFakeLoader(info=make_video_source())
    install_input(monkeypatch, loader)

    result = invoke(
        [URL, option, "recommended", "--yes", "--out", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert loader.download_calls[0].selections[0].variant_id == expected


def test_download_options_come_from_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = DownloadingFakeLoader(info=make_source_info())
    config = AppConfig(
        download=DownloadConfig(
            write_thumbnail=False,
            write_info_json=True,
            collision=CollisionPolicy.SKIP,
        )
    )
    install_input(monkeypatch, loader, config=config)

    result = invoke(
        [
            URL,
            "--variant",
            "audio-best",
            "--out",
            str(tmp_path / "staging"),
        ]
    )

    assert result.exit_code == 0, result.output
    options = loader.download_calls[0].options
    assert options.target_dir == tmp_path / "staging"
    assert options.write_thumbnail is False
    assert options.write_info_json is True
    assert options.collision is CollisionPolicy.SKIP


def test_dry_run_does_not_create_staging(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = DownloadingFakeLoader(info=make_source_info())
    install_input(monkeypatch, loader)
    staging = tmp_path / "staging"

    result = invoke(
        [
            URL,
            "--variant",
            "audio-best",
            "--dry-run",
            "--out",
            str(staging),
        ]
    )

    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert not staging.exists()
    assert loader.download_calls == []

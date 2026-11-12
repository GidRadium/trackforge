"""Top-level dispatch and ``tfg inspect`` preservation tests."""

# pyright: standard

from __future__ import annotations

from pathlib import Path

import json

import pytest
from cli_fakes import URL, DownloadingFakeLoader, install_input
from fakes import make_source_info
from typer.testing import CliRunner

from trackforge.acquire.config import AppConfig
from trackforge.acquire.registry import LoaderRegistry
from trackforge.cli import app
from trackforge.cli.commands import inspect as inspect_command

runner = CliRunner()


def install_inspect(
    monkeypatch: pytest.MonkeyPatch,
    *loaders: DownloadingFakeLoader,
) -> None:
    config = AppConfig()

    def fake_load_config(*args: object, **kwargs: object) -> AppConfig:
        del args, kwargs
        return config

    monkeypatch.setattr(inspect_command, "load_config", fake_load_config)
    monkeypatch.setattr(
        inspect_command,
        "default_registry",
        lambda loaded: LoaderRegistry(loaders, loaded),
    )


def test_inspect_remains_data_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = DownloadingFakeLoader(info=make_source_info())
    install_inspect(monkeypatch, loader)

    result = runner.invoke(app, ["inspect", URL, "--json"])

    assert result.exit_code == 0, result.output
    assert loader.download_calls == []
    assert '"schema_version": 2' in result.stdout


def test_top_level_url_routes_to_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = DownloadingFakeLoader(info=make_source_info())
    install_input(monkeypatch, loader)

    result = runner.invoke(
        app,
        [URL, "--variant", "audio-best", "--out", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert len(loader.download_calls) == 1


@pytest.mark.parametrize("command", ["input", "i"])
def test_input_aliases_are_equivalent(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = DownloadingFakeLoader(info=make_source_info())
    install_input(monkeypatch, loader)

    result = runner.invoke(
        app,
        [command, URL, "--variant", "audio-best", "--out", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert len(loader.download_calls) == 1


def test_registered_subcommands_have_priority() -> None:
    result = runner.invoke(app, ["info", "--help"])

    assert result.exit_code == 0
    assert "Show normalized metadata for a media file" in result.output
    assert "URL to inspect and download" not in result.output


def test_sources_lists_loaders_without_network() -> None:
    result = runner.invoke(app, ["sources", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    names = [source["name"] for source in payload["sources"]]
    assert "youtube" in names
    assert "yt-dlp" in names
    assert payload["schema_version"] == 1


def test_options_before_url_still_route_to_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    loader = DownloadingFakeLoader(info=make_source_info())
    install_input(monkeypatch, loader)

    result = runner.invoke(
        app,
        [
            "--variant",
            "audio-best",
            "--out",
            str(tmp_path),
            URL,
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(loader.download_calls) == 1


def test_root_help_is_not_shadowed() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Commands" in result.output
    assert "info" in result.output

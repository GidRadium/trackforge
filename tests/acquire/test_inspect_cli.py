"""Tests for the public ``tfg inspect`` CLI (L1.5)."""

# pyright: standard

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fakes import FakeLoader, make_source_info
from typer.testing import CliRunner

from trackforge.acquire.config import AppConfig
from trackforge.acquire.errors import ConfigError
from trackforge.acquire.models import ResolutionState
from trackforge.acquire.registry import LoaderRegistry
from trackforge.cli import app
from trackforge.cli.commands import inspect as inspect_command

runner = CliRunner()
URL = "https://example.test/media/1"


def install_fake_command(
    monkeypatch: pytest.MonkeyPatch,
    *loaders: FakeLoader,
    config: AppConfig | None = None,
) -> None:
    app_config = config or AppConfig()

    def fake_load_config(*args: object, **kwargs: object) -> AppConfig:
        del args, kwargs
        return app_config

    def fake_registry(loaded: AppConfig) -> LoaderRegistry:
        return LoaderRegistry(loaders, loaded)

    monkeypatch.setattr(inspect_command, "load_config", fake_load_config)
    monkeypatch.setattr(inspect_command, "default_registry", fake_registry)


def test_inspect_help_is_public() -> None:
    result = runner.invoke(app, ["inspect", "--help"])

    assert result.exit_code == 0
    for option in ("--json", "--flat", "--limit", "--source"):
        assert option in result.output
    assert "TEMPORARY" not in result.output
    assert "--raw" not in result.output


def test_inspect_json(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = FakeLoader(name="fake", info=make_source_info())
    install_fake_command(monkeypatch, loader)

    result = runner.invoke(app, ["inspect", URL, "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == 2
    assert payload["kind"] == "single"
    assert payload["title"] == "Fake media"
    assert payload["items"][0]["variants"][0]["id"] == "audio-best"
    assert payload["items"][0]["recommendations"]["audio"] == "audio-best"


def test_inspect_human_output(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = FakeLoader(name="fake", info=make_source_info())
    install_fake_command(monkeypatch, loader)

    result = runner.invoke(app, ["inspect", URL])

    assert result.exit_code == 0, result.output
    assert "Source:" in result.output
    assert "Kind:" in result.output
    assert "Title:" in result.output
    assert "Creator:" in result.output
    assert "Thumbnail:" in result.output
    assert "variants:" in result.output
    assert "audio-best" in result.output


def test_inspect_flat_limit_and_source_are_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = FakeLoader(name="first", priority=10, supports=False)
    second = FakeLoader(name="second", priority=20)
    install_fake_command(monkeypatch, first, second)

    result = runner.invoke(
        app,
        [
            "inspect",
            URL,
            "--source",
            "second",
            "--flat",
            "--limit",
            "3",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    ref, flat, limit = second.info_requests[0]
    assert ref.source == "second"
    assert flat is True
    assert limit == 3
    assert first.info_requests == []


def test_inspect_defaults_to_non_flat(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = FakeLoader(name="fake", info=make_source_info())
    install_fake_command(monkeypatch, loader)

    result = runner.invoke(app, ["inspect", URL, "--json"])

    assert result.exit_code == 0, result.output
    _, flat, limit = loader.info_requests[0]
    assert flat is False
    assert limit is None


def test_inspect_flat_collection_has_empty_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = make_source_info()
    item = replace(
        info.items[0],
        resolution=ResolutionState.FLAT,
        variants=(),
    )
    loader = FakeLoader(name="fake", info=replace(info, items=(item,)))
    install_fake_command(monkeypatch, loader)

    result = runner.invoke(app, ["inspect", URL, "--flat", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["items"][0]["resolution"] == "flat"
    assert payload["items"][0]["variants"] == []


def test_inspect_typed_error_returns_exit_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = FakeLoader(name="fake", supports=False)
    install_fake_command(monkeypatch, loader)

    result = runner.invoke(app, ["inspect", URL])

    assert result.exit_code == 1
    assert "error" in result.output
    assert "UnsupportedUrl" in result.output
    assert "hint:" in result.output


def test_inspect_config_error_returns_exit_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_config(*args: object, **kwargs: object) -> AppConfig:
        del args, kwargs
        raise ConfigError("broken config")

    monkeypatch.setattr(inspect_command, "load_config", broken_config)
    monkeypatch.setattr(inspect_command, "default_registry", lambda c: LoaderRegistry([], c))

    result = runner.invoke(app, ["inspect", URL])

    assert result.exit_code == 1
    assert "ConfigError" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["inspect", "not-a-url"],
        ["inspect", "ftp://example.test/media"],
        ["inspect", URL, "--limit", "0"],
        ["inspect"],
        ["inspect", URL, "https://example.test/media/2"],
    ],
)
def test_inspect_invalid_arguments_exit_two(
    args: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(*args: object, **kwargs: object) -> AppConfig:
        del args, kwargs
        raise AssertionError("config must not be loaded for invalid arguments")

    monkeypatch.setattr(inspect_command, "load_config", fail_if_called)
    monkeypatch.setattr(inspect_command, "default_registry", lambda c: LoaderRegistry([], c))

    result = runner.invoke(app, args)

    assert result.exit_code == 2, result.output
    assert "error" in result.output or "Usage" in result.output


def test_inspect_cookies_and_proxy_overrides_reach_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = FakeLoader(name="youtube", info=make_source_info())
    captured: dict[str, object] = {}

    def fake_load_config(*args: object, **kwargs: object) -> AppConfig:
        del args
        captured.update(kwargs)
        return AppConfig()

    monkeypatch.setattr(inspect_command, "load_config", fake_load_config)
    monkeypatch.setattr(
        inspect_command,
        "default_registry",
        lambda c: LoaderRegistry([loader], c),
    )

    result = runner.invoke(
        app,
        [
            "inspect",
            URL,
            "--source",
            "youtube",
            "--cookies",
            "cookies.txt",
            "--proxy",
            "none",
        ],
    )

    assert result.exit_code == 0, result.output
    overrides = captured["overrides"]
    assert isinstance(overrides, dict)
    assert overrides["sources"]["youtube"]["cookies"] == "cookies.txt"
    assert overrides["sources"]["youtube"]["proxy"] == "none"


def test_one_shot_overrides_apply_to_every_configured_source() -> None:
    overrides = inspect_command._overrides(
        cookies=Path("cookies.txt"),
        proxy="none",
        source=None,
        source_names={"soundcloud": object(), "yandex_music": object()},
    )

    assert isinstance(overrides, dict)
    sources = overrides["sources"]
    assert isinstance(sources, dict)
    assert set(sources) == {"default", "soundcloud", "yandex_music"}
    for name in sources:
        assert sources[name] == {
            "cookies": "cookies.txt",
            "proxy": "none",
        }


def test_one_shot_overrides_include_selected_source() -> None:
    overrides = inspect_command._overrides(
        cookies=None,
        proxy="none",
        source="yt-dlp",
        source_names={"soundcloud": object()},
    )

    assert isinstance(overrides, dict)
    sources = overrides["sources"]
    assert isinstance(sources, dict)
    assert sources["yt_dlp"] == {"proxy": "none"}
    assert sources["soundcloud"] == {"proxy": "none"}

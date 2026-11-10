"""Tests for Yandex Music plugin readiness without touching the network."""

# pyright: standard

from __future__ import annotations

import importlib.util

import pytest

from trackforge.acquire.config import AppConfig, SourceProfile
from trackforge.acquire.errors import MissingPlugin
from trackforge.acquire.loaders import yandex_music
from trackforge.acquire.loaders.yandex_music import YandexMusicLoader
from trackforge.acquire.loaders.ytdlp import YtDlpLoader
from trackforge.acquire.registry import LoaderRegistry
from trackforge.acquire.yandex_plugin import (
    DISTRIBUTION,
    distribution_version,
    import_yandex_plugin,
    plugin_available,
)


def test_plugin_distribution_is_installed() -> None:
    version = distribution_version()
    assert isinstance(version, str)
    assert version


def test_plugin_module_imports() -> None:
    module = import_yandex_plugin()
    assert module.__name__ in {
        "yt_dlp_plugins.extractor.yandex_music_v2",
        "yt_dlp_plugins.extractor.yandex_music",
    }
    assert importlib.util.find_spec("yt_dlp_plugins.extractor.yandex_music_v2")


def test_plugin_available_requires_distribution_and_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "trackforge.acquire.yandex_plugin.distribution_version",
        lambda: None,
    )
    assert plugin_available() is False

    monkeypatch.setattr(
        "trackforge.acquire.yandex_plugin.distribution_version",
        lambda: "2026.9.24",
    )

    def broken_import():
        raise ModuleNotFoundError("missing")

    monkeypatch.setattr(
        "trackforge.acquire.yandex_plugin.import_yandex_plugin",
        broken_import,
    )
    assert plugin_available() is False


def test_is_ready_without_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = YandexMusicLoader()
    monkeypatch.setattr(yandex_music, "plugin_available", lambda: False)

    assert loader.is_ready(SourceProfile(name="yandex_music")) is False
    error = loader.unready_error(SourceProfile(name="yandex_music"))
    assert isinstance(error, MissingPlugin)
    assert DISTRIBUTION in str(error)
    assert "uv add" in (error.hint or "")


def test_is_ready_with_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = YandexMusicLoader()
    monkeypatch.setattr(yandex_music, "plugin_available", lambda: True)

    assert loader.is_ready(SourceProfile(name="yandex_music")) is True
    assert loader.unready_error(SourceProfile(name="yandex_music")) is None


def test_missing_plugin_does_not_fall_through_to_builtin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(yandex_music, "plugin_available", lambda: False)
    registry = LoaderRegistry([YandexMusicLoader(), YtDlpLoader()], AppConfig())

    with pytest.raises(MissingPlugin, match="yt-dlp-yandex-music"):
        registry.resolve("https://music.yandex.ru/album/1/track/2")


def test_required_plugin_is_declared() -> None:
    assert YandexMusicLoader.required_plugins == ("yt-dlp-yandex-music",)

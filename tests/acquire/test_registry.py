"""Tests for loader registry resolution and profile binding."""

# pyright: standard

from __future__ import annotations

import pytest
from fakes import FakeLoader, make_source_info

from trackforge.acquire.config import AppConfig, SourceProfile
from trackforge.acquire.errors import ConfigError, UnsupportedUrl
from trackforge.acquire.loaders.ytdlp import YtDlpLoader
from trackforge.acquire.registry import LoaderRegistry, default_registry


def test_resolve_picks_lowest_priority_supporting_loader() -> None:
    info = make_source_info()
    first = FakeLoader(name="first", priority=10, info=info)
    second = FakeLoader(name="second", priority=20, info=info)
    registry = LoaderRegistry([second, first], AppConfig())

    resolved = registry.resolve("https://example.test/x")
    assert resolved.loader is first


def test_resolve_skips_non_supporting_loader() -> None:
    info = make_source_info()
    first = FakeLoader(name="first", priority=10, supports=False, info=info)
    second = FakeLoader(name="second", priority=20, supports=True, info=info)
    registry = LoaderRegistry([first, second], AppConfig())

    assert registry.resolve("https://example.test/x").loader is second


def test_resolve_binds_merged_profile() -> None:
    config = AppConfig(
        sources={
            "default": SourceProfile(
                name="default",
                retries=7,
                socket_timeout=42.0,
                headers={"X-Default": "1"},
            ),
            "fake": SourceProfile(
                name="fake",
                retries=2,
                proxy="http://proxy.test:8080",
            ),
        }
    )
    loader = FakeLoader(name="fake", info=make_source_info())
    resolved = LoaderRegistry([loader], config).resolve("https://example.test/x")

    assert resolved.profile.name == "fake"
    assert resolved.profile.retries == 2
    assert resolved.profile.socket_timeout == 42.0
    assert resolved.profile.proxy == "http://proxy.test:8080"
    assert resolved.profile.headers == {"X-Default": "1"}


def test_disabled_loader_is_skipped_automatically() -> None:
    info = make_source_info()
    disabled = FakeLoader(name="disabled", priority=1, info=info)
    fallback = FakeLoader(name="fallback", priority=100, info=info)
    config = AppConfig(
        sources={
            "disabled": SourceProfile(name="disabled", enabled=False),
        }
    )
    registry = LoaderRegistry([disabled, fallback], config)

    assert registry.resolve("https://example.test/x").loader is fallback


def test_unready_loader_is_skipped_automatically() -> None:
    info = make_source_info()
    unready = FakeLoader(name="unready", priority=1, ready=False, info=info)
    fallback = FakeLoader(name="fallback", priority=100, info=info)
    registry = LoaderRegistry([unready, fallback], AppConfig())

    resolved = registry.resolve("https://example.test/x")
    assert resolved.loader is fallback


def test_no_supporting_loader_raises_unsupported_url() -> None:
    loader = FakeLoader(name="fake", supports=False)
    registry = LoaderRegistry([loader], AppConfig())
    with pytest.raises(UnsupportedUrl):
        registry.resolve("https://example.test/x")


def test_all_loaders_unready_raises_unsupported_url() -> None:
    loader = FakeLoader(name="fake", ready=False)
    registry = LoaderRegistry([loader], AppConfig())
    with pytest.raises(UnsupportedUrl):
        registry.resolve("https://example.test/x")


def test_source_override_uses_loader_even_if_lower_priority() -> None:
    info = make_source_info()
    preferred = FakeLoader(name="preferred", priority=1, info=info)
    override = FakeLoader(name="override", priority=100, info=info)
    registry = LoaderRegistry([preferred, override], AppConfig())

    resolved = registry.resolve("https://example.test/x", source="override")
    assert resolved.loader is override
    assert resolved.profile.name == "override"


def test_source_override_checks_disabled_and_readiness() -> None:
    loader = FakeLoader(name="fake")
    config = AppConfig(
        sources={"fake": SourceProfile(name="fake", enabled=False)}
    )
    registry = LoaderRegistry([loader], config)
    with pytest.raises(ConfigError, match="disabled"):
        registry.resolve("https://example.test/x", source="fake")

    unready = FakeLoader(name="fake", ready=False)
    registry = LoaderRegistry([unready], AppConfig())
    with pytest.raises(ConfigError, match="not configured"):
        registry.resolve("https://example.test/x", source="fake")


def test_source_override_checks_supports_url() -> None:
    loader = FakeLoader(name="fake", supports=False)
    registry = LoaderRegistry([loader], AppConfig())
    with pytest.raises(UnsupportedUrl):
        registry.resolve("https://example.test/x", source="fake")


def test_source_override_accepts_profile_name() -> None:
    loader = FakeLoader(name="yt-dlp", config_key="yt_dlp")
    registry = LoaderRegistry([loader], AppConfig())
    resolved = registry.resolve("https://example.test/x", source="yt_dlp")
    assert resolved.loader is loader


def test_unknown_source_is_a_config_error() -> None:
    registry = LoaderRegistry([FakeLoader(name="fake")], AppConfig())
    with pytest.raises(ConfigError, match="unknown loader"):
        registry.resolve("https://example.test/x", source="missing")


def test_by_name_and_by_profile_name() -> None:
    loader = FakeLoader(name="yt-dlp", config_key="yt_dlp")
    registry = LoaderRegistry([loader], AppConfig())
    assert registry.by_name("yt-dlp") is loader
    assert registry.by_profile_name("yt_dlp") is loader
    with pytest.raises(ConfigError):
        registry.by_name("yt_dlp")
    with pytest.raises(ConfigError):
        registry.by_profile_name("yt-dlp")


def test_loaders_are_sorted_and_exposed() -> None:
    low = FakeLoader(name="low", priority=1)
    high = FakeLoader(name="high", priority=100)
    registry = LoaderRegistry([high, low], AppConfig())
    assert registry.loaders == (low, high)
    assert registry.config == AppConfig()


def test_default_registry_contains_ytdlp() -> None:
    registry = default_registry()
    assert isinstance(registry.by_name("yt-dlp"), YtDlpLoader)
    resolved = registry.resolve("https://example.test/watch/1")
    assert isinstance(resolved.loader, YtDlpLoader)
    assert resolved.profile.name == "yt_dlp"

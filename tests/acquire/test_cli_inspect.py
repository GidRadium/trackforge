"""Tests for the temporary ``tfg inspect`` debug command."""

# pyright: standard

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fakes import FakeLoader, make_source_info
from typer.testing import CliRunner

from trackforge.acquire.config import AppConfig
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.models import (
    LiveStatus,
    SourceInfo,
    SourceKind,
    SourceRef,
    SubtitleTrack,
    UrlHint,
)
from trackforge.acquire.registry import LoaderRegistry
from trackforge.cli import app
from trackforge.cli.commands import inspect as inspect_command

runner = CliRunner()
URL = "https://example.test/media/1"


def install_fake_command(
    monkeypatch: pytest.MonkeyPatch,
    loader: FakeLoader,
) -> None:
    config = AppConfig()

    def fake_load_config(*args: object, **kwargs: object) -> AppConfig:
        del args, kwargs
        return config

    def fake_registry(_config: AppConfig) -> LoaderRegistry:
        return LoaderRegistry([loader], config)

    monkeypatch.setattr(inspect_command, "load_config", fake_load_config)
    monkeypatch.setattr(inspect_command, "default_registry", fake_registry)


def test_inspect_help() -> None:
    result = runner.invoke(app, ["inspect", "--help"])

    assert result.exit_code == 0
    assert "--raw" in result.output
    assert "--json" in result.output
    assert "TEMPORARY" in result.output


def test_inspect_json(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = FakeLoader(name="fake", info=make_source_info())
    install_fake_command(monkeypatch, loader)

    result = runner.invoke(app, ["inspect", URL, "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == "single"
    assert payload["title"] == "Fake media"
    assert payload["items"][0]["variants"][0]["id"] == "audio-best"
    assert payload["items"][0]["recommendations"]["audio"] == "audio-best"


def test_inspect_human_output(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = FakeLoader(name="fake", info=make_source_info())
    install_fake_command(monkeypatch, loader)

    result = runner.invoke(app, ["inspect", URL, "--extras", "--flat-variants"])

    assert result.exit_code == 0, result.output
    assert "Source:" in result.output
    assert "Fake media" in result.output
    assert "recommendations:" in result.output
    assert "variants:" in result.output
    assert "audio-best" in result.output
    assert "selector:" in result.output


def test_inspect_grouped_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = FakeLoader(name="fake", info=make_source_info())
    install_fake_command(monkeypatch, loader)

    result = runner.invoke(app, ["inspect", URL])

    assert result.exit_code == 0, result.output
    assert "audio-only choices:" in result.output
    assert "audio-best" in result.output
    assert "use --flat-variants" in result.output
    assert "selector:" not in result.output


def test_inspect_subtitles(monkeypatch: pytest.MonkeyPatch) -> None:
    info = make_source_info()
    item = replace(
        info.items[0],
        subtitles=(
            SubtitleTrack(
                language="en",
                ext="vtt",
                name="English",
                url="https://example.test/en.vtt",
            ),
            SubtitleTrack(
                language="de",
                ext="vtt",
                name="German (auto)",
                is_automatic=True,
                url="https://example.test/de.vtt",
            ),
        ),
    )
    info = replace(info, items=(item,))
    loader = FakeLoader(name="fake", info=info)
    install_fake_command(monkeypatch, loader)

    summary = runner.invoke(app, ["inspect", URL])
    assert summary.exit_code == 0, summary.output
    assert "manual 1 tracks / 1 languages" in summary.output
    assert "automatic 1 tracks / 1 languages" in summary.output
    assert "English" not in summary.output

    languages = runner.invoke(app, ["inspect", URL, "--subtitles"])
    assert languages.exit_code == 0, languages.output
    assert "manual languages (1): en" in languages.output
    assert "automatic languages (1): de" in languages.output
    assert "English" not in languages.output

    detailed = runner.invoke(app, ["inspect", URL, "--all-subtitles"])
    assert detailed.exit_code == 0, detailed.output
    assert "English" in detailed.output
    assert "German (auto)" in detailed.output
    assert "auto" in detailed.output


def test_inspect_no_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = FakeLoader(name="fake", info=make_source_info())
    install_fake_command(monkeypatch, loader)

    result = runner.invoke(app, ["inspect", URL, "--no-variants"])

    assert result.exit_code == 0, result.output
    assert "variants:" not in result.output


def test_inspect_multiple_urls_as_json(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = FakeLoader(name="fake", info=make_source_info())
    install_fake_command(monkeypatch, loader)

    result = runner.invoke(
        app,
        ["inspect", URL, "https://example.test/media/2", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) == 2
    assert all(entry["ok"] is True for entry in payload)
    assert payload[0]["data"]["title"] == "Fake media"


def test_inspect_typed_error_and_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = FakeLoader(name="fake", supports=False)
    install_fake_command(monkeypatch, loader)

    result = runner.invoke(app, ["inspect", URL])

    assert result.exit_code == 1
    assert "error" in result.output
    assert "UnsupportedUrl" in result.output
    assert "hint:" in result.output


class RawLoader(FakeLoader):
    def preflight(self, url: str, profile: object) -> None:
        del url, profile

    def _extract(
        self,
        url: str,
        *,
        ctx: object,
        flat: bool = False,
        limit: int | None = None,
    ) -> dict[str, object]:
        del ctx, flat, limit
        return {
            "_type": "video",
            "id": "raw-id",
            "title": "Raw title",
            "webpage_url": url,
            "formats": [],
        }


def test_inspect_raw_info_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    loader = RawLoader(name="fake", info=make_source_info())
    install_fake_command(monkeypatch, loader)

    result = runner.invoke(app, ["inspect", URL, "--raw"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["title"] == "Raw title"
    assert payload["id"] == "raw-id"


def test_inspect_raw_requires_ytdlp_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = FakeLoader(name="fake", info=make_source_info())
    install_fake_command(monkeypatch, loader)

    result = runner.invoke(app, ["inspect", URL, "--raw"])

    assert result.exit_code == 1
    assert "--raw is only supported" in result.output


def test_inspect_cookies_override_is_passed_to_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = FakeLoader(name="fake", info=make_source_info())
    install_fake_command(monkeypatch, loader)
    captured: dict[str, object] = {}

    def fake_load_config(*args: object, **kwargs: object) -> AppConfig:
        captured.update(kwargs)
        return AppConfig()

    monkeypatch.setattr(inspect_command, "load_config", fake_load_config)

    result = runner.invoke(
        app,
        ["inspect", URL, "--cookies", str(Path("cookies.txt")), "--proxy", "none"],
    )

    assert result.exit_code == 0, result.output
    overrides = captured["overrides"]
    assert isinstance(overrides, dict)
    assert overrides["sources"]["youtube"]["cookies"] == "cookies.txt"
    assert overrides["sources"]["youtube"]["proxy"] == "none"


def test_profile_player_client_override() -> None:
    from trackforge.cli.commands.inspect import _profile_with_client_override

    profile = inspect_command.SourceProfile(
        name="youtube",
        extractor_args={
            "youtube": {
                "player_client": ("web", "tv"),
                "player_skip": ("configs",),
            }
        },
    )
    overridden = _profile_with_client_override(
        profile,
        player_client="web_embedded, tv_samsung",
        no_extractor_args=False,
    )
    assert overridden.extractor_args["youtube"]["player_client"] == (
        "web_embedded",
        "tv_samsung",
    )
    assert overridden.extractor_args["youtube"]["player_skip"] == ("configs",)

    default = _profile_with_client_override(
        profile,
        player_client="default",
        no_extractor_args=False,
    )
    assert "player_client" not in default.extractor_args["youtube"]
    assert default.extractor_args["youtube"]["player_skip"] == ("configs",)

    cleared = _profile_with_client_override(
        profile,
        player_client="web",
        no_extractor_args=True,
    )
    assert cleared.extractor_args == {}


def test_inspect_player_client_reaches_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = RawLoader(name="fake", info=make_source_info())
    install_fake_command(monkeypatch, loader)
    observed: dict[str, object] = {}

    def capture(
        url: str,
        *,
        ctx: LoaderContext,
        flat: bool = False,
        limit: int | None = None,
    ) -> dict[str, object]:
        del flat, limit
        profile = ctx.profile
        observed.update(profile.extractor_args)
        return {
            "_type": "video",
            "id": "raw-id",
            "title": "Raw title",
            "webpage_url": url,
            "formats": [],
        }

    monkeypatch.setattr(loader, "_extract", capture)
    result = runner.invoke(
        app,
        ["inspect", URL, "--raw", "--player-client", "web_embedded"],
    )

    assert result.exit_code == 0, result.output
    youtube = observed.get("youtube")
    assert isinstance(youtube, dict)
    assert youtube.get("player_client") == ("web_embedded",)


def test_inspect_marks_live_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    info = make_source_info()
    item = replace(info.items[0], live_status=LiveStatus.LIVE, duration=None)
    info = replace(info, items=(item,))
    loader = FakeLoader(name="fake", info=info)
    install_fake_command(monkeypatch, loader)

    result = runner.invoke(app, ["inspect", URL, "--no-variants"])

    assert result.exit_code == 0, result.output
    assert "live_status:   is_live" in result.output
    assert "LIVE STREAM" in result.output
    assert "duration:      —" in result.output


class CollectionLoader(FakeLoader):
    """Fake YouTube-like loader that reports a collection hint."""

    def __init__(
        self,
        *,
        name: str = "fake",
        info: SourceInfo | None = None,
    ) -> None:
        super().__init__(name=name, info=info)
        self.flat_calls: list[bool | None] = []

    def classify_url(self, url: str) -> UrlHint:
        del url
        return UrlHint.COLLECTION

    def get_info(
        self,
        ref: SourceRef,
        *,
        ctx: LoaderContext,
        flat: bool | None = False,
        limit: int | None = None,
    ) -> SourceInfo:
        self.flat_calls.append(flat)
        return super().get_info(
            ref,
            ctx=ctx,
            flat=flat if flat is not None else False,
            limit=limit,
        )


class UnknownLoader(CollectionLoader):
    """Fake generic loader whose URL shape cannot be classified offline."""

    def classify_url(self, url: str) -> UrlHint:
        del url
        return UrlHint.UNKNOWN


def test_inspect_auto_flattens_collections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = CollectionLoader(
        name="fake",
        info=make_source_info(kind=SourceKind.COLLECTION),
    )
    install_fake_command(monkeypatch, loader)

    result = runner.invoke(app, ["inspect", URL, "--json"])
    assert result.exit_code == 0, result.output
    assert loader.flat_calls == [True]

    result = runner.invoke(app, ["inspect", URL, "--no-flat", "--json"])
    assert result.exit_code == 0, result.output
    assert loader.flat_calls == [True, False]


def test_inspect_unknown_url_uses_loader_auto_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = UnknownLoader(
        name="fake",
        info=make_source_info(kind=SourceKind.COLLECTION),
    )
    install_fake_command(monkeypatch, loader)

    result = runner.invoke(app, ["inspect", URL, "--json"])

    assert result.exit_code == 0, result.output
    assert loader.flat_calls == [None]


def test_resolve_flat_honours_explicit_choice() -> None:
    assert inspect_command._resolve_flat(None, UrlHint.COLLECTION) is True
    assert inspect_command._resolve_flat(None, UrlHint.SINGLE) is False
    assert inspect_command._resolve_flat(None, UrlHint.UNKNOWN) is None
    assert inspect_command._resolve_flat(True, UrlHint.SINGLE) is True
    assert inspect_command._resolve_flat(False, UrlHint.COLLECTION) is False

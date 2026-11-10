"""Yandex ``smart_preview`` detection tests (offline)."""

# pyright: standard

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.loaders import yandex_music
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.loaders.yandex_music import (
    YandexMusicLoader,
    warn_on_smart_preview,
)
from trackforge.acquire.loaders.ytdlp import INFO_WARNINGS_KEY
from trackforge.acquire.loaders.ytdlp_mapper import YtDlpMapper
from trackforge.acquire.models import SourceRef

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def inspect_fixture(
    monkeypatch: pytest.MonkeyPatch,
    info: dict[str, Any],
):
    loader = YandexMusicLoader()
    monkeypatch.setattr(yandex_music, "plugin_available", lambda: True)
    monkeypatch.setattr(
        yandex_music,
        "fetch_image_size",
        lambda *args, **kwargs: None,
    )

    def fake_extract(
        url: str,
        *,
        ctx: LoaderContext,
        flat: bool = False,
        limit: int | None = None,
    ) -> dict[str, Any]:
        del url, ctx, flat, limit
        return info

    monkeypatch.setattr(loader, "_extract", fake_extract)
    ctx = LoaderContext(
        job_id="job-preview",
        profile=SourceProfile(
            name="yandex_music",
            cookies_from_browser=("chrome",),
        ),
    )
    ref = SourceRef(
        "yandex_music",
        str(info["webpage_url"]),
        str(info["id"]),
    )
    return loader.get_info(ref, ctx=ctx)


def test_smart_preview_quality_adds_preview_only_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = load_fixture("yandex_smart_preview.info.json")
    source = inspect_fixture(monkeypatch, info)

    assert source.warnings
    assert any("PreviewOnly" in warning for warning in source.warnings)
    assert any("smart_preview" in warning for warning in source.warnings)


def test_short_duration_is_detected_without_quality_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = copy.deepcopy(load_fixture("yandex_smart_preview.info.json"))
    info.pop("quality")
    source = inspect_fixture(monkeypatch, info)

    assert any("13–30s" in warning for warning in source.warnings)
    assert any("PreviewOnly" in warning for warning in source.warnings)


def test_normal_track_has_no_preview_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = load_fixture("yandex_track.info.json")
    source = inspect_fixture(monkeypatch, info)

    assert source.warnings == ()
    assert source.items[0].duration == pytest.approx(215.0)


def test_warn_on_smart_preview_is_idempotent() -> None:
    info = load_fixture("yandex_smart_preview.info.json")
    source = YtDlpMapper(SourceProfile(name="yandex_music")).map(
        info,
        SourceRef("yandex_music", str(info["webpage_url"]), str(info["id"])),
    )
    once = warn_on_smart_preview(source)
    twice = warn_on_smart_preview(once)

    assert once.warnings
    assert once.warnings == twice.warnings


def test_plugin_preview_warning_is_surfaced() -> None:
    warnings = YandexMusicLoader()._info_warnings(
        {
            INFO_WARNINGS_KEY: (
                "Track 32345678: server served a 'smart_preview' stream",
                "some unrelated extractor chatter",
            )
        }
    )

    assert len(warnings) == 1
    assert warnings[0].startswith("PreviewOnly:")
    assert "smart_preview" in warnings[0]

"""Yandex ``smart_preview`` detection tests (offline)."""

# pyright: standard

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeYoutubeDL, make_source_info

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.download import looks_like_preview
from trackforge.acquire.loaders import yandex_music
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.loaders.yandex_music import (
    YandexMusicLoader,
    warn_on_smart_preview,
)
from trackforge.acquire.loaders.ytdlp import INFO_WARNINGS_KEY
from trackforge.acquire.loaders.ytdlp_mapper import YtDlpMapper
from trackforge.acquire.models import (
    DownloadOptions,
    DownloadRequest,
    ItemSelection,
    SourceRef,
)
from trackforge.acquire.staging import StagingArea

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def inspect_fixture(
    monkeypatch: pytest.MonkeyPatch,
    info: dict[str, Any],
):
    loader = YandexMusicLoader()
    monkeypatch.setattr(yandex_music, "plugin_available", lambda: True)

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


@pytest.fixture(autouse=True)
def reset_download_fake() -> Iterator[None]:
    FakeYoutubeDL.reset()
    yield
    FakeYoutubeDL.reset()


def run_yandex_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    actual: float,
    expected: float | None = 215.0,
    quality: str | None = None,
):
    source = make_source_info(
        source="yandex_music",
        url="https://music.yandex.ru/track/123",
    )
    item = source.items[0]
    extras = dict(item.extras)
    if quality is not None:
        extras["quality"] = quality
    item = replace(item, duration=expected, extras=extras)
    source = replace(source, items=(item,))
    request = DownloadRequest(
        source_info=source,
        selections=(ItemSelection(item.id, item.variants[0].id),),
        options=DownloadOptions(
            target_dir=tmp_path / "staging",
            write_thumbnail=False,
        ),
    )
    loader = YandexMusicLoader()
    monkeypatch.setattr(yandex_music, "plugin_available", lambda: True)
    monkeypatch.setattr(
        loader,
        "_create_youtube_dl",
        lambda options: FakeYoutubeDL(options),
    )
    monkeypatch.setattr(
        "trackforge.acquire.download.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr(
        yandex_music,
        "probe_media_duration",
        lambda path, **kwargs: actual,
    )
    ctx = LoaderContext(
        job_id="job-preview-download",
        profile=SourceProfile(
            name="yandex_music",
            cookies_from_browser=("chrome",),
        ),
        staging=StagingArea(tmp_path / "staging"),
    )
    result = loader.download(request, ctx=ctx)
    return result, tmp_path / "staging" / "job-preview-download"


def test_download_rejects_short_yandex_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, job_dir = run_yandex_download(
        tmp_path,
        monkeypatch,
        actual=20.0,
        expected=215.0,
    )

    assert result.files == ()
    assert len(result.failures) == 1
    assert "Yandex preview" in result.failures[0].message
    assert not any(path.is_file() for path in job_dir.rglob("*"))


def test_download_rejects_smart_preview_quality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, job_dir = run_yandex_download(
        tmp_path,
        monkeypatch,
        actual=215.0,
        expected=215.0,
        quality="smart_preview",
    )

    assert result.files == ()
    assert len(result.failures) == 1
    assert "smart_preview" in result.failures[0].message
    assert not any(path.is_file() for path in job_dir.rglob("*"))


def test_download_accepts_normal_yandex_duration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _job_dir = run_yandex_download(
        tmp_path,
        monkeypatch,
        actual=215.0,
        expected=215.0,
    )

    assert result.failures == ()
    assert len(result.files) == 1
    assert result.files[0].path.exists()


def test_short_but_matching_track_is_not_a_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _job_dir = run_yandex_download(
        tmp_path,
        monkeypatch,
        actual=20.0,
        expected=20.0,
    )

    assert result.failures == ()
    assert len(result.files) == 1


@pytest.mark.parametrize(
    ("actual", "expected", "preview"),
    [
        (20.0, 215.0, True),
        (20.0, None, True),
        (20.0, 20.0, False),
        (215.0, 215.0, False),
        (10.0, 215.0, False),
        (45.0, 215.0, False),
    ],
)
def test_looks_like_preview(
    actual: float,
    expected: float | None,
    preview: bool,
) -> None:
    assert looks_like_preview(actual, expected) is preview

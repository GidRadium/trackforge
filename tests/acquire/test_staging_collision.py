"""Staging names and in-plan collisions: flat first, then ``1/``, ``2/``."""

# pyright: standard

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fakes import FakeYoutubeDL, make_item, make_source_info

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.loaders.ytdlp import YtDlpLoader
from trackforge.acquire.models import (
    DownloadOptions,
    DownloadRequest,
    ItemSelection,
    SourceKind,
)
from trackforge.acquire.planner import YtDlpPlanner, safe_stem
from trackforge.acquire.staging import StagingArea


@pytest.fixture(autouse=True)
def reset_fake() -> Iterator[None]:
    FakeYoutubeDL.reset()
    yield
    FakeYoutubeDL.reset()


def test_same_stem_gets_numeric_subfolder_and_sidecars_follow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = make_item(
        "one",
        title="Same title",
        stem="Same title",
        url="https://example.test/one",
    )
    second = make_item(
        "two",
        title="Same title",
        stem="Same title",
        url="https://example.test/two",
    )
    source = replace(
        make_source_info(),
        kind=SourceKind.COLLECTION,
        items=(first, second),
    )
    request = DownloadRequest(
        source_info=source,
        selections=(
            ItemSelection(first.id, first.variants[0].id),
            ItemSelection(second.id, second.variants[0].id),
        ),
        options=DownloadOptions(
            target_dir=tmp_path / "staging",
            write_thumbnail=True,
        ),
    )
    loader = YtDlpLoader()
    monkeypatch.setattr(
        loader,
        "_create_youtube_dl",
        lambda options: FakeYoutubeDL(options),
    )
    ctx = LoaderContext(
        job_id="job-collision",
        profile=SourceProfile(name="yt_dlp"),
        staging=StagingArea(tmp_path / "staging"),
    )

    result = loader.download(request, ctx=ctx)

    job = tmp_path / "staging" / "job-collision"
    assert result.failures == ()
    assert result.files[0].path == job / "Same title.m4a"
    assert result.files[1].path == job / "1" / "Same title.m4a"
    assert result.files[0].sidecars == (job / "Same title.jpg",)
    assert result.files[1].sidecars == (job / "1" / "Same title.jpg",)


def test_staging_name_override_is_sanitized_and_used(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = make_item("one", stem="Original stem")
    source = replace(make_source_info(), items=(item,))
    request = DownloadRequest(
        source_info=source,
        selections=(ItemSelection(item.id, item.variants[0].id),),
        options=DownloadOptions(
            target_dir=tmp_path / "staging",
            staging_name="../escape/name",
            write_thumbnail=False,
        ),
    )
    loader = YtDlpLoader()
    monkeypatch.setattr(
        loader,
        "_create_youtube_dl",
        lambda options: FakeYoutubeDL(options),
    )
    ctx = LoaderContext(
        job_id="job-override",
        profile=SourceProfile(name="yt_dlp"),
        staging=StagingArea(tmp_path / "staging"),
    )

    result = loader.download(request, ctx=ctx)

    job = (tmp_path / "staging" / "job-override").resolve()
    path = result.files[0].path.resolve()
    assert path.parent == job
    assert path.name == "_escape_name.m4a"


def test_planner_allocates_1_and_2_for_three_duplicates() -> None:
    items = tuple(
        make_item(f"item-{index}", stem="Duplicate") for index in range(3)
    )
    source = replace(make_source_info(), items=items)
    request = DownloadRequest(
        source_info=source,
        selections=tuple(
            ItemSelection(item.id, item.variants[0].id) for item in items
        ),
        options=DownloadOptions(target_dir=Path("staging")),
    )

    plan = YtDlpPlanner().plan(request, job_id="job")

    assert [item.output_stem for item in plan.items] == [
        "Duplicate",
        "1/Duplicate",
        "2/Duplicate",
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", "media"),
        ("..", "media"),
        ("a/b", "a_b"),
        ("a\\b", "a_b"),
        ("CON", "_CON"),
        ("CON.txt", "_CON.txt"),
        ("  name.  ", "name"),
        ("nul", "_nul"),
    ],
)
def test_safe_stem(raw: str, expected: str) -> None:
    assert safe_stem(raw) == expected


def test_selection_of_flat_item_is_rejected() -> None:
    from trackforge.acquire.errors import VariantUnavailable
    from trackforge.acquire.models import ResolutionState

    item = replace(make_item("flat"), resolution=ResolutionState.FLAT)
    source = replace(make_source_info(), items=(item,))
    request = DownloadRequest(
        source_info=source,
        selections=(ItemSelection(item.id, item.variants[0].id),),
        options=DownloadOptions(target_dir=Path("staging")),
    )

    with pytest.raises(VariantUnavailable, match="not fully resolved"):
        YtDlpPlanner().plan(request, job_id="job")

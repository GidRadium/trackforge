"""Tests for the acquisition domain model."""

# pyright: standard

from __future__ import annotations

from pathlib import Path

import pytest

from trackforge.acquire import (
    CollisionPolicy,
    DownloadedFile,
    DownloadFailure,
    DownloadOptions,
    DownloadRequest,
    DownloadResult,
    DownloadVariant,
    Input,
    InputKind,
    ItemSelection,
    MediaItem,
    MediaKind,
    MediaUse,
    Recommendations,
    ResolutionState,
    SearchCandidate,
    SourceInfo,
    SourceKind,
    SourceRef,
    StreamInfo,
    StreamRole,
    Thumbnail,
    UrlHint,
)
from trackforge.metadata import PartialDate


def make_item() -> MediaItem:
    return MediaItem(
        id="item-1",
        title="Song",
        suggested_stem="Artist - Song",
        kind=MediaKind.AUDIO,
        artists=("Artist",),
        album="Album",
        duration=123.4,
        release_date=PartialDate(2024, 5),
        page_url="https://example.test/watch/1",
    )


def make_source(item: MediaItem | None = None) -> SourceInfo:
    item = item or make_item()
    return SourceInfo(
        source="example",
        url=item.page_url or "https://example.test",
        kind=SourceKind.SINGLE,
        id=item.id,
        title=item.title,
        creator="Artist",
        description="A description",
        thumbnails=(),
        items=(item,),
        publisher="Label",
    )


def test_input_kinds_and_fields() -> None:
    value = Input(
        raw="/tmp/music",
        kind=InputKind.LOCAL_DIR,
        paths=(Path("/tmp/music"),),
    )
    assert value.kind is InputKind.LOCAL_DIR
    assert value.paths == (Path("/tmp/music"),)
    assert InputKind("url") is InputKind.URL


def test_source_ref_and_info_ref() -> None:
    source = make_source()
    assert source.ref() == SourceRef("example", source.url, "item-1")
    assert source.publisher == "Label"


def test_source_info_recommendations_uses_first_item() -> None:
    item = make_item()
    recommended = Recommendations(audio="best-audio", video="best-video")
    item = MediaItem(
        id=item.id,
        title=item.title,
        recommendations=recommended,
    )
    source = make_source(item)
    assert source.recommendations is recommended
    assert SourceInfo(
        source="x",
        url="u",
        kind=SourceKind.SINGLE,
        id=None,
        title=None,
        creator=None,
        description=None,
        thumbnails=(),
        items=(),
    ).recommendations == Recommendations()


def test_recommendations_for_use() -> None:
    values = Recommendations(
        audio="a",
        video="v",
        image="i",
        document="d",
    )
    assert values.for_use(MediaUse.AUDIO) == "a"
    assert values.for_use(MediaUse.VIDEO) == "v"
    assert values.for_use(MediaUse.IMAGE) == "i"
    assert values.for_use(MediaUse.DOCUMENT) == "d"


def test_variant_size_label() -> None:
    exact = DownloadVariant(
        id="1",
        label="exact",
        kind=MediaKind.AUDIO,
        container="m4a",
        streams=(),
        filesize=4_100_000,
    )
    approximate = DownloadVariant(
        id="2",
        label="approx",
        kind=MediaKind.VIDEO,
        container="mp4",
        streams=(),
        filesize_approx=25_000_000,
    )
    unknown = DownloadVariant(
        id="3",
        label="unknown",
        kind=MediaKind.VIDEO,
        container="mp4",
        streams=(),
    )
    assert exact.size_label == "4.1 MB"
    assert approximate.size_label == "~25.0 MB"
    assert unknown.size_label == "?"


def test_stream_url_and_thumbnail_headers_are_not_in_repr() -> None:
    stream = StreamInfo(
        role=StreamRole.AUDIO,
        url="https://cdn.test/signed?token=SECRET",
    )
    thumbnail = Thumbnail(
        url="https://cdn.test/cover.jpg",
        http_headers={"Authorization": "Bearer SECRET"},
    )
    assert "SECRET" not in repr(stream)
    assert "SECRET" not in repr(thumbnail)


def test_item_extras_are_not_in_repr() -> None:
    item = MediaItem(
        id="1",
        title="T",
        extras={"api_token": "SECRET", "track_number": 3},
    )
    assert "SECRET" not in repr(item)
    assert item.extras["track_number"] == 3


def test_resolution_and_search_candidate() -> None:
    item = make_item()
    assert item.resolution is ResolutionState.FULL
    candidate = SearchCandidate(
        source="youtube",
        id="abc",
        url="https://example.test/abc",
        title="Candidate",
        creator="Channel",
        duration=10.0,
        published_at=PartialDate(2024),
        score=0.75,
    )
    assert candidate.published_at == PartialDate(2024)
    assert candidate.score == 0.75


def test_download_request_and_result() -> None:
    item = make_item()
    source = make_source(item)
    options = DownloadOptions(target_dir=Path("/tmp/out"))
    selection = ItemSelection(item_id=item.id, variant_id="best-audio")
    request = DownloadRequest(
        source_info=source,
        selections=(selection,),
        options=options,
    )
    assert request.source == source.ref()
    assert options.collision is CollisionPolicy.SUBFOLDER

    file = DownloadedFile(
        item_id=item.id,
        path=Path("/tmp/out/song.m4a"),
        item=item,
        sidecars=(Path("/tmp/out/song.lrc"),),
    )
    failure = DownloadFailure(item_id="other", message="boom", retryable=True)
    result = DownloadResult(files=(file,), failures=(failure,), warnings=("w",))
    assert result.files[0].sidecars[0].suffix == ".lrc"
    assert result.failures[0].retryable is True
    assert result.warnings == ("w",)


@pytest.mark.parametrize("hint", list(UrlHint))
def test_url_hint_values(hint: UrlHint) -> None:
    assert hint.value in {"single", "post", "collection", "unknown"}

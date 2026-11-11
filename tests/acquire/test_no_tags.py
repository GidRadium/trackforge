"""Downloading must not write tags or final names."""

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
from trackforge.acquire.models import DownloadOptions, DownloadRequest, ItemSelection
from trackforge.acquire.staging import StagingArea

TAGGED_BYTES = (
    b"ID3\x04\x00\x00\x00\x00\x00\x0a"
    b"TIT2\x00\x00\x00\x07\x00\x00\x00"
    b"Title"
    b"\xff\xfb\x90\x64"
)


@pytest.fixture(autouse=True)
def reset_fake() -> Iterator[None]:
    FakeYoutubeDL.reset()
    yield
    FakeYoutubeDL.reset()


def test_download_never_uses_metadata_or_thumbnail_postprocessors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = make_item("item", stem="Artist - Title")
    source = replace(make_source_info(), items=(item,))
    request = DownloadRequest(
        source_info=source,
        selections=(ItemSelection(item.id, item.variants[0].id),),
        options=DownloadOptions(
            target_dir=tmp_path / "staging",
            write_thumbnail=False,
            write_info_json=False,
        ),
    )
    loader = YtDlpLoader()
    monkeypatch.setattr(
        loader,
        "_create_youtube_dl",
        lambda options: FakeYoutubeDL(options),
    )
    monkeypatch.setattr(FakeYoutubeDL, "payload", TAGGED_BYTES)
    ctx = LoaderContext(
        job_id="job-no-tags",
        profile=SourceProfile(name="yt_dlp"),
        staging=StagingArea(tmp_path / "staging"),
    )

    # The metadata layer must not be touched by the download path.
    from trackforge.metadata.facade import MediaFile

    def fail_save(*args: object, **kwargs: object) -> None:
        raise AssertionError("download must not save media metadata")

    monkeypatch.setattr(MediaFile, "save", fail_save)

    result = loader.download(request, ctx=ctx)

    assert result.failures == ()
    assert len(result.files) == 1
    path = result.files[0].path
    assert path.read_bytes() == TAGGED_BYTES
    assert path.parent == tmp_path / "staging" / "job-no-tags"
    assert result.files[0].sidecars == ()
    options = FakeYoutubeDL.instances[-1].options
    postprocessors = options["postprocessors"]
    assert postprocessors == []
    keys = {entry["key"] for entry in postprocessors}
    assert "FFmpegMetadata" not in keys
    assert "EmbedThumbnail" not in keys
    assert "MetadataFromField" not in keys


def test_download_does_not_touch_an_existing_final_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final_dir = tmp_path / "final"
    final_dir.mkdir()
    marker = final_dir / "keep.txt"
    marker.write_text("untouched", encoding="utf-8")

    item = make_item("item")
    source = replace(make_source_info(), items=(item,))
    request = DownloadRequest(
        source_info=source,
        selections=(ItemSelection(item.id, item.variants[0].id),),
        options=DownloadOptions(
            target_dir=tmp_path / "staging",
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
        job_id="job-final-untouched",
        profile=SourceProfile(name="yt_dlp"),
        staging=StagingArea(tmp_path / "staging"),
    )

    loader.download(request, ctx=ctx)

    assert marker.read_text(encoding="utf-8") == "untouched"
    assert list(final_dir.iterdir()) == [marker]

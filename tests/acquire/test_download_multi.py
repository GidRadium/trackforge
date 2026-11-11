"""Multi-item downloads: one failure must not stop the other items."""

# pyright: standard

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fakes import FakeYoutubeDL, make_item, make_source_info

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.errors import DownloadFailed
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.loaders.ytdlp import YtDlpLoader
from trackforge.acquire.models import (
    DownloadOptions,
    DownloadRequest,
    ItemSelection,
    SourceKind,
)
from trackforge.acquire.progress import RecordingSink
from trackforge.acquire.staging import StagingArea


@pytest.fixture(autouse=True)
def reset_fake() -> Iterator[None]:
    FakeYoutubeDL.reset()
    yield
    FakeYoutubeDL.reset()


def test_one_success_and_one_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ok = make_item("ok", title="Good track", url="https://example.test/good")
    bad = make_item("bad", title="Bad track", url="https://example.test/fail")
    source = replace(
        make_source_info(),
        kind=SourceKind.COLLECTION,
        items=(ok, bad),
    )
    request = DownloadRequest(
        source_info=source,
        selections=(
            ItemSelection(ok.id, ok.variants[0].id),
            ItemSelection(bad.id, bad.variants[0].id),
        ),
        options=DownloadOptions(
            target_dir=tmp_path / "staging",
            write_thumbnail=False,
        ),
    )
    FakeYoutubeDL.fail_urls = {"https://example.test/fail"}
    loader = YtDlpLoader()
    monkeypatch.setattr(
        loader,
        "_create_youtube_dl",
        lambda options: FakeYoutubeDL(options),
    )
    ctx = LoaderContext(
        job_id="job-multi",
        profile=SourceProfile(name="yt_dlp"),
        staging=StagingArea(tmp_path / "staging"),
        progress=RecordingSink(),
    )

    result = loader.download(request, ctx=ctx)

    assert len(result.files) == 1
    assert result.files[0].item_id == "ok"
    assert result.files[0].path.exists()
    assert len(result.failures) == 1
    assert result.failures[0].item_id == "bad"
    assert isinstance(result.failures[0].retryable, bool)
    # Both items were attempted, in order.
    attempted = [
        url
        for instance in FakeYoutubeDL.instances
        for url, _download in instance.extract_calls
    ]
    assert attempted == [
        "https://example.test/good",
        "https://example.test/fail",
    ]


def test_failure_is_typed_when_ytdlp_reports_a_download_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = make_item("bad", url="https://example.test/fail")
    source = replace(make_source_info(), items=(bad,))
    request = DownloadRequest(
        source_info=source,
        selections=(ItemSelection(bad.id, bad.variants[0].id),),
        options=DownloadOptions(
            target_dir=tmp_path / "staging",
            write_thumbnail=False,
        ),
    )
    FakeYoutubeDL.fail_urls = {"https://example.test/fail"}
    loader = YtDlpLoader()
    monkeypatch.setattr(
        loader,
        "_create_youtube_dl",
        lambda options: FakeYoutubeDL(options),
    )
    ctx = LoaderContext(
        job_id="job-failure",
        profile=SourceProfile(name="yt_dlp"),
        staging=StagingArea(tmp_path / "staging"),
    )

    result = loader.download(request, ctx=ctx)

    assert result.files == ()
    assert len(result.failures) == 1
    assert "simulated download failure" in result.failures[0].message


def test_unexpected_item_exception_becomes_a_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = make_item("broken")
    source = replace(make_source_info(), items=(item,))
    request = DownloadRequest(
        source_info=source,
        selections=(ItemSelection(item.id, item.variants[0].id),),
        options=DownloadOptions(
            target_dir=tmp_path / "staging",
            write_thumbnail=False,
        ),
    )

    class ExplodingYoutubeDL(FakeYoutubeDL):
        def extract_info(self, url: str, *, download: bool):
            del url, download
            raise RuntimeError("boom")

    loader = YtDlpLoader()
    monkeypatch.setattr(
        loader,
        "_create_youtube_dl",
        lambda options: ExplodingYoutubeDL(options),
    )
    ctx = LoaderContext(
        job_id="job-boom",
        profile=SourceProfile(name="yt_dlp"),
        staging=StagingArea(tmp_path / "staging"),
    )

    result = loader.download(request, ctx=ctx)

    assert result.files == ()
    assert result.failures[0].message == "boom"
    assert result.failures[0].retryable is False


def test_cancel_before_download_leaves_no_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trackforge.acquire.errors import CancelledError
    from trackforge.acquire.progress import CancellationToken

    item = make_item("item")
    source = replace(make_source_info(), items=(item,))
    request = DownloadRequest(
        source_info=source,
        selections=(ItemSelection(item.id, item.variants[0].id),),
        options=DownloadOptions(target_dir=tmp_path / "staging"),
    )
    token = CancellationToken()
    token.cancel()
    loader = YtDlpLoader()
    monkeypatch.setattr(
        loader,
        "_create_youtube_dl",
        lambda options: FakeYoutubeDL(options),
    )
    ctx = LoaderContext(
        job_id="job-cancelled",
        profile=SourceProfile(name="yt_dlp"),
        staging=StagingArea(tmp_path / "staging"),
        cancel=token,
    )

    with pytest.raises(CancelledError):
        loader.download(request, ctx=ctx)

    assert not (tmp_path / "staging" / "job-cancelled").exists()


def test_cancel_during_download_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trackforge.acquire.errors import CancelledError
    from trackforge.acquire.progress import CancellationToken

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
    token = CancellationToken()

    class CancellingYoutubeDL(FakeYoutubeDL):
        def extract_info(self, url: str, *, download: bool):
            token.cancel()
            return super().extract_info(url, download=download)

    loader = YtDlpLoader()
    monkeypatch.setattr(
        loader,
        "_create_youtube_dl",
        lambda options: CancellingYoutubeDL(options),
    )
    ctx = LoaderContext(
        job_id="job-cancel-mid",
        profile=SourceProfile(name="yt_dlp"),
        staging=StagingArea(tmp_path / "staging"),
        cancel=token,
    )

    with pytest.raises(CancelledError):
        loader.download(request, ctx=ctx)

    assert not (tmp_path / "staging" / "job-cancel-mid").exists()


def test_download_failed_is_retryable_by_default() -> None:
    assert DownloadFailed().retryable is True

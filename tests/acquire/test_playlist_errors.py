"""Typed failures for empty and inaccessible playlists."""

# pyright: standard

from __future__ import annotations

from typing import Any, Self

import pytest

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.errors import AuthRequired, Unavailable
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.loaders.ytdlp import YtDlpLoader
from trackforge.acquire.models import SourceRef


def context() -> LoaderContext:
    return LoaderContext(
        job_id="job-errors",
        profile=SourceProfile(
            name="yt_dlp",
            cookies_from_browser=("chrome",),
        ),
    )


def collection_ref() -> SourceRef:
    return SourceRef(
        "yt-dlp",
        "https://www.youtube.com/playlist?list=PLmissing",
    )


def test_empty_public_playlist_is_a_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = YtDlpLoader()
    monkeypatch.setattr(
        loader,
        "_extract",
        lambda *args, **kwargs: {
            "_type": "playlist",
            "id": "PLmissing",
            "title": "Empty",
            "availability": "public",
            "entries": [],
        },
    )

    with pytest.raises(Unavailable, match="empty or has no accessible items"):
        loader.get_info(collection_ref(), ctx=context(), flat=True)


def test_private_empty_playlist_is_an_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = YtDlpLoader()
    monkeypatch.setattr(
        loader,
        "_extract",
        lambda *args, **kwargs: {
            "_type": "playlist",
            "id": "PLprivate",
            "title": "Private",
            "availability": "private",
            "entries": [],
        },
    )

    with pytest.raises(AuthRequired, match="private"):
        loader.get_info(collection_ref(), ctx=context(), flat=True)


class FailingYoutubeDL:
    def __init__(self, options: dict[str, object]) -> None:
        del options

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def extract_info(self, url: str, *, download: bool) -> dict[str, Any]:
        del url, download
        from yt_dlp.utils import DownloadError

        raise DownloadError("ERROR: This playlist is private")

    def sanitize_info(self, info: dict[str, Any]) -> dict[str, Any]:
        return info


def test_private_playlist_download_error_is_mapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = YtDlpLoader()
    monkeypatch.setattr(
        loader,
        "_create_youtube_dl",
        lambda options: FailingYoutubeDL(options),
    )

    with pytest.raises(AuthRequired, match="private"):
        loader.get_info(collection_ref(), ctx=context(), flat=True)

"""Single-item download tests using a fake ``YoutubeDL`` and a local server."""

# pyright: standard

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fakes import FakeYoutubeDL, make_item, make_source_info, make_variant

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.download import YtDlpProgressHook
from trackforge.acquire.errors import PostprocessError
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.loaders.ytdlp import YtDlpLoader
from trackforge.acquire.models import (
    DownloadOptions,
    DownloadRequest,
    ItemSelection,
    MediaKind,
)
from trackforge.acquire.planner import YtDlpPlanner
from trackforge.acquire.progress import RecordingSink
from trackforge.acquire.staging import StagingArea

PAYLOAD = b"real-looking media bytes" * 32


@pytest.fixture
def media_server() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "audio/mp4")
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/media.m4a"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def reset_fake() -> Iterator[None]:
    FakeYoutubeDL.reset()
    yield
    FakeYoutubeDL.reset()


def install_fake(monkeypatch: pytest.MonkeyPatch, loader: YtDlpLoader) -> None:
    monkeypatch.setattr(
        loader,
        "_create_youtube_dl",
        lambda options: FakeYoutubeDL(options),
    )


def make_context(
    tmp_path: Path,
    *,
    profile: SourceProfile | None = None,
) -> LoaderContext:
    return LoaderContext(
        job_id="job-single",
        profile=profile or SourceProfile(name="yt_dlp"),
        staging=StagingArea(tmp_path / "staging"),
        progress=RecordingSink(),
    )


def test_single_audio_download_collects_real_path_and_sidecars(
    tmp_path: Path,
    media_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_source_info(url=media_server)
    item = source.items[0]
    variant = item.variants[0]
    request = DownloadRequest(
        source_info=source,
        selections=(ItemSelection(item.id, variant.id),),
        options=DownloadOptions(
            target_dir=tmp_path / "staging",
            write_thumbnail=True,
            write_info_json=True,
        ),
    )
    loader = YtDlpLoader()
    install_fake(monkeypatch, loader)
    ctx = make_context(tmp_path)

    result = loader.download(request, ctx=ctx)

    assert result.failures == ()
    assert len(result.files) == 1
    downloaded = result.files[0]
    assert downloaded.item_id == item.id
    assert downloaded.path.read_bytes() == PAYLOAD
    assert downloaded.path.parent == tmp_path / "staging" / "job-single"
    assert downloaded.path.suffix == ".m4a"
    assert {path.name for path in downloaded.sidecars} == {
        f"{item.suggested_stem}.jpg",
        f"{item.suggested_stem}.info.json",
    }
    assert all(path.exists() for path in downloaded.sidecars)

    fake = FakeYoutubeDL.instances[-1]
    assert fake.extract_calls == [(media_server, True)]
    assert fake.sanitized is True
    assert fake.options["noplaylist"] is True
    assert fake.options["format"] == variant.selector
    assert fake.options["paths"]["home"] == str(tmp_path / "staging" / "job-single")
    assert fake.options["outtmpl"] == {
        "default": f"{item.suggested_stem}.%(ext)s"
    }


def test_download_passes_cookies_and_proxy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cookie = tmp_path / "cookies.txt"
    cookie.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tCOOKIE-SECRET\n",
        encoding="utf-8",
    )
    cookie.chmod(0o600)
    source = make_source_info()
    item = source.items[0]
    request = DownloadRequest(
        source_info=source,
        selections=(ItemSelection(item.id, item.variants[0].id),),
        options=DownloadOptions(target_dir=tmp_path / "staging"),
    )
    loader = YtDlpLoader()
    install_fake(monkeypatch, loader)
    ctx = make_context(
        tmp_path,
        profile=SourceProfile(
            name="yt_dlp",
            cookies_file=cookie,
            proxy="http://proxy.test:8080",
        ),
    )

    loader.download(request, ctx=ctx)

    options = FakeYoutubeDL.instances[-1].options
    assert options["cookiefile"] == str(cookie)
    assert options["proxy"] == "http://proxy.test:8080"


def test_merge_requires_ffmpeg_before_creating_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    variant = make_variant(
        "1080p-mp4",
        kind=MediaKind.AUDIO_VIDEO,
        container="mp4",
        selector="137+140/bv*[height=1080]+ba/b[height=1080]",
        requires_merge=True,
    )
    item = make_item("video", stem="Video", variant=variant)
    source = replace(make_source_info(), items=(item,))
    request = DownloadRequest(
        source_info=source,
        selections=(ItemSelection(item.id, variant.id),),
        options=DownloadOptions(target_dir=tmp_path / "staging"),
    )
    loader = YtDlpLoader()
    ctx = make_context(tmp_path)
    monkeypatch.setattr(
        "trackforge.acquire.download.shutil.which",
        lambda name: None,
    )

    with pytest.raises(PostprocessError, match="ffmpeg"):
        loader.download(request, ctx=ctx)

    assert not (tmp_path / "staging" / "job-single").exists()


def test_audio_only_adds_extract_audio_postprocessor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_source_info()
    item = source.items[0]
    request = DownloadRequest(
        source_info=source,
        selections=(ItemSelection(item.id, item.variants[0].id),),
        options=DownloadOptions(
            target_dir=tmp_path / "staging",
            audio_only=True,
            container="mp3",
            write_thumbnail=False,
        ),
    )
    loader = YtDlpLoader()
    install_fake(monkeypatch, loader)
    monkeypatch.setattr(
        "trackforge.acquire.download.shutil.which",
        lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None,
    )
    ctx = make_context(tmp_path)

    result = loader.download(request, ctx=ctx)

    assert result.failures == ()
    assert result.files[0].path.suffix == ".mp3"
    postprocessors = FakeYoutubeDL.instances[-1].options["postprocessors"]
    assert [entry["key"] for entry in postprocessors] == ["FFmpegExtractAudio"]
    assert postprocessors[0]["preferredcodec"] == "mp3"


def test_container_remux_postprocessor_for_progressive_video(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    variant = make_variant(
        "720p-webm",
        kind=MediaKind.AUDIO_VIDEO,
        container="webm",
        selector="248/137",
    )
    item = make_item("video", stem="Video", variant=variant)
    source = replace(make_source_info(), items=(item,))
    request = DownloadRequest(
        source_info=source,
        selections=(ItemSelection(item.id, variant.id),),
        options=DownloadOptions(
            target_dir=tmp_path / "staging",
            container="mp4",
            write_thumbnail=False,
        ),
    )
    loader = YtDlpLoader()
    install_fake(monkeypatch, loader)
    monkeypatch.setattr(
        "trackforge.acquire.download.shutil.which",
        lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None,
    )
    ctx = make_context(tmp_path)

    result = loader.download(request, ctx=ctx)

    assert result.files[0].path.suffix == ".mp4"
    postprocessors = FakeYoutubeDL.instances[-1].options["postprocessors"]
    assert postprocessors == [
        {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}
    ]


def test_planner_rejects_unknown_variant() -> None:
    source = make_source_info()
    item = source.items[0]
    request = DownloadRequest(
        source_info=source,
        selections=(ItemSelection(item.id, "missing"),),
        options=DownloadOptions(target_dir=Path("staging")),
    )

    from trackforge.acquire.errors import VariantUnavailable

    with pytest.raises(VariantUnavailable, match="missing"):
        YtDlpPlanner().plan(request, job_id="job")


def test_progress_hook_is_wired_into_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_source_info()
    item = source.items[0]
    request = DownloadRequest(
        source_info=source,
        selections=(ItemSelection(item.id, item.variants[0].id),),
        options=DownloadOptions(target_dir=tmp_path / "staging"),
    )
    loader = YtDlpLoader()
    install_fake(monkeypatch, loader)
    ctx = make_context(tmp_path)
    sink = ctx.progress
    assert isinstance(sink, RecordingSink)

    loader.download(request, ctx=ctx)

    assert any(event.stage.value == "download" for event in sink.events)
    download_events = [
        event for event in sink.events if event.stage.value == "download"
    ]
    assert download_events[0].item_index == 1
    assert download_events[0].item_count == 1
    assert any(
        isinstance(getattr(hook, "__self__", None), YtDlpProgressHook)
        for hook in FakeYoutubeDL.instances[-1].options["progress_hooks"]
    )


def test_service_download_resolves_and_dispatches(
    tmp_path: Path,
) -> None:
    from fakes import FakeLoader

    from trackforge.acquire.registry import LoaderRegistry
    from trackforge.acquire.service import AcquisitionService

    source = make_source_info(source="fake")
    item = source.items[0]
    request = DownloadRequest(
        source_info=source,
        selections=(ItemSelection(item.id, item.variants[0].id),),
        options=DownloadOptions(target_dir=tmp_path / "staging"),
    )
    loader = FakeLoader(info=source)
    service = AcquisitionService(registry=LoaderRegistry([loader]))

    result = service.download(request, job_id="job-service")

    assert result.files == ()
    assert loader.download_calls == [request]


def test_merge_uses_merge_output_format_without_double_remux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    variant = make_variant(
        "1080p-mp4",
        kind=MediaKind.AUDIO_VIDEO,
        container="mp4",
        selector="137+140/bv*[height=1080]+ba/b[height=1080]",
        requires_merge=True,
    )
    item = make_item("video", stem="Video", variant=variant)
    source = replace(make_source_info(), items=(item,))
    request = DownloadRequest(
        source_info=source,
        selections=(ItemSelection(item.id, variant.id),),
        options=DownloadOptions(
            target_dir=tmp_path / "staging",
            container="mp4",
            write_thumbnail=False,
        ),
    )
    loader = YtDlpLoader()
    install_fake(monkeypatch, loader)
    monkeypatch.setattr(
        "trackforge.acquire.download.shutil.which",
        lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None,
    )
    ctx = make_context(tmp_path)

    result = loader.download(request, ctx=ctx)

    assert result.failures == ()
    assert result.files[0].path.suffix == ".mp4"
    options = FakeYoutubeDL.instances[-1].options
    assert options["merge_output_format"] == "mp4"
    assert all(
        entry["key"] != "FFmpegVideoRemuxer"
        for entry in options["postprocessors"]
    )

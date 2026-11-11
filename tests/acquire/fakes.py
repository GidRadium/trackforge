"""Test-only loaders.

Kept separate from ``conftest.py`` so tests can import the class directly as
well as use the fixtures.
"""

# pyright: standard

from __future__ import annotations

import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar, Self

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.errors import SearchNotSupported
from trackforge.acquire.loaders.base import (
    LoaderCapabilities,
    LoaderContext,
    MediaLoader,
)
from trackforge.acquire.models import (
    DownloadRequest,
    DownloadResult,
    DownloadVariant,
    MediaItem,
    MediaKind,
    Recommendations,
    SearchCandidate,
    SourceInfo,
    SourceKind,
    SourceRef,
    StreamInfo,
    StreamRole,
    Thumbnail,
)
from trackforge.acquire.progress import RecordingSink
from trackforge.acquire.staging import StagingArea


def make_source_info(
    *,
    source: str = "fake",
    url: str = "https://example.test/media/1",
    title: str = "Fake media",
    kind: SourceKind = SourceKind.SINGLE,
    item_id: str = "item-1",
) -> SourceInfo:
    """Build a small, fully offline SourceInfo."""
    thumbnail = Thumbnail(
        url="https://example.test/cover.jpg",
        width=1280,
        height=720,
        ext="jpg",
    )
    variant = DownloadVariant(
        id="audio-best",
        label="audio original",
        kind=MediaKind.AUDIO,
        container="m4a",
        streams=(
            StreamInfo(
                role=StreamRole.AUDIO,
                format_id="140",
                codec="aac",
                ext="m4a",
                bitrate_kbps=128,
            ),
        ),
        selector="bestaudio[ext=m4a]/bestaudio",
        filesize=4_100_000,
    )
    item = MediaItem(
        id=item_id,
        title=title,
        suggested_stem=title,
        kind=MediaKind.AUDIO,
        creator="Fake creator",
        artists=("Fake creator",),
        duration=180.0,
        page_url=url,
        thumbnails=(thumbnail,),
        thumbnail=thumbnail,
        variants=(variant,),
        recommendations=Recommendations(audio=variant.id),
    )
    return SourceInfo(
        source=source,
        url=url,
        kind=kind,
        id=item_id,
        title=title,
        creator=item.creator,
        description=None,
        thumbnails=(thumbnail,),
        items=(item,),
        thumbnail=thumbnail,
    )


class FakeLoader(MediaLoader):
    """A deterministic loader that never touches the network."""

    name = "fake"
    config_key = None
    priority = 50
    capabilities = LoaderCapabilities(search=True)

    def __init__(
        self,
        *,
        name: str = "fake",
        config_key: str | None = None,
        priority: int = 50,
        supports: bool | Callable[[str], bool] | tuple[str, ...] = True,
        supports_url: bool | Callable[[str], bool] | tuple[str, ...] | None = None,
        ready: bool = True,
        info: SourceInfo | None = None,
        capabilities: LoaderCapabilities | None = None,
        search_results: list[SearchCandidate] | None = None,
    ) -> None:
        self.name = name  # pyright: ignore[reportAttributeAccessIssue]
        self.config_key = config_key  # pyright: ignore[reportAttributeAccessIssue]
        self.priority = priority  # pyright: ignore[reportAttributeAccessIssue]
        self.capabilities = (  # pyright: ignore[reportAttributeAccessIssue]
            capabilities or LoaderCapabilities(search=True)
        )
        if supports_url is not None:
            supports = supports_url
        self._supports = supports
        self._ready = ready
        self._info = info
        self._search_results = search_results
        self.info_calls: list[SourceRef] = []
        self.info_requests: list[tuple[SourceRef, bool | None, int | None]] = []
        self.download_calls: list[DownloadRequest] = []
        self.profiles: list[SourceProfile] = []

    def supports_url(self, url: str) -> bool:
        if callable(self._supports):
            return bool(self._supports(url))
        if isinstance(self._supports, tuple):
            return url.startswith(self._supports)
        return bool(self._supports)

    def profile_name(self) -> str:  # pyright: ignore[reportIncompatibleMethodOverride]
        # The base contract is a classmethod; the fake needs per-instance
        # names so several loaders can coexist in one registry.
        return self.config_key or self.name

    def is_ready(self, profile: SourceProfile) -> bool:
        self.profiles.append(profile)
        return self._ready

    def get_info(
        self,
        ref: SourceRef,
        *,
        ctx: LoaderContext,
        flat: bool | None = False,
        limit: int | None = None,
    ) -> SourceInfo:
        self.info_calls.append(ref)
        self.info_requests.append((ref, flat, limit))
        # Touch the context so the test exercises the real contract.
        assert ctx.profile.name
        assert isinstance(ctx.progress, RecordingSink) or ctx.progress is not None
        if self._info is None:
            return make_source_info(source=self.name, url=ref.url)
        return replace(self._info, source=self.name, url=ref.url)

    def download(
        self,
        request: DownloadRequest,
        *,
        ctx: LoaderContext,
    ) -> DownloadResult:
        del ctx
        self.download_calls.append(request)
        return DownloadResult(files=())

    def search(
        self,
        query: str,
        *,
        limit: int,
        ctx: LoaderContext,
    ) -> list[SearchCandidate]:
        del query, ctx
        if self._search_results is None:
            raise SearchNotSupported(self.name)
        return self._search_results[:limit]


def make_context(
    profile: SourceProfile | None = None,
    *,
    staging: StagingArea | None = None,
) -> LoaderContext:
    return LoaderContext(
        job_id="job-1",
        profile=profile or SourceProfile(name="fake"),
        progress=RecordingSink(),
        staging=staging,
    )


def make_variant(
    variant_id: str = "audio-best",
    *,
    kind: MediaKind = MediaKind.AUDIO,
    container: str = "m4a",
    selector: str = "140/ba[ext=m4a]/ba/b",
    requires_merge: bool = False,
    format_id: str = "140",
) -> DownloadVariant:
    """Build a simple variant for download-plan tests."""
    if kind is MediaKind.AUDIO_VIDEO:
        streams = (
            StreamInfo(
                role=StreamRole.VIDEO,
                format_id="137",
                codec="h264",
                ext="mp4",
                height=1080,
            ),
            StreamInfo(
                role=StreamRole.AUDIO,
                format_id="140",
                codec="aac",
                ext="m4a",
            ),
        )
    elif kind is MediaKind.VIDEO:
        streams = (
            StreamInfo(
                role=StreamRole.VIDEO,
                format_id=format_id,
                codec="h264",
                ext=container,
                height=720,
            ),
        )
    else:
        streams = (
            StreamInfo(
                role=StreamRole.AUDIO,
                format_id=format_id,
                codec="aac",
                ext=container,
            ),
        )
    return DownloadVariant(
        id=variant_id,
        label=variant_id,
        kind=kind,
        container=container,
        streams=streams,
        selector=selector,
        requires_merge=requires_merge,
        requires_ffmpeg=requires_merge,
    )


def make_item(
    item_id: str,
    *,
    title: str | None = None,
    stem: str | None = None,
    url: str | None = None,
    variant: DownloadVariant | None = None,
    position: int | None = 1,
) -> MediaItem:
    """Build one fully resolved item with a single variant."""
    selected = variant or make_variant()
    page = url or f"https://example.test/media/{item_id}"
    return MediaItem(
        id=item_id,
        title=title or item_id,
        suggested_stem=stem or title or item_id,
        kind=selected.kind,
        creator="Fake creator",
        artists=("Fake creator",),
        duration=180.0,
        page_url=page,
        variants=(selected,),
        recommendations=Recommendations(
            audio=selected.id if selected.kind is MediaKind.AUDIO else None,
            video=(
                selected.id
                if selected.kind in {MediaKind.VIDEO, MediaKind.AUDIO_VIDEO}
                else None
            ),
        ),
        position=position,
    )


class FakeYoutubeDL:
    """A small in-process replacement for ``yt_dlp.YoutubeDL``.

    It writes real files under ``paths.home`` so the adapter's output
    discovery, sidecar collection and progress hooks are exercised without
    touching the network (unless the test uses a local HTTP URL).
    """

    instances: ClassVar[list[FakeYoutubeDL]] = []
    fail_urls: ClassVar[set[str]] = set()
    extension: ClassVar[str | None] = None
    payload: ClassVar[bytes | None] = None

    def __init__(self, options: Mapping[str, Any]) -> None:
        self.options: dict[str, Any] = dict(options)
        self.extract_calls: list[tuple[str, bool]] = []
        self.closed = False
        self.sanitized = False
        type(self).instances.append(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.fail_urls = set()
        cls.extension = None
        cls.payload = None

    def sanitize_info(self, info: Mapping[str, Any]) -> Mapping[str, Any]:
        self.sanitized = True
        return info

    def close(self) -> None:
        self.closed = True

    def _template(self) -> str:
        template = self.options.get("outtmpl", "%(title)s.%(ext)s")
        if isinstance(template, Mapping):
            template = str(template.get("default", "%(title)s.%(ext)s"))
        return str(template)

    def _extension(self) -> str:
        extension = type(self).extension or "m4a"
        for postprocessor in self.options.get("postprocessors", ()):
            if not isinstance(postprocessor, Mapping):
                continue
            if postprocessor.get("key") == "FFmpegExtractAudio":
                preferred = str(postprocessor.get("preferredcodec") or "")
                if preferred and preferred != "best":
                    extension = preferred
            elif postprocessor.get("key") == "FFmpegVideoRemuxer":
                preferred = str(postprocessor.get("preferedformat") or "")
                if preferred:
                    extension = preferred
        merged = self.options.get("merge_output_format")
        if isinstance(merged, str) and merged:
            extension = merged
        return extension

    def _payload(self, url: str) -> bytes:
        payload = type(self).payload
        if payload is not None:
            return payload
        if url.startswith(("http://127.0.0.1", "http://localhost")):
            with urllib.request.urlopen(url) as response:
                return response.read()
        return b"fake-media-bytes"

    def extract_info(
        self,
        url: str,
        *,
        download: bool,
    ) -> Mapping[str, Any]:
        self.extract_calls.append((url, download))
        if url in type(self).fail_urls:
            from yt_dlp.utils import DownloadError

            raise DownloadError("ERROR: simulated download failure")

        home = Path(str(self.options["paths"]["home"]))
        template = self._template().replace(".%(ext)s", "")
        name = Path(template).name
        path = home / f"{template}.{self._extension()}"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._payload(url)
        path.write_bytes(payload)

        postprocessors = [
            entry
            for entry in self.options.get("postprocessors", ())
            if isinstance(entry, Mapping)
        ]
        if self.options.get("writethumbnail"):
            (path.parent / f"{name}.jpg").write_bytes(b"thumbnail")
        if self.options.get("writeinfojson"):
            (path.parent / f"{name}.info.json").write_text(
                '{"id": "fake"}',
                encoding="utf-8",
            )
        if self.options.get("writesubtitles"):
            languages = self.options.get("subtitleslangs") or ["en"]
            for language in languages:
                (path.parent / f"{name}.{language}.vtt").write_text(
                    "WEBVTT\n",
                    encoding="utf-8",
                )
        if any(
            entry.get("key") == "FFmpegSubtitlesConvertor"
            for entry in postprocessors
        ):
            languages = self.options.get("subtitleslangs") or ["en"]
            for language in languages:
                (path.parent / f"{name}.{language}.lrc").write_text(
                    "[00:00.00]fake\n",
                    encoding="utf-8",
                )
                (path.parent / f"{name}.{language}.vtt").unlink(
                    missing_ok=True
                )

        progress_hooks = self.options.get("progress_hooks", ())
        for hook in progress_hooks:
            hook(
                {
                    "status": "downloading",
                    "filename": str(path),
                    "downloaded_bytes": len(payload) // 2,
                    "total_bytes_estimate": len(payload),
                    "speed": 1024.0,
                    "eta": 1.0,
                }
            )
        processor_hooks = self.options.get("postprocessor_hooks", ())
        for postprocessor in postprocessors:
            for hook in processor_hooks:
                hook(
                    {
                        "status": "started",
                        "postprocessor": postprocessor.get("key"),
                        "filename": str(path),
                    }
                )
                hook(
                    {
                        "status": "finished",
                        "postprocessor": postprocessor.get("key"),
                        "filename": str(path),
                    }
                )
        for hook in progress_hooks:
            hook(
                {
                    "status": "finished",
                    "filename": str(path),
                    "downloaded_bytes": len(payload),
                    "total_bytes": len(payload),
                }
            )
        return {
            "_type": "video",
            "id": path.stem,
            "title": path.stem,
            "filepath": str(path),
        }

"""Test-only loaders.

Kept separate from ``conftest.py`` so tests can import the class directly as
well as use the fixtures.
"""

# pyright: standard

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

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

"""Helpers shared by the L3 CLI tests."""

# pyright: standard

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from fakes import FakeLoader, make_source_info, make_variant

from trackforge.acquire.config import AppConfig
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.models import (
    DownloadRequest,
    DownloadResult,
    MediaKind,
    Recommendations,
    ResolutionState,
    SourceInfo,
    SourceKind,
)
from trackforge.acquire.registry import LoaderRegistry
from trackforge.cli.commands import input as input_command

URL = "https://example.test/media/1"


class DownloadingFakeLoader(FakeLoader):
    """A fake loader that also returns a configurable download result."""

    def __init__(
        self,
        *,
        result_factory: Callable[[DownloadRequest], DownloadResult] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self._result_factory = result_factory

    def download(
        self,
        request: DownloadRequest,
        *,
        ctx: LoaderContext,
    ) -> DownloadResult:
        del ctx
        self.download_calls.append(request)
        if self._result_factory is not None:
            return self._result_factory(request)
        return DownloadResult(files=())


def install_input(
    monkeypatch: pytest.MonkeyPatch,
    *loaders: FakeLoader,
    config: AppConfig | None = None,
) -> AppConfig:
    """Patch config/registry lookup for the ``tfg input`` command."""
    app_config = config or AppConfig()

    def fake_load_config(*args: object, **kwargs: object) -> AppConfig:
        del args, kwargs
        return app_config

    def fake_registry(loaded: AppConfig) -> LoaderRegistry:
        return LoaderRegistry(loaders, loaded)

    monkeypatch.setattr(input_command, "load_config", fake_load_config)
    monkeypatch.setattr(input_command, "default_registry", fake_registry)
    return app_config


def make_video_source() -> SourceInfo:
    """A source with both audio and video recommendations."""
    audio = make_variant("audio-best", kind=MediaKind.AUDIO, container="m4a")
    video = make_variant(
        "1080p-mp4",
        kind=MediaKind.AUDIO_VIDEO,
        container="mp4",
        selector="137+140/bv*[height=1080]+ba/b[height=1080]",
        requires_merge=True,
    )
    source = make_source_info()
    item = replace(
        source.items[0],
        kind=MediaKind.AUDIO_VIDEO,
        variants=(video, audio),
        recommendations=replace(
            source.items[0].recommendations,
            audio=audio.id,
            video=video.id,
        ),
    )
    return replace(source, items=(item,))


def write_media(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"media")
    return path


def make_flat_collection() -> SourceInfo:
    """A two-item flat playlist used by the interactive playlist tests."""
    source = make_source_info()
    items = []
    for index, name in enumerate(("one", "two"), start=1):
        item = replace(
            source.items[0],
            id=name,
            title=name.title(),
            suggested_stem=name.title(),
            page_url=f"https://example.test/media/{name}",
            position=index,
            resolution=ResolutionState.FLAT,
            variants=(),
            recommendations=Recommendations(),
        )
        items.append(item)
    return replace(source, kind=SourceKind.COLLECTION, items=tuple(items))


def full_item_for(item) -> SourceInfo:
    variant = make_variant("audio-best", kind=MediaKind.AUDIO, container="m4a")
    full = replace(
        item,
        resolution=ResolutionState.FULL,
        variants=(variant,),
        recommendations=Recommendations(audio=variant.id),
    )
    return replace(
        make_source_info(),
        items=(full,),
        title=full.title,
    )


class PlaylistFakeLoader(DownloadingFakeLoader):
    """Resolve a two-item flat collection one page at a time."""

    def __init__(self, collection: SourceInfo) -> None:
        super().__init__(info=collection)
        self.collection = collection
        self.get_info_calls: list[str] = []

    def get_info(
        self,
        ref,
        *,
        ctx: LoaderContext,
        flat: bool | None = False,
        limit: int | None = None,
    ):
        del ctx
        self.info_calls.append(ref)
        self.info_requests.append((ref, flat, limit))
        self.get_info_calls.append(ref.url)
        if ref.url == self.collection.url:
            return self.collection
        for item in self.collection.items:
            if ref.url == item.page_url:
                return full_item_for(item)
        return self.collection

"""Shared fixtures/helpers for resolution application tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from trackforge.application.dto.observations import (
    AudioProbePayload,
    InitialTagsPayload,
    PathStructurePayload,
)
from trackforge.bootstrap.container import CatalogContext, build_catalog_context
from trackforge.config import TrackForgeConfig
from trackforge.domain.model.claims import Claim, Subject
from trackforge.domain.model.ids import ClaimId, ItemId, new_uuid7
from trackforge.domain.model.item import Item, ItemStatus


@pytest.fixture
def catalog_context(config: TrackForgeConfig) -> Iterator[CatalogContext]:
    """An initialized catalog context rooted in a temporary data directory."""

    with build_catalog_context(config) as context:
        yield context


@pytest.fixture
def create_item(catalog_context: CatalogContext) -> Callable[..., Any]:
    """Return an async helper that persists a bare item in a chosen status."""

    async def _create(status: ItemStatus = ItemStatus.OBSERVED) -> ItemId:
        item_id = ItemId(new_uuid7())
        async with catalog_context.unit_of_work() as uow:
            await uow.items.save(Item(id=item_id, status=status))
        return item_id

    return _create


@dataclass(frozen=True, slots=True)
class ClaimSpec:
    """Small test description for one claim."""

    field: str
    value: Any
    source: str = "initial_tags"
    confidence: float = 0.4
    subject: Subject = Subject.ITEM
    role: str | None = None
    order: int | None = None
    method: str = "test_fixture/v1"


@pytest.fixture
def append_claim(catalog_context: CatalogContext) -> Callable[..., Any]:
    """Return an async helper that appends one claim to the catalog."""

    async def _append(item_id: ItemId, spec: ClaimSpec) -> Claim:
        claim = Claim(
            id=ClaimId(new_uuid7()),
            item_id=item_id,
            subject=spec.subject,
            field=spec.field,
            value=spec.value,
            role=spec.role,
            order=spec.order,
            source=spec.source,
            method=spec.method,
            confidence=spec.confidence,
            evidence=("fixture-evidence",),
        )
        async with catalog_context.unit_of_work() as uow:
            return await uow.claims.append(claim)

    return _append


def observations(
    *,
    title: str = "Song",
    artists: tuple[str, ...] = ("Artist",),
    album: str | None = "Album",
    albumartist: tuple[str, ...] = (),
    date: str | None = None,
    genre: tuple[str, ...] = (),
    track: int | None = None,
    track_total: int | None = None,
    disc: int | None = None,
    disc_total: int | None = None,
    comment: str | None = None,
    lyrics: str | None = None,
    mbids: dict[str, list[str]] | None = None,
    path: str | None = "/music/Artist/Album/01 - Song.mp3",
    stem: str = "01 - Song",
    parent_names: tuple[str, ...] = ("music", "Artist", "Album"),
    duration: float | None = 180.0,
    chapters: int = 0,
) -> dict[str, bytes]:
    """Build a valid initial.tags/path.structure/audio.probe observation set."""

    tags = InitialTagsPayload.model_validate(
        {
            "tags": {
                "title": title,
                "artist": list(artists),
                "album": album,
                "albumartist": list(albumartist),
                "date": date,
                "genre": list(genre),
                "track": track,
                "track_total": track_total,
                "disc": disc,
                "disc_total": disc_total,
                "comment": comment,
                "lyrics": lyrics,
                "mbids": mbids or {},
                "artwork": [],
            },
            "raw": {
                "format": "MP3",
                "mime_types": ["audio/mpeg"],
                "tag_keys": ["TIT2", "TPE1"],
            },
            "reader": {"id": "mutagen", "version": "1.47"},
        }
    )
    payloads: dict[str, bytes] = {
        "initial.tags.v1": tags.model_dump_json().encode("utf-8"),
        "audio.probe.v1": _probe(duration, chapters).model_dump_json().encode("utf-8"),
    }
    if path is not None:
        path_payload = PathStructurePayload.model_validate(
            {
                "path": path,
                "parent_names": list(parent_names),
                "stem": stem,
                "suffix": ".mp3",
            }
        )
        payloads["path.structure.v1"] = path_payload.model_dump_json().encode("utf-8")
    return payloads


@pytest.fixture
def make_observations() -> Callable[..., dict[str, bytes]]:
    """Return the observation payload factory."""

    return observations


def _probe(duration: float | None, chapters: int) -> AudioProbePayload:
    return AudioProbePayload.model_validate(
        {
            "container": "mp3",
            "format_name": "mp3",
            "duration_seconds": duration,
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "mp3",
                    "duration_seconds": duration,
                }
            ],
            "chapters": [
                {"id": index, "start_seconds": float(index * 60), "title": f"Chapter {index}"}
                for index in range(chapters)
            ],
        }
    )

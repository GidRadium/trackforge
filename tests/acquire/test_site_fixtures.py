"""Every sanitized Vimeo/Bandcamp/Pinterest/VK fixture must map cleanly."""

# pyright: standard

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from trackforge.acquire.config import SourceProfile
from trackforge.acquire.loaders.ytdlp_mapper import YtDlpMapper
from trackforge.acquire.models import ResolutionState, SourceInfo, SourceKind, SourceRef

FIXTURES = Path(__file__).parent / "fixtures"

# name, expected source kind, flat mapping (the payload itself is a listing)
CASES = (
    ("vimeo_video.info.json", SourceKind.SINGLE, False),
    ("vimeo_playlist.info.json", SourceKind.COLLECTION, True),
    ("bandcamp_track.info.json", SourceKind.SINGLE, False),
    ("bandcamp_album.info.json", SourceKind.COLLECTION, False),
    ("pinterest_pin.info.json", SourceKind.SINGLE, False),
    ("vk_wall.info.json", SourceKind.COLLECTION, False),
)


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def map_fixture(info: dict[str, Any], *, flat: bool) -> SourceInfo:
    url = str(info.get("webpage_url") or "https://example.test/source")
    ref = SourceRef("yt-dlp", url, str(info.get("id") or ""))
    return YtDlpMapper(SourceProfile(name="yt_dlp")).map(
        info,
        ref,
        flat=flat,
    )


@pytest.mark.parametrize(("name", "kind", "flat"), CASES)
def test_every_site_fixture_maps_without_exceptions(
    name: str,
    kind: SourceKind,
    flat: bool,
) -> None:
    source = map_fixture(load_fixture(name), flat=flat)

    assert source.kind is kind
    assert source.items
    assert all(item.id and item.title for item in source.items)
    assert all(item.page_url for item in source.items)
    if flat:
        assert all(item.resolution is ResolutionState.FLAT for item in source.items)
        assert all(item.variants == () for item in source.items)
    else:
        assert all(item.resolution is ResolutionState.FULL for item in source.items)


FORBIDDEN_KEYS = frozenset(
    {
        "cookies",
        "http_headers",
        "manifest_url",
        "requested_formats",
        "fragments",
        "requested_subtitles",
    }
)
SIGNED_URL_MARKERS = (
    "bcbits.com/stream",
    "vimeocdn.com/exp=",
    "okcdn.ru/?expires=",
    "token=",
    "sig=",
)


def _walk(value: object):
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _walk(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk(item)
    elif isinstance(value, str):
        yield value


@pytest.mark.parametrize(("name", "_kind", "_flat"), CASES)
def test_site_fixtures_contain_no_raw_transport_secrets(
    name: str,
    _kind: SourceKind,
    _flat: bool,
) -> None:
    info = load_fixture(name)
    keys = set(_walk(info))

    assert not (keys & FORBIDDEN_KEYS)
    for value in _walk(info):
        assert not any(marker in value for marker in SIGNED_URL_MARKERS), value

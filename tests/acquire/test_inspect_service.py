"""Tests for ``AcquisitionService.inspect`` (L1.5)."""

# pyright: standard

from __future__ import annotations

import json

import pytest
from fakes import FakeLoader, make_source_info

from trackforge.acquire.config import AppConfig
from trackforge.acquire.errors import AuthRequired, UnsupportedUrl
from trackforge.acquire.loaders.base import LoaderContext
from trackforge.acquire.models import SourceInfo, SourceKind, SourceRef
from trackforge.acquire.registry import LoaderRegistry
from trackforge.acquire.service import AcquisitionService

URL = "https://example.test/media/1"


def make_service(*loaders: FakeLoader) -> AcquisitionService:
    return AcquisitionService(
        config=AppConfig(),
        registry=LoaderRegistry(loaders, AppConfig()),
    )


def test_inspect_single_resolves_and_builds_a_context() -> None:
    loader = FakeLoader(name="fake", info=make_source_info())
    service = make_service(loader)

    source = service.inspect(URL)

    assert source.source == "fake"
    assert source.url == URL
    assert source.kind is SourceKind.SINGLE
    assert source.items

    ref, flat, limit = loader.info_requests[0]
    assert ref == SourceRef("fake", URL)
    assert flat is False
    assert limit is None
    assert loader.profiles[-1].name == "fake"
    assert loader.download_calls == []


def test_inspect_forwards_flat_and_limit_to_the_loader() -> None:
    loader = FakeLoader(name="fake", info=make_source_info())
    service = make_service(loader)

    service.inspect(URL, flat=True, limit=3)

    _, flat, limit = loader.info_requests[0]
    assert flat is True
    assert limit == 3


def test_inspect_collection_keeps_kind_and_items() -> None:
    loader = FakeLoader(
        name="fake",
        info=make_source_info(kind=SourceKind.COLLECTION),
    )
    service = make_service(loader)

    source = service.inspect(URL, flat=True, limit=2)

    assert source.kind is SourceKind.COLLECTION
    assert source.items
    _, flat, limit = loader.info_requests[0]
    assert flat is True
    assert limit == 2


def test_inspect_source_override_selects_the_requested_loader() -> None:
    first = FakeLoader(name="first", priority=10)
    second = FakeLoader(name="second", priority=20)
    service = make_service(first, second)

    assert service.inspect(URL).source == "first"

    source = service.inspect(URL, source="second")

    assert source.source == "second"
    assert first.info_calls == [SourceRef("first", URL)]
    assert second.info_calls == [SourceRef("second", URL)]


def test_inspect_source_override_accepts_profile_name() -> None:
    loader = FakeLoader(name="generic", config_key="generic_profile")
    service = make_service(loader)

    source = service.inspect(URL, source="generic_profile")

    assert source.source == "generic"


def test_inspect_propagates_unsupported_url() -> None:
    loader = FakeLoader(name="fake", supports=False)
    service = make_service(loader)

    with pytest.raises(UnsupportedUrl):
        service.inspect(URL)


class AuthLoader(FakeLoader):
    def get_info(
        self,
        ref: SourceRef,
        *,
        ctx: LoaderContext,
        flat: bool | None = False,
        limit: int | None = None,
    ) -> SourceInfo:
        del ref, ctx, flat, limit
        raise AuthRequired("sign in with cookies")


def test_inspect_propagates_loader_errors_unchanged() -> None:
    loader = AuthLoader(name="fake")
    service = make_service(loader)

    with pytest.raises(AuthRequired, match="sign in"):
        service.inspect(URL)


def test_inspect_rejects_non_positive_limit() -> None:
    loader = FakeLoader(name="fake")
    service = make_service(loader)

    with pytest.raises(ValueError, match="limit"):
        service.inspect(URL, limit=0)

    assert loader.info_requests == []


def test_inspect_json_returns_the_frozen_document() -> None:
    loader = FakeLoader(name="fake", info=make_source_info())
    service = make_service(loader)

    text = service.inspect_json(URL)
    payload = json.loads(text)

    assert payload["schema_version"] == 2
    assert payload["source"] == "fake"
    assert payload["items"][0]["variants"][0]["id"] == "audio-best"
    assert loader.download_calls == []

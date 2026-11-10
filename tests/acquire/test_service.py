"""Tests for the high-level inspection service facade."""

# pyright: standard

from __future__ import annotations

from fakes import FakeLoader, make_source_info

from trackforge.acquire.config import AppConfig
from trackforge.acquire.registry import LoaderRegistry
from trackforge.acquire.service import AcquisitionService


def test_inspect_resolves_and_passes_a_context() -> None:
    loader = FakeLoader(name="fake", info=make_source_info())
    registry = LoaderRegistry([loader], AppConfig())
    service = AcquisitionService(registry=registry)

    source = service.inspect("https://example.test/media/1")

    assert source.source == "fake"
    assert source.url == "https://example.test/media/1"
    assert loader.info_calls[0].url == "https://example.test/media/1"
    assert loader.info_calls[0].source == "fake"


def test_inspect_forwards_flat_and_limit_to_the_loader() -> None:
    loader = FakeLoader(name="fake", info=make_source_info())
    registry = LoaderRegistry([loader], AppConfig())
    service = AcquisitionService(registry=registry)

    source = service.inspect("https://example.test/media/1", flat=True, limit=3)

    assert source.kind.value == "single"

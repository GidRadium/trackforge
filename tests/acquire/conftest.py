"""Shared fixtures for acquisition tests."""

# pyright: standard

from __future__ import annotations

import pytest
from fakes import FakeLoader, make_context, make_source_info

from trackforge.acquire.config import AppConfig, SourceProfile


@pytest.fixture
def source_info():
    return make_source_info()


@pytest.fixture
def fake_loader(source_info) -> FakeLoader:
    return FakeLoader(info=source_info)


@pytest.fixture
def fake_config() -> AppConfig:
    return AppConfig(
        sources={
            "default": SourceProfile(
                name="default",
                retries=7,
                socket_timeout=42.0,
                headers={"X-Default": "1"},
            ),
            "fake": SourceProfile(
                name="fake",
                retries=2,
                proxy="http://proxy.test:8080",
            ),
        }
    )


@pytest.fixture
def context():
    return make_context()

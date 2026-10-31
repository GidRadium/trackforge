"""Fixtures for the optional HTTP API tests."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.http.fake import TEST_TOKEN, FakeFacade

from trackforge.config import TrackForgeConfig
from trackforge.interfaces.http.app import create_app


@pytest.fixture
def fake_facade(config: TrackForgeConfig) -> FakeFacade:
    return FakeFacade(config)


@pytest.fixture
def http_app(config: TrackForgeConfig, fake_facade: FakeFacade) -> Any:
    return create_app(config, facade=fake_facade, token=TEST_TOKEN)


@pytest.fixture
def client(http_app: Any) -> Any:
    with TestClient(http_app) as test_client:
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


__all__ = ["auth_headers", "client", "fake_facade", "http_app"]

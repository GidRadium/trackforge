"""Redacted provider HTTP error diagnostics."""

from __future__ import annotations

import pytest

from trackforge.adapters.http.client import HttpResponse
from trackforge.domain.errors import ProviderUnavailable

pytestmark = pytest.mark.unit


def test_provider_4xx_exposes_status_and_redacted_body_excerpt() -> None:
    response = HttpResponse(
        status_code=400,
        url="https://example.test/api?token=secret",
        headers={"set-cookie": "session=secret"},
        content=(
            b'{"error":"bad key","url":"https://example.test/callback?token=secret",'
            b'"cookie":"session=secret"}'
        ),
    )

    with pytest.raises(ProviderUnavailable) as caught:
        response.raise_for_status()

    context = caught.value.context
    assert context["status"] == 400
    assert context["url"] == "https://example.test/api"
    assert "token=secret" not in context["body_excerpt"]
    assert context["body_excerpt"].startswith('{"error":"bad key"')
    assert "session=secret" not in context["body_excerpt"]
    assert "set-cookie" not in context

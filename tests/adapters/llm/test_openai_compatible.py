"""OpenAI-compatible provider tests with respx; no network is used."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from trackforge.adapters.analysis.llm import (
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
)
from trackforge.application.ports.llm import LLMRequest, LLMTask, SourceSnippet
from trackforge.application.services.prompts import PromptLibrary
from trackforge.domain.errors import (
    BudgetExceeded,
    LLMConfigurationError,
    LLMInvalidOutput,
    ProviderUnavailable,
    RateLimited,
)

pytestmark = pytest.mark.unit

BASE_URL = "https://llm.test/v1"
ENDPOINT = f"{BASE_URL}/chat/completions"
KEY_ENV = "TRACKFORGE_TEST_LLM_KEY"


def _content(
    *,
    claims: list[dict[str, Any]] | None = None,
    decisions: list[dict[str, Any]] | None = None,
    unknown: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "claims": claims or [],
            "decisions": decisions or [],
            "unknown": unknown or [],
        }
    )


def _http_response(
    content: str,
    *,
    model: str = "test-model",
    usage: dict[str, int] | None = None,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [{"message": {"content": content}}],
            "usage": usage
            if usage is not None
            else {"prompt_tokens": 1000, "completion_tokens": 500},
        },
    )


def _config(**overrides: Any) -> OpenAICompatibleConfig:
    data: dict[str, Any] = {
        "base_url": BASE_URL,
        "model": "test-model",
        "api_key_env": KEY_ENV,
        "price_in": 1.0,
        "price_out": 2.0,
        "max_retries": 1,
    }
    data.update(overrides)
    return OpenAICompatibleConfig(**data)


def _request(**overrides: Any) -> LLMRequest:
    data: dict[str, Any] = {
        "task": LLMTask.EXTRACT,
        "claims": (),
        "source_snippets": (
            SourceSnippet(
                text="Song (Nightcore) [Official Video]",
                source="source_metadata",
                label="comment",
            ),
        ),
        "target_fields": ("title", "artist"),
    }
    data.update(overrides)
    return LLMRequest(**data)


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch, clean_env: None) -> None:
    monkeypatch.setenv(KEY_ENV, "secret-test-key")


@pytest.mark.asyncio
@respx.mock
async def test_dotenv_key_fallback(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(f"# test\n{KEY_ENV}=from-dotenv\n", encoding="utf-8")
    route = respx.post(ENDPOINT).mock(return_value=_http_response(_content(unknown=["title"])))
    provider = OpenAICompatibleProvider(
        _config(dotenv_path=str(env_file)),
        prompts=PromptLibrary(),
    )

    response = await provider.complete(_request())

    assert response.unknown == ("title",)
    assert route.calls[0].request.headers["Authorization"] == "Bearer from-dotenv"


@pytest.mark.asyncio
@respx.mock
async def test_json_mode_and_thinking_payload_for_deepseek() -> None:
    route = respx.post(ENDPOINT).mock(return_value=_http_response(_content(unknown=["title"])))
    provider = OpenAICompatibleProvider(
        _config(strict_schema=False, thinking="disabled"),
        prompts=PromptLibrary(),
    )

    await provider.complete(_request())

    body = json.loads(route.calls[0].request.content)
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
@respx.mock
async def test_extract_success_prompt_and_cost() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=_http_response(
            _content(
                claims=[
                    {
                        "field": "title",
                        "value": "Song",
                        "confidence": 0.8,
                        "evidence_claim_ids": [],
                        "quote": "Song (Nightcore)",
                        "reason": "base title",
                    }
                ],
                unknown=["album"],
            )
        )
    )
    provider = OpenAICompatibleProvider(_config(), prompts=PromptLibrary())

    response = await provider.complete(_request())

    assert len(response.claims) == 1
    assert response.claims[0].value == "Song"
    assert response.model == "test-model"
    assert response.prompt_version == "extract/v1"
    assert response.usage.input_tokens == 1000
    assert response.cost_usd == pytest.approx(0.002)
    assert response.warnings == ()
    body = json.loads(route.calls[0].request.content)
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["messages"][0]["role"] == "system"
    assert "UNTRUSTED SOURCE TEXT" in body["messages"][1]["content"]
    assert "secret-test-key" not in json.dumps(body)


@pytest.mark.asyncio
@respx.mock
async def test_invalid_json_is_retried_once_then_fails() -> None:
    route = respx.post(ENDPOINT).mock(return_value=_http_response("this is not json"))
    provider = OpenAICompatibleProvider(
        _config(),
        prompts=PromptLibrary(),
        sleep=_no_sleep,
    )

    with pytest.raises(LLMInvalidOutput):
        await provider.complete(_request())

    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_invalid_json_retry_can_succeed() -> None:
    route = respx.post(ENDPOINT).mock(
        side_effect=[
            _http_response("not json"),
            _http_response(_content(unknown=["title"])),
        ]
    )
    provider = OpenAICompatibleProvider(
        _config(),
        prompts=PromptLibrary(),
        sleep=_no_sleep,
    )

    response = await provider.complete(_request())

    assert response.unknown == ("title",)
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_429_is_retried_with_backoff() -> None:
    route = respx.post(ENDPOINT).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            _http_response(_content(unknown=["title"])),
        ]
    )
    sleeps: list[float] = []

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    provider = OpenAICompatibleProvider(
        _config(),
        prompts=PromptLibrary(),
        sleep=_sleep,
    )

    response = await provider.complete(_request())

    assert response.unknown == ("title",)
    assert route.call_count == 2
    assert len(sleeps) == 1
    assert sleeps[0] >= 0.0


@pytest.mark.asyncio
@respx.mock
async def test_429_exhausted_raises_rate_limited() -> None:
    respx.post(ENDPOINT).mock(return_value=httpx.Response(429))
    provider = OpenAICompatibleProvider(
        _config(max_retries=0),
        prompts=PromptLibrary(),
        sleep=_no_sleep,
    )

    with pytest.raises(RateLimited):
        await provider.complete(_request())


@pytest.mark.asyncio
@respx.mock
async def test_401_is_not_retried() -> None:
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(401))
    provider = OpenAICompatibleProvider(_config(), prompts=PromptLibrary())

    with pytest.raises(ProviderUnavailable) as caught:
        await provider.complete(_request())

    assert caught.value.retryable is False
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_schema_rejection_falls_back_to_json_mode() -> None:
    route = respx.post(ENDPOINT).mock(
        side_effect=[
            httpx.Response(
                400,
                json={"error": {"message": "response_format json_schema is unsupported"}},
            ),
            _http_response(_content(unknown=["title"])),
        ]
    )
    provider = OpenAICompatibleProvider(_config(), prompts=PromptLibrary())

    response = await provider.complete(_request())

    assert response.unknown == ("title",)
    assert any("JSON mode fallback" in warning for warning in response.warnings)
    assert route.call_count == 2
    second = json.loads(route.calls[1].request.content)
    assert second["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
@respx.mock
async def test_unknown_field_in_output_is_rejected() -> None:
    payload = json.loads(_content(unknown=["title"]))
    payload["extra"] = "not allowed"
    respx.post(ENDPOINT).mock(return_value=_http_response(json.dumps(payload)))
    provider = OpenAICompatibleProvider(_config(), prompts=PromptLibrary())

    with pytest.raises(LLMInvalidOutput):
        await provider.complete(_request())


@pytest.mark.asyncio
@respx.mock
async def test_budget_is_enforced_after_usage() -> None:
    respx.post(ENDPOINT).mock(return_value=_http_response(_content(unknown=["title"])))
    provider = OpenAICompatibleProvider(
        _config(),
        prompts=PromptLibrary(),
    )

    with pytest.raises(BudgetExceeded):
        await provider.complete(_request(max_cost_usd=0.0001))


@pytest.mark.asyncio
async def test_missing_api_key_does_not_call_network() -> None:
    provider = OpenAICompatibleProvider(
        _config(api_key_env="TRACKFORGE_ABSENT_KEY"),
        prompts=PromptLibrary(),
    )

    with pytest.raises(LLMConfigurationError):
        await provider.complete(_request())


@pytest.mark.asyncio
async def test_missing_endpoint_or_model_is_configuration_error() -> None:
    provider = OpenAICompatibleProvider(
        _config(base_url="", model=""),
        prompts=PromptLibrary(),
    )

    with pytest.raises(LLMConfigurationError):
        await provider.complete(_request())


@pytest.mark.asyncio
@respx.mock
async def test_missing_usage_yields_null_cost_and_warning() -> None:
    response = _http_response(_content(unknown=["title"]))
    document = response.json()
    document.pop("usage")
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=document))
    provider = OpenAICompatibleProvider(_config(), prompts=PromptLibrary())

    result = await provider.complete(_request())

    assert result.cost_usd is None
    assert any("cost_usd is null" in warning for warning in result.warnings)


async def _no_sleep(_delay: float) -> None:
    return None

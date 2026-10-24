"""OpenAI-compatible chat-completions adapter for the LLM port.

Uses ``httpx`` directly: no provider SDK, no default endpoint, and the API key
is read from the environment only.  Strict JSON schema is preferred; when an
endpoint rejects it, one JSON-mode fallback is attempted and the degradation is
reported as a warning.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from trackforge.application.ports.llm import (
    LLMClaim,
    LLMDecision,
    LLMRequest,
    LLMResponse,
    LLMUsage,
)
from trackforge.application.services.llm_guardrails import validate_payload
from trackforge.application.services.prompts import OUTPUT_SCHEMA, PromptLibrary, RenderedPrompt
from trackforge.domain.errors import (
    BudgetExceeded,
    LLMConfigurationError,
    LLMInvalidOutput,
    ProviderUnavailable,
    RateLimited,
)

_SLEEP = Callable[[float], Awaitable[None]]


class _RawClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    value: Any = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_claim_ids: list[str] = Field(default_factory=list)
    quote: str | None = None
    reason: str = ""


class _RawDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    value: Any = None
    chosen_claim_ids: list[str] = Field(default_factory=list)
    derived_from: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""


class _RawOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[_RawClaim] = Field(default_factory=list)
    decisions: list[_RawDecision] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class OpenAICompatibleConfig:
    """Non-secret provider configuration plus optional token prices."""

    base_url: str = ""
    model: str = ""
    api_key_env: str = "TRACKFORGE_LLM_API_KEY"
    price_in: float | None = None
    price_out: float | None = None
    timeout_seconds: float = 30.0
    max_retries: int = 1
    max_tokens: int = 2_000
    temperature: float = 0.1
    strict_schema: bool = True
    thinking: str = "default"
    dotenv_path: str | None = None
    proxy: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip() and self.model.strip())

    @property
    def api_key_present(self) -> bool:
        return bool(self._resolve_api_key())

    def _resolve_api_key(self) -> str:
        name = self.api_key_env.strip()
        if not name:
            return ""
        value = os.environ.get(name, "").strip()
        if value:
            return value
        if self.dotenv_path:
            return _read_dotenv_value(Path(self.dotenv_path).expanduser(), name)
        return ""


class OpenAICompatibleProvider:
    """HTTP implementation of :class:`LLMProvider`."""

    def __init__(
        self,
        config: OpenAICompatibleConfig,
        *,
        prompts: PromptLibrary | None = None,
        client: httpx.AsyncClient | None = None,
        sleep: _SLEEP = asyncio.sleep,
        rng: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._prompts = prompts or PromptLibrary()
        self._client = client
        self._sleep = sleep
        self._rng = rng or (lambda: 0.0)

    @property
    def config(self) -> OpenAICompatibleConfig:
        return self._config

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Render the prompt, call the endpoint, and parse strict output."""

        if not self._config.configured:
            raise LLMConfigurationError(
                "LLM base_url and model are required for non-policy modes",
                context={
                    "base_url_configured": bool(self._config.base_url.strip()),
                    "model_configured": bool(self._config.model.strip()),
                },
            )
        api_key = self._config._resolve_api_key()
        if not api_key:
            raise LLMConfigurationError(
                "LLM API key is not set",
                context={"api_key_env": self._config.api_key_env},
            )
        rendered = self._prompts.render(request)
        payload = self._build_payload(
            request,
            rendered,
            mode="json_schema" if self._config.strict_schema else "json_object",
        )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        warnings: list[str] = []
        invalid_attempts = 1
        while True:
            document, degraded = await self._request_json(payload, headers, warnings)
            if degraded:
                warnings.append("provider rejected json_schema; used JSON mode fallback")
            try:
                return self._parse_response(
                    document,
                    request=request,
                    rendered=rendered,
                    warnings=warnings,
                )
            except LLMInvalidOutput:
                if invalid_attempts >= max(0, self._config.max_retries) + 1:
                    raise
                invalid_attempts += 1
                warnings.append("invalid LLM output; retrying once")

    def _build_payload(
        self,
        request: LLMRequest,
        rendered: RenderedPrompt,
        *,
        mode: str,
    ) -> dict[str, Any]:
        schema = request.schema or OUTPUT_SCHEMA
        if mode == "json_schema":
            response_format: dict[str, Any] = {
                "type": "json_schema",
                "json_schema": {
                    "name": f"trackforge_{request.task.value}",
                    "strict": True,
                    "schema": _strict_schema(schema),
                },
            }
        else:
            response_format = {"type": "json_object"}
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": rendered.system},
                {"role": "user", "content": rendered.user},
            ],
            "temperature": request.temperature
            if request.temperature != 0.1
            else self._config.temperature,
            "max_tokens": request.max_tokens or self._config.max_tokens,
            "response_format": response_format,
        }
        if self._config.thinking in {"enabled", "disabled"}:
            payload["thinking"] = {"type": self._config.thinking}
        return payload

    async def _request_json(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        warnings: list[str],
    ) -> tuple[dict[str, Any], bool]:
        client = self._client
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(
                timeout=self._config.timeout_seconds,
                proxy=self._config.proxy,
                trust_env=False,
            )
        degraded = False
        attempts = max(0, self._config.max_retries) + 1
        invalid_output_attempts = 1
        try:
            for attempt in range(attempts):
                try:
                    response = await client.post(
                        _completions_url(self._config.base_url),
                        headers=headers,
                        json=payload,
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt + 1 < attempts:
                        await self._sleep_then_retry(attempt)
                        continue
                    raise ProviderUnavailable(
                        "LLM endpoint is unreachable",
                        context={"error": type(exc).__name__},
                    ) from None

                if response.status_code == 429:
                    if attempt + 1 < attempts:
                        await self._sleep_then_retry(
                            attempt,
                            retry_after=response.headers.get("Retry-After"),
                        )
                        continue
                    raise RateLimited(
                        "LLM endpoint rate-limited the request",
                        context={"status": response.status_code},
                    )
                if response.status_code >= 500:
                    if attempt + 1 < attempts:
                        await self._sleep_then_retry(attempt)
                        continue
                    raise ProviderUnavailable(
                        "LLM endpoint returned a server error",
                        context={"status": response.status_code},
                    )
                if response.status_code in {401, 403}:
                    raise ProviderUnavailable(
                        "LLM endpoint rejected the credential",
                        retryable=False,
                        user_action="Check api_key_env and the endpoint permissions.",
                        context={"status": response.status_code},
                    )
                if response.status_code == 400 and not degraded and self._config.strict_schema:
                    if _schema_rejected(response):
                        degraded = True
                        warnings.append("strict schema unsupported by endpoint")
                        fallback = dict(payload)
                        fallback["response_format"] = {"type": "json_object"}
                        payload = fallback
                        # The capability fallback is not one of the retry attempts.
                        return await self._request_json_once(client, payload, headers), True
                    raise ProviderUnavailable(
                        "LLM endpoint rejected the request",
                        retryable=False,
                        user_action="Check the configured model and request limits.",
                        context={"status": response.status_code},
                    )
                if response.status_code >= 400:
                    raise ProviderUnavailable(
                        "LLM endpoint rejected the request",
                        retryable=False,
                        context={"status": response.status_code},
                    )

                try:
                    document = _decode_document(response)
                except LLMInvalidOutput:
                    if invalid_output_attempts < attempts:
                        invalid_output_attempts += 1
                        continue
                    raise
                return document, degraded
            raise ProviderUnavailable("LLM endpoint did not return a response")
        finally:
            if owns_client:
                await client.aclose()

    async def _request_json_once(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        # This helper owns the fallback attempt; transient failures surface
        # directly because the schema fallback must not multiply retries.
        try:
            response = await client.post(
                _completions_url(self._config.base_url),
                headers=headers,
                json=payload,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderUnavailable(
                "LLM endpoint is unreachable during JSON-mode fallback",
                context={"error": type(exc).__name__},
            ) from None
        if response.status_code == 429:
            raise RateLimited("LLM endpoint rate-limited the request")
        if response.status_code >= 400:
            raise ProviderUnavailable(
                "LLM endpoint rejected the JSON-mode fallback",
                retryable=False,
                context={"status": response.status_code},
            )
        return _decode_document(response)

    async def _sleep_then_retry(self, attempt: int, *, retry_after: str | None = None) -> None:
        delay = min(8.0, 0.5 * (2**attempt)) + self._rng()
        if retry_after:
            try:
                delay = max(delay, min(30.0, float(retry_after)))
            except ValueError:
                pass
        await self._sleep(delay)

    def _parse_response(
        self,
        document: Mapping[str, Any],
        *,
        request: LLMRequest,
        rendered: RenderedPrompt,
        warnings: list[str],
    ) -> LLMResponse:
        raw = _extract_content(document)
        schema = request.schema or OUTPUT_SCHEMA
        validate_payload(raw, schema)
        try:
            parsed = _RawOutput.model_validate(raw)
        except ValidationError as exc:
            raise LLMInvalidOutput(
                "LLM output failed strict schema validation",
                context={"errors": exc.errors(include_url=False)[:5]},
            ) from None

        claims = tuple(
            LLMClaim(
                field=item.field,
                value=item.value,
                confidence=item.confidence,
                evidence_claim_ids=tuple(item.evidence_claim_ids),
                quote=item.quote,
                reason=item.reason,
            )
            for item in parsed.claims
        )
        decisions = tuple(
            LLMDecision(
                field=item.field,
                value=item.value,
                chosen_claim_ids=tuple(item.chosen_claim_ids),
                derived_from=tuple(item.derived_from),
                confidence=item.confidence,
                reason=item.reason,
            )
            for item in parsed.decisions
        )
        usage = _usage(document)
        cost = self._cost(usage)
        if cost is None:
            if self._config.price_in is None or self._config.price_out is None:
                warnings.append("cost_usd is null: price_in/price_out are not configured")
            else:
                warnings.append("cost_usd is null: the endpoint did not report token usage")
        if cost is not None and request.max_cost_usd is not None:
            if cost > request.max_cost_usd:
                raise BudgetExceeded(
                    "LLM cost exceeds the per-item budget",
                    context={
                        "cost_usd": cost,
                        "max_cost_usd_per_item": request.max_cost_usd,
                    },
                )
        if cost is None and request.max_cost_usd is not None:
            warnings.append("budget could not be enforced: cost is unknown")
        response_model = document.get("model")
        model = (
            response_model
            if isinstance(response_model, str) and response_model.strip()
            else self._config.model
        )
        return LLMResponse(
            claims=claims,
            decisions=decisions,
            unknown=tuple(parsed.unknown),
            model=model,
            prompt_version=rendered.version,
            usage=usage,
            cost_usd=cost,
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _cost(self, usage: LLMUsage) -> float | None:
        if (
            usage.input_tokens is None
            or usage.output_tokens is None
            or self._config.price_in is None
            or self._config.price_out is None
        ):
            return None
        return round(
            usage.input_tokens / 1_000_000 * self._config.price_in
            + usage.output_tokens / 1_000_000 * self._config.price_out,
            8,
        )


def _read_dotenv_value(path: Path, name: str) -> str:
    """Read one KEY=value from a dotenv file without exposing other secrets."""

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        if not separator or key.strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value
    return ""


def _completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def _schema_rejected(response: httpx.Response) -> bool:
    text = response.text.lower()
    return any(
        marker in text
        for marker in (
            "response_format",
            "json_schema",
            "structured output",
            "unsupported parameter",
            "unknown parameter",
        )
    )


def _decode_document(response: httpx.Response) -> dict[str, Any]:
    try:
        document = response.json()
    except ValueError:
        raise LLMInvalidOutput(
            "LLM endpoint returned non-JSON HTTP content",
            context={"status": response.status_code},
        ) from None
    if not isinstance(document, Mapping):
        raise LLMInvalidOutput("LLM endpoint response must be a JSON object")
    return dict(document)


def _extract_content(document: Mapping[str, Any]) -> dict[str, Any]:
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMInvalidOutput("LLM response has no choices")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise LLMInvalidOutput("LLM response choice is not an object")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise LLMInvalidOutput("LLM response has no message")
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return _decode_json_content(content)
    if isinstance(content, Mapping):
        return dict(content)
    parsed = message.get("parsed")
    if isinstance(parsed, Mapping):
        return dict(parsed)
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        call = tool_calls[0]
        if isinstance(call, Mapping):
            function = call.get("function")
            if isinstance(function, Mapping):
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    return _decode_json_content(arguments)
    raise LLMInvalidOutput("LLM response message has no JSON content")


def _decode_json_content(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMInvalidOutput(
            "LLM response content is not valid JSON",
            context={"error": exc.msg, "line": exc.lineno, "column": exc.colno},
        ) from None
    if not isinstance(decoded, Mapping):
        raise LLMInvalidOutput("LLM response content must be a JSON object")
    return dict(decoded)


def _usage(document: Mapping[str, Any]) -> LLMUsage:
    usage = document.get("usage")
    if not isinstance(usage, Mapping):
        return LLMUsage()
    return LLMUsage(
        input_tokens=_optional_int(usage.get("prompt_tokens", usage.get("input_tokens"))),
        output_tokens=_optional_int(usage.get("completion_tokens", usage.get("output_tokens"))),
    )


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _strict_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Convert our JSON schema to the strict-provider subset."""

    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "type" and isinstance(value, (list, tuple)):
            result["anyOf"] = [{"type": item} for item in value]
        elif isinstance(value, Mapping):
            result[key] = _strict_schema(value)
        elif isinstance(value, (list, tuple)):
            result[key] = [
                _strict_schema(item) if isinstance(item, Mapping) else item for item in value
            ]
        else:
            result[key] = value
    return result


__all__ = ["OpenAICompatibleConfig", "OpenAICompatibleProvider"]

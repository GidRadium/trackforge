"""Budget/cost behavior for LLM-assisted and autopilot resolution."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from trackforge.application.ports.llm import (
    LLMDecision,
    LLMRequest,
    LLMResponse,
    LLMUsage,
)
from trackforge.application.services.llm_guardrails import LLMGuardrails
from trackforge.application.use_cases.resolve_item import ResolveItem
from trackforge.bootstrap.container import CatalogContext
from trackforge.domain.errors import BudgetExceeded, DomainValidationError
from trackforge.domain.model.decisions import DecisionMode, DecisionRecord
from trackforge.domain.model.ids import DecisionRecordId, ItemId, new_uuid7

pytestmark = pytest.mark.unit


class _FakeProvider:
    def __init__(
        self,
        *,
        response: LLMResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if self.response is None:
            raise AssertionError("fake provider has no response")
        return self.response


def test_llm_response_accepts_usage_mapping() -> None:
    response = LLMResponse(
        model="test-model",
        prompt_version="extract/v1",
        usage={"input_tokens": 7, "output_tokens": 3},  # type: ignore[arg-type]
    )

    assert response.usage.input_tokens == 7
    assert response.usage["output_tokens"] == 3
    assert dict(response.usage) == {"input_tokens": 7, "output_tokens": 3}


def test_decision_record_accepts_cost_and_usage() -> None:
    record = DecisionRecord(
        id=DecisionRecordId(new_uuid7()),
        item_id=ItemId(new_uuid7()),
        version=1,
        llm_model="test-model",
        prompt_version="adjudicate/v1",
        llm_cost_usd=0.0123,
        llm_usage={"input_tokens": 100, "output_tokens": 50},
    )

    assert record.llm_cost_usd == pytest.approx(0.0123)
    assert record.llm_usage == {"input_tokens": 100, "output_tokens": 50}

    with pytest.raises(DomainValidationError):
        DecisionRecord(
            id=DecisionRecordId(new_uuid7()),
            item_id=ItemId(new_uuid7()),
            version=1,
            llm_cost_usd=-1.0,
        )


@pytest.mark.asyncio
async def test_provider_budget_error_falls_back_to_policy(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await create_item()
    claim = await append_claim(
        item_id,
        _spec("title", "Policy Song", source="initial_tags", confidence=0.5),
    )
    provider = _FakeProvider(error=BudgetExceeded("over budget"))
    resolver = ResolveItem(
        catalog_context.unit_of_work,
        llm_provider=provider,
        guardrails=LLMGuardrails(max_cost_usd_per_item=0.01),
        max_cost_usd_per_item=0.01,
    )

    result = await resolver.resolve(item_id, DecisionMode.LLM_AUTOPILOT)

    assert result.fields["title"] == "Policy Song"
    assert any("budget_exceeded" in warning for warning in result.warnings)
    async with catalog_context.unit_of_work() as uow:
        records = await uow.decisions.list_records(item_id)
    assert records[-1].llm_cost_usd is None
    # The policy decision still points at the original claim.
    assert records[-1].decision_for("title") is not None
    assert records[-1].decision_for("title").decided_by.value == "policy"
    _ = claim


@pytest.mark.asyncio
async def test_response_over_budget_is_not_applied_but_cost_is_recorded(
    catalog_context: CatalogContext,
    create_item: Callable[..., Any],
    append_claim: Callable[..., Any],
) -> None:
    item_id = await create_item()
    claim = await append_claim(
        item_id,
        _spec("title", "Policy Song", source="initial_tags", confidence=0.5),
    )
    provider = _FakeProvider(
        response=LLMResponse(
            decisions=(
                LLMDecision(
                    field="title",
                    value="Policy Song",
                    chosen_claim_ids=(str(claim.id),),
                    confidence=0.9,
                ),
            ),
            model="fake-model",
            prompt_version="autopilot/v1",
            usage=LLMUsage(input_tokens=2000, output_tokens=1000),
            cost_usd=0.5,
        )
    )
    resolver = ResolveItem(
        catalog_context.unit_of_work,
        llm_provider=provider,
        guardrails=LLMGuardrails(max_cost_usd_per_item=0.01),
        max_cost_usd_per_item=0.01,
    )

    result = await resolver.resolve(item_id, DecisionMode.LLM_AUTOPILOT)

    assert result.fields["title"] == "Policy Song"
    assert any("budget_exceeded" in warning for warning in result.warnings)
    async with catalog_context.unit_of_work() as uow:
        record = (await uow.decisions.list_records(item_id))[-1]
    assert record.llm_cost_usd == pytest.approx(0.5)
    assert record.decision_for("title").decided_by.value == "policy"


def _spec(
    field: str,
    value: Any,
    *,
    source: str = "musicbrainz",
    confidence: float = 0.9,
) -> Any:
    from tests.application.conftest import ClaimSpec

    return ClaimSpec(field=field, value=value, source=source, confidence=confidence)

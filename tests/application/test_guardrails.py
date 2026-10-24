"""Guardrail tests: grounding, IDs, synthesis, autopilot, and locks."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from trackforge.application.ports.llm import (
    LLMClaim,
    LLMDecision,
    LLMResponse,
    LLMUsage,
)
from trackforge.application.services.llm_guardrails import (
    LLMGuardrails,
    validate_payload,
)
from trackforge.application.services.prompts import OUTPUT_SCHEMA
from trackforge.domain.errors import BudgetExceeded, LLMInvalidOutput
from trackforge.domain.model.claims import Claim
from trackforge.domain.model.decisions import DecisionMode
from trackforge.domain.model.ids import new_uuid7

pytestmark = pytest.mark.unit

MBID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_MBID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _response(**overrides: object) -> LLMResponse:
    data: dict[str, object] = {
        "model": "fake-model",
        "prompt_version": "autopilot/v1",
        "usage": LLMUsage(input_tokens=100, output_tokens=20),
        "cost_usd": 0.001,
    }
    data.update(overrides)
    return LLMResponse(**data)  # type: ignore[arg-type]


def test_new_mbid_is_rejected(make_claim: Callable[..., Claim]) -> None:
    existing = make_claim(field="mb_recording_id", value=MBID, source="musicbrainz")
    response = _response(
        decisions=(
            LLMDecision(
                field="mb_recording_id",
                value=OTHER_MBID,
                chosen_claim_ids=(str(existing.id),),
                confidence=0.9,
                reason="hallucinated",
            ),
        )
    )

    result = LLMGuardrails().check_response(
        response,
        claims=(existing,),
        target_fields=("mb_recording_id",),
        mode=DecisionMode.LLM_ASSISTED,
    )

    assert result.decisions == ()
    assert any(item.code == "invented_id" for item in result.rejections)


def test_existing_mbid_can_be_selected(make_claim: Callable[..., Claim]) -> None:
    existing = make_claim(field="mb_recording_id", value=MBID, source="musicbrainz")
    response = _response(
        decisions=(
            LLMDecision(
                field="mb_recording_id",
                value=MBID,
                chosen_claim_ids=(str(existing.id),),
                confidence=0.9,
                reason="select authority id",
            ),
        )
    )

    result = LLMGuardrails().check_response(
        response,
        claims=(existing,),
        target_fields=("mb_recording_id",),
        mode=DecisionMode.LLM_ASSISTED,
    )

    assert len(result.decisions) == 1


def test_invented_url_is_rejected(make_claim: Callable[..., Claim]) -> None:
    existing = make_claim(field="artwork", value="https://example.test/a.png")
    response = _response(
        claims=(
            LLMClaim(
                field="artwork",
                value="https://evil.test/invented.png",
                confidence=0.8,
                quote="cover",
                reason="invented",
            ),
        )
    )

    result = LLMGuardrails().check_response(
        response,
        claims=(existing,),
        target_fields=("artwork",),
    )

    assert result.claims == ()
    assert any(item.code == "invented_reference" for item in result.rejections)


def test_synthesis_requires_derived_from(make_claim: Callable[..., Claim]) -> None:
    title = make_claim(field="title", value="Song")
    response = _response(
        decisions=(
            LLMDecision(
                field="title",
                value="Song (Nightcore)",
                chosen_claim_ids=(str(title.id),),
                confidence=0.8,
                reason="synthesized without derived_from",
            ),
        )
    )

    result = LLMGuardrails().check_response(
        response,
        claims=(title,),
        target_fields=("title",),
    )

    assert result.decisions == ()
    assert any(item.code == "synthesis_without_derived_from" for item in result.rejections)


def test_synthesis_with_derived_from_is_accepted(make_claim: Callable[..., Claim]) -> None:
    title = make_claim(field="title", value="Song")
    derivative = make_claim(field="transformation", value="nightcore")
    response = _response(
        decisions=(
            LLMDecision(
                field="title",
                value="Song (Nightcore)",
                derived_from=(str(title.id), str(derivative.id)),
                confidence=0.8,
                reason="title plus transformation",
            ),
        )
    )

    result = LLMGuardrails().check_response(
        response,
        claims=(title, derivative),
        target_fields=("title",),
    )

    assert len(result.decisions) == 1
    assert result.decisions[0].derived_from == (str(title.id), str(derivative.id))


def test_unknown_claim_id_is_rejected(make_claim: Callable[..., Claim]) -> None:
    title = make_claim(field="title", value="Song")
    response = _response(
        decisions=(
            LLMDecision(
                field="title",
                value="Song",
                chosen_claim_ids=(str(new_uuid7()),),
                confidence=0.8,
                reason="bad id",
            ),
        )
    )

    result = LLMGuardrails().check_response(
        response,
        claims=(title,),
        target_fields=("title",),
    )

    assert result.decisions == ()
    assert any(item.code == "unknown_claim_id" for item in result.rejections)


def test_autopilot_id_field_is_forbidden(make_claim: Callable[..., Claim]) -> None:
    existing = make_claim(field="mb_recording_id", value=MBID, source="musicbrainz")
    response = _response(
        decisions=(
            LLMDecision(
                field="mb_recording_id",
                value=MBID,
                chosen_claim_ids=(str(existing.id),),
                confidence=0.9,
                reason="id",
            ),
        )
    )

    result = LLMGuardrails().check_response(
        response,
        claims=(existing,),
        target_fields=("mb_recording_id",),
        mode=DecisionMode.LLM_AUTOPILOT,
    )

    assert result.decisions == ()
    assert any(item.code == "autopilot_id_forbidden" for item in result.rejections)


def test_autopilot_field_allowlist(make_claim: Callable[..., Claim]) -> None:
    claim = make_claim(field="lyrics", value="la la")
    response = _response(
        decisions=(
            LLMDecision(
                field="lyrics",
                value="la la",
                chosen_claim_ids=(str(claim.id),),
                confidence=0.8,
                reason="not allowed",
            ),
        )
    )

    result = LLMGuardrails().check_response(
        response,
        claims=(claim,),
        target_fields=("lyrics",),
        mode=DecisionMode.LLM_AUTOPILOT,
    )

    assert result.decisions == ()
    assert any(item.code == "autopilot_field_forbidden" for item in result.rejections)


def test_locked_field_is_rejected(make_claim: Callable[..., Claim]) -> None:
    claim = make_claim(field="title", value="Song")
    response = _response(
        decisions=(
            LLMDecision(
                field="title",
                value="Song",
                chosen_claim_ids=(str(claim.id),),
                confidence=0.9,
            ),
        )
    )

    result = LLMGuardrails().check_response(
        response,
        claims=(claim,),
        target_fields=("title",),
        locked_fields=("title",),
    )

    assert result.decisions == ()
    assert any(item.code == "locked_field" for item in result.rejections)


def test_claim_without_evidence_is_rejected() -> None:
    response = _response(
        claims=(
            LLMClaim(
                field="title",
                value="Invented",
                confidence=0.9,
                quote=None,
                reason="no evidence",
            ),
        )
    )

    result = LLMGuardrails().check_response(response, claims=(), target_fields=("title",))

    assert result.claims == ()
    assert any(item.code == "missing_evidence" for item in result.rejections)


def test_invented_url_inside_list_is_rejected(make_claim: Callable[..., Claim]) -> None:
    existing = make_claim(field="artwork", value="https://example.test/a.png")
    response = _response(
        decisions=(
            LLMDecision(
                field="artwork",
                value=["https://example.test/a.png", "https://evil.test/b.png"],
                chosen_claim_ids=(str(existing.id),),
                derived_from=(str(existing.id),),
                confidence=0.8,
            ),
        )
    )

    result = LLMGuardrails().check_response(
        response,
        claims=(existing,),
        target_fields=("artwork",),
    )

    assert result.decisions == ()
    assert any(item.code == "invented_reference" for item in result.rejections)


def test_unknown_decision_cannot_carry_evidence(
    make_claim: Callable[..., Claim],
) -> None:
    claim = make_claim(field="title", value="Song")
    response = _response(
        decisions=(
            LLMDecision(
                field="title",
                value=None,
                chosen_claim_ids=(str(claim.id),),
                confidence=0.2,
            ),
        )
    )

    result = LLMGuardrails().check_response(
        response,
        claims=(claim,),
        target_fields=("title",),
    )

    assert result.decisions == ()
    assert any(item.code == "unknown_with_evidence" for item in result.rejections)


def test_budget_exceeded() -> None:
    response = _response(cost_usd=0.20)

    with pytest.raises(BudgetExceeded):
        LLMGuardrails(max_cost_usd_per_item=0.05).check_response(
            response,
            claims=(),
            target_fields=(),
        )


def test_schema_validation_accepts_request_normalized_schema() -> None:
    from trackforge.application.ports.llm import LLMRequest, LLMTask

    payload = {
        "claims": [
            {
                "field": "title",
                "value": "Song",
                "confidence": 0.8,
                "evidence_claim_ids": [],
                "quote": "Song",
                "reason": "quoted",
            }
        ],
        "decisions": [],
        "unknown": [],
    }
    normalized = LLMRequest(task=LLMTask.EXTRACT, schema=OUTPUT_SCHEMA).schema

    validate_payload(payload, normalized)


def test_schema_rejects_unknown_fields() -> None:
    payload = {
        "claims": [],
        "decisions": [],
        "unknown": [],
        "free_form": "not allowed",
    }

    with pytest.raises(LLMInvalidOutput) as caught:
        validate_payload(payload, OUTPUT_SCHEMA)

    assert "unknown fields" in caught.value.message


def test_schema_rejects_missing_required_fields() -> None:
    with pytest.raises(LLMInvalidOutput):
        validate_payload({"claims": [], "unknown": []}, OUTPUT_SCHEMA)

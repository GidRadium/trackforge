"""Model-registry and LLM cost persistence contracts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from trackforge.adapters.persistence.sqlite.unit_of_work import SqliteUnitOfWork
from trackforge.application.ports.models import ModelInfo, ModelKind, ModelStatus
from trackforge.domain.model.decisions import (
    Decision,
    DecisionMode,
    DecisionRecord,
)
from trackforge.domain.model.ids import ClaimId, DecisionRecordId, new_uuid7
from trackforge.domain.model.item import Item

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_model_registry_round_trip(
    uow_factory: Callable[[], SqliteUnitOfWork],
) -> None:
    model = ModelInfo(
        id="ranker-test",
        version="2026-10-23",
        kind=ModelKind.RANKER,
        license="MIT",
        status=ModelStatus.AVAILABLE,
        path="/models/model.onnx",
        checksum="a" * 64,
        size_bytes=123,
        source="test",
        heavy=True,
        metadata={"strategy": "cross_encoder"},
    )

    async with uow_factory() as uow:
        saved = await uow.models.upsert_model(model)
        assert saved.id == "ranker-test"

    async with uow_factory() as uow:
        loaded = await uow.models.get_model("ranker-test")
        assert loaded is not None
        assert loaded.license == "MIT"
        assert loaded.status is ModelStatus.AVAILABLE
        assert loaded.kind is ModelKind.RANKER
        assert loaded.metadata == {"strategy": "cross_encoder"}

        # Upsert, not duplicate.
        changed = ModelInfo(
            id="ranker-test",
            version="2",
            kind=ModelKind.RANKER,
            license="MIT",
            status=ModelStatus.MISSING,
        )
        await uow.models.upsert_model(changed)
        records = await uow.models.list_models()
        assert len(records) == 1
        assert records[0].version == "2"


@pytest.mark.asyncio
async def test_decision_record_cost_and_usage_round_trip(
    uow_factory: Callable[[], SqliteUnitOfWork],
    make_item: Callable[..., Item],
    make_claim: Callable[..., object],
) -> None:
    item = make_item()
    claim = make_claim(item.id, field="title", value="Song")
    decision = Decision(
        field="title",
        subject=claim.subject,
        value="Song",
        chosen_claim_id=claim.id,
        alternatives=(),
        policy_id="title",
        mode=DecisionMode.LLM_ASSISTED,
        confidence=0.8,
        rationale="llm assisted",
    )
    record = DecisionRecord(
        id=DecisionRecordId(new_uuid7()),
        item_id=item.id,
        version=1,
        decisions=(decision,),
        policy_versions=("title@1",),
        llm_model="test-model",
        prompt_version="adjudicate/v1",
        input_claim_ids=(ClaimId(claim.id),),
        llm_cost_usd=0.0042,
        llm_usage={"input_tokens": 120, "output_tokens": 30},
        created_at=datetime.now(UTC),
    )

    async with uow_factory() as uow:
        await uow.items.save(item)
        await uow.claims.append(claim)
        await uow.decisions.save_record(record)

    async with uow_factory() as uow:
        loaded = await uow.decisions.get_record(record.id)
        assert loaded is not None
        assert loaded.llm_model == "test-model"
        assert loaded.prompt_version == "adjudicate/v1"
        assert loaded.llm_cost_usd == pytest.approx(0.0042)
        assert loaded.llm_usage == {"input_tokens": 120, "output_tokens": 30}

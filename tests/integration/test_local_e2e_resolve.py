"""End-to-end local observe → interpret → resolve → explain resolution test."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.fixtures.media import generate_media, skip_without_media_tools

from trackforge.application.dto.plan import AcquireOptions, PlanOptions
from trackforge.bootstrap.container import build_catalog_context
from trackforge.config import TrackForgeConfig
from trackforge.domain.model.item import ItemStatus
from trackforge.domain.model.source import LocalPath

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_local_observe_interpret_resolve_explain(
    tmp_path: Path,
    config: TrackForgeConfig,
) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", "mp3")

    with build_catalog_context(config) as context:
        plan = await context.plan_item().plan(
            LocalPath(str(fixture.path)),
            PlanOptions(),
            context.source_resolvers(),
        )
        assert plan.blocked_reason is None
        acquired = await context.acquire_item().acquire(
            plan.candidates[0],
            AcquireOptions(),
            context.acquirers(),
        )
        observed = await context.observe_item().observe(acquired.item_id, "balanced")
        assert observed.status is ItemStatus.OBSERVED

        interpretation = await context.interpret_item().interpret(acquired.item_id)
        assert interpretation.status is ItemStatus.INTERPRETED
        titles = [
            claim.value
            for claim in interpretation.claims
            if claim.subject.value == "item" and claim.field == "title"
        ]
        assert "Fixture Song" in titles
        assert interpretation.identity is not None
        assert interpretation.identity["status"] == "proposed"

        resolved = await context.resolve_item().resolve(acquired.item_id)
        assert resolved.fields["title"] == "Fixture Song"
        assert resolved.fields["artist"] == "Fixture Artist"
        assert resolved.fields["album"] == "Fixture Album"
        assert resolved.fields["date"] == "2024"
        assert resolved.fields["track"] == 3

        explanation = await context.explain_item().explain(acquired.item_id, "title")
        assert explanation.decision == "Fixture Song"
        assert explanation.chosen_claim is not None
        assert explanation.chosen_claim.source == "initial_tags"
        assert explanation.alternatives
        assert explanation.rationale

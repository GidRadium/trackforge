"""Shared fake facade and identifiers for HTTP tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trackforge.application.dto.db import RunItemStatus, RunStatus
from trackforge.application.dto.decisions import (
    ExplanationView,
)
from trackforge.application.dto.plan import PlanStepView, PlanView
from trackforge.application.dto.results import (
    DoctorCheck,
    DoctorReport,
    DoctorStatus,
    RunItemView,
    RunOptions,
    RunResult,
    RunStepView,
    StatusView,
)
from trackforge.application.dto.views import (
    DecisionView,
    ItemView,
    LibraryEntryView,
    ReviewFieldView,
    ReviewView,
)
from trackforge.application.ports.models import ModelInfo, ModelKind, ModelStatus
from trackforge.config import TrackForgeConfig
from trackforge.domain.model.decisions import DecisionMode
from trackforge.domain.model.ids import CandidateId, ItemId, RunId, new_uuid7
from trackforge.domain.model.item import ItemStatus
from trackforge.domain.model.outputs import LibraryStatus, TargetFormat
from trackforge.domain.model.source import Candidate, LocalPath, SourceRef, Url
from trackforge.pipeline.events import Event, EventType

TEST_TOKEN = "test-http-token-" + ("x" * 48)
RUN_ID = RunId(new_uuid7())
ITEM_ID = ItemId(new_uuid7())
SOURCE = LocalPath("/tmp/trackforge-fixture.mp3")


def _candidate(title: str = "Fixture Song") -> Candidate:
    return Candidate(
        id=CandidateId(new_uuid7()),
        source_ref=SOURCE,
        external_id="fixture-video",
        title=title,
        uploader="Fixture Artist",
        duration_seconds=180.0,
    )


class FakeFacade:
    """Structural stand-in for ``AsyncTrackForge`` used by HTTP tests."""

    def __init__(self, config: TrackForgeConfig) -> None:
        self._config = config
        self.plan_calls: list[tuple[SourceRef, RunOptions | None]] = []
        self.run_calls: list[tuple[Any, RunOptions | None]] = []
        self.review_calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.explain_calls: list[tuple[str, str]] = []
        self.cancel_calls: list[str] = []
        self.plan_error: BaseException | None = None
        self.run_error: BaseException | None = None
        self.review_error: BaseException | None = None
        self.run_gate: asyncio.Event | None = None
        self.cancel_result = True
        self.events_list: tuple[Event, ...] = (
            Event(type=EventType.RUN_STARTED, run_id=RUN_ID, status="running"),
            Event(type=EventType.RUN_FINISHED, run_id=RUN_ID, status="completed"),
        )
        self.run_result = RunResult(
            run_id=RUN_ID,
            status=RunStatus.COMPLETED,
            items=(
                RunItemView(
                    item_id=ITEM_ID,
                    status=ItemStatus.PUBLISHED,
                    output_path="/tmp/library/Fixture Song.mp3",
                ),
            ),
            output_paths=("/tmp/library/Fixture Song.mp3",),
            steps=(RunStepView(capability_id="plan", status="succeeded"),),
        )
        self.status_view = StatusView(
            run_id=RUN_ID,
            status=RunStatus.RUNNING,
            profile="balanced",
            mode="policy",
            steps=(RunStepView(capability_id="plan", status="succeeded"),),
            items=((ITEM_ID, RunItemStatus.RUNNING),),
        )
        self.item_view = ItemView(
            item_id=ITEM_ID,
            status=ItemStatus.RESOLVED,
            media_id=str(new_uuid7()),
            content_hash="a" * 64,
            title="Fixture Song",
            artist="Fixture Artist",
        )
        self.decision_views = (
            DecisionView(
                field="title",
                value="Fixture Song",
                confidence=0.93,
                rationale="source metadata",
            ),
            DecisionView(field="artist", value="Fixture Artist", confidence=0.9),
        )
        self.library_entries = (
            LibraryEntryView(
                id=str(new_uuid7()),
                item_id=ITEM_ID,
                bundle_id=str(new_uuid7()),
                path="/tmp/library/Fixture Song.mp3",
                status=LibraryStatus.PUBLISHED,
                published_at=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
                target_format=TargetFormat.MP3,
            ),
        )
        self.model_entries = (
            ModelInfo(
                id="fixture/asr",
                version="v1",
                kind=ModelKind.ASR,
                license="MIT",
                status=ModelStatus.MISSING,
            ),
        )
        self.doctor_report = DoctorReport(
            checks=(
                DoctorCheck(
                    name="python",
                    status=DoctorStatus.OK,
                    message="python is available",
                    details={"token": "super-secret", "path": "/tmp/data"},
                ),
            ),
            generated_at=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
            version="0.1.0",
        )

    @property
    def config(self) -> TrackForgeConfig:
        return self._config

    async def plan(
        self,
        source: SourceRef | str | Path,
        options: RunOptions | None = None,
    ) -> PlanView:
        self.plan_calls.append((source, options))
        if self.plan_error is not None:
            raise self.plan_error
        if isinstance(source, Url):
            ref: SourceRef = source
        elif isinstance(source, Path):
            ref = LocalPath(str(source))
        elif isinstance(source, str):
            ref = Url(source) if source.startswith(("http://", "https://")) else LocalPath(source)
        else:
            ref = source
        return PlanView(
            source=ref,
            candidates=(_candidate(),),
            steps=(
                PlanStepView(
                    node_id="observe",
                    capability_id="observe",
                    description="Probe local media",
                ),
            ),
            warnings=("fixture warning",),
        )

    async def run(
        self,
        plan_or_source: PlanView | SourceRef | str | Path,
        options: RunOptions | None = None,
        *,
        on_start: Any = None,
    ) -> RunResult:
        self.run_calls.append((plan_or_source, options))
        if self.run_error is not None:
            raise self.run_error
        if on_start is not None:
            on_start(RUN_ID)
        if self.run_gate is not None:
            await self.run_gate.wait()
        return self.run_result

    async def status(self, run_id: RunId | str) -> StatusView:
        if str(run_id) != str(RUN_ID):
            from trackforge.domain.errors import RecordNotFound

            raise RecordNotFound("run not found", context={"run_id": run_id})
        return self.status_view

    async def cancel(self, run_id: RunId | str) -> bool:
        self.cancel_calls.append(str(run_id))
        return self.cancel_result

    def events(self, run_id: RunId | str) -> AsyncIterator[Event]:
        async def iterator() -> AsyncIterator[Event]:
            for event in self.events_list:
                yield event

        return iterator()

    async def item(self, item_id: str) -> ItemView:
        if str(item_id) != str(ITEM_ID):
            from trackforge.domain.errors import RecordNotFound

            raise RecordNotFound("item not found", context={"item_id": item_id})
        return self.item_view

    async def decisions(self, item_id: str) -> tuple[DecisionView, ...]:
        await self.item(item_id)
        return self.decision_views

    async def review(
        self,
        item_id: str,
        corrections: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
        *,
        action: str = "show",
        field: str | None = None,
        value: Any = None,
        lock: bool = False,
        reason: str | None = None,
        reason_code: str | None = None,
        reason_text: str | None = None,
        scope: str = "item",
        mode: str | None = None,
        auto: bool = False,
    ) -> ReviewView:
        if self.review_error is not None:
            raise self.review_error
        self.review_calls.append(
            (
                item_id,
                corrections,
                {
                    "action": action,
                    "field": field,
                    "value": value,
                    "lock": lock,
                    "reason": reason,
                    "reason_code": reason_code,
                    "reason_text": reason_text,
                    "scope": scope,
                    "mode": mode,
                    "auto": auto,
                },
            )
        )
        return ReviewView(
            item_id=ITEM_ID,
            status=ItemStatus.RESOLVED,
            fields=(ReviewFieldView(field="title", current="Fixture Song"),),
            locked_fields=("title",),
            action=action,
            requires_render=corrections is not None,
        )

    async def explain(self, item_id: str, field: str) -> ExplanationView:
        self.explain_calls.append((item_id, field))
        return ExplanationView(
            item_id=ITEM_ID,
            field=field,
            decision="Fixture Song" if field == "title" else None,
            mode=DecisionMode.POLICY,
            policy_id="title",
            policy_version=1,
            confidence=0.93,
            rationale="fixture source claim",
            warnings=(),
        )

    async def library(self) -> tuple[LibraryEntryView, ...]:
        return self.library_entries

    async def models(self) -> tuple[ModelInfo, ...]:
        return self.model_entries

    async def doctor(self) -> DoctorReport:
        return self.doctor_report

    async def close(self) -> None:
        return None

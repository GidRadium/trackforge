"""Dependency helpers shared by HTTP routes.

Routes depend on the public facade protocol, never on adapters or domain
services.  Tests can inject a fake facade with the same structural shape.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from fastapi import Request

from trackforge.application.dto.collections import CollectionPlanView, CollectionRunResult
from trackforge.application.dto.decisions import ExplanationView
from trackforge.application.dto.plan import PlanView
from trackforge.application.dto.results import DoctorReport, RunOptions, RunResult, StatusView
from trackforge.application.dto.views import DecisionView, ItemView, LibraryEntryView, ReviewView
from trackforge.application.ports.models import ModelInfo
from trackforge.config import TrackForgeConfig
from trackforge.domain.model.ids import RunId
from trackforge.domain.model.source import SourceRef
from trackforge.interfaces.http.errors import ApiError
from trackforge.pipeline.events import Event


class Facade(Protocol):
    """The subset of ``AsyncTrackForge`` used by the HTTP transport."""

    @property
    def config(self) -> TrackForgeConfig: ...

    async def plan(
        self,
        source: SourceRef | str | Path,
        options: RunOptions | None = None,
    ) -> PlanView | CollectionPlanView: ...

    async def run(
        self,
        plan_or_source: PlanView | SourceRef | str | Path,
        options: RunOptions | None = None,
        *,
        on_start: Callable[[RunId], None] | None = None,
    ) -> RunResult | PlanView | CollectionPlanView | CollectionRunResult: ...

    async def status(self, run_id: RunId | str) -> StatusView: ...

    async def cancel(self, run_id: RunId | str) -> bool: ...

    def events(self, run_id: RunId | str) -> AsyncIterator[Event]: ...

    async def item(self, item_id: str) -> ItemView: ...

    async def decisions(self, item_id: str) -> tuple[DecisionView, ...]: ...

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
    ) -> ReviewView: ...

    async def explain(self, item_id: str, field: str) -> ExplanationView: ...

    async def library(self) -> tuple[LibraryEntryView, ...]: ...

    async def models(self) -> tuple[ModelInfo, ...]: ...

    async def doctor(self) -> DoctorReport: ...

    async def close(self) -> None: ...


def get_facade(request: Request) -> Facade:
    facade: Facade = request.app.state.facade
    return facade


def get_config(request: Request) -> TrackForgeConfig:
    config: TrackForgeConfig = request.app.state.config
    return config


def get_token(request: Request) -> str:
    token: str = request.app.state.http_token
    return token


def validate_id(value: str, *, kind: str) -> str:
    """Reject non-UUID path parameters before they reach the catalog."""

    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise ApiError(
            400,
            "invalid_id",
            f"invalid {kind} id",
            user_action=f"Pass a UUIDv7 {kind} id.",
            context={"kind": kind},
        ) from None
    return value


__all__ = ["Facade", "get_config", "get_facade", "get_token", "validate_id"]

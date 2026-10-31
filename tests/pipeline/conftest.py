"""Shared fixtures and fakes for pipeline tests."""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from trackforge.adapters.persistence.filesystem.artifacts import FileSystemArtifactStore
from trackforge.adapters.persistence.sqlite.engine import Catalog, initialize_catalog
from trackforge.adapters.persistence.sqlite.unit_of_work import SqliteUnitOfWork
from trackforge.application.dto.artifacts import ArtifactManifest
from trackforge.application.dto.db import (
    RunItemRecord,
    RunItemStatus,
    RunRecord,
    RunStatus,
    StepRecord,
    StepStatus,
)
from trackforge.application.ports.execution import (
    ArtifactDraft,
    Capability,
    StepContext,
    StepInput,
    StepResult,
)
from trackforge.application.ports.storage import (
    ArtifactConflict,
    ArtifactCorrupted,
    ArtifactRef,
    PayloadFormat,
    require_cache_key,
    validate_payload_format,
)
from trackforge.application.services.canonical_json import canonical_json, sha256_hex
from trackforge.domain.errors import RecordNotFound
from trackforge.domain.model.capabilities import (
    AnalysisProfile,
    ArtifactType,
    CapabilityKind,
    CapabilityManifest,
    Cost,
)
from trackforge.domain.model.ids import ItemId, RunId, StepId, new_uuid7
from trackforge.pipeline.cache import Cache
from trackforge.pipeline.events import EventBus
from trackforge.pipeline.executor import Executor
from trackforge.pipeline.planner import Plan, Planner

# ---------------------------------------------------------------------------
# Manifest and capability fakes
# ---------------------------------------------------------------------------


@pytest.fixture
def make_manifest() -> Callable[..., CapabilityManifest]:
    def _make(
        capability_id: str,
        *,
        version: str = "1",
        requires: Iterable[ArtifactType | str] = (),
        optional_requires: Iterable[ArtifactType | str] = (),
        provides: Iterable[ArtifactType | str] = (),
        cost: Cost | None = None,
        kind: CapabilityKind = CapabilityKind.OBSERVER,
        cacheable: bool = True,
        config_schema: dict[str, Any] | None = None,
    ) -> CapabilityManifest:
        return CapabilityManifest(
            id=capability_id,
            version=version,
            kind=kind,
            requires=tuple(requires),
            optional_requires=tuple(optional_requires),
            provides=tuple(provides),
            cost=cost or Cost(),
            cacheable=cacheable,
            config_schema=config_schema or {},
        )

    return _make


def _default_result(manifest: CapabilityManifest, call_index: int) -> StepResult:
    if not manifest.provides:
        return StepResult()
    target = manifest.provides[0]
    if not isinstance(target, ArtifactType):
        return StepResult()
    return StepResult(
        artifacts=(
            ArtifactDraft(
                type=target,
                payload={"capability": manifest.id, "call": call_index},
            ),
        )
    )


@dataclass
class FakeCapability:
    """A controllable capability used by planner/executor tests."""

    manifest: CapabilityManifest
    behaviors: list[Any] = field(default_factory=list)
    calls: list[StepContext] = field(default_factory=list)
    delay: float = 0.0
    cancelled_result: StepResult | None = None

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def run(self, ctx: StepContext) -> StepResult:
        self.calls.append(ctx)
        if self.delay > 0:
            deadline = asyncio.get_running_loop().time() + self.delay
            while asyncio.get_running_loop().time() < deadline:
                if ctx.cancelled.is_set():
                    if self.cancelled_result is not None:
                        return self.cancelled_result
                    raise asyncio.CancelledError
                await asyncio.sleep(min(0.01, self.delay))
        if self.behaviors:
            behavior = self.behaviors.pop(0)
            if isinstance(behavior, BaseException):
                raise behavior
            if callable(behavior):
                value = behavior(ctx)
                if isinstance(value, StepResult):
                    return value
                raise TypeError("behavior callable must return StepResult")
            if isinstance(behavior, StepResult):
                return behavior
            raise TypeError("behavior must be StepResult, exception, or callable")
        return _default_result(self.manifest, len(self.calls))


@pytest.fixture
def make_capability() -> Callable[..., FakeCapability]:
    def _make(
        manifest: CapabilityManifest,
        *,
        behaviors: Iterable[Any] = (),
        delay: float = 0.0,
        cancelled_result: StepResult | None = None,
    ) -> FakeCapability:
        return FakeCapability(
            manifest=manifest,
            behaviors=list(behaviors),
            delay=delay,
            cancelled_result=cancelled_result,
        )

    return _make


@pytest.fixture
def capability_contract() -> type[Capability]:
    return Capability


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


@pytest.fixture
def make_plan() -> Callable[..., Plan]:
    def _make(
        capabilities: Iterable[Capability],
        desired: Iterable[ArtifactType | str] | ArtifactType | str,
        *,
        required: Iterable[ArtifactType | str] | ArtifactType | str = (),
        profile: AnalysisProfile | None = None,
        provider_chains: dict[ArtifactType | str, list[str]] | None = None,
        params: dict[str, dict[str, Any]] | None = None,
        inputs: Iterable[ArtifactType | str] = (),
    ) -> Plan:
        return Planner(
            provider_chains=provider_chains,
            params=params,
        ).plan(
            desired,
            profile or AnalysisProfile(name="test"),
            capabilities,
            required_outputs=required,
            inputs=inputs,
        )

    return _make


# ---------------------------------------------------------------------------
# Artifact stores and caches
# ---------------------------------------------------------------------------


class FakeArtifactStore:
    """In-memory ArtifactStore with the same key/payload invariants as the FS store."""

    def __init__(self) -> None:
        self.entries: dict[str, tuple[ArtifactManifest, bytes, PayloadFormat]] = {}

    def _ref(self, key: str, manifest: ArtifactManifest, fmt: PayloadFormat) -> ArtifactRef:
        suffix = "json" if fmt == "json" else "bin"
        directory = Path("/fake-artifacts") / key
        return ArtifactRef(
            cache_key=key,
            manifest_path=directory / "manifest.json",
            payload_path=directory / f"payload.{suffix}",
            payload_hash=manifest.payload_hash,
        )

    async def put(
        self,
        cache_key: str,
        manifest: ArtifactManifest,
        payload: bytes | Path,
        payload_format: PayloadFormat = "json",
    ) -> ArtifactRef:
        key = require_cache_key(cache_key)
        fmt = validate_payload_format(payload_format)
        data = payload.read_bytes() if isinstance(payload, Path) else bytes(payload)
        params_hash = sha256_hex(canonical_json(manifest.params))
        payload_hash = sha256_hex(data)
        if manifest.params_hash and manifest.params_hash != params_hash:
            raise ArtifactCorrupted("params hash mismatch")
        if manifest.payload_hash and manifest.payload_hash != payload_hash:
            raise ArtifactCorrupted("payload hash mismatch")
        stored = manifest.model_copy(
            update={"params_hash": params_hash, "payload_hash": payload_hash}
        )
        existing = self.entries.get(key)
        if existing is not None:
            old_manifest, old_data, old_format = existing
            if old_format != fmt or old_data != data:
                raise ArtifactConflict("cache key conflict")
            return self._ref(key, old_manifest, old_format)
        self.entries[key] = (stored, data, fmt)
        return self._ref(key, stored, fmt)

    async def find(self, cache_key: str) -> ArtifactRef | None:
        key = require_cache_key(cache_key)
        entry = self.entries.get(key)
        if entry is None:
            return None
        manifest, _, fmt = entry
        return self._ref(key, manifest, fmt)

    async def load(self, ref: ArtifactRef) -> tuple[ArtifactManifest, bytes]:
        entry = self.entries.get(ref.cache_key)
        if entry is None:
            raise ArtifactCorrupted("artifact not found")
        manifest, data, _ = entry
        if manifest.payload_hash != ref.payload_hash:
            raise ArtifactCorrupted("reference hash mismatch")
        if sha256_hex(data) != manifest.payload_hash:
            raise ArtifactCorrupted("payload hash mismatch")
        return manifest, data

    async def verify(self, ref: ArtifactRef) -> bool:
        await self.load(ref)
        return True

    async def open_payload(self, ref: ArtifactRef) -> io.BytesIO:
        _, data = await self.load(ref)
        return io.BytesIO(data)


@pytest.fixture
def fake_store() -> FakeArtifactStore:
    return FakeArtifactStore()


@pytest.fixture
def artifact_store(config: Any) -> FileSystemArtifactStore:
    return FileSystemArtifactStore(data_dir=config.data_dir, cache_dir=config.cache_dir)


@pytest.fixture
def cache(fake_store: FakeArtifactStore) -> Cache:
    return Cache(fake_store)


@pytest.fixture
def real_cache(artifact_store: FileSystemArtifactStore) -> Cache:
    return Cache(artifact_store)


# ---------------------------------------------------------------------------
# Run repository fake
# ---------------------------------------------------------------------------


class FakeRunRepository:
    """In-memory implementation of the RunRepository port."""

    def __init__(self) -> None:
        self.runs: dict[str, RunRecord] = {}
        self.steps: dict[str, StepRecord] = {}
        self.items: dict[tuple[str, str], RunItemRecord] = {}

    async def create_run(
        self,
        profile: str,
        mode: str,
        *,
        status: RunStatus | str = RunStatus.PLANNED,
        run_id: RunId | None = None,
        started_at: datetime | None = None,
        config_snapshot_hash: str | None = None,
    ) -> RunRecord:
        record = RunRecord(
            id=RunId(run_id) if run_id is not None else RunId(new_uuid7()),
            profile=profile,
            mode=mode,
            status=RunStatus(status),
            started_at=started_at,
            config_snapshot_hash=config_snapshot_hash,
        )
        self.runs[str(record.id)] = record
        return record

    async def get_run(self, run_id: RunId) -> RunRecord | None:
        return self.runs.get(str(run_id))

    async def update_run(
        self,
        run_id: RunId,
        *,
        status: RunStatus | str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        error: dict[str, Any] | None = None,
        config_snapshot_hash: str | None = None,
    ) -> RunRecord:
        record = self.runs.get(str(run_id))
        if record is None:
            raise RecordNotFound("run not found")
        updated = replace(
            record,
            status=record.status if status is None else RunStatus(status),
            started_at=record.started_at if started_at is None else started_at,
            finished_at=record.finished_at if finished_at is None else finished_at,
            error=record.error if error is None else error,
            config_snapshot_hash=(
                record.config_snapshot_hash
                if config_snapshot_hash is None
                else config_snapshot_hash
            ),
        )
        self.runs[str(run_id)] = updated
        return updated

    async def add_item(
        self,
        run_id: RunId,
        item_id: ItemId,
        *,
        status: RunItemStatus | str = RunItemStatus.PENDING,
    ) -> RunItemRecord:
        key = (str(run_id), str(item_id))
        if key in self.items:
            from trackforge.domain.errors import DuplicateRecord

            raise DuplicateRecord("run item already exists")
        record = RunItemRecord(
            run_id=run_id,
            item_id=item_id,
            status=RunItemStatus(status),
        )
        self.items[key] = record
        return record

    async def set_item_status(
        self,
        run_id: RunId,
        item_id: ItemId,
        status: RunItemStatus | str,
    ) -> RunItemRecord:
        key = (str(run_id), str(item_id))
        record = self.items.get(key)
        if record is None:
            raise RecordNotFound("run item not found")
        updated = replace(record, status=RunItemStatus(status))
        self.items[key] = updated
        return updated

    async def create_step(
        self,
        run_id: RunId,
        item_id: ItemId,
        capability_id: str,
        capability_version: str,
        *,
        status: StepStatus | str = StepStatus.PENDING,
        step_id: StepId | None = None,
        cache_key: str | None = None,
    ) -> StepRecord:
        if str(run_id) not in self.runs:
            raise RecordNotFound("run not found")
        record = StepRecord(
            id=StepId(step_id) if step_id is not None else StepId(new_uuid7()),
            run_id=run_id,
            item_id=item_id,
            capability_id=capability_id,
            capability_version=capability_version,
            cache_key=cache_key,
            status=StepStatus(status),
        )
        if str(record.id) in self.steps:
            from trackforge.domain.errors import DuplicateRecord

            raise DuplicateRecord("step id already exists")
        self.steps[str(record.id)] = record
        return record

    async def update_step(
        self,
        step_id: StepId,
        *,
        status: StepStatus | str | None = None,
        attempts: int | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        error: dict[str, Any] | None = None,
        warnings: tuple[dict[str, Any] | str, ...] | None = None,
        cache_key: str | None = None,
    ) -> StepRecord:
        record = self.steps.get(str(step_id))
        if record is None:
            raise RecordNotFound("step not found")
        updated = replace(
            record,
            status=record.status if status is None else StepStatus(status),
            attempts=record.attempts if attempts is None else attempts,
            started_at=record.started_at if started_at is None else started_at,
            finished_at=record.finished_at if finished_at is None else finished_at,
            error=record.error if error is None else error,
            warnings=record.warnings if warnings is None else warnings,
            cache_key=record.cache_key if cache_key is None else cache_key,
        )
        self.steps[str(step_id)] = updated
        return updated

    async def list_steps(
        self,
        run_id: RunId,
        *,
        item_id: ItemId | None = None,
    ) -> list[StepRecord]:
        result = [
            step
            for step in self.steps.values()
            if step.run_id == run_id and (item_id is None or step.item_id == item_id)
        ]
        return sorted(result, key=lambda step: str(step.id))

    async def list_steps_for_item(self, item_id: ItemId) -> list[StepRecord]:
        return sorted(
            (step for step in self.steps.values() if step.item_id == item_id),
            key=lambda step: str(step.id),
        )

    async def find_resumable(self) -> list[RunRecord]:
        return [
            run for run in self.runs.values() if run.status in (RunStatus.RUNNING, RunStatus.PAUSED)
        ]


@pytest.fixture
def fake_runs() -> FakeRunRepository:
    return FakeRunRepository()


# ---------------------------------------------------------------------------
# Executors and events
# ---------------------------------------------------------------------------


@pytest.fixture
def event_bus() -> EventBus:
    return EventBus()


@pytest.fixture
def make_executor(
    fake_store: FakeArtifactStore,
    event_bus: EventBus,
) -> Callable[..., Executor]:
    def _make(
        capabilities: Iterable[Capability],
        *,
        runs: FakeRunRepository | None = None,
        event_bus_override: EventBus | None = None,
        **kwargs: Any,
    ) -> Executor:
        return Executor(
            capabilities,
            fake_store,
            runs or FakeRunRepository(),
            events=event_bus_override or event_bus,
            **kwargs,
        )

    return _make


# ---------------------------------------------------------------------------
# SQLite integration fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def catalog(config: Any) -> Iterator[Catalog]:
    value = initialize_catalog(config.data_dir)
    try:
        yield value
    finally:
        value.close()


@pytest_asyncio.fixture
async def uow(catalog: Catalog) -> AsyncIterator[SqliteUnitOfWork]:
    async with SqliteUnitOfWork(catalog) as value:
        yield value


@pytest.fixture
def make_step_input() -> Callable[..., StepInput]:
    def _make(
        artifact_type: ArtifactType,
        *,
        payload: bytes = b"{}",
        payload_hash: str | None = None,
        cache_key: str | None = None,
        artifact_id: str | None = None,
    ) -> StepInput:
        return StepInput(
            type=artifact_type,
            artifact_id=artifact_id or new_uuid7(),
            schema_version=artifact_type.schema_version,
            cache_key=cache_key or "0" * 64,
            payload_hash=payload_hash or sha256_hex(payload),
            payload=payload,
        )

    return _make


__all__ = [
    "FakeArtifactStore",
    "FakeCapability",
    "FakeRunRepository",
]

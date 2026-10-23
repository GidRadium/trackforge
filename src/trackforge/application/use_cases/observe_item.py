"""Run the intrinsic observation DAG and persist its catalog references."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from trackforge.application.dto.db import ArtifactRecord, RunStatus, StepStatus
from trackforge.application.dto.observations import (
    ArtifactSummary,
    AudioProbePayload,
    MediaInput,
    MediaSummary,
    ObservationStepView,
    ObservationView,
    ObservationWarning,
)
from trackforge.application.ports.execution import Capability, StepInput, StepWarning
from trackforge.application.ports.observations import (
    MEDIA_ARTIFACT,
    PATH_STRUCTURE_ARTIFACT,
    PROBE_ARTIFACT,
)
from trackforge.application.ports.repositories import UnitOfWork
from trackforge.application.ports.storage import ArtifactStore, StorageError
from trackforge.application.services.canonical_json import canonical_json, sha256_hex
from trackforge.application.use_cases.plan_item import (
    DEFAULT_OBSERVE_OUTPUTS,
    DEFAULT_REQUIRED_OUTPUTS,
)
from trackforge.domain.errors import (
    DomainValidationError,
    ObservationFailed,
    ProbeFailed,
    ProviderUnavailable,
    RecordNotFound,
    TrackForgeError,
)
from trackforge.domain.model.capabilities import AnalysisProfile
from trackforge.domain.model.ids import ArtifactId, IntakeId, ItemId
from trackforge.domain.model.item import Item, ItemStatus
from trackforge.domain.model.media import FileRole, MediaObject
from trackforge.pipeline.executor import Executor, RunResult
from trackforge.pipeline.planner import Planner


class ObserveItem:
    """Build and execute probe/tags/path observations for one acquired item."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], UnitOfWork],
        capabilities: Sequence[Capability],
        artifact_store: ArtifactStore,
        *,
        config: Mapping[str, Any] | None = None,
        max_attempts: int = 2,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._uow_factory = unit_of_work_factory
        self._capabilities = tuple(capabilities)
        self._artifact_store = artifact_store
        self._config = dict(config or {})
        self._max_attempts = max_attempts

    async def observe(
        self,
        item_id: ItemId,
        profile: str | AnalysisProfile = "balanced",
    ) -> ObservationView:
        """Execute observations, update measured media fields, and mark observed."""

        analysis_profile = self._profile(profile)
        warnings: list[ObservationWarning] = []
        run: RunResult
        view: ObservationView

        async with self._uow_factory() as uow:
            item = await uow.items.get(item_id)
            if item is None:
                raise RecordNotFound(
                    "item not found",
                    context={"item_id": item_id},
                )
            if item.media_id is None:
                raise DomainValidationError(
                    "item has no acquired media to observe",
                    user_action="Acquire the item before observing it.",
                    context={"item_id": item_id},
                )
            media = await uow.media.get(item.media_id)
            if media is None:
                raise RecordNotFound(
                    "media object not found",
                    context={"media_id": item.media_id},
                )

            source_path = await self._source_path(uow, item.primary_intake_id, media)
            desired = tuple(
                output
                for output in DEFAULT_OBSERVE_OUTPUTS
                if source_path is not None or output != PATH_STRUCTURE_ARTIFACT
            )
            if source_path is None:
                warnings.append(
                    ObservationWarning(
                        code="path_unavailable",
                        message="no source file path is recorded; path.structure.v1 was skipped",
                    )
                )
            params: dict[str, dict[str, Any]] = {}
            if source_path is not None:
                params["path_structure"] = {"path": source_path}

            plan = Planner().plan(
                desired,
                analysis_profile,
                self._capabilities,
                required_outputs=DEFAULT_REQUIRED_OUTPUTS,
                inputs=(MEDIA_ARTIFACT,),
                params=params,
            )
            if plan.blocked_reason is not None:
                raise ObservationFailed(
                    f"observation plan is blocked: {plan.blocked_reason}",
                    user_action="Check the installed observation capabilities.",
                    context={"item_id": item_id, "blocked_reason": plan.blocked_reason},
                )

            executor = Executor(
                self._capabilities,
                self._artifact_store,
                uow.runs,
                config=self._config,
                max_attempts=self._max_attempts,
                commit=uow.commit,
            )
            run = await executor.run(
                plan,
                item_id=item.id,
                profile=analysis_profile.name,
                mode="auto",
                initial_inputs={MEDIA_ARTIFACT: _media_step_input(media)},
            )

            summaries = await self._index_artifacts(uow, run, warnings)
            await self._update_media_from_probe(uow, media, run, summaries)
            if run.status is RunStatus.COMPLETED:
                item = await self._mark_observed(uow, item)
            media = await uow.media.get(media.id) or media

            view = ObservationView(
                item_id=item.id,
                status=item.status,
                run_id=str(run.run_id),
                media=_media_summary(media),
                artifacts=summaries,
                steps=_step_views(run),
                warnings=_merge_warnings(run.warnings, warnings),
            )

        if run.status is not RunStatus.COMPLETED:
            raise _observation_error(run)
        return view

    @staticmethod
    def _profile(value: str | AnalysisProfile) -> AnalysisProfile:
        if isinstance(value, AnalysisProfile):
            return value
        if isinstance(value, str) and value.strip():
            return AnalysisProfile(name=value)
        raise DomainValidationError("profile must be a non-empty name or AnalysisProfile")

    async def _source_path(
        self,
        uow: UnitOfWork,
        primary_intake_id: IntakeId | None,
        media: MediaObject,
    ) -> str | None:
        instances = await uow.file_instances.find_by_media(media.id)
        sources = [item for item in instances if item.role is FileRole.SOURCE]
        if primary_intake_id is not None:
            for instance in sources:
                if instance.intake_id == primary_intake_id:
                    return instance.path
        return sources[0].path if sources else None

    async def _index_artifacts(
        self,
        uow: UnitOfWork,
        run: RunResult,
        warnings: list[ObservationWarning],
    ) -> tuple[ArtifactSummary, ...]:
        cached_types = self._cached_types(run)
        summaries: list[ArtifactSummary] = []
        for artifact_type, step_input in sorted(
            run.artifacts.items(),
            key=lambda item: item[0],
        ):
            if artifact_type == str(MEDIA_ARTIFACT):
                continue
            ref = step_input.ref
            if ref is None and step_input.cache_key:
                try:
                    ref = await self._artifact_store.find(step_input.cache_key)
                except (StorageError, OSError) as exc:
                    warnings.append(
                        ObservationWarning(
                            code="artifact_index_failed",
                            message=(f"could not index {artifact_type}: {exc}"),
                        )
                    )
                    ref = None
            if ref is not None:
                try:
                    manifest, _ = await self._artifact_store.load(ref)
                except (StorageError, OSError) as exc:
                    warnings.append(
                        ObservationWarning(
                            code="artifact_index_failed",
                            message=f"could not load {artifact_type}: {exc}",
                        )
                    )
                else:
                    existing = await uow.artifacts.find_by_cache_key(ref.cache_key)
                    if existing is None:
                        await uow.artifacts.save(
                            ArtifactRecord.from_manifest(
                                manifest,
                                cache_key=ref.cache_key,
                                payload_path=str(ref.payload_path),
                            )
                        )
            cache_key = step_input.cache_key or (str(ref.cache_key) if ref is not None else "")
            summaries.append(
                ArtifactSummary(
                    type=artifact_type,
                    artifact_id=step_input.artifact_id,
                    cache_key=cache_key,
                    payload_hash=step_input.payload_hash,
                    cached=artifact_type in cached_types,
                )
            )
        return tuple(summaries)

    def _cached_types(self, run: RunResult) -> set[str]:
        cached_capabilities = {
            step.capability_id for step in run.steps if step.status is StepStatus.CACHED
        }
        return {
            str(provided)
            for capability in self._capabilities
            if capability.manifest.id in cached_capabilities
            for provided in capability.manifest.provides
        }

    async def _update_media_from_probe(
        self,
        uow: UnitOfWork,
        media: MediaObject,
        run: RunResult,
        summaries: tuple[ArtifactSummary, ...],
    ) -> None:
        probe = run.artifact(PROBE_ARTIFACT)
        if probe is None:
            return
        try:
            payload = AudioProbePayload.model_validate_json(probe.payload)
        except ValueError:
            return
        if not any(item.artifact_id == probe.artifact_id for item in summaries):
            # The artifact row was not indexed, so the FK-backed probe link
            # cannot be written safely.  The probe artifact still exists in
            # the cache and the run warnings explain the indexing failure.
            return
        stream = payload.first_audio_stream()
        await uow.media.update_probe(
            media.id,
            probe_artifact_id=ArtifactId(probe.artifact_id),
            codec=stream.codec_name if stream is not None else None,
            bitrate=(
                stream.bit_rate
                if stream is not None and stream.bit_rate is not None
                else payload.bit_rate
            ),
            sample_rate=stream.sample_rate if stream is not None else None,
            channels=stream.channels if stream is not None else None,
            duration_seconds=(
                payload.duration_seconds
                if payload.duration_seconds is not None
                else (stream.duration_seconds if stream is not None else None)
            ),
        )

    @staticmethod
    async def _mark_observed(uow: UnitOfWork, item: Item) -> Item:
        if item.status is not ItemStatus.ACQUIRED:
            # A forced observation after resolution must not move the item
            # backwards; the observation artifacts are still refreshed.
            return item
        item.transition_to(ItemStatus.OBSERVED)
        return await uow.items.save(item)


def _media_step_input(media: MediaObject) -> StepInput:
    descriptor = MediaInput(
        media_id=media.id,
        content_hash=media.content_hash,
        size_bytes=media.size_bytes,
        cas_path=media.cas_path,
        container=media.container.value,
    )
    payload = canonical_json(descriptor.model_dump(mode="json"))
    return StepInput(
        type=MEDIA_ARTIFACT,
        artifact_id=media.id,
        schema_version=MEDIA_ARTIFACT.schema_version,
        cache_key=media.content_hash,
        payload_hash=sha256_hex(payload),
        payload=payload,
    )


def _media_summary(media: MediaObject) -> MediaSummary:
    return MediaSummary(
        media_id=media.id,
        content_hash=media.content_hash,
        container=media.container.value,
        codec=media.codec,
        bitrate=media.bitrate,
        sample_rate=media.sample_rate,
        channels=media.channels,
        duration_seconds=media.duration_seconds,
        probe_artifact_id=media.probe_artifact_id,
    )


def _step_views(run: RunResult) -> tuple[ObservationStepView, ...]:
    return tuple(
        ObservationStepView(
            node_id=step.node_id,
            capability_id=step.capability_id,
            status=step.status.value,
            attempts=step.attempts,
            warning_codes=step.warning_codes,
        )
        for step in run.steps
    )


def _merge_warnings(
    run_warnings: tuple[StepWarning, ...],
    extra: Sequence[ObservationWarning],
) -> tuple[ObservationWarning, ...]:
    merged: list[ObservationWarning] = []
    seen: set[tuple[str, str]] = set()
    for run_warning in run_warnings:
        key = (run_warning.code, run_warning.message)
        if key in seen:
            continue
        seen.add(key)
        merged.append(
            ObservationWarning(
                code=run_warning.code,
                message=run_warning.message,
                user_action=run_warning.user_action,
            )
        )
    for extra_warning in extra:
        key = (extra_warning.code, extra_warning.message)
        if key in seen:
            continue
        seen.add(key)
        merged.append(extra_warning)
    return tuple(merged)


def _observation_error(run: RunResult) -> TrackForgeError:
    failed = [step for step in run.steps if step.status is StepStatus.FAILED]
    context: dict[str, Any] = {
        "run_id": str(run.run_id),
        "steps": [step.node_id for step in failed],
    }
    error_types = {
        str(step.error.get("type"))
        for step in failed
        if isinstance(step.error, Mapping) and step.error.get("type")
    }
    messages = [
        str(step.error.get("message"))
        for step in failed
        if isinstance(step.error, Mapping) and step.error.get("message")
    ]
    message = "; ".join(messages) or f"observation run ended with status {run.status.value}"
    if "ProbeFailed" in error_types:
        return ProbeFailed(message, retryable=False, context=context)
    if "ProviderUnavailable" in error_types:
        return ProviderUnavailable(
            message,
            retryable=False,
            user_action="Install ffmpeg/ffprobe and ensure ffprobe is on PATH.",
            context=context,
        )
    return ObservationFailed(message, retryable=False, context=context)


__all__ = ["ObserveItem"]

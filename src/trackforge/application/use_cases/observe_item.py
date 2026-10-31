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
from trackforge.application.dto.progress import ProgressContext
from trackforge.application.ports.execution import Capability, StepInput, StepWarning
from trackforge.application.ports.observations import (
    ACOUSTID_ARTIFACT,
    ASR_TRANSCRIPT_ARTIFACT,
    COVER_ARTIFACT,
    FINGERPRINT_ARTIFACT,
    LYRICS_ARTIFACT,
    MEDIA_ARTIFACT,
    MUSICBRAINZ_ARTIFACT,
    PATH_STRUCTURE_ARTIFACT,
    PROBE_ARTIFACT,
    SOURCE_IDENTITY_ARTIFACT,
    SOURCE_METADATA_ARTIFACT,
    SOURCE_SNAPSHOT_ARTIFACT,
    STEMS_ARTIFACT,
)
from trackforge.application.ports.progress import ProgressSink
from trackforge.application.ports.repositories import UnitOfWork
from trackforge.application.ports.storage import ArtifactStore, StorageError
from trackforge.application.services.canonical_json import canonical_json, sha256_hex
from trackforge.application.use_cases.plan_item import (
    ASR_CAPABILITY_ALIASES,
    DEFAULT_OBSERVE_OUTPUTS,
    DEFAULT_REQUIRED_OUTPUTS,
    METADATA_OBSERVE_OUTPUTS,
    STEMS_CAPABILITY_ALIASES,
    asr_requested,
    planner_disabled,
    planner_enabled,
    stems_requested,
)
from trackforge.domain.errors import (
    DomainValidationError,
    ObservationFailed,
    ProbeFailed,
    ProviderUnavailable,
    RecordNotFound,
    TrackForgeError,
)
from trackforge.domain.model.capabilities import AnalysisProfile, ArtifactType
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
        metadata_enabled: bool | None = None,
        metadata: bool | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._uow_factory = unit_of_work_factory
        self._capabilities = tuple(capabilities)
        self._artifact_store = artifact_store
        self._config = dict(config or {})
        self._max_attempts = max_attempts
        registered_metadata = any(
            isinstance(artifact, ArtifactType) and artifact in METADATA_OBSERVE_OUTPUTS
            for capability in self._capabilities
            for artifact in capability.manifest.provides
        )
        requested = metadata if metadata is not None else metadata_enabled
        if requested is None:
            requested = bool(
                self._config.get("metadata")
                or self._config.get("metadata_enabled")
                or registered_metadata
            )
        self._metadata_enabled = bool(requested)

    async def observe(
        self,
        item_id: ItemId,
        profile: str | AnalysisProfile = "balanced",
        *,
        refresh: bool | str | Sequence[str] | None = None,
        metadata: bool | None = None,
        enabled: Sequence[str] = (),
        disabled: Sequence[str] = (),
        progress: ProgressSink | None = None,
        progress_context: ProgressContext | None = None,
    ) -> ObservationView:
        """Execute observations, update measured media fields, and mark observed.

        ``metadata=True`` (or ``metadata_enabled`` on the use case) adds the
        opt-in fingerprint/AcoustID/MusicBrainz/cover/lyrics observers.  ASR is
        requested by ``profile=full`` or ``enabled`` (``asr``/``whisper``) and
        can be turned off with ``disabled=('asr',)``.  Demucs is requested by
        ``profile=full`` or ``enabled`` (``stems``/``demucs``) and can be
        turned off with ``disabled=('stems',)``.  When ``refresh`` is true,
        expensive metadata/ASR/stems capabilities bypass the artifact cache;
        local observations remain cacheable.
        """

        analysis_profile = self._profile(profile, enabled=enabled, disabled=disabled)
        profile_name = analysis_profile.name
        asr_on = asr_requested(
            profile_name,
            analysis_profile.enabled,
            analysis_profile.disabled,
        )
        asr_off_for_full = profile_name.strip().casefold() == "full" and bool(
            ASR_CAPABILITY_ALIASES.intersection(
                {item.strip().casefold() for item in analysis_profile.disabled if item.strip()}
            )
        )
        stems_on = stems_requested(
            profile_name,
            analysis_profile.enabled,
            analysis_profile.disabled,
        )
        stems_off_for_full = profile_name.strip().casefold() == "full" and bool(
            STEMS_CAPABILITY_ALIASES.intersection(
                {item.strip().casefold() for item in analysis_profile.disabled if item.strip()}
            )
        )
        warnings: list[ObservationWarning] = []
        if asr_off_for_full:
            warnings.append(
                ObservationWarning(
                    code="asr_disabled",
                    message="ASR was requested by the full profile but disabled by flags",
                    user_action="Remove --disable asr to run Whisper again.",
                )
            )
        if stems_off_for_full:
            warnings.append(
                ObservationWarning(
                    code="stems_disabled",
                    message="stems were requested by the full profile but disabled by flags",
                    user_action="Remove --disable stems to run Demucs again.",
                )
            )
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
            desired_list = [
                output
                for output in DEFAULT_OBSERVE_OUTPUTS
                if source_path is not None or output != PATH_STRUCTURE_ARTIFACT
            ]
            include_metadata = self._metadata_enabled if metadata is None else bool(metadata)
            if include_metadata:
                desired_list.extend(self._metadata_outputs())
            if (asr_on or asr_off_for_full) and self._provides(ASR_TRANSCRIPT_ARTIFACT):
                desired_list.append(ASR_TRANSCRIPT_ARTIFACT)
            if (stems_on or stems_off_for_full) and self._provides(STEMS_ARTIFACT):
                desired_list.append(STEMS_ARTIFACT)
            if source_path is None:
                warnings.append(
                    ObservationWarning(
                        code="path_unavailable",
                        message="no source file path is recorded; path.structure.v1 was skipped",
                    )
                )
            snapshot = await self._source_snapshot_input(uow, item)
            has_source_metadata = any(
                SOURCE_METADATA_ARTIFACT in capability.manifest.provides
                for capability in self._capabilities
            )
            if snapshot is not None and has_source_metadata:
                desired_list.append(SOURCE_METADATA_ARTIFACT)
                if include_metadata and self._provides(SOURCE_IDENTITY_ARTIFACT):
                    desired_list.append(SOURCE_IDENTITY_ARTIFACT)
                if self._provides(COVER_ARTIFACT):
                    desired_list.append(COVER_ARTIFACT)
            else:
                snapshot = None
            desired = tuple(desired_list)
            params: dict[str, dict[str, Any]] = {}
            if source_path is not None:
                params["path_structure"] = {"path": source_path}

            inputs = (
                (MEDIA_ARTIFACT, SOURCE_SNAPSHOT_ARTIFACT)
                if snapshot is not None
                else (MEDIA_ARTIFACT,)
            )
            plan = Planner().plan(
                desired,
                analysis_profile,
                self._capabilities,
                required_outputs=DEFAULT_REQUIRED_OUTPUTS,
                inputs=inputs,
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
                config={**self._config, "offline": self._config.get("offline", False)},
                max_attempts=self._max_attempts,
                force_steps=self._refresh_steps(
                    self._config.get("refresh", False) if refresh is None else refresh
                ),
                commit=uow.commit,
            )
            initial_inputs: dict[ArtifactType | str, StepInput] = {
                MEDIA_ARTIFACT: _media_step_input(media)
            }
            if snapshot is not None:
                initial_inputs[SOURCE_SNAPSHOT_ARTIFACT] = snapshot
            run = await executor.run(
                plan,
                item_id=item.id,
                profile=analysis_profile.name,
                mode="auto",
                initial_inputs=initial_inputs,
                progress=progress,
                progress_context=progress_context,
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
                step_reports=run.handler_reports,
            )

        if run.status is not RunStatus.COMPLETED:
            raise _observation_error(run)
        return view

    async def _source_snapshot_input(
        self,
        uow: UnitOfWork,
        item: Item,
    ) -> StepInput | None:
        if item.primary_intake_id is None:
            return None
        intake = await uow.intakes.get(item.primary_intake_id)
        if intake is None or intake.source_snapshot_artifact_id is None:
            return None
        record = await uow.artifacts.get(intake.source_snapshot_artifact_id)
        if record is None:
            return None
        try:
            reference = await self._artifact_store.find(record.cache_key)
            if reference is None:
                return None
            manifest, payload = await self._artifact_store.load(reference)
        except (StorageError, OSError):
            return None
        return StepInput(
            type=SOURCE_SNAPSHOT_ARTIFACT,
            artifact_id=manifest.artifact_id,
            schema_version=manifest.schema_version,
            cache_key=reference.cache_key,
            payload_hash=manifest.payload_hash,
            payload=payload,
            manifest=manifest,
            ref=reference,
        )

    def _provides(self, artifact: ArtifactType) -> bool:
        return any(artifact in capability.manifest.provides for capability in self._capabilities)

    def _metadata_outputs(self) -> list[ArtifactType]:
        provided = {
            artifact
            for capability in self._capabilities
            for artifact in capability.manifest.provides
            if isinstance(artifact, ArtifactType)
        }
        outputs: list[ArtifactType] = []
        if FINGERPRINT_ARTIFACT in provided:
            outputs.append(FINGERPRINT_ARTIFACT)
        if ACOUSTID_ARTIFACT in provided and FINGERPRINT_ARTIFACT in outputs:
            outputs.append(ACOUSTID_ARTIFACT)
        if MUSICBRAINZ_ARTIFACT in provided:
            outputs.append(MUSICBRAINZ_ARTIFACT)
        if COVER_ARTIFACT in provided and MUSICBRAINZ_ARTIFACT in outputs:
            outputs.append(COVER_ARTIFACT)
        if LYRICS_ARTIFACT in provided:
            outputs.append(LYRICS_ARTIFACT)
        return outputs

    def _refresh_steps(self, refresh: bool | str | Sequence[str]) -> tuple[str, ...]:
        if refresh is False:
            return ()
        if refresh is True:
            return tuple(
                capability.manifest.id
                for capability in self._capabilities
                if any(
                    isinstance(artifact, ArtifactType)
                    and (
                        artifact in METADATA_OBSERVE_OUTPUTS
                        or artifact in {ASR_TRANSCRIPT_ARTIFACT, STEMS_ARTIFACT}
                    )
                    for artifact in capability.manifest.provides
                )
            )
        if isinstance(refresh, str):
            requested = tuple(part.strip() for part in refresh.split(",") if part.strip())
        else:
            requested = tuple(str(item).strip() for item in refresh if str(item).strip())
        aliases = {
            "fingerprint": "chromaprint",
            "musicbrainz": "musicbrainz_search",
            "acoustid": "acoustid_lookup",
            "cover": "cover_search",
            "cover_art_archive": "cover_search",
            "itunes": "cover_search",
            "deezer": "cover_search",
            "lrclib": "lrclib_search",
            "lyrics": "lrclib_search",
            "asr": "whisper",
            "transcript": "whisper",
            "stems": "demucs",
            "demucs": "demucs",
        }
        return tuple(aliases.get(item.casefold(), item) for item in requested)

    @staticmethod
    def _profile(
        value: str | AnalysisProfile,
        *,
        enabled: Sequence[str] = (),
        disabled: Sequence[str] = (),
    ) -> AnalysisProfile:
        if isinstance(value, AnalysisProfile):
            name = value.name
            base_enabled = tuple(value.enabled)
            base_disabled = tuple(value.disabled)
        elif isinstance(value, str) and value.strip():
            name = value
            base_enabled = ()
            base_disabled = ()
        else:
            raise DomainValidationError("profile must be a non-empty name or AnalysisProfile")
        requested_enabled = tuple(enabled) or base_enabled
        requested_disabled = tuple(disabled) or base_disabled
        return AnalysisProfile(
            name=name,
            enabled=planner_enabled(requested_enabled),
            disabled=planner_disabled(name, requested_enabled, requested_disabled),
        )

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

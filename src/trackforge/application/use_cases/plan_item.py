"""Plan the discover → acquire → observe path for one source reference."""

from __future__ import annotations

from collections.abc import Sequence

from trackforge.application.dto.plan import PlanOptions, PlanStepView, PlanView
from trackforge.application.ports.execution import Capability
from trackforge.application.ports.observations import (
    ACOUSTID_ARTIFACT,
    ASR_TRANSCRIPT_ARTIFACT,
    COVER_ARTIFACT,
    FINGERPRINT_ARTIFACT,
    INITIAL_TAGS_ARTIFACT,
    LYRICS_ARTIFACT,
    MEDIA_ARTIFACT,
    MUSICBRAINZ_ARTIFACT,
    PATH_STRUCTURE_ARTIFACT,
    PROBE_ARTIFACT,
    SOURCE_METADATA_ARTIFACT,
    SOURCE_SNAPSHOT_ARTIFACT,
    STEMS_ARTIFACT,
)
from trackforge.application.ports.sources import SourceResolver
from trackforge.domain.errors import CookiesPermissionsError, OfflineModeError, UnsupportedSource
from trackforge.domain.model.capabilities import AnalysisProfile, ArtifactType, Cost
from trackforge.domain.model.source import SourceRef, Url
from trackforge.pipeline.planner import Planner

DEFAULT_OBSERVE_OUTPUTS = (
    PROBE_ARTIFACT,
    INITIAL_TAGS_ARTIFACT,
    PATH_STRUCTURE_ARTIFACT,
)
METADATA_OBSERVE_OUTPUTS = (
    FINGERPRINT_ARTIFACT,
    ACOUSTID_ARTIFACT,
    MUSICBRAINZ_ARTIFACT,
    COVER_ARTIFACT,
    LYRICS_ARTIFACT,
)
ASR_CAPABILITY_ALIASES = frozenset({"asr", "whisper"})
STEMS_CAPABILITY_ALIASES = frozenset({"stems", "demucs"})
DEFAULT_REQUIRED_OUTPUTS = (PROBE_ARTIFACT,)


def _names(values: Sequence[str] | str) -> tuple[str, ...]:
    if isinstance(values, str):
        return tuple(part.strip() for part in values.split(",") if part.strip())
    return tuple(str(item).strip() for item in values if str(item).strip())


def asr_requested(
    profile: str,
    enabled: Sequence[str] | str = (),
    disabled: Sequence[str] | str = (),
) -> bool:
    """Return whether the profile/flags request ASR."""

    enabled_names = {item.casefold() for item in _names(enabled)}
    disabled_names = {item.casefold() for item in _names(disabled)}
    if ASR_CAPABILITY_ALIASES.intersection(disabled_names):
        return False
    return profile.strip().casefold() == "full" or bool(
        ASR_CAPABILITY_ALIASES.intersection(enabled_names)
    )


def stems_requested(
    profile: str,
    enabled: Sequence[str] | str = (),
    disabled: Sequence[str] | str = (),
) -> bool:
    """Return whether the profile/flags request Demucs stems."""

    enabled_names = {item.casefold() for item in _names(enabled)}
    disabled_names = {item.casefold() for item in _names(disabled)}
    if STEMS_CAPABILITY_ALIASES.intersection(disabled_names):
        return False
    return profile.strip().casefold() == "full" or bool(
        STEMS_CAPABILITY_ALIASES.intersection(enabled_names)
    )


def planner_enabled(enabled: Sequence[str] | str) -> tuple[str, ...]:
    """Drop user-facing capability aliases from capability-id filters."""

    aliases = ASR_CAPABILITY_ALIASES | STEMS_CAPABILITY_ALIASES
    return tuple(item for item in _names(enabled) if item.casefold() not in aliases)


def planner_disabled(
    profile: str,
    enabled: Sequence[str] | str,
    disabled: Sequence[str] | str,
) -> tuple[str, ...]:
    """Map an explicit ``--disable asr`` to the Whisper capability id."""

    aliases = ASR_CAPABILITY_ALIASES | STEMS_CAPABILITY_ALIASES
    names = [item for item in _names(disabled) if item.casefold() not in aliases]
    disabled_names = {item.casefold() for item in _names(disabled)}
    enabled_names = {item.casefold() for item in _names(enabled)}
    if profile.strip().casefold() == "full":
        if ASR_CAPABILITY_ALIASES.intersection(disabled_names) and not (
            ASR_CAPABILITY_ALIASES.intersection(enabled_names)
        ):
            names.append("whisper")
        if STEMS_CAPABILITY_ALIASES.intersection(disabled_names) and not (
            STEMS_CAPABILITY_ALIASES.intersection(enabled_names)
        ):
            names.append("demucs")
    return tuple(names)


# Local copying is cheap relative to the analysis steps.  The estimate is
# deliberately coarse; Plan 09 will replace it with measured throughput.
_ACQUIRE_ESTIMATE = Cost(cpu_seconds=0.1, memory_mb=64.0)
_DOWNLOAD_ESTIMATE = Cost(cpu_seconds=1.0, memory_mb=64.0, network_calls=1)


class PlanItem:
    """Resolve a source and build a side-effect-free ingest/observe plan."""

    def __init__(
        self,
        capabilities: Sequence[Capability] = (),
        *,
        resolvers: Sequence[SourceResolver] = (),
    ) -> None:
        self._capabilities = tuple(capabilities)
        self._resolvers = tuple(resolvers)

    async def plan(
        self,
        source: SourceRef,
        options: PlanOptions | None = None,
        resolvers: Sequence[SourceResolver] | None = None,
    ) -> PlanView:
        """Return candidates plus the DAG the later use cases would execute."""

        opts = options or PlanOptions()
        if opts.offline and isinstance(source, Url):
            raise OfflineModeError(
                "offline mode does not allow URL sources",
                context={"source_type": "url"},
            )
        available_resolvers = tuple(resolvers) if resolvers is not None else self._resolvers
        resolver = next(
            (candidate for candidate in available_resolvers if candidate.supports(source)),
            None,
        )
        if resolver is None:
            return PlanView(
                source=source,
                warnings=(
                    "No installed source resolver supports this input. "
                    "Local MVP supports .mp3, .m4a, .flac, .ogg and .opus files.",
                ),
                blocked_reason=f"unsupported source: {type(source).__name__}",
            )

        try:
            resolved = await resolver.resolve(
                source,
                {"profile": opts.profile, "offline": opts.offline},
            )
        except (CookiesPermissionsError, OfflineModeError):
            raise
        except UnsupportedSource as exc:
            return PlanView(
                source=source,
                resolver_id=resolver.id,
                warnings=(exc.message,),
                blocked_reason=exc.message,
            )
        if not resolved.candidates:
            return PlanView(
                source=source,
                resolver_id=resolver.id,
                warnings=resolved.warnings,
                blocked_reason="source resolved to zero candidates",
            )

        profile = AnalysisProfile(
            name=opts.profile,
            enabled=planner_enabled(opts.enabled),
            disabled=planner_disabled(opts.profile, opts.enabled, opts.disabled),
        )
        is_url = isinstance(source, Url)
        observe_outputs: tuple[ArtifactType, ...] = tuple(
            output
            for output in DEFAULT_OBSERVE_OUTPUTS
            if not (is_url and output == PATH_STRUCTURE_ARTIFACT)
        )
        provided = {
            artifact
            for capability in self._capabilities
            for artifact in capability.manifest.provides
            if isinstance(artifact, ArtifactType)
        }
        for output in METADATA_OBSERVE_OUTPUTS:
            if output not in provided:
                continue
            if output == ACOUSTID_ARTIFACT and FINGERPRINT_ARTIFACT not in provided:
                continue
            if output == COVER_ARTIFACT and MUSICBRAINZ_ARTIFACT not in provided:
                continue
            observe_outputs = (*observe_outputs, output)
        if ASR_TRANSCRIPT_ARTIFACT in provided and (
            asr_requested(opts.profile, opts.enabled, opts.disabled)
            or (
                opts.profile.strip().casefold() == "full"
                and ASR_CAPABILITY_ALIASES.intersection(
                    {item.strip().casefold() for item in opts.disabled if item.strip()}
                )
            )
        ):
            observe_outputs = (*observe_outputs, ASR_TRANSCRIPT_ARTIFACT)
        if STEMS_ARTIFACT in provided and (
            stems_requested(opts.profile, opts.enabled, opts.disabled)
            or (
                opts.profile.strip().casefold() == "full"
                and STEMS_CAPABILITY_ALIASES.intersection(
                    {item.strip().casefold() for item in opts.disabled if item.strip()}
                )
            )
        ):
            observe_outputs = (*observe_outputs, STEMS_ARTIFACT)
        inputs: tuple[ArtifactType, ...] = (MEDIA_ARTIFACT,)
        has_source_metadata = any(
            SOURCE_METADATA_ARTIFACT in capability.manifest.provides
            for capability in self._capabilities
        )
        if is_url and has_source_metadata:
            observe_outputs = (*observe_outputs, SOURCE_METADATA_ARTIFACT)
            if COVER_ARTIFACT in provided and COVER_ARTIFACT not in observe_outputs:
                observe_outputs = (*observe_outputs, COVER_ARTIFACT)
            inputs = (MEDIA_ARTIFACT, SOURCE_SNAPSHOT_ARTIFACT)
        pipeline_plan = Planner().plan(
            observe_outputs,
            profile,
            self._capabilities,
            required_outputs=DEFAULT_REQUIRED_OUTPUTS,
            inputs=inputs,
        )

        acquire_provides: tuple[str, ...] = (str(MEDIA_ARTIFACT),)
        if is_url and has_source_metadata:
            acquire_provides = (str(MEDIA_ARTIFACT), str(SOURCE_SNAPSHOT_ARTIFACT))
        acquire_estimate = _DOWNLOAD_ESTIMATE if is_url else _ACQUIRE_ESTIMATE
        steps = [
            PlanStepView(
                node_id="acquire",
                capability_id="acquire",
                description=(
                    "Download the selected audio candidate and snapshot its metadata."
                    if is_url
                    else "Copy the selected candidate into the content-addressed store."
                ),
                provides=acquire_provides,
                estimated_cost=acquire_estimate,
            )
        ]
        steps.extend(
            PlanStepView(
                node_id=node.node_id,
                capability_id=node.capability_id,
                description=f"Run {node.capability_id}@{node.capability_version}.",
                requires=node.requires_keys,
                provides=node.provides_keys,
                estimated_cost=node.cost,
            )
            for node in pipeline_plan.nodes
        )

        warnings = list(resolved.warnings)
        warnings.extend(warning.message for warning in pipeline_plan.warnings)
        return PlanView(
            source=source,
            resolver_id=resolver.id,
            candidates=resolved.candidates,
            steps=tuple(steps),
            estimated_cost=pipeline_plan.estimated_cost + acquire_estimate,
            warnings=tuple(dict.fromkeys(warnings)),
            blocked_reason=pipeline_plan.blocked_reason,
        )


__all__ = [
    "DEFAULT_OBSERVE_OUTPUTS",
    "DEFAULT_REQUIRED_OUTPUTS",
    "METADATA_OBSERVE_OUTPUTS",
    "PlanItem",
    "STEMS_CAPABILITY_ALIASES",
    "asr_requested",
    "planner_disabled",
    "planner_enabled",
    "stems_requested",
]

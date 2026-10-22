"""Plan the discover → acquire → observe path for one source reference."""

from __future__ import annotations

from collections.abc import Sequence

from trackforge.application.dto.plan import PlanOptions, PlanStepView, PlanView
from trackforge.application.ports.execution import Capability
from trackforge.application.ports.observations import (
    INITIAL_TAGS_ARTIFACT,
    MEDIA_ARTIFACT,
    PATH_STRUCTURE_ARTIFACT,
    PROBE_ARTIFACT,
)
from trackforge.application.ports.sources import SourceResolver
from trackforge.domain.errors import UnsupportedSource
from trackforge.domain.model.capabilities import AnalysisProfile, Cost
from trackforge.domain.model.source import SourceRef
from trackforge.pipeline.planner import Planner

DEFAULT_OBSERVE_OUTPUTS = (
    PROBE_ARTIFACT,
    INITIAL_TAGS_ARTIFACT,
    PATH_STRUCTURE_ARTIFACT,
)
DEFAULT_REQUIRED_OUTPUTS = (PROBE_ARTIFACT,)

# Local copying is cheap relative to the analysis steps.  The estimate is
# deliberately coarse; Plan 09 will replace it with measured throughput.
_ACQUIRE_ESTIMATE = Cost(cpu_seconds=0.1, memory_mb=64.0)


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
            resolved = await resolver.resolve(source, {"profile": opts.profile})
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
            enabled=opts.enabled,
            disabled=opts.disabled,
        )
        pipeline_plan = Planner().plan(
            DEFAULT_OBSERVE_OUTPUTS,
            profile,
            self._capabilities,
            required_outputs=DEFAULT_REQUIRED_OUTPUTS,
            inputs=(MEDIA_ARTIFACT,),
        )

        steps = [
            PlanStepView(
                node_id="acquire",
                capability_id="acquire",
                description="Copy the selected candidate into the content-addressed store.",
                provides=(str(MEDIA_ARTIFACT),),
                estimated_cost=_ACQUIRE_ESTIMATE,
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
            estimated_cost=pipeline_plan.estimated_cost + _ACQUIRE_ESTIMATE,
            warnings=tuple(dict.fromkeys(warnings)),
            blocked_reason=pipeline_plan.blocked_reason,
        )


__all__ = ["DEFAULT_OBSERVE_OUTPUTS", "DEFAULT_REQUIRED_OUTPUTS", "PlanItem"]

"""Observe the filesystem shape of a local path as weak evidence."""

from __future__ import annotations

from pathlib import Path

from trackforge.application.dto.observations import PathStructurePayload
from trackforge.application.ports.execution import (
    ArtifactDraft,
    StepContext,
    StepResult,
)
from trackforge.application.ports.observations import (
    MEDIA_ARTIFACT,
    PATH_STRUCTURE_ARTIFACT,
)
from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.capabilities import (
    CapabilityKind,
    CapabilityManifest,
    Cost,
    Determinism,
    SideEffect,
)


def build_path_structure(path: str) -> PathStructurePayload:
    """Split a path into components without interpreting artist/album semantics."""

    value = Path(path)
    parent_names = [part for part in value.parent.parts if part not in ("", "/")]
    return PathStructurePayload(
        path=path,
        parent_names=parent_names,
        stem=value.stem,
        suffix=value.suffix,
    )


class PathStructureObserver:
    """Emit the path/stem/suffix observation used later by interpreters."""

    manifest = CapabilityManifest(
        id="path_structure",
        version="1",
        kind=CapabilityKind.OBSERVER,
        requires=(MEDIA_ARTIFACT,),
        provides=(PATH_STRUCTURE_ARTIFACT,),
        cost=Cost(cpu_seconds=0.0),
        determinism=Determinism.DETERMINISTIC,
        cacheable=True,
        side_effects=SideEffect.NONE,
        description="Observe local path components; does not parse them into claims.",
    )

    async def run(self, ctx: StepContext) -> StepResult:
        raw_path = ctx.params.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise DomainValidationError(
                "path_structure requires a non-empty 'path' parameter",
                user_action="Pass the original file path when planning the observation.",
            )
        payload = build_path_structure(raw_path)
        return StepResult(
            artifacts=(
                ArtifactDraft(
                    type=PATH_STRUCTURE_ARTIFACT,
                    payload=payload.model_dump(mode="json"),
                ),
            )
        )


__all__ = ["PathStructureObserver", "build_path_structure"]

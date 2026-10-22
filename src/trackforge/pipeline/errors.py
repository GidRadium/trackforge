"""Typed errors raised by the pipeline planner and executor."""

from __future__ import annotations

from typing import Any

from trackforge.domain.errors import InvalidStateTransition, TrackForgeError


class PipelineError(TrackForgeError):
    """Base class for expected pipeline failures."""

    default_user_action = "Inspect the pipeline plan and run state."


class PipelinePlanError(PipelineError):
    """Raised when a plan cannot be built or is internally inconsistent."""

    default_user_action = "Check capability manifests, desired outputs, and provider chains."


class CycleError(PipelinePlanError):
    """Raised when the capability graph contains a cycle."""

    def __init__(self, nodes: tuple[str, ...] | list[str]) -> None:
        cycle = tuple(str(node) for node in nodes)
        super().__init__(
            "capability graph contains a cycle",
            context={"cycle": list(cycle)},
        )
        self.cycle = cycle


class DuplicateNodeError(PipelinePlanError):
    """Raised when two plan nodes share a node id."""

    def __init__(self, node_id: str) -> None:
        super().__init__(
            "duplicate plan node id",
            context={"node_id": node_id},
        )
        self.node_id = node_id


class MissingProviderError(PipelinePlanError):
    """Raised when a requested artifact has no provider capability."""

    def __init__(self, artifact_type: str) -> None:
        super().__init__(
            "no provider for requested output",
            context={"artifact_type": artifact_type},
        )
        self.artifact_type = artifact_type


class PlanBlocked(PipelineError):
    """Raised when an executor is asked to run a blocked plan."""

    def __init__(self, reason: str) -> None:
        super().__init__(
            "pipeline plan is blocked",
            context={"reason": reason},
        )
        self.reason = reason


class InvalidTransition(InvalidStateTransition):
    """Raised when a run or step state transition is not allowed."""

    def __init__(
        self,
        entity: str,
        current: str,
        target: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "entity": entity,
            "current": current,
            "target": target,
        }
        if context:
            payload.update(context)
        super().__init__(
            f"illegal {entity} transition: {current} -> {target}",
            context=payload,
        )
        self.entity = entity
        self.current = current
        self.target = target


class RunNotFound(PipelineError):
    """Raised when a run id does not exist in the catalog."""

    def __init__(self, run_id: str) -> None:
        super().__init__(
            "run not found",
            context={"run_id": run_id},
        )
        self.run_id = run_id


class StepNotFound(PipelineError):
    """Raised when a step id does not exist in the catalog."""

    def __init__(self, step_id: str) -> None:
        super().__init__(
            "step not found",
            context={"step_id": step_id},
        )
        self.step_id = step_id


class RunAlreadyCompleted(PipelineError):
    """Raised when a completed run is resumed again."""

    def __init__(self, run_id: str) -> None:
        super().__init__(
            "run has already completed; start a new run to execute again",
            context={"run_id": run_id},
        )
        self.run_id = run_id


class RunNotResumable(PipelineError):
    """Raised when a terminal or otherwise non-resumable run is resumed."""

    def __init__(self, run_id: str, status: str) -> None:
        super().__init__(
            "run is not resumable in its current state",
            context={"run_id": run_id, "status": status},
        )
        self.run_id = run_id
        self.status = status


class UnknownCapability(PipelineError):
    """Raised when the plan references a capability not registered with the executor."""

    def __init__(self, capability_id: str, version: str | None = None) -> None:
        super().__init__(
            "capability is not registered with the executor",
            context={"capability_id": capability_id, "version": version},
        )
        self.capability_id = capability_id
        self.version = version


class BudgetExceeded(PipelineError):
    """Raised when executing a step would exceed a hard budget."""

    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(message, context=context)
        self.context = dict(context or {})


class InvalidStepResult(PipelineError):
    """Raised when a capability returns artifacts inconsistent with its manifest."""

    default_retryable = True

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, retryable=retryable, context=context)


class StepFailed(PipelineError):
    """Internal marker for a failed attempt; the executor persists the detail."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, retryable=retryable, context=context)


__all__ = [
    "BudgetExceeded",
    "CycleError",
    "DuplicateNodeError",
    "InvalidStepResult",
    "InvalidTransition",
    "MissingProviderError",
    "PipelineError",
    "PipelinePlanError",
    "PlanBlocked",
    "RunAlreadyCompleted",
    "RunNotFound",
    "RunNotResumable",
    "StepFailed",
    "StepNotFound",
    "UnknownCapability",
]

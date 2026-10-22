"""Pipeline planning, caching, state, and execution.

The package root is a convenience facade only; the individual modules remain
the source of truth.  Nothing here imports concrete adapters, so capability
plugins can depend on the execution contracts without pulling in SQLite or
filesystem implementations.
"""

from __future__ import annotations

from trackforge.application.ports.execution import (
    ArtifactDraft,
    BudgetSnapshot,
    Capability,
    StepContext,
    StepInput,
    StepResult,
    StepResultStatus,
    StepWarning,
)
from trackforge.pipeline.budget import Budget, BudgetUsage
from trackforge.pipeline.cache import (
    Cache,
    CacheEntry,
    CacheInput,
    CacheMiss,
    CacheSchemaMismatch,
    LoadedArtifact,
    cache_key_for,
    cache_key_for_step,
    relevant_config,
)
from trackforge.pipeline.dag import Edge, EdgeKind, Graph, Node, artifact_key
from trackforge.pipeline.degradation import (
    DEFAULT_DESIRED_OUTPUTS,
    DEFAULT_OPTIONAL_OUTPUTS,
    DEFAULT_REQUIRED_OUTPUTS,
    DegradationPolicy,
    FailureAction,
    OutputClass,
    Warning,
    WarningCode,
    budget_warning,
    disabled_warning,
    make_warning,
)
from trackforge.pipeline.errors import (
    BudgetExceeded,
    CycleError,
    DuplicateNodeError,
    InvalidStepResult,
    InvalidTransition,
    MissingProviderError,
    PipelineError,
    PipelinePlanError,
    PlanBlocked,
    RunAlreadyCompleted,
    RunNotFound,
    RunNotResumable,
    StepFailed,
    StepNotFound,
    UnknownCapability,
)
from trackforge.pipeline.events import Event, EventBus, EventType
from trackforge.pipeline.executor import Executor, RunResult, StepOutcome
from trackforge.pipeline.jobs import (
    JobManager,
    RecoveryReport,
    RunStateMachine,
    StepStateMachine,
)
from trackforge.pipeline.planner import Plan, PlanEdge, Planner, PlanNode

__all__ = [
    "DEFAULT_DESIRED_OUTPUTS",
    "DEFAULT_OPTIONAL_OUTPUTS",
    "DEFAULT_REQUIRED_OUTPUTS",
    "ArtifactDraft",
    "Budget",
    "BudgetExceeded",
    "BudgetSnapshot",
    "BudgetUsage",
    "Cache",
    "CacheEntry",
    "CacheInput",
    "CacheMiss",
    "CacheSchemaMismatch",
    "Capability",
    "CycleError",
    "DegradationPolicy",
    "DuplicateNodeError",
    "Edge",
    "EdgeKind",
    "Event",
    "EventBus",
    "EventType",
    "Executor",
    "FailureAction",
    "Graph",
    "InvalidStepResult",
    "InvalidTransition",
    "JobManager",
    "LoadedArtifact",
    "MissingProviderError",
    "Node",
    "OutputClass",
    "PipelineError",
    "PipelinePlanError",
    "Plan",
    "PlanBlocked",
    "PlanEdge",
    "PlanNode",
    "Planner",
    "RecoveryReport",
    "RunAlreadyCompleted",
    "RunNotFound",
    "RunNotResumable",
    "RunResult",
    "RunStateMachine",
    "StepContext",
    "StepFailed",
    "StepInput",
    "StepNotFound",
    "StepOutcome",
    "StepResult",
    "StepResultStatus",
    "StepStateMachine",
    "StepWarning",
    "UnknownCapability",
    "Warning",
    "WarningCode",
    "artifact_key",
    "budget_warning",
    "cache_key_for",
    "cache_key_for_step",
    "disabled_warning",
    "make_warning",
    "relevant_config",
]

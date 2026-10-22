"""Visible degradation policy and standard warning codes.

A missing optional analyzer never fails a run, but it is never silent either.
The planner and executor attach :class:`StepWarning` records to plans, steps,
events, and run results; the codes here are the vocabulary shared by the CLI,
sidecar, and future GUI.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from trackforge.application.ports.execution import StepWarning
from trackforge.domain.model.capabilities import ArtifactRef
from trackforge.pipeline.dag import artifact_key

Warning = StepWarning


class WarningCode(StrEnum):
    """Standard warning codes from ``03-pipeline.md`` plus pipeline-specific ones."""

    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RATE_LIMITED = "rate_limited"
    DISABLED = "disabled"
    NO_DATA = "no_data"
    MODEL_MISSING = "model_missing"
    BUDGET_EXCEEDED = "budget_exceeded"
    INVALID_OUTPUT = "invalid_output"
    MISSING_PROVIDER = "missing_provider"
    CAPABILITY_FAILED = "capability_failed"
    RETRY_EXHAUSTED = "retry_exhausted"
    FALLBACK_USED = "fallback_used"
    CACHE_WRITE_FAILED = "cache_write_failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    DEGRADED = "degraded"


class OutputClass(StrEnum):
    """How a missing output affects the run."""

    REQUIRED = "required"
    DESIRED = "desired"
    OPTIONAL = "optional"


class FailureAction(StrEnum):
    """What the executor should do after a step cannot produce its output."""

    FAIL_RUN = "fail_run"
    DEGRADE = "degrade"
    SKIP = "skip"


DEFAULT_REQUIRED_OUTPUTS: frozenset[str] = frozenset({"output.bundle.v1"})
DEFAULT_DESIRED_OUTPUTS: frozenset[str] = frozenset({"title", "artist"})
DEFAULT_OPTIONAL_OUTPUTS: frozenset[str] = frozenset(
    {
        "audio.features.v1",
        "cover.candidates.v1",
        "lyrics.candidates.v1",
        "bpm",
        "key",
        "genre",
        "mood",
        "language",
    }
)


def output_key(output: ArtifactRef | str) -> str:
    """Return the stable key used to compare outputs and fields."""

    if isinstance(output, str):
        return output
    return artifact_key(output)


@dataclass(frozen=True, slots=True)
class DegradationPolicy:
    """Classify outputs and choose a run outcome when a provider fails."""

    required: frozenset[str] = field(default_factory=lambda: DEFAULT_REQUIRED_OUTPUTS)
    desired: frozenset[str] = field(default_factory=lambda: DEFAULT_DESIRED_OUTPUTS)
    optional: frozenset[str] = field(default_factory=lambda: DEFAULT_OPTIONAL_OUTPUTS)

    def classify(self, output: ArtifactRef | str) -> OutputClass:
        key = output_key(output)
        if key in self.required:
            return OutputClass.REQUIRED
        if key in self.desired:
            return OutputClass.DESIRED
        if key in self.optional:
            return OutputClass.OPTIONAL
        # Unknown outputs are safe to degrade: only an explicitly required
        # output is allowed to fail the whole run.
        return OutputClass.OPTIONAL

    def action_for(self, output: ArtifactRef | str) -> FailureAction:
        output_class = self.classify(output)
        if output_class is OutputClass.REQUIRED:
            return FailureAction.FAIL_RUN
        if output_class is OutputClass.DESIRED:
            return FailureAction.DEGRADE
        return FailureAction.SKIP

    def warning_for(
        self,
        output: ArtifactRef | str,
        *,
        capability_id: str,
        provider: str | None = None,
        code: WarningCode | str = WarningCode.PROVIDER_UNAVAILABLE,
        retryable: bool = False,
        user_action: str | None = None,
        message: str | None = None,
    ) -> StepWarning:
        output_class = self.classify(output).value
        text = message or (
            f"{output_key(output)} ({output_class}) was not produced by {capability_id}"
        )
        return StepWarning(
            code=code,
            message=text,
            capability_id=capability_id,
            provider=provider,
            retryable=retryable,
            user_action=user_action,
        )

    def require(self, outputs: Iterable[ArtifactRef | str]) -> DegradationPolicy:
        """Return a policy with additional required outputs."""

        return DegradationPolicy(
            required=self.required | {output_key(item) for item in outputs},
            desired=self.desired,
            optional=self.optional,
        )


def classify_output(
    output: ArtifactRef | str,
    *,
    required: Iterable[str] = (),
    desired: Iterable[str] = (),
    optional: Iterable[str] = (),
) -> OutputClass:
    """Classify one output using explicit sets, defaulting to optional."""

    return DegradationPolicy(
        required=frozenset(required),
        desired=frozenset(desired),
        optional=frozenset(optional),
    ).classify(output)


def action_for(
    output: ArtifactRef | str,
    *,
    required: Iterable[str] = (),
    desired: Iterable[str] = (),
    optional: Iterable[str] = (),
) -> FailureAction:
    """Return the executor action for a failed output."""

    return DegradationPolicy(
        required=frozenset(required),
        desired=frozenset(desired),
        optional=frozenset(optional),
    ).action_for(output)


def make_warning(
    code: WarningCode | str,
    message: str,
    *,
    capability_id: str | None = None,
    provider: str | None = None,
    retryable: bool = False,
    user_action: str | None = None,
) -> StepWarning:
    """Build a standard warning without importing the policy."""

    return StepWarning(
        code=str(code),
        message=message,
        capability_id=capability_id,
        provider=provider,
        retryable=retryable,
        user_action=user_action,
    )


def disabled_warning(
    *,
    capability_id: str,
    output: ArtifactRef | str,
) -> StepWarning:
    return make_warning(
        WarningCode.DISABLED,
        f"capability {capability_id} is disabled for output {output_key(output)}",
        capability_id=capability_id,
    )


def budget_warning(
    *,
    capability_id: str | None,
    output: ArtifactRef | str,
    message: str | None = None,
) -> StepWarning:
    return make_warning(
        WarningCode.BUDGET_EXCEEDED,
        message or f"output {output_key(output)} was skipped because the budget is exhausted",
        capability_id=capability_id,
        user_action="Increase the profile budget or disable the optional analysis.",
    )


__all__ = [
    "DEFAULT_DESIRED_OUTPUTS",
    "DEFAULT_OPTIONAL_OUTPUTS",
    "DEFAULT_REQUIRED_OUTPUTS",
    "DegradationPolicy",
    "FailureAction",
    "OutputClass",
    "Warning",
    "WarningCode",
    "budget_warning",
    "disabled_warning",
    "make_warning",
    "output_key",
]

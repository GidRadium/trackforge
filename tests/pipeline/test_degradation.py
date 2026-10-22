"""Unit tests for degradation classes and warning codes."""

from __future__ import annotations

import pytest

from trackforge.domain.model.capabilities import ArtifactType
from trackforge.pipeline.degradation import (
    DegradationPolicy,
    FailureAction,
    OutputClass,
    WarningCode,
    budget_warning,
    disabled_warning,
    make_warning,
)

pytestmark = pytest.mark.unit

PROBE = ArtifactType.parse("audio.probe.v1")


def test_classify_output_classes() -> None:
    policy = DegradationPolicy(
        required=frozenset({"output.bundle.v1"}),
        desired=frozenset({"title", "artist"}),
        optional=frozenset({"audio.probe.v1"}),
    )
    assert policy.classify("output.bundle.v1") is OutputClass.REQUIRED
    assert policy.classify("title") is OutputClass.DESIRED
    assert policy.classify(PROBE) is OutputClass.OPTIONAL
    assert policy.classify("something_else") is OutputClass.OPTIONAL


def test_actions_follow_output_class() -> None:
    policy = DegradationPolicy()
    assert policy.action_for("output.bundle.v1") is FailureAction.FAIL_RUN
    assert policy.action_for("title") is FailureAction.DEGRADE
    assert policy.action_for("bpm") is FailureAction.SKIP


def test_warning_for_contains_class_and_provider() -> None:
    policy = DegradationPolicy()
    warning = policy.warning_for(
        "title",
        capability_id="metadata",
        provider="musicbrainz",
        code=WarningCode.NO_DATA,
        retryable=True,
        user_action="Check the network.",
    )
    assert warning.code == "no_data"
    assert warning.capability_id == "metadata"
    assert warning.provider == "musicbrainz"
    assert warning.retryable is True
    assert "desired" in warning.message
    assert warning.user_action == "Check the network."


def test_policy_require_adds_required_output() -> None:
    policy = DegradationPolicy().require([PROBE])
    assert policy.classify(PROBE) is OutputClass.REQUIRED


def test_make_warning_and_standard_codes() -> None:
    warning = make_warning(
        WarningCode.BUDGET_EXCEEDED,
        "too expensive",
        capability_id="demucs",
    )
    assert warning.to_dict() == {
        "code": "budget_exceeded",
        "message": "too expensive",
        "capability_id": "demucs",
        "provider": None,
        "retryable": False,
        "user_action": None,
    }
    assert WarningCode.PROVIDER_UNAVAILABLE.value == "provider_unavailable"
    assert WarningCode.RATE_LIMITED.value == "rate_limited"
    assert WarningCode.DISABLED.value == "disabled"
    assert WarningCode.NO_DATA.value == "no_data"
    assert WarningCode.MODEL_MISSING.value == "model_missing"
    assert WarningCode.BUDGET_EXCEEDED.value == "budget_exceeded"
    assert WarningCode.INVALID_OUTPUT.value == "invalid_output"


def test_disabled_and_budget_warnings() -> None:
    disabled = disabled_warning(capability_id="asr", output=PROBE)
    assert disabled.code == "disabled"
    assert "asr" in disabled.message
    budget = budget_warning(capability_id="asr", output=PROBE)
    assert budget.code == "budget_exceeded"
    assert budget.user_action is not None

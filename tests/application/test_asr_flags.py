"""Profile/flag selection for ASR and RunOptions normalization."""

from __future__ import annotations

import pytest

from trackforge.application.dto.results import RunOptions
from trackforge.application.use_cases.plan_item import (
    asr_requested,
    planner_disabled,
    planner_enabled,
    stems_requested,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("profile", "enabled", "disabled", "expected"),
    [
        ("full", (), (), True),
        ("balanced", (), (), False),
        ("balanced", ("asr",), (), True),
        ("balanced", ("whisper",), (), True),
        ("full", (), ("asr",), False),
        ("full", (), ("whisper",), False),
    ],
)
def test_asr_requested(
    profile: str,
    enabled: tuple[str, ...],
    disabled: tuple[str, ...],
    expected: bool,
) -> None:
    assert asr_requested(profile, enabled, disabled) is expected


def test_asr_aliases_are_not_capability_filters() -> None:
    assert planner_enabled(("asr", "ffprobe")) == ("ffprobe",)
    assert planner_disabled("full", (), ("asr",)) == ("whisper",)
    assert planner_disabled("balanced", (), ("asr",)) == ()


@pytest.mark.parametrize(
    ("profile", "enabled", "disabled", "expected"),
    [
        ("full", (), (), True),
        ("balanced", (), (), False),
        ("balanced", ("stems",), (), True),
        ("balanced", ("demucs",), (), True),
        ("full", (), ("stems",), False),
        ("full", (), ("demucs",), False),
    ],
)
def test_stems_requested(
    profile: str,
    enabled: tuple[str, ...],
    disabled: tuple[str, ...],
    expected: bool,
) -> None:
    assert stems_requested(profile, enabled, disabled) is expected


def test_stems_aliases_are_not_capability_filters() -> None:
    assert planner_enabled(("stems", "ffprobe")) == ("ffprobe",)
    assert planner_disabled("full", (), ("stems",)) == ("demucs",)
    assert planner_disabled("full", ("stems",), ("stems",)) == ()
    assert planner_disabled("balanced", (), ("stems",)) == ()


def test_run_options_parse_enable_disable_strings() -> None:
    options = RunOptions(profile="balanced", enabled="asr, whisper", disabled="lrclib")

    assert options.enabled == ("asr", "whisper")
    assert options.disabled == ("lrclib",)


def test_run_options_reject_enable_disable_overlap() -> None:
    with pytest.raises(Exception, match="overlap"):
        RunOptions(enabled="asr", disabled="asr")

"""Unit tests for the capability planner."""

from __future__ import annotations

import pytest

from trackforge.domain.model.capabilities import (
    AnalysisProfile,
    ArtifactType,
    CapabilityKind,
    Cost,
    ProviderChain,
)
from trackforge.pipeline.dag import EdgeKind, Node
from trackforge.pipeline.errors import DuplicateNodeError, PipelinePlanError
from trackforge.pipeline.planner import Plan, Planner

pytestmark = pytest.mark.unit

MEDIA = ArtifactType.parse("media.raw.v1")
PROBE = ArtifactType.parse("audio.probe.v1")
FEATURES = ArtifactType.parse("audio.features.v1")
LYRICS = ArtifactType.parse("lyrics.candidates.v1")


def test_planner_builds_transitive_dag(make_capability, make_manifest, make_plan) -> None:
    acquire = make_capability(
        make_manifest("acquire", provides=(MEDIA,), kind=CapabilityKind.PROVIDER)
    )
    probe = make_capability(make_manifest("probe", requires=(MEDIA,), provides=(PROBE,)))
    features = make_capability(make_manifest("features", requires=(PROBE,), provides=(FEATURES,)))

    plan = make_plan([acquire, probe, features], [FEATURES], required=[FEATURES])

    assert plan.blocked_reason is None
    assert [node.node_id for node in plan.ordered_nodes] == [
        "acquire",
        "probe",
        "features",
    ]
    assert plan.provider_groups == {
        "media.raw.v1": ("acquire",),
        "audio.probe.v1": ("probe",),
        "audio.features.v1": ("features",),
    }
    data_edges = {edge for edge in plan.edges if edge.kind is EdgeKind.DATA}
    assert {(edge.source, edge.target) for edge in data_edges} == {
        ("acquire", "probe"),
        ("probe", "features"),
    }


def test_provider_chain_adds_fallback_edges(make_manifest, make_plan) -> None:
    first = make_manifest("first", provides=(PROBE,))
    second = make_manifest("second", provides=(PROBE,))
    plan = make_plan([first, second], [PROBE], required=[PROBE])

    assert plan.provider_groups[PROBE.render()] == ("first", "second")
    fallback = [edge for edge in plan.edges if edge.kind is EdgeKind.FALLBACK]
    assert [(edge.source, edge.target) for edge in fallback] == [("first", "second")]


def test_explicit_provider_chain_order_wins(make_manifest, make_plan) -> None:
    first = make_manifest("first", provides=(PROBE,))
    second = make_manifest("second", provides=(PROBE,))
    plan = make_plan(
        [first, second],
        [PROBE],
        required=[PROBE],
        provider_chains={PROBE: ["second", "first"]},
    )
    assert plan.provider_groups[PROBE.render()] == ("second", "first")


def test_disabled_provider_blocks_required_output(make_manifest, make_plan) -> None:
    probe = make_manifest("probe", provides=(PROBE,))
    plan = make_plan(
        [probe],
        [PROBE],
        required=[PROBE],
        profile=AnalysisProfile(name="test", disabled=("probe",)),
    )
    assert plan.blocked_reason is not None
    assert "probe" in plan.blocked_reason or "audio.probe" in plan.blocked_reason
    assert any(warning.code == "disabled" for warning in plan.warnings)


def test_enabled_profile_restricts_providers(make_manifest, make_plan) -> None:
    probe = make_manifest("probe", provides=(PROBE,))
    other = make_manifest("other", provides=(PROBE,))
    plan = make_plan(
        [probe, other],
        [PROBE],
        required=[PROBE],
        profile=AnalysisProfile(name="test", enabled=("other",)),
    )
    assert plan.provider_groups[PROBE.render()] == ("other",)
    assert all(node.capability_id != "probe" for node in plan.nodes)


def test_missing_required_provider_is_blocked(make_plan) -> None:
    plan = make_plan([], [PROBE], required=[PROBE])
    assert plan.blocked_reason is not None
    assert "no provider" in plan.blocked_reason
    assert plan.nodes == ()


def test_missing_optional_provider_is_warning_only(make_plan) -> None:
    plan = make_plan([], [PROBE])
    assert plan.blocked_reason is None
    assert any(warning.code == "missing_provider" for warning in plan.warnings)


def test_missing_transitive_requirement_is_blocked(make_manifest, make_plan) -> None:
    probe = make_manifest("probe", requires=(MEDIA,), provides=(PROBE,))
    plan = make_plan([probe], [PROBE], required=[PROBE])
    assert plan.blocked_reason is not None
    assert "media.raw.v1" in plan.blocked_reason
    assert any(warning.code == "missing_provider" for warning in plan.warnings)


def test_cycle_raises_pipeline_plan_error(make_manifest, make_plan) -> None:
    left = make_manifest("left", requires=(FEATURES,), provides=(PROBE,))
    right = make_manifest("right", requires=(PROBE,), provides=(FEATURES,))
    with pytest.raises(PipelinePlanError):
        make_plan([left, right], [PROBE], required=[PROBE])


def test_budget_blocks_required_capability(make_manifest, make_plan) -> None:
    probe = make_manifest("probe", provides=(PROBE,), cost=Cost(cpu_seconds=10))
    plan = make_plan(
        [probe],
        [PROBE],
        required=[PROBE],
        profile=AnalysisProfile(name="test", budget=Cost(cpu_seconds=5)),
    )
    assert plan.blocked_reason is not None
    assert "budget" in plan.blocked_reason


def test_budget_drops_optional_capability(make_manifest, make_plan) -> None:
    probe = make_manifest("probe", provides=(PROBE,), cost=Cost(cpu_seconds=1))
    features = make_manifest(
        "features",
        requires=(PROBE,),
        provides=(FEATURES,),
        cost=Cost(cpu_seconds=10),
    )
    plan = make_plan(
        [probe, features],
        [PROBE, FEATURES],
        required=[PROBE],
        profile=AnalysisProfile(name="test", budget=Cost(cpu_seconds=5)),
    )
    assert plan.blocked_reason is None
    assert [node.node_id for node in plan.ordered_nodes] == ["probe"]
    assert any(warning.code == "budget_exceeded" for warning in plan.warnings)
    assert plan.estimated_cost.cpu_seconds == 1


def test_optional_capability_with_missing_dependency_is_dropped(
    make_manifest,
    make_plan,
) -> None:
    probe = make_manifest("probe", requires=(MEDIA,), provides=(PROBE,))
    plan = make_plan([probe], [PROBE])
    assert plan.blocked_reason is None
    assert plan.nodes == ()
    assert any(warning.code == "missing_provider" for warning in plan.warnings)


def test_inputs_satisfy_requirements(make_manifest, make_plan) -> None:
    probe = make_manifest("probe", requires=(MEDIA,), provides=(PROBE,))
    plan = make_plan([probe], [PROBE], required=[PROBE], inputs=[MEDIA])
    assert plan.blocked_reason is None
    assert [node.node_id for node in plan.nodes] == ["probe"]
    assert plan.inputs == (MEDIA,)


def test_optional_capability_uses_profile_and_params(make_manifest, make_plan) -> None:
    probe = make_manifest("probe", provides=(PROBE,))
    plan = make_plan(
        [probe],
        [PROBE],
        required=[PROBE],
        params={"probe": {"temperature": 0.2, "nested": {"a": 1}}},
    )
    assert plan.node("probe").params["temperature"] == 0.2
    assert plan.node("probe").params["nested"] == {"a": 1}


def test_plan_serializes_to_dict(make_manifest, make_plan) -> None:
    probe = make_manifest("probe", provides=(PROBE,))
    plan = make_plan([probe], [PROBE], required=[PROBE])
    payload = plan.to_dict()
    assert payload["blocked_reason"] is None
    assert payload["desired_outputs"] == ["audio.probe.v1"]
    assert payload["nodes"][0]["node_id"] == "probe"
    assert payload["edges"] == []


def test_plan_rejects_duplicate_node_ids() -> None:
    node = Node(
        node_id="same",
        capability_id="one",
        capability_version="1",
        provides=(PROBE,),
    )
    other = Node(
        node_id="same",
        capability_id="two",
        capability_version="1",
        provides=(FEATURES,),
    )
    with pytest.raises(DuplicateNodeError):
        Plan(nodes=(node, other), edges=())


def test_provider_chain_objects_are_accepted(make_manifest) -> None:
    first = make_manifest("first", provides=(PROBE,))
    second = make_manifest("second", provides=(PROBE,))
    planner = Planner(
        provider_chains=[ProviderChain(artifact_type=PROBE, providers=("second", "first"))]
    )
    plan = planner.plan(
        [PROBE], AnalysisProfile(name="test"), [first, second], required_outputs=[PROBE]
    )
    assert plan.provider_groups[PROBE.render()] == ("second", "first")

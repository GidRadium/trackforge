"""Artifact type, cost, manifest, and profile tests."""

from __future__ import annotations

import pytest

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.capabilities import (
    AnalysisProfile,
    ArtifactType,
    CapabilityKind,
    CapabilityManifest,
    Cost,
    Determinism,
    OnEmpty,
    ProviderChain,
    SelectionStrategy,
    SideEffect,
)


def test_artifact_type_render_and_parse() -> None:
    artifact_type = ArtifactType("audio", "probe", 2)

    assert str(artifact_type) == "audio.probe.v2"
    assert artifact_type.render() == "audio.probe.v2"
    assert ArtifactType.parse("audio.probe.v2") == artifact_type
    assert ArtifactType.from_string("audio.probe.v2") == artifact_type


def test_artifact_type_supports_nested_namespace() -> None:
    artifact_type = ArtifactType.parse("llm.openai.chat.v1")

    assert artifact_type.namespace == "llm.openai"
    assert artifact_type.name == "chat"
    assert artifact_type.schema_version == 1


@pytest.mark.parametrize(
    "value",
    [
        "",
        "audio",
        "audio.probe",
        "Audio.probe.v1",
        "audio.probe.v0",
        "audio..probe.v1",
        "audio.probe.v1.extra",
        "audio.probe.1",
        "1audio.probe.v1",
    ],
)
def test_artifact_type_rejects_invalid_text(value: str) -> None:
    with pytest.raises(DomainValidationError):
        ArtifactType.parse(value)


@pytest.mark.parametrize(
    ("namespace", "name", "version"),
    [
        ("", "probe", 1),
        ("audio", "", 1),
        ("Audio", "probe", 1),
        ("audio", "Probe", 1),
        ("audio", "probe", 0),
        ("audio", "probe", True),
    ],
)
def test_artifact_type_rejects_invalid_fields(
    namespace: str,
    name: str,
    version: object,
) -> None:
    with pytest.raises(DomainValidationError):
        ArtifactType(namespace, name, version)  # type: ignore[arg-type]


def test_cost_non_negative() -> None:
    assert Cost().cpu_seconds == 0.0
    with pytest.raises(DomainValidationError):
        Cost(cpu_seconds=-1)
    with pytest.raises(DomainValidationError):
        Cost(memory_mb=-1)
    with pytest.raises(DomainValidationError):
        Cost(network_calls=-1)
    with pytest.raises(DomainValidationError):
        Cost(money_usd=-0.01)


def test_cost_addition() -> None:
    total = Cost(cpu_seconds=1.5, network_calls=2) + Cost(
        cpu_seconds=0.5,
        network_calls=1,
        money_usd=0.1,
    )

    assert total == Cost(cpu_seconds=2.0, network_calls=3, money_usd=0.1)


def test_capability_manifest() -> None:
    manifest = CapabilityManifest(
        id="ffprobe",
        version="1.0",
        kind=CapabilityKind.OBSERVER,
        requires=(),
        provides=(ArtifactType("audio", "probe", 1),),
        cost=Cost(cpu_seconds=0.2),
        determinism=Determinism.DETERMINISTIC,
        cacheable=True,
        side_effects=SideEffect.DISK,
        rate_limit={"host": "musicbrainz.org", "requests_per_second": 1},
        license="GPL-3.0-or-later",
    )

    assert manifest.kind is CapabilityKind.OBSERVER
    assert manifest.provides[0] == ArtifactType("audio", "probe", 1)
    assert manifest.rate_limit["requests_per_second"] == 1


def test_capability_manifest_rejects_unknown_kind() -> None:
    with pytest.raises(DomainValidationError):
        CapabilityManifest(id="x", version="1", kind="unknown")


def test_provider_chain() -> None:
    chain = ProviderChain(
        artifact_type="audio.probe.v1",
        providers=["ffprobe"],
        on_empty=OnEmpty.DEGRADE,
    )

    assert chain.artifact_type == ArtifactType("audio", "probe", 1)
    assert chain.on_empty is OnEmpty.DEGRADE
    with pytest.raises(DomainValidationError):
        ProviderChain(artifact_type="audio.probe.v1", providers=[])


def test_analysis_profile() -> None:
    profile = AnalysisProfile(
        name="balanced",
        enabled=["probe", "features"],
        disabled=["stems"],
        budget=Cost(cpu_seconds=30),
        quality_target=0.8,
        fallback_policy="degrade",
        selection_strategy=SelectionStrategy.RANKER,
    )

    assert profile.enabled == ("probe", "features")
    assert profile.disabled == ("stems",)
    assert profile.budget.cpu_seconds == 30.0
    assert profile.selection_strategy is SelectionStrategy.RANKER


def test_analysis_profile_rejects_enabled_disabled_overlap() -> None:
    with pytest.raises(DomainValidationError):
        AnalysisProfile(
            name="custom",
            enabled=["probe"],
            disabled=["probe"],
        )

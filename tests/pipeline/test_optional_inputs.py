"""Contract tests for optional capability inputs."""

from __future__ import annotations

import pytest

from trackforge.domain.model.capabilities import (
    ArtifactType,
    SideEffect,
)
from trackforge.pipeline.cache import cache_key_for_step

pytestmark = pytest.mark.unit

MEDIA = ArtifactType.parse("media.raw.v1")
SOURCE_METADATA = ArtifactType.parse("source.metadata.v1")
QUERY = ArtifactType.parse("metadata.query.v1")
ITEM = "01a00000-0000-7000-8000-000000000001"


def test_optional_input_is_planned_when_its_provider_is_available(
    make_manifest,
    make_capability,
    make_plan,
) -> None:
    source = make_capability(
        make_manifest("source_metadata", requires=(MEDIA,), provides=(SOURCE_METADATA,))
    )
    consumer = make_capability(
        make_manifest(
            "metadata_query",
            requires=(MEDIA,),
            optional_requires=(SOURCE_METADATA,),
            provides=(QUERY,),
        )
    )
    plan = make_plan(
        [source, consumer],
        [QUERY],
        required=[QUERY],
        inputs=[MEDIA],
    )

    assert plan.blocked_reason is None
    assert [node.node_id for node in plan.ordered_nodes] == ["source_metadata", "metadata_query"]
    assert plan.node("metadata_query").optional_requires_keys == ("source.metadata.v1",)
    assert any(
        edge.source == "source_metadata" and edge.target == "metadata_query" for edge in plan.edges
    )


def test_missing_optional_provider_does_not_block_capability(
    make_manifest,
    make_capability,
    make_plan,
) -> None:
    consumer = make_capability(
        make_manifest(
            "metadata_query",
            requires=(MEDIA,),
            optional_requires=(SOURCE_METADATA,),
            provides=(QUERY,),
        )
    )
    plan = make_plan(
        [consumer],
        [QUERY],
        required=[QUERY],
        inputs=[MEDIA],
    )

    assert plan.blocked_reason is None
    assert [node.node_id for node in plan.ordered_nodes] == ["metadata_query"]
    assert plan.node("metadata_query").optional_requires_keys == ("source.metadata.v1",)


def test_network_optional_provider_is_not_fetched_unless_requested(
    make_manifest,
    make_capability,
    make_plan,
) -> None:
    source_manifest = make_manifest("network_source", provides=(SOURCE_METADATA,))
    source_manifest = source_manifest.__class__(
        id=source_manifest.id,
        version=source_manifest.version,
        kind=source_manifest.kind,
        requires=source_manifest.requires,
        provides=source_manifest.provides,
        side_effects=SideEffect.NETWORK,
    )
    source = make_capability(source_manifest)
    consumer = make_capability(
        make_manifest(
            "metadata_query",
            requires=(MEDIA,),
            optional_requires=(SOURCE_METADATA,),
            provides=(QUERY,),
        )
    )
    plan = make_plan([source, consumer], [QUERY], required=[QUERY], inputs=[MEDIA])

    assert plan.blocked_reason is None
    assert [node.node_id for node in plan.ordered_nodes] == ["metadata_query"]


@pytest.mark.asyncio
async def test_executor_passes_optional_artifact_and_keeps_local_fallback(
    make_manifest,
    make_capability,
    make_plan,
    make_executor,
    make_step_input,
    fake_runs,
) -> None:
    source = make_capability(
        make_manifest("source_metadata", requires=(MEDIA,), provides=(SOURCE_METADATA,))
    )
    consumer = make_capability(
        make_manifest(
            "metadata_query",
            requires=(MEDIA,),
            optional_requires=(SOURCE_METADATA,),
            provides=(QUERY,),
        )
    )
    plan = make_plan(
        [source, consumer],
        [QUERY],
        required=[QUERY],
        inputs=[MEDIA],
    )
    executor = make_executor([source, consumer], runs=fake_runs)
    result = await executor.run(
        plan,
        item_id=ITEM,
        initial_inputs={MEDIA: make_step_input(MEDIA)},
    )

    assert result.status.value == "completed"
    assert result.artifact(SOURCE_METADATA) is not None
    assert SOURCE_METADATA in consumer.calls[0].inputs

    local_consumer = make_capability(
        make_manifest(
            "local_consumer",
            requires=(MEDIA,),
            optional_requires=(SOURCE_METADATA,),
            provides=(QUERY,),
        )
    )
    local_plan = make_plan(
        [local_consumer],
        [QUERY],
        required=[QUERY],
        inputs=[MEDIA],
    )
    local_executor = make_executor([local_consumer], runs=fake_runs)
    local_result = await local_executor.run(
        local_plan,
        item_id=ITEM,
        initial_inputs={MEDIA: make_step_input(MEDIA)},
    )

    assert local_result.status.value == "completed"
    assert SOURCE_METADATA not in local_consumer.calls[0].inputs


def test_cache_key_includes_optional_input_only_when_present(
    make_manifest,
    make_step_input,
) -> None:
    manifest = make_manifest(
        "metadata_query",
        requires=(MEDIA,),
        optional_requires=(SOURCE_METADATA,),
        provides=(QUERY,),
    )
    media = make_step_input(MEDIA)
    source = make_step_input(SOURCE_METADATA, payload=b'{"title":"Song"}')
    without = cache_key_for_step(
        {MEDIA: media},
        manifest,
    )
    with_optional = cache_key_for_step(
        {MEDIA: media, SOURCE_METADATA: source},
        manifest,
    )

    assert without != with_optional

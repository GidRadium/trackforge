"""Unit tests for the generic capability DAG."""

from __future__ import annotations

import pytest

from trackforge.domain.model.capabilities import ArtifactType
from trackforge.pipeline.dag import Edge, EdgeKind, Graph, Node, artifact_key
from trackforge.pipeline.errors import CycleError, DuplicateNodeError, PipelinePlanError

pytestmark = pytest.mark.unit

MEDIA = ArtifactType.parse("media.raw.v1")
PROBE = ArtifactType.parse("audio.probe.v1")
FEATURES = ArtifactType.parse("audio.features.v1")


def node(
    node_id: str,
    *,
    requires: tuple[ArtifactType, ...] = (),
    provides: tuple[ArtifactType, ...] = (),
    optional: bool = False,
    fallback_group: str | None = None,
) -> Node:
    return Node(
        node_id=node_id,
        capability_id=node_id,
        capability_version="1",
        requires=requires,
        provides=provides,
        optional=optional,
        fallback_group=fallback_group,
    )


def test_artifact_key_supports_types_and_fields() -> None:
    assert artifact_key(MEDIA) == "media.raw.v1"
    assert artifact_key("title") == "title"


def test_graph_topological_sort_is_deterministic() -> None:
    graph = Graph(
        nodes=[node("c"), node("a"), node("b")],
        edges=[Edge("a", "c"), Edge("b", "c")],
    )
    assert [item.node_id for item in graph.topological_sort()] == ["a", "b", "c"]
    assert graph.topological_ids() == ("a", "b", "c")


def test_graph_keeps_insertion_order_but_sorts_ready_nodes() -> None:
    graph = Graph(nodes=[node("z"), node("a")])
    assert [item.node_id for item in graph.nodes] == ["z", "a"]
    assert [item.node_id for item in graph.topological_sort()] == ["a", "z"]


def test_graph_rejects_duplicate_node_ids() -> None:
    graph = Graph(nodes=[node("a")])
    with pytest.raises(DuplicateNodeError):
        graph.add_node(node("a"))


def test_graph_rejects_edges_with_unknown_endpoints() -> None:
    with pytest.raises(PipelinePlanError):
        Graph(nodes=[node("a")], edges=[Edge("a", "missing")])


def test_graph_rejects_self_cycles() -> None:
    graph = Graph(nodes=[node("a")], edges=[Edge("a", "a")])
    cycle = graph.detect_cycle()
    assert cycle is not None
    assert cycle[0] == "a"
    with pytest.raises(CycleError):
        graph.topological_sort()


def test_graph_detects_two_node_cycle() -> None:
    graph = Graph(
        nodes=[node("a"), node("b")],
        edges=[Edge("a", "b"), Edge("b", "a")],
    )
    with pytest.raises(CycleError):
        graph.validate()
    assert graph.detect_cycle() is not None


def test_graph_deduplicates_identical_edges() -> None:
    graph = Graph(nodes=[node("a"), node("b")])
    graph.add_edge(Edge("a", "b"))
    graph.add_edge(Edge("a", "b"))
    assert len(graph.edges) == 1


def test_graph_accepts_data_and_fallback_edges_between_same_nodes() -> None:
    graph = Graph(
        nodes=[node("a"), node("b")],
        edges=[
            Edge("a", "b", EdgeKind.DATA, PROBE),
            Edge("a", "b", EdgeKind.FALLBACK, PROBE),
        ],
    )
    assert {edge.kind for edge in graph.edges} == {EdgeKind.DATA, EdgeKind.FALLBACK}


def test_graph_predecessors_and_successors() -> None:
    graph = Graph(
        nodes=[node("a"), node("b"), node("c")],
        edges=[Edge("a", "b"), Edge("a", "c")],
    )
    assert graph.predecessors("b") == ("a",)
    assert set(graph.successors("a")) == {"b", "c"}


def test_node_normalizes_requirements_and_rejects_duplicates() -> None:
    item = node("a", requires=(MEDIA, "title"), provides=(PROBE,))
    assert item.requires_keys == ("media.raw.v1", "title")
    assert item.provides_keys == ("audio.probe.v1",)
    with pytest.raises(ValueError):
        node("b", requires=(MEDIA, MEDIA))


def test_node_rejects_empty_id() -> None:
    with pytest.raises(ValueError):
        node("")

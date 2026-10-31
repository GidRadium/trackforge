"""Generic capability DAG with deterministic topological ordering.

The graph is deliberately independent of capabilities' implementations: nodes
carry only manifest facts, and edges describe either data flow or fallback
order.  The planner builds a graph, validates it, and keeps it alongside the
serializable plan.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model._validation import (
    coerce_enum,
    ensure_bool,
    json_mapping,
    require_text,
)
from trackforge.domain.model.capabilities import ArtifactRef, ArtifactType, Cost
from trackforge.pipeline.errors import CycleError, DuplicateNodeError, PipelinePlanError


def artifact_key(value: ArtifactRef) -> str:
    """Return the stable string form of an artifact/field reference."""

    if isinstance(value, ArtifactType):
        return str(value)
    if isinstance(value, str):
        return require_text(value, "artifact reference")
    raise DomainValidationError(
        "artifact reference must be an ArtifactType or string",
        context={"actual_type": type(value).__name__},
    )


def normalize_artifact_ref(value: ArtifactRef) -> ArtifactRef:
    """Validate and normalize an artifact/field reference."""

    artifact_key(value)
    return value


def _artifact_refs(value: object, field_name: str) -> tuple[ArtifactRef, ...]:
    if isinstance(value, (str, bytes, bytearray)):
        raise DomainValidationError(f"{field_name} must be a sequence")
    try:
        items: tuple[object, ...] = tuple(value)  # type: ignore[arg-type]
    except TypeError:
        raise DomainValidationError(f"{field_name} must be a sequence") from None
    result: list[ArtifactRef] = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            result.append(require_text(item, f"{field_name}[{index}]"))
        elif isinstance(item, ArtifactType):
            result.append(item)
        else:
            raise DomainValidationError(
                f"{field_name}[{index}] must be an ArtifactType or string",
                context={"index": index},
            )
    if len({artifact_key(item) for item in result}) != len(result):
        raise DomainValidationError(f"{field_name} must not contain duplicates")
    return tuple(result)


class EdgeKind(StrEnum):
    """Why one node depends on another."""

    DATA = "data"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class Node:
    """One capability invocation in a plan.

    ``fallback_group`` is the artifact type whose provider chain this node
    belongs to (rendered as a string).  Nodes in the same group are tried in
    order until one produces the required artifact.
    """

    node_id: str
    capability_id: str
    capability_version: str
    requires: tuple[ArtifactRef, ...] = ()
    provides: tuple[ArtifactRef, ...] = ()
    fallback_group: str | None = None
    optional: bool = False
    cost: Cost = field(default_factory=Cost)
    params: Mapping[str, Any] = field(default_factory=dict)
    cacheable: bool = True
    kind: str | None = None
    optional_requires: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", require_text(self.node_id, "node_id"))
        object.__setattr__(
            self,
            "capability_id",
            require_text(self.capability_id, "capability_id"),
        )
        object.__setattr__(
            self,
            "capability_version",
            require_text(self.capability_version, "capability_version"),
        )
        object.__setattr__(self, "requires", _artifact_refs(self.requires, "requires"))
        object.__setattr__(
            self,
            "optional_requires",
            _artifact_refs(self.optional_requires, "optional_requires"),
        )
        required_keys = {artifact_key(item) for item in self.requires}
        optional_keys = {artifact_key(item) for item in self.optional_requires}
        if required_keys.intersection(optional_keys):
            raise DomainValidationError(
                "requires and optional_requires must not overlap",
                context={"overlap": sorted(required_keys.intersection(optional_keys))},
            )
        object.__setattr__(self, "provides", _artifact_refs(self.provides, "provides"))
        if self.fallback_group is not None:
            object.__setattr__(
                self,
                "fallback_group",
                require_text(self.fallback_group, "fallback_group"),
            )
        object.__setattr__(self, "optional", ensure_bool(self.optional, "optional"))
        if not isinstance(self.cost, Cost):
            raise DomainValidationError("cost must be a Cost")
        object.__setattr__(self, "params", json_mapping(self.params, "params"))
        object.__setattr__(self, "cacheable", ensure_bool(self.cacheable, "cacheable"))
        if self.kind is not None:
            object.__setattr__(self, "kind", require_text(self.kind, "kind"))

    @property
    def id(self) -> str:
        """Alias for ``node_id`` used by compact plan renderers."""

        return self.node_id

    @property
    def requires_keys(self) -> tuple[str, ...]:
        return tuple(artifact_key(item) for item in self.requires)

    @property
    def optional_requires_keys(self) -> tuple[str, ...]:
        return tuple(artifact_key(item) for item in self.optional_requires)

    @property
    def provides_keys(self) -> tuple[str, ...]:
        return tuple(artifact_key(item) for item in self.provides)


@dataclass(frozen=True, slots=True)
class Edge:
    """A directed dependency between two plan nodes."""

    source: str
    target: str
    kind: EdgeKind = EdgeKind.DATA
    artifact_type: ArtifactRef | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", require_text(self.source, "source"))
        object.__setattr__(self, "target", require_text(self.target, "target"))
        object.__setattr__(self, "kind", coerce_enum(self.kind, EdgeKind, "kind"))
        if self.artifact_type is not None:
            object.__setattr__(
                self,
                "artifact_type",
                normalize_artifact_ref(self.artifact_type),
            )

    @property
    def key(self) -> tuple[str, str, str, str | None]:
        artifact = None if self.artifact_type is None else artifact_key(self.artifact_type)
        return (self.source, self.target, self.kind.value, artifact)


class Graph:
    """A mutable capability graph with cycle detection and topo sort."""

    def __init__(
        self,
        nodes: Iterable[Node] = (),
        edges: Iterable[Edge] = (),
    ) -> None:
        self._nodes: dict[str, Node] = {}
        self._edges: list[Edge] = []
        self._edge_keys: set[tuple[str, str, str, str | None]] = set()
        for node in nodes:
            self.add_node(node)
        for edge in edges:
            self.add_edge(edge)

    # -- construction -------------------------------------------------

    def add_node(self, node: Node) -> Node:
        if not isinstance(node, Node):
            raise DomainValidationError("node must be a Node")
        if node.node_id in self._nodes:
            raise DuplicateNodeError(node.node_id)
        self._nodes[node.node_id] = node
        return node

    def add_edge(self, edge: Edge | str, target: str | None = None) -> Edge:
        if isinstance(edge, str):
            if target is None:
                raise DomainValidationError("target is required when source is a string")
            edge = Edge(edge, target)
        if not isinstance(edge, Edge):
            raise DomainValidationError("edge must be an Edge")
        for endpoint in (edge.source, edge.target):
            if endpoint not in self._nodes:
                raise PipelinePlanError(
                    "edge references an unknown node",
                    context={"edge": edge.key, "node_id": endpoint},
                )
        if edge.key not in self._edge_keys:
            self._edge_keys.add(edge.key)
            self._edges.append(edge)
        return edge

    @classmethod
    def from_nodes_edges(
        cls,
        nodes: Iterable[Node],
        edges: Iterable[Edge] = (),
    ) -> Graph:
        return cls(nodes=nodes, edges=edges)

    # -- inspection ---------------------------------------------------

    @property
    def nodes(self) -> tuple[Node, ...]:
        return tuple(self._nodes.values())

    @property
    def edges(self) -> tuple[Edge, ...]:
        return tuple(self._edges)

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(self._nodes)

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, node_id: object) -> bool:
        return node_id in self._nodes

    def __iter__(self) -> Iterator[Node]:
        return iter(self._nodes.values())

    def node(self, node_id: str) -> Node:
        try:
            return self._nodes[node_id]
        except KeyError:
            raise PipelinePlanError(
                "unknown plan node",
                context={"node_id": node_id},
            ) from None

    def outgoing(self, node_id: str) -> tuple[Edge, ...]:
        self.node(node_id)
        return tuple(edge for edge in self._edges if edge.source == node_id)

    def incoming(self, node_id: str) -> tuple[Edge, ...]:
        self.node(node_id)
        return tuple(edge for edge in self._edges if edge.target == node_id)

    def predecessors(self, node_id: str) -> tuple[str, ...]:
        return tuple(edge.source for edge in self.incoming(node_id))

    def successors(self, node_id: str) -> tuple[str, ...]:
        return tuple(edge.target for edge in self.outgoing(node_id))

    # -- validation ---------------------------------------------------

    def validate(self) -> None:
        """Validate endpoints and reject cycles."""

        for edge in self._edges:
            for endpoint in (edge.source, edge.target):
                if endpoint not in self._nodes:
                    raise PipelinePlanError(
                        "edge references an unknown node",
                        context={"edge": edge.key, "node_id": endpoint},
                    )
        cycle = self.detect_cycle()
        if cycle is not None:
            raise CycleError(cycle)

    def has_cycle(self) -> bool:
        return self.detect_cycle() is not None

    def detect_cycle(self) -> tuple[str, ...] | None:
        """Return one cycle as node ids, or ``None`` when the graph is acyclic."""

        state: dict[str, int] = {node_id: 0 for node_id in self._nodes}
        stack: list[str] = []
        positions: dict[str, int] = {}

        def visit(node_id: str) -> tuple[str, ...] | None:
            state[node_id] = 1
            positions[node_id] = len(stack)
            stack.append(node_id)
            for edge in self.outgoing(node_id):
                target = edge.target
                if state[target] == 0:
                    found = visit(target)
                    if found is not None:
                        return found
                elif state[target] == 1:
                    index = positions[target]
                    return tuple(stack[index:] + [target])
            stack.pop()
            positions.pop(node_id, None)
            state[node_id] = 2
            return None

        for node_id in sorted(self._nodes):
            if state[node_id] == 0:
                found = visit(node_id)
                if found is not None:
                    return found
        return None

    # -- ordering -----------------------------------------------------

    def topological_sort(self) -> tuple[Node, ...]:
        """Return a deterministic Kahn topological order.

        Ready nodes are ordered by node id so identical graphs always produce
        identical plans and event sequences.
        """

        indegree = {node_id: 0 for node_id in self._nodes}
        outgoing: dict[str, list[str]] = {node_id: [] for node_id in self._nodes}
        for edge in self._edges:
            indegree[edge.target] += 1
            outgoing[edge.source].append(edge.target)

        ready = [node_id for node_id, degree in indegree.items() if degree == 0]
        heapq.heapify(ready)
        ordered: list[Node] = []
        while ready:
            node_id = heapq.heappop(ready)
            ordered.append(self._nodes[node_id])
            for target in outgoing[node_id]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    heapq.heappush(ready, target)

        if len(ordered) != len(self._nodes):
            cycle = self.detect_cycle()
            raise CycleError(cycle or ())
        return tuple(ordered)

    def topological_ids(self) -> tuple[str, ...]:
        return tuple(node.node_id for node in self.topological_sort())

    def topological_order(self) -> tuple[Node, ...]:
        """Alias for :meth:`topological_sort`."""

        return self.topological_sort()

    # Convenience aliases used by callers that prefer the shorter spelling.
    topo_sort = topological_sort
    cycle = detect_cycle


__all__ = [
    "Edge",
    "EdgeKind",
    "Graph",
    "Node",
    "artifact_key",
    "normalize_artifact_ref",
]

"""Build a capability DAG from desired outputs, a profile, and manifests.

Planning is pure: it performs no I/O, mutates no capability, and returns a
serializable :class:`Plan`.  Execution is the executor's job.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from trackforge.application.ports.execution import Capability
from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model._validation import json_mapping, require_text
from trackforge.domain.model.capabilities import (
    AnalysisProfile,
    ArtifactRef,
    ArtifactType,
    CapabilityManifest,
    Cost,
    ProviderChain,
)
from trackforge.pipeline.budget import Budget
from trackforge.pipeline.dag import (
    Edge,
    EdgeKind,
    Graph,
    Node,
    artifact_key,
)
from trackforge.pipeline.degradation import (
    Warning,
    WarningCode,
    budget_warning,
    disabled_warning,
    make_warning,
)
from trackforge.pipeline.errors import (
    CycleError,
    DuplicateNodeError,
    PipelinePlanError,
)

PlanNode = Node
PlanEdge = Edge


def _normalize_outputs(values: object) -> tuple[ArtifactRef, ...]:
    if isinstance(values, (str, ArtifactType)):
        items: tuple[object, ...] = (values,)
    elif isinstance(values, Mapping):
        items = tuple(values)
    else:
        try:
            items = tuple(values)  # type: ignore[arg-type]
        except TypeError:
            raise DomainValidationError("outputs must be a sequence") from None
    result: list[ArtifactRef] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if isinstance(item, ArtifactType):
            ref: ArtifactRef = item
        elif isinstance(item, str):
            text = require_text(item, f"outputs[{index}]")
            try:
                ref = ArtifactType.parse(text)
            except DomainValidationError:
                ref = text
        else:
            raise DomainValidationError(f"outputs[{index}] must be an ArtifactType or string")
        key = artifact_key(ref)
        if key not in seen:
            seen.add(key)
            result.append(ref)
    return tuple(result)


def _manifest_from_available(
    item: Capability | CapabilityManifest,
) -> CapabilityManifest:
    if isinstance(item, CapabilityManifest):
        return item
    manifest = getattr(item, "manifest", None)
    if isinstance(manifest, CapabilityManifest):
        return manifest
    raise DomainValidationError(
        "available capabilities must expose a CapabilityManifest",
        context={"actual_type": type(item).__name__},
    )


def _coerce_available(
    available: object,
) -> tuple[CapabilityManifest, ...]:
    if isinstance(available, Mapping):
        source: Iterable[object] = available.values()
    else:
        try:
            source = tuple(available)  # type: ignore[arg-type]
        except TypeError:
            raise DomainValidationError("available must be an iterable or mapping") from None
    manifests: list[CapabilityManifest] = []
    for item in source:
        manifests.append(_manifest_from_available(item))  # type: ignore[arg-type]
    return tuple(manifests)


def _normalize_chains(
    chains: Mapping[ArtifactRef, Sequence[str]] | Iterable[ProviderChain] | None,
) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    if chains is None:
        return result
    items: Iterable[tuple[ArtifactRef, Sequence[str]]]
    if isinstance(chains, Mapping):
        items = chains.items()
    else:
        items = ((entry.artifact_type, entry.providers) for entry in chains)
    for raw_type, providers in items:
        key = artifact_key(raw_type)
        normalized = tuple(require_text(item, f"provider[{key}]") for item in providers)
        if key in result:
            raise DomainValidationError(
                "duplicate provider chain for one artifact type",
                context={"artifact_type": key},
            )
        result[key] = normalized
    return result


def _dedupe_warnings(warnings: Iterable[Warning]) -> tuple[Warning, ...]:
    result: list[Warning] = []
    seen: set[tuple[str, str, str | None]] = set()
    for warning in warnings:
        key = (warning.code, warning.message, warning.capability_id)
        if key not in seen:
            seen.add(key)
            result.append(warning)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class Plan:
    """An executable, serializable capability plan.

    A blocked plan is still a valid object: it records everything that could
    be selected plus ``blocked_reason`` so a caller can explain or persist the
    failure without executing anything.
    """

    nodes: tuple[PlanNode, ...]
    edges: tuple[Edge, ...]
    desired_outputs: tuple[ArtifactRef, ...] = ()
    required_outputs: tuple[ArtifactRef, ...] = ()
    estimated_cost: Cost = field(default_factory=Cost)
    warnings: tuple[Warning, ...] = ()
    blocked_reason: str | None = None
    provider_groups: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    inputs: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        nodes = tuple(self.nodes)
        for index, node in enumerate(nodes):
            if not isinstance(node, Node):
                raise DomainValidationError(f"nodes[{index}] must be a Node")
        object.__setattr__(self, "nodes", nodes)

        edges = tuple(self.edges)
        for index, edge in enumerate(edges):
            if not isinstance(edge, Edge):
                raise DomainValidationError(f"edges[{index}] must be an Edge")
        object.__setattr__(self, "edges", edges)

        object.__setattr__(self, "desired_outputs", _normalize_outputs(self.desired_outputs))
        object.__setattr__(
            self,
            "required_outputs",
            _normalize_outputs(self.required_outputs),
        )
        object.__setattr__(self, "inputs", _normalize_outputs(self.inputs))
        if not isinstance(self.estimated_cost, Cost):
            raise DomainValidationError("estimated_cost must be a Cost")

        warnings: list[Warning] = []
        for index, warning in enumerate(tuple(self.warnings)):
            if isinstance(warning, Warning):
                warnings.append(warning)
            elif isinstance(warning, str):  # type: ignore[unreachable]
                warnings.append(Warning(code=warning, message=warning))
            else:
                raise DomainValidationError(f"warnings[{index}] must be a warning")
        object.__setattr__(self, "warnings", _dedupe_warnings(warnings))

        if self.blocked_reason is not None:
            object.__setattr__(
                self,
                "blocked_reason",
                require_text(self.blocked_reason, "blocked_reason"),
            )

        groups: dict[str, tuple[str, ...]] = {}
        for raw_key, values in dict(self.provider_groups).items():
            key = artifact_key(raw_key)
            groups[key] = tuple(require_text(value, f"provider_groups[{key}]") for value in values)
        object.__setattr__(self, "provider_groups", groups)

        node_ids = [node.node_id for node in nodes]
        if len(node_ids) != len(set(node_ids)):
            duplicate = next(item for item in node_ids if node_ids.count(item) > 1)
            raise DuplicateNodeError(duplicate)

        graph = Graph(nodes=nodes, edges=edges)
        graph.validate()
        # Expose the executable order directly; callers should not have to
        # reproduce the topological sort.
        object.__setattr__(self, "nodes", graph.topological_sort())

    @property
    def blocked(self) -> bool:
        return self.blocked_reason is not None

    @property
    def is_blocked(self) -> bool:
        return self.blocked_reason is not None

    @property
    def provider_chains(self) -> Mapping[str, tuple[str, ...]]:
        """Alias for ``provider_groups``."""

        return self.provider_groups

    @property
    def graph(self) -> Graph:
        return Graph(nodes=self.nodes, edges=self.edges)

    @property
    def ordered_nodes(self) -> tuple[PlanNode, ...]:
        return self.graph.topological_sort()

    def node(self, node_id: str) -> PlanNode:
        return self.graph.node(node_id)

    def nodes_for(self, artifact_type: ArtifactRef | str) -> tuple[PlanNode, ...]:
        key = artifact_key(artifact_type)
        allowed = set(self.provider_groups.get(key, ()))
        if allowed:
            return tuple(node for node in self.nodes if node.node_id in allowed)
        return tuple(node for node in self.nodes if key in node.provides_keys)

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocked_reason": self.blocked_reason,
            "desired_outputs": [artifact_key(item) for item in self.desired_outputs],
            "required_outputs": [artifact_key(item) for item in self.required_outputs],
            "inputs": [artifact_key(item) for item in self.inputs],
            "estimated_cost": {
                "cpu_seconds": self.estimated_cost.cpu_seconds,
                "memory_mb": self.estimated_cost.memory_mb,
                "network_calls": self.estimated_cost.network_calls,
                "money_usd": self.estimated_cost.money_usd,
            },
            "warnings": [warning.to_dict() for warning in self.warnings],
            "provider_groups": {key: list(value) for key, value in self.provider_groups.items()},
            "nodes": [
                {
                    "node_id": node.node_id,
                    "capability_id": node.capability_id,
                    "capability_version": node.capability_version,
                    "requires": list(node.requires_keys),
                    "provides": list(node.provides_keys),
                    "fallback_group": node.fallback_group,
                    "optional": node.optional,
                }
                for node in self.nodes
            ],
            "edges": [
                {
                    "source": edge.source,
                    "target": edge.target,
                    "kind": edge.kind.value,
                    "artifact_type": (
                        None if edge.artifact_type is None else artifact_key(edge.artifact_type)
                    ),
                }
                for edge in self.edges
            ],
        }


class Planner:
    """Turn desired outputs and capability manifests into a DAG."""

    def __init__(
        self,
        *,
        provider_chains: Mapping[ArtifactRef, Sequence[str]]
        | Iterable[ProviderChain]
        | None = None,
        params: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self._chains = _normalize_chains(provider_chains)
        self._params = {
            require_text(key, "params key"): json_mapping(value, f"params[{key}]")
            for key, value in (params or {}).items()
        }

    def plan(
        self,
        desired_outputs: Iterable[ArtifactRef] | ArtifactRef,
        profile: AnalysisProfile,
        available: (
            Iterable[Capability | CapabilityManifest]
            | Mapping[str, Capability | CapabilityManifest]
        ),
        *,
        required_outputs: Iterable[ArtifactRef] | ArtifactRef = (),
        inputs: Iterable[ArtifactRef] | ArtifactRef = (),
        params: Mapping[str, Mapping[str, Any]] | None = None,
        provider_chains: Mapping[ArtifactRef, Sequence[str]]
        | Iterable[ProviderChain]
        | None = None,
    ) -> Plan:
        if not isinstance(profile, AnalysisProfile):
            raise DomainValidationError("profile must be an AnalysisProfile")

        desired = _normalize_outputs(desired_outputs)
        required = _normalize_outputs(required_outputs)
        input_refs = _normalize_outputs(inputs)
        input_keys = {artifact_key(item) for item in input_refs}
        warnings: list[Warning] = []

        chain_overrides = (
            _normalize_chains(provider_chains) if provider_chains is not None else self._chains
        )
        node_params = dict(self._params)
        if params is not None:
            node_params.update(
                {
                    require_text(key, "params key"): json_mapping(value, f"params[{key}]")
                    for key, value in params.items()
                }
            )

        manifests = _coerce_available(available)
        unique: dict[tuple[str, str], CapabilityManifest] = {}
        by_id: dict[str, CapabilityManifest] = {}
        for manifest in manifests:
            identity = (manifest.id, manifest.version)
            if identity in unique:
                warnings.append(
                    make_warning(
                        WarningCode.SKIPPED,
                        f"duplicate capability {manifest.id}@{manifest.version} ignored",
                        capability_id=manifest.id,
                    )
                )
                continue
            unique[identity] = manifest
            by_id.setdefault(manifest.id, manifest)

        enabled: list[CapabilityManifest] = []
        disabled: list[CapabilityManifest] = []
        enabled_ids = set(profile.enabled)
        disabled_ids = set(profile.disabled)
        for manifest in unique.values():
            if manifest.id in disabled_ids or (enabled_ids and manifest.id not in enabled_ids):
                disabled.append(manifest)
            else:
                enabled.append(manifest)

        all_types: list[str] = []
        for ref in (*desired, *required):
            artifact_type_key = artifact_key(ref)
            if artifact_type_key not in all_types:
                all_types.append(artifact_type_key)

        for manifest in disabled:
            if any(artifact_key(provided) in all_types for provided in manifest.provides):
                warnings.append(
                    disabled_warning(
                        capability_id=manifest.id,
                        output=next(
                            provided
                            for provided in manifest.provides
                            if artifact_key(provided) in all_types
                        ),
                    )
                )

        caps: dict[str, CapabilityManifest] = {manifest.id: manifest for manifest in enabled}

        # Provider chains: explicit order wins, otherwise manifest order.
        chains: dict[str, tuple[str, ...]] = {}

        def chain_for(artifact_type: str) -> tuple[str, ...]:
            if artifact_type in chains:
                return chains[artifact_type]
            providers: list[str] = []
            explicit = chain_overrides.get(artifact_type)
            if explicit is not None:
                for capability_id in explicit:
                    chain_manifest = by_id.get(capability_id)
                    if chain_manifest is None:
                        continue
                    if chain_manifest not in enabled:
                        continue
                    if artifact_type not in {
                        artifact_key(item) for item in chain_manifest.provides
                    }:
                        warnings.append(
                            make_warning(
                                WarningCode.MISSING_PROVIDER,
                                f"{capability_id} does not provide {artifact_type}",
                                capability_id=capability_id,
                            )
                        )
                        continue
                    providers.append(capability_id)
            else:
                providers = [
                    manifest.id
                    for manifest in enabled
                    if artifact_type in {artifact_key(item) for item in manifest.provides}
                ]
            chains[artifact_type] = tuple(providers)
            return chains[artifact_type]

        required_keys = {artifact_key(item) for item in required}

        # Start with all provider chains for requested outputs.
        selected_ids: list[str] = []
        for artifact_type in all_types:
            if not chain_for(artifact_type) and artifact_type in required_keys:
                warnings.append(
                    make_warning(
                        WarningCode.MISSING_PROVIDER,
                        f"no enabled provider for required output {artifact_type}",
                    )
                )
            for capability_id in chain_for(artifact_type):
                if capability_id not in selected_ids:
                    selected_ids.append(capability_id)

        # Primary providers for required outputs, then their transitive
        # dependencies, are required; fallbacks and optional branches are not.
        required_ids: list[str] = []
        blocked_reason: str | None = None
        pending_required: list[tuple[str | None, str]] = [
            (key, key) for key in sorted(required_keys)
        ]
        while pending_required:
            output_key, required_type = pending_required.pop(0)
            if required_type not in all_types:
                all_types.append(required_type)
            provider_ids = chain_for(required_type)
            if not provider_ids:
                if required_type not in input_keys:
                    blocked_reason = f"no provider for required output '{required_type}'"
                    warnings.append(
                        make_warning(
                            WarningCode.MISSING_PROVIDER,
                            blocked_reason,
                        )
                    )
                continue
            primary = provider_ids[0]
            if primary in required_ids:
                continue
            required_ids.append(primary)
            manifest = caps[primary]
            for requirement in manifest.requires:
                req_key = artifact_key(requirement)
                if req_key in input_keys:
                    continue
                if req_key not in all_types:
                    all_types.append(req_key)
                if not chain_for(req_key):
                    blocked_reason = (
                        f"required capability '{manifest.id}' needs missing input '{req_key}'"
                    )
                    warnings.append(
                        make_warning(
                            WarningCode.MISSING_PROVIDER,
                            blocked_reason,
                            capability_id=manifest.id,
                        )
                    )
                    continue
                pending_required.append((req_key, req_key))

        # Expand selected capabilities through their requirements.
        index = 0
        while index < len(selected_ids):
            capability_id = selected_ids[index]
            index += 1
            manifest = caps[capability_id]
            for requirement in manifest.requires:
                req_key = artifact_key(requirement)
                if req_key in input_keys:
                    continue
                if req_key not in all_types:
                    all_types.append(req_key)
                for provider in chain_for(req_key):
                    if provider not in selected_ids:
                        selected_ids.append(provider)

        # Drop optional capabilities whose dependencies cannot be satisfied.
        changed = True
        while changed:
            changed = False
            for capability_id in list(selected_ids):
                if capability_id in required_ids:
                    continue
                manifest = caps[capability_id]
                missing: str | None = None
                for requirement in manifest.requires:
                    req_key = artifact_key(requirement)
                    if req_key in input_keys:
                        continue
                    if not any(
                        provider in selected_ids and provider != capability_id
                        for provider in chain_for(req_key)
                    ):
                        missing = req_key
                        break
                if missing is not None:
                    selected_ids.remove(capability_id)
                    changed = True
                    warnings.append(
                        make_warning(
                            WarningCode.MISSING_PROVIDER,
                            f"optional capability {capability_id} dropped: "
                            f"no provider for '{missing}'",
                            capability_id=capability_id,
                        )
                    )

        # A required capability that lost its provider is an unrecoverable plan.
        for capability_id in required_ids:
            manifest = caps[capability_id]
            for requirement in manifest.requires:
                req_key = artifact_key(requirement)
                if req_key in input_keys:
                    continue
                if not any(provider in selected_ids for provider in chain_for(req_key)):
                    blocked_reason = (
                        f"required capability '{capability_id}' needs missing input '{req_key}'"
                    )

        # Rebuild provider groups from the surviving capabilities.
        survivor_ids = set(selected_ids)
        groups: dict[str, tuple[str, ...]] = {}
        for artifact_type, chain_providers in chains.items():
            surviving_providers = tuple(item for item in chain_providers if item in survivor_ids)
            if surviving_providers:
                groups[artifact_type] = surviving_providers

        # Budget: required nodes must fit; optional nodes are dropped greedily.
        budget = Budget(profile.budget if isinstance(profile.budget, Cost) else Cost())
        for capability_id in required_ids:
            cost = _manifest_cost(caps[capability_id])
            if not budget.can_afford(cost):
                blocked_reason = f"required capability '{capability_id}' exceeds the profile budget"

        included_optional: list[str] = []
        simulated = Budget(profile.budget if isinstance(profile.budget, Cost) else Cost())
        for capability_id in required_ids:
            simulated.consume(_manifest_cost(caps[capability_id]))

        for capability_id in selected_ids:
            if capability_id in required_ids:
                continue
            manifest = caps[capability_id]
            if not simulated.can_afford(_manifest_cost(manifest)):
                warnings.append(
                    budget_warning(
                        capability_id=capability_id,
                        output=manifest.provides[0] if manifest.provides else capability_id,
                    )
                )
                continue
            simulated.consume(_manifest_cost(manifest))
            included_optional.append(capability_id)

        final_ids = [
            capability_id
            for capability_id in selected_ids
            if capability_id in required_ids or capability_id in included_optional
        ]
        # Removing an optional provider can leave an optional consumer without
        # a requirement.  Drop such consumers as visible skips.
        changed = True
        while changed:
            changed = False
            for capability_id in list(final_ids):
                if capability_id in required_ids:
                    continue
                manifest = caps[capability_id]
                for requirement in manifest.requires:
                    req_key = artifact_key(requirement)
                    if req_key in input_keys:
                        continue
                    if not any(
                        provider in final_ids and provider != capability_id
                        for provider in groups.get(req_key, ())
                    ):
                        final_ids.remove(capability_id)
                        changed = True
                        warnings.append(
                            make_warning(
                                WarningCode.BUDGET_EXCEEDED
                                if capability_id in included_optional
                                else WarningCode.SKIPPED,
                                f"optional capability {capability_id} skipped: missing '{req_key}'",
                                capability_id=capability_id,
                            )
                        )
                        break

        # Guards: every required capability must still be present.
        for capability_id in required_ids:
            if capability_id not in final_ids:
                blocked_reason = f"required capability '{capability_id}' was dropped"

        # Drop budget-skipped providers from the public chains as well, then
        # rebuild edges only between surviving nodes.
        groups = {
            artifact_type: tuple(provider for provider in chain_providers if provider in final_ids)
            for artifact_type, chain_providers in groups.items()
        }
        groups = {key: value for key, value in groups.items() if value}

        node_ids = _assign_node_ids(final_ids)
        nodes: list[Node] = []
        for capability_id in final_ids:
            manifest = caps[capability_id]
            group = next(
                (
                    artifact_type
                    for artifact_type in all_types
                    if capability_id in groups.get(artifact_type, ())
                ),
                None,
            )
            nodes.append(
                Node(
                    node_id=node_ids[capability_id],
                    capability_id=manifest.id,
                    capability_version=manifest.version,
                    requires=manifest.requires,
                    provides=manifest.provides,
                    fallback_group=group,
                    optional=capability_id not in required_ids,
                    cost=_manifest_cost(manifest),
                    params=node_params.get(capability_id, {}),
                    cacheable=manifest.cacheable,
                    kind=manifest.kind.value,
                )
            )

        edges: list[Edge] = []
        seen_edges: set[tuple[str, str, str, str | None]] = set()
        for artifact_type, group_providers in groups.items():
            for source, target in zip(group_providers, group_providers[1:], strict=False):
                edge = Edge(
                    source=node_ids[source],
                    target=node_ids[target],
                    kind=EdgeKind.FALLBACK,
                    artifact_type=artifact_type,
                )
                if edge.key not in seen_edges:
                    seen_edges.add(edge.key)
                    edges.append(edge)

        final_id_set = set(final_ids)
        for capability_id in final_ids:
            manifest = caps[capability_id]
            for requirement in manifest.requires:
                req_key = artifact_key(requirement)
                if req_key in input_keys:
                    continue
                for provider in groups.get(req_key, ()):
                    if provider not in final_id_set:
                        continue
                    edge = Edge(
                        source=node_ids[provider],
                        target=node_ids[capability_id],
                        kind=EdgeKind.DATA,
                        artifact_type=requirement,
                    )
                    if edge.key not in seen_edges:
                        seen_edges.add(edge.key)
                        edges.append(edge)

        try:
            Graph(nodes=nodes, edges=edges).validate()
        except CycleError as exc:
            raise PipelinePlanError(
                "planned capability graph contains a cycle",
                context={"cycle": list(exc.cycle)},
            ) from exc

        estimated = Cost()
        for capability_id in final_ids:
            estimated = estimated + _manifest_cost(caps[capability_id])

        # Missing optional providers are warnings, not failures.
        for artifact_type in all_types:
            if artifact_type not in groups and artifact_type not in input_keys:
                if artifact_type in required_keys:
                    blocked_reason = f"no provider for required output '{artifact_type}'"
                else:
                    warnings.append(
                        make_warning(
                            WarningCode.MISSING_PROVIDER,
                            f"no enabled provider for desired output {artifact_type}; "
                            "it will be skipped",
                        )
                    )

        return Plan(
            nodes=tuple(nodes),
            edges=tuple(edges),
            desired_outputs=desired,
            required_outputs=required,
            estimated_cost=estimated,
            warnings=_dedupe_warnings(warnings),
            blocked_reason=blocked_reason,
            provider_groups=groups,
            inputs=input_refs,
        )


def _manifest_cost(manifest: CapabilityManifest) -> Cost:
    return manifest.cost if isinstance(manifest.cost, Cost) else Cost()


def _assign_node_ids(capability_ids: Sequence[str]) -> dict[str, str]:
    """Give every selected capability a unique, stable node id."""

    result: dict[str, str] = {}
    used: set[str] = set()
    for capability_id in capability_ids:
        candidate = capability_id
        if candidate in used:
            suffix = 2
            while f"{capability_id}#{suffix}" in used:
                suffix += 1
            candidate = f"{capability_id}#{suffix}"
        used.add(candidate)
        result[capability_id] = candidate
    return result


__all__ = [
    "Plan",
    "PlanEdge",
    "PlanNode",
    "Planner",
]

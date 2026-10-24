"""Step-result cache built on the artifact store.

The cache key covers input artifact hashes, capability id/version, normalized
parameters, the capability's relevant config subset, and optional model/prompt
identity.  A cache entry stores one or more artifacts; the first artifact lives
under the step cache key and additional artifacts get deterministic derived
keys.  The primary manifest carries a small ``_cache`` index so a hit can
reconstruct the complete :class:`StepResult` without re-running the capability.

Errors and skipped results are never stored: there is no negative cache.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from trackforge.application.dto.artifacts import (
    ArtifactInput,
    ArtifactManifest,
    ArtifactProducer,
)
from trackforge.application.ports.execution import (
    ArtifactDraft,
    StepInput,
    StepResult,
    StepResultStatus,
    StepWarning,
)
from trackforge.application.ports.storage import (
    ArtifactConflict,
    ArtifactCorrupted,
    ArtifactNotFound,
    ArtifactRef,
    ArtifactStore,
    StorageError,
    require_cache_key,
)
from trackforge.application.services.canonical_json import (
    cache_key_for as _canonical_cache_key,
)
from trackforge.application.services.canonical_json import canonical_json, sha256_hex
from trackforge.domain.model._validation import utc_now
from trackforge.domain.model.capabilities import (
    ArtifactRef as ArtifactRefType,
)
from trackforge.domain.model.capabilities import (
    ArtifactType,
    CapabilityManifest,
    Cost,
)
from trackforge.pipeline.dag import artifact_key
from trackforge.pipeline.errors import PipelineError

_CACHE_META_KEY = "_cache"
_CACHE_META_VERSION = 1


class CacheMiss(PipelineError):
    """Raised when a cached entry cannot be loaded or is incompatible."""

    default_user_action = "Recompute the step or refresh the artifact cache."


class CacheSchemaMismatch(CacheMiss):
    """Raised when cached artifact schemas are incompatible with the request."""


@dataclass(frozen=True, slots=True)
class CacheInput:
    """One input identity included in a step cache key."""

    type: str
    hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.type, str):
            raise TypeError("type must be a string")
        if not isinstance(self.hash, str) or not self.hash:
            raise ValueError("hash must be a non-empty string")


@dataclass(frozen=True, slots=True)
class LoadedArtifact:
    """An artifact reconstructed from a cache entry."""

    type: ArtifactType
    payload: Any
    payload_bytes: bytes
    payload_hash: str
    cache_key: str
    schema_version: int
    manifest: ArtifactManifest
    ref: ArtifactRef

    def to_step_input(self) -> StepInput:
        return StepInput(
            type=self.type,
            artifact_id=self.manifest.artifact_id,
            schema_version=self.schema_version,
            cache_key=self.cache_key,
            payload_hash=self.payload_hash,
            payload=self.payload_bytes,
            manifest=self.manifest,
            ref=self.ref,
        )


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One reconstructed step result and its artifacts."""

    ref: ArtifactRef
    manifest: ArtifactManifest
    result: StepResult
    artifacts: tuple[LoadedArtifact, ...] = ()
    refs: tuple[ArtifactRef, ...] = ()

    @property
    def cache_key(self) -> str:
        return self.ref.cache_key


def cache_key_for(
    inputs: object,
    capability: CapabilityManifest | Mapping[str, object] | str,
    params: Mapping[str, object] | None = None,
    relevant_config: Mapping[str, object] | None = None,
    model: Mapping[str, object] | None = None,
    prompt: Mapping[str, object] | None = None,
) -> str:
    """Build a stable cache key for one capability invocation.

    ``inputs`` accepts the flexible shapes used by adapters and tests:
    :class:`CacheInput`, :class:`StepInput`, ``(type, hash)`` pairs, mappings
    with ``type``/``hash`` keys, plain hashes, or a mapping from artifact type
    to hash.  Inputs are sorted by type and hash, so iteration order never
    changes the key.
    """

    entries = _normalize_inputs(inputs)
    capability_value: Mapping[str, object]
    if isinstance(capability, CapabilityManifest):
        capability_value = {"id": capability.id, "version": capability.version}
    elif isinstance(capability, str):
        capability_value = {"id": capability}
    else:
        capability_value = dict(capability)
    return _canonical_cache_key(
        inputs=[{"type": entry.type, "hash": entry.hash} for entry in entries],
        capability=capability_value,
        params=params,
        config=relevant_config,
        model=model,
        prompt=prompt,
    )


def cache_key_for_step(
    inputs: Mapping[ArtifactType, StepInput] | Iterable[StepInput],
    capability: CapabilityManifest,
    params: Mapping[str, object] | None = None,
    config: Mapping[str, object] | None = None,
    model: Mapping[str, object] | None = None,
    prompt: Mapping[str, object] | None = None,
) -> str:
    """Cache key helper that filters config through the manifest declaration."""

    relevant = relevant_config(capability, config or {})
    return cache_key_for(
        inputs=inputs,
        capability=capability,
        params=params,
        relevant_config=relevant,
        model=model,
        prompt=prompt,
    )


def _normalize_inputs(inputs: object) -> tuple[CacheInput, ...]:
    entries: list[CacheInput] = []
    if isinstance(inputs, Mapping):
        for raw_type, raw_value in cast(Mapping[object, object], inputs).items():
            type_text = (
                artifact_key(raw_type)
                if isinstance(raw_type, (ArtifactType, str))
                else str(raw_type)
            )
            if isinstance(raw_value, StepInput):
                entries.append(CacheInput(type=type_text, hash=raw_value.payload_hash))
            elif isinstance(raw_value, CacheInput):
                entries.append(raw_value)
            elif isinstance(raw_value, str):
                entries.append(CacheInput(type=type_text, hash=raw_value))
            else:
                hash_value = getattr(raw_value, "payload_hash", None)
                if not isinstance(hash_value, str):
                    raise ValueError(
                        "mapping input values must be hashes, StepInput, or CacheInput"
                    )
                entries.append(CacheInput(type=type_text, hash=hash_value))
    elif isinstance(inputs, (str, bytes, bytearray)):
        entries.append(CacheInput(type="", hash=str(inputs)))
    else:
        try:
            items: tuple[object, ...] = tuple(inputs)  # type: ignore[arg-type]
        except TypeError:
            raise ValueError("inputs must be a mapping or sequence") from None
        for index, item in enumerate(items):
            if isinstance(item, CacheInput):
                entries.append(item)
            elif isinstance(item, StepInput):
                entries.append(CacheInput(type=str(item.type), hash=item.payload_hash))
            elif isinstance(item, str):
                entries.append(CacheInput(type="", hash=item))
            elif isinstance(item, Mapping):
                raw = cast(Mapping[str, object], item)
                type_value = raw.get("type", raw.get("artifact_type", ""))
                hash_value = raw.get("hash", raw.get("payload_hash", ""))
                if not isinstance(hash_value, str) or not hash_value:
                    raise ValueError(f"inputs[{index}] must contain a hash")
                entries.append(CacheInput(type=str(type_value), hash=hash_value))
            elif isinstance(item, tuple) and len(item) == 2:
                type_value, hash_value = item
                if not isinstance(hash_value, str) or not hash_value:
                    raise ValueError(f"inputs[{index}] must contain a hash")
                entries.append(CacheInput(type=str(type_value), hash=hash_value))
            else:
                hash_value = getattr(item, "payload_hash", None)
                type_value = getattr(item, "type", "")
                if not isinstance(hash_value, str) or not hash_value:
                    raise ValueError(f"unsupported input at index {index}")
                entries.append(CacheInput(type=str(type_value), hash=hash_value))
    return tuple(sorted(entries, key=lambda entry: (entry.type, entry.hash)))


def _declared_config_keys(config_schema: Mapping[str, Any]) -> frozenset[str]:
    if not config_schema:
        return frozenset()
    properties = config_schema.get("properties")
    if isinstance(properties, Mapping):
        return frozenset(str(key) for key in properties)
    schema_keywords = {
        "$schema",
        "additionalProperties",
        "default",
        "description",
        "enum",
        "properties",
        "required",
        "title",
        "type",
    }
    keys = {
        str(key) for key in config_schema if isinstance(key, str) and key not in schema_keywords
    }
    return frozenset(keys)


def relevant_config(
    capability: CapabilityManifest,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Return only the config keys declared by the capability manifest.

    An empty schema means the capability declares no config influence, so the
    result is empty and unrelated config changes cannot invalidate its cache.
    """

    declared = _declared_config_keys(capability.config_schema)
    return {key: config[key] for key in declared if key in config}


def _iso_z(value: datetime) -> str:
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_iso_z(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _derive_key(primary: str, index: int, artifact_type: ArtifactRefType) -> str:
    if index == 0:
        return primary
    return sha256_hex(
        canonical_json(
            {
                "cache_key": primary,
                "index": index,
                "type": artifact_key(artifact_type),
            }
        )
    )


def _coerce_producer(
    producer: (
        ArtifactProducer | CapabilityManifest | Mapping[str, object] | tuple[str, str] | None
    ),
) -> ArtifactProducer:
    if producer is None:
        return ArtifactProducer(id="pipeline", version="1")
    if isinstance(producer, ArtifactProducer):
        return producer
    if isinstance(producer, CapabilityManifest):
        return ArtifactProducer(id=producer.id, version=producer.version)
    if isinstance(producer, tuple) and len(producer) == 2:
        return ArtifactProducer(id=str(producer[0]), version=str(producer[1]))
    if isinstance(producer, Mapping):
        return ArtifactProducer.model_validate(dict(producer))
    raise TypeError("producer must be ArtifactProducer, mapping, tuple, or None")


def _coerce_inputs(inputs: object) -> list[ArtifactInput]:
    if inputs is None:
        return []
    result: list[ArtifactInput] = []
    if isinstance(inputs, Mapping):
        for raw_type, raw_hash in cast(Mapping[object, object], inputs).items():
            if isinstance(raw_hash, StepInput):
                raw_hash = raw_hash.payload_hash
            if not isinstance(raw_hash, str):
                raise ValueError("input hashes must be strings")
            result.append(ArtifactInput(type=str(raw_type), hash=raw_hash))
        return result
    for item in cast(Iterable[object], inputs):
        if isinstance(item, ArtifactInput):
            result.append(item)
        elif isinstance(item, StepInput):
            result.append(ArtifactInput(type=str(item.type), hash=item.payload_hash))
        elif isinstance(item, CacheInput):
            result.append(ArtifactInput(type=item.type, hash=item.hash))
        elif isinstance(item, str):
            result.append(ArtifactInput(type="", hash=item))
        elif isinstance(item, Mapping):
            raw = cast(Mapping[str, object], item)
            result.append(
                ArtifactInput(
                    type=str(raw.get("type", raw.get("artifact_type", ""))),
                    hash=str(raw.get("hash", raw.get("payload_hash", ""))),
                )
            )
        elif isinstance(item, tuple) and len(item) == 2:
            result.append(ArtifactInput(type=str(item[0]), hash=str(item[1])))
        else:
            raise ValueError("unsupported artifact input")
    return result


def _warnings_from_dicts(raw: Iterable[object]) -> tuple[StepWarning, ...]:
    warnings: list[StepWarning] = []
    for item in raw:
        if isinstance(item, StepWarning):
            warnings.append(item)
        elif isinstance(item, Mapping):
            value = cast(Mapping[str, object], item)
            code = str(value.get("code", "unknown"))
            message = str(value.get("message", code))
            warnings.append(
                StepWarning(
                    code=code,
                    message=message,
                    capability_id=(
                        None
                        if value.get("capability_id") is None
                        else str(value.get("capability_id"))
                    ),
                    provider=(
                        None if value.get("provider") is None else str(value.get("provider"))
                    ),
                    retryable=bool(value.get("retryable", False)),
                    user_action=(
                        None if value.get("user_action") is None else str(value.get("user_action"))
                    ),
                )
            )
        elif isinstance(item, str):
            warnings.append(StepWarning(code=item, message=item))
        else:
            raise ValueError("unsupported warning entry")
    return tuple(warnings)


class Cache:
    """A thin policy layer over :class:`ArtifactStore`."""

    def __init__(
        self,
        store: ArtifactStore,
        *,
        clock: Callable[[], datetime] = utc_now,
        refresh: bool = False,
        force: bool = False,
        force_all: bool | None = None,
        force_keys: Iterable[str] = (),
        force_steps: Iterable[str] = (),
    ) -> None:
        self._store = store
        self._clock = clock
        self._refresh = refresh
        self._force = force if force_all is None else force_all
        self._force_keys = set(force_keys) | set(force_steps)

    @property
    def artifact_store(self) -> ArtifactStore:
        return self._store

    @property
    def refresh(self) -> bool:
        return self._refresh

    async def lookup(
        self,
        key: str,
        schema_version: int | Mapping[str, int] | ArtifactType | None = None,
        *,
        expected_type: ArtifactType | str | None = None,
        refresh: bool | None = None,
        force: bool | None = None,
    ) -> ArtifactRef | None:
        """Return a compatible cached entry, or ``None`` for a miss.

        Schema mismatch, TTL expiry, and corrupted entries are misses.  A
        forced key ignores the lookup without deleting the old artifact.
        """

        ref, _ = await self.lookup_with_status(
            key,
            schema_version,
            expected_type=expected_type,
            refresh=refresh,
            force=force,
        )
        return ref

    async def lookup_with_status(
        self,
        key: str,
        schema_version: int | Mapping[str, int] | ArtifactType | None = None,
        *,
        expected_type: ArtifactType | str | None = None,
        refresh: bool | None = None,
        force: bool | None = None,
        allow_stale: bool = False,
    ) -> tuple[ArtifactRef | None, bool]:
        """Return ``(ref, stale)``; stale entries are optional.

        ``allow_stale`` is used by network-backed steps: an expired external
        response can still be reused with a visible ``stale`` warning.  A
        refresh/force lookup never returns a stale entry.
        """

        cache_key = require_cache_key(key)
        force_lookup = (self._force if force is None else force) or cache_key in self._force_keys
        if force_lookup:
            return None, False
        try:
            ref = await self._store.find(cache_key)
            if ref is None:
                return None, False
            manifest, _ = await self._store.load(ref)
        except (ArtifactCorrupted, ArtifactNotFound, StorageError, ValueError, KeyError):
            return None, False

        if expected_type is not None:
            expected = (
                expected_type
                if isinstance(expected_type, ArtifactType)
                else ArtifactType.parse(expected_type)
            )
            if manifest.type != str(expected):
                return None, False

        if not self._schema_matches(manifest, schema_version):
            return None, False

        if not (self._refresh if refresh is None else refresh):
            expires_at = manifest.expires_at
            now = self._clock()
            if now.tzinfo is None:
                now = now.replace(tzinfo=UTC)
            if expires_at is not None and _parse_iso_z(expires_at) <= now:
                if allow_stale:
                    return ref, True
                return None, False
        return ref, False

    async def load(
        self,
        ref: ArtifactRef,
        schema_version: int | Mapping[str, int] | ArtifactType | None = None,
        *,
        expected_type: ArtifactType | str | None = None,
    ) -> CacheEntry:
        """Load a cache entry and reconstruct the complete step result."""

        manifest, payload = await self._store.load(ref)
        if expected_type is not None:
            expected = (
                expected_type
                if isinstance(expected_type, ArtifactType)
                else ArtifactType.parse(expected_type)
            )
            if manifest.type != str(expected):
                raise CacheSchemaMismatch(
                    "cached artifact type does not match the requested type",
                    context={"expected": str(expected), "actual": manifest.type},
                )
        if not self._schema_matches(manifest, schema_version):
            raise CacheSchemaMismatch(
                "cached artifact schema is not compatible",
                context={"cache_key": ref.cache_key, "manifest_schema": manifest.schema_version},
            )

        meta = manifest.params.get(_CACHE_META_KEY)
        if not isinstance(meta, Mapping) or not _valid_meta(meta):
            artifact = _loaded_from_manifest(manifest, payload, ref)
            result = StepResult(artifacts=(_draft_from_loaded(artifact),))
            return CacheEntry(
                ref=ref,
                manifest=manifest,
                result=result,
                artifacts=(artifact,),
                refs=(ref,),
            )

        status = StepResultStatus(str(meta["status"]))
        result_warnings = _warnings_from_dicts(cast(Iterable[object], meta.get("warnings", ())))
        raw_cost = meta.get("cost")
        actual_cost = (
            Cost(
                cpu_seconds=float(raw_cost.get("cpu_seconds", 0.0)),
                memory_mb=float(raw_cost.get("memory_mb", 0.0)),
                network_calls=int(raw_cost.get("network_calls", 0)),
                money_usd=float(raw_cost.get("money_usd", 0.0)),
            )
            if isinstance(raw_cost, Mapping)
            else Cost()
        )
        entries = cast(Iterable[object], meta["artifacts"])
        loaded: list[LoadedArtifact] = []
        refs: list[ArtifactRef] = []
        for index, raw_entry in enumerate(entries):
            if not isinstance(raw_entry, Mapping):
                raise CacheMiss("cache index entry is not an object")
            entry = cast(Mapping[str, object], raw_entry)
            entry_key = str(entry["cache_key"])
            ArtifactType.parse(str(entry["type"]))
            if index == 0 or entry_key == ref.cache_key:
                entry_ref = ref
                entry_manifest = manifest
                entry_payload = payload
            else:
                found = await self._store.find(entry_key)
                if found is None:
                    raise CacheMiss(
                        "cached artifact referenced by the index is missing",
                        context={"cache_key": entry_key},
                    )
                entry_manifest, entry_payload = await self._store.load(found)
                entry_ref = found
            if isinstance(schema_version, Mapping):
                expected_schema = schema_version.get(entry_manifest.type)
                if expected_schema is None:
                    for raw_key, raw_value in schema_version.items():
                        if str(raw_key) == entry_manifest.type:
                            expected_schema = raw_value
                            break
                if expected_schema is not None and entry_manifest.schema_version != expected_schema:
                    raise CacheSchemaMismatch(
                        "cached artifact schema is not compatible",
                        context={
                            "cache_key": entry_key,
                            "artifact_type": entry_manifest.type,
                            "schema_version": entry_manifest.schema_version,
                            "expected": expected_schema,
                        },
                    )
            artifact = _loaded_from_manifest(entry_manifest, entry_payload, entry_ref)
            loaded.append(artifact)
            refs.append(entry_ref)

        artifacts = tuple(_draft_from_loaded(item) for item in loaded)
        result = StepResult(
            artifacts=artifacts,
            warnings=result_warnings,
            actual_cost=actual_cost,
            status=status,
        )
        return CacheEntry(
            ref=ref,
            manifest=manifest,
            result=result,
            artifacts=tuple(loaded),
            refs=tuple(refs),
        )

    async def store(
        self,
        key: str,
        result: StepResult,
        *,
        producer: (
            ArtifactProducer | CapabilityManifest | Mapping[str, object] | tuple[str, str] | None
        ) = None,
        params: Mapping[str, Any] | None = None,
        inputs: object = None,
    ) -> list[ArtifactRef]:
        """Persist a successful/degraded step result; skipped results are ignored.

        A forced/refreshed step may produce a different payload for the same
        deterministic key.  Artifacts are immutable, so the new result is
        written under a payload-derived sibling key; the old artifact remains
        available.  Re-running an unchanged result keeps the normal key.
        """

        cache_key = require_cache_key(key)
        if not isinstance(result, StepResult):
            raise TypeError("result must be a StepResult")
        if result.status is StepResultStatus.SKIPPED or not result.artifacts:
            return []

        producer_model = _coerce_producer(producer)
        artifact_inputs = _coerce_inputs(inputs)
        try:
            return await self._store_result(
                cache_key,
                result,
                producer_model,
                artifact_inputs,
                params or {},
            )
        except ArtifactConflict:
            primary = result.artifacts[0]
            primary_payload = (
                canonical_json(primary.payload)
                if primary.payload_format == "json"
                else bytes(primary.payload)
            )
            refreshed_key = sha256_hex(
                canonical_json(
                    {
                        "cache_key": cache_key,
                        "payload_hash": sha256_hex(primary_payload),
                    }
                )
            )
            return await self._store_result(
                refreshed_key,
                result,
                producer_model,
                artifact_inputs,
                params or {},
            )

    async def _store_result(
        self,
        cache_key: str,
        result: StepResult,
        producer: ArtifactProducer,
        artifact_inputs: list[ArtifactInput],
        params: Mapping[str, Any],
    ) -> list[ArtifactRef]:
        now = self._clock()
        entries: list[dict[str, Any]] = []
        for index, draft in enumerate(result.artifacts):
            entries.append(
                {
                    "index": index,
                    "type": str(draft.type),
                    "schema_version": draft.type.schema_version,
                    "artifact_id": draft.artifact_id,
                    "cache_key": _derive_key(cache_key, index, draft.type),
                    "params": dict(draft.params),
                }
            )

        meta = {
            "version": _CACHE_META_VERSION,
            "status": result.status.value,
            "warnings": [warning.to_dict() for warning in result.warnings],
            "cost": {
                "cpu_seconds": result.actual_cost.cpu_seconds,
                "memory_mb": result.actual_cost.memory_mb,
                "network_calls": result.actual_cost.network_calls,
                "money_usd": result.actual_cost.money_usd,
            },
            "artifacts": entries,
        }

        stored_by_index: dict[int, ArtifactRef] = {}
        # Store extra artifacts first; the primary entry acts as the commit
        # marker, so an interrupted write is seen as a cache miss.
        ordered = list(enumerate(result.artifacts))
        for index, draft in reversed(ordered):
            entry_key = cast(str, entries[index]["cache_key"])
            manifest_params = {**dict(params), **dict(draft.params)}
            if index == 0:
                manifest_params[_CACHE_META_KEY] = meta
            warnings = list(draft.warnings) + list(result.warnings)
            manifest = ArtifactManifest(
                artifact_id=draft.artifact_id,
                type=str(draft.type),
                schema_version=draft.type.schema_version,
                producer=producer,
                inputs=artifact_inputs,
                params=manifest_params,
                warnings=[warning.to_dict() for warning in warnings],
                created_at=_iso_z(now),
                expires_at=(_iso_z(draft.expires_at) if draft.expires_at is not None else None),
            )
            payload = (
                canonical_json(draft.payload)
                if draft.payload_format == "json"
                else bytes(draft.payload)
            )
            ref = await self._store.put(
                entry_key,
                manifest,
                payload,
                payload_format=draft.payload_format,
            )
            stored_by_index[index] = ref
        return [stored_by_index[index] for index in range(len(stored_by_index))]

    async def has(
        self,
        key: str,
        schema_version: int | Mapping[str, int] | ArtifactType | None = None,
        *,
        refresh: bool | None = None,
        force: bool | None = None,
    ) -> bool:
        return (await self.lookup(key, schema_version, refresh=refresh, force=force)) is not None

    @staticmethod
    def _schema_matches(
        manifest: ArtifactManifest,
        schema_version: int | Mapping[str, int] | ArtifactType | None,
    ) -> bool:
        if schema_version is None:
            return True
        if isinstance(schema_version, ArtifactType):
            return (
                manifest.type == str(schema_version)
                and manifest.schema_version == schema_version.schema_version
            )
        if isinstance(schema_version, int) and not isinstance(schema_version, bool):
            return manifest.schema_version == schema_version
        if isinstance(schema_version, Mapping):
            expected = schema_version.get(manifest.type)
            if expected is None:
                for key, value in schema_version.items():
                    if str(key) == manifest.type:
                        expected = value
                        break
            if expected is None:
                # A mapping scoped to other artifact types does not constrain
                # this manifest.
                return True
            return manifest.schema_version == expected
        raise TypeError("schema_version must be int, mapping, ArtifactType, or None")


def _valid_meta(meta: Mapping[object, object]) -> bool:
    return meta.get("version") == _CACHE_META_VERSION and isinstance(
        meta.get("artifacts"), (list, tuple)
    )


def _loaded_from_manifest(
    manifest: ArtifactManifest,
    payload: bytes,
    ref: ArtifactRef,
) -> LoadedArtifact:
    artifact_type = ArtifactType.parse(manifest.type)
    if ref.payload_path.name.endswith(".json"):
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CacheMiss(
                "cached JSON payload is invalid",
                context={"cache_key": ref.cache_key},
            ) from exc
        payload_bytes = canonical_json(value)
    else:
        value = payload
        payload_bytes = payload
    return LoadedArtifact(
        type=artifact_type,
        payload=value,
        payload_bytes=payload_bytes,
        payload_hash=manifest.payload_hash,
        cache_key=ref.cache_key,
        schema_version=manifest.schema_version,
        manifest=manifest,
        ref=ref,
    )


def _draft_from_loaded(artifact: LoadedArtifact) -> ArtifactDraft:
    warnings = _warnings_from_dicts(artifact.manifest.warnings)
    return ArtifactDraft(
        type=artifact.type,
        payload=artifact.payload,
        payload_format=("json" if artifact.ref.payload_path.name.endswith(".json") else "bin"),
        params={
            key: value for key, value in artifact.manifest.params.items() if key != _CACHE_META_KEY
        },
        warnings=warnings,
        expires_at=(
            _parse_iso_z(artifact.manifest.expires_at)
            if artifact.manifest.expires_at is not None
            else None
        ),
        artifact_id=artifact.manifest.artifact_id,
    )


__all__ = [
    "Cache",
    "CacheEntry",
    "CacheInput",
    "CacheMiss",
    "CacheSchemaMismatch",
    "LoadedArtifact",
    "cache_key_for",
    "cache_key_for_step",
    "relevant_config",
]

"""Unit tests for cache keys, schema compatibility, TTL, and store/load."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trackforge.application.ports.execution import (
    ArtifactDraft,
    StepResult,
    StepResultStatus,
    StepWarning,
)
from trackforge.domain.model.capabilities import ArtifactType, Cost
from trackforge.pipeline.cache import (
    Cache,
    CacheInput,
    CacheSchemaMismatch,
    cache_key_for,
    cache_key_for_step,
    relevant_config,
)

pytestmark = pytest.mark.unit

PROBE = ArtifactType.parse("audio.probe.v1")
FEATURES = ArtifactType.parse("audio.features.v1")
NOW = datetime(2026, 10, 22, 13, 0, tzinfo=UTC)


def result(*, warnings: tuple[StepWarning, ...] = ()) -> StepResult:
    return StepResult(
        artifacts=(
            ArtifactDraft(
                type=PROBE,
                payload={"duration": 12.5, "tags": ["a", "b"]},
            ),
        ),
        warnings=warnings,
        actual_cost=Cost(cpu_seconds=0.5),
    )


def test_cache_key_is_stable_and_input_order_independent(make_manifest) -> None:
    manifest = make_manifest("probe", provides=(PROBE,))
    first = cache_key_for(
        [CacheInput("media", "a" * 64), CacheInput("source", "b" * 64)],
        manifest,
        {"a": 1},
        {"path": "/bin/ffprobe"},
    )
    second = cache_key_for(
        [{"type": "source", "hash": "b" * 64}, {"type": "media", "hash": "a" * 64}],
        manifest,
        {"a": 1},
        {"path": "/bin/ffprobe"},
    )
    assert first == second
    assert len(first) == 64
    assert first == cache_key_for(
        {"media": "a" * 64, "source": "b" * 64},
        manifest,
        {"a": 1},
        {"path": "/bin/ffprobe"},
    )


def test_cache_key_changes_with_versions_params_config_model_and_prompt(
    make_manifest,
) -> None:
    manifest = make_manifest("probe", provides=(PROBE,), version="1")
    other_version = make_manifest("probe", provides=(PROBE,), version="2")
    base = cache_key_for([], manifest, {"temperature": 0.0})
    assert base != cache_key_for([], other_version, {"temperature": 0.0})
    assert base != cache_key_for([], manifest, {"temperature": 1.0})
    assert base != cache_key_for([], manifest, {"temperature": 0.0}, {"x": 1})
    assert base != cache_key_for([], manifest, {"temperature": 0.0}, model={"id": "m"})
    assert base != cache_key_for([], manifest, {"temperature": 0.0}, prompt={"id": "p"})
    assert base != cache_key_for([], manifest, {"temperature": 0.0}, {"ffprobe": "/a"})


def test_cache_key_changes_with_input_payload_hash(make_manifest) -> None:
    manifest = make_manifest("probe", provides=(PROBE,))
    assert cache_key_for([CacheInput("media", "a" * 64)], manifest) != cache_key_for(
        [CacheInput("media", "b" * 64)], manifest
    )


def test_relevant_config_uses_only_declared_keys(make_manifest) -> None:
    manifest = make_manifest(
        "probe",
        provides=(PROBE,),
        config_schema={"properties": {"ffprobe_path": {}, "timeout": {}}},
    )
    subset = relevant_config(
        manifest,
        {"ffprobe_path": "/bin/ffprobe", "timeout": 10, "unrelated": "ignored"},
    )
    assert subset == {"ffprobe_path": "/bin/ffprobe", "timeout": 10}
    assert relevant_config(make_manifest("other", provides=(PROBE,)), {"x": 1}) == {}


def test_cache_key_for_step_filters_unrelated_config(make_manifest) -> None:
    manifest = make_manifest(
        "probe",
        provides=(PROBE,),
        config_schema={"properties": {"ffprobe_path": {}}},
    )
    first = cache_key_for_step({}, manifest, {}, {"ffprobe_path": "/a"})
    second = cache_key_for_step({}, manifest, {}, {"ffprobe_path": "/a", "unrelated": 1})
    assert first == second


@pytest.mark.asyncio
async def test_store_then_lookup_returns_a_hit(make_manifest, cache) -> None:
    manifest = make_manifest("probe", provides=(PROBE,))
    key = cache_key_for([], manifest)
    assert await cache.lookup(key, 1) is None
    assert await cache.store(key, result(), producer=manifest)
    ref = await cache.lookup(key, 1)
    assert ref is not None
    entry = await cache.load(ref, 1)
    assert entry.result.artifacts[0].payload == {"duration": 12.5, "tags": ["a", "b"]}
    assert entry.result.actual_cost.cpu_seconds == 0.5


@pytest.mark.asyncio
async def test_schema_mismatch_is_a_miss(make_manifest, cache) -> None:
    manifest = make_manifest("probe", provides=(PROBE,))
    key = cache_key_for([], manifest)
    await cache.store(key, result(), producer=manifest)
    assert await cache.lookup(key, 1) is not None
    assert await cache.lookup(key, 2) is None
    assert await cache.lookup(key, {PROBE.render(): 2}) is None
    ref = await cache.lookup(key, 1)
    assert ref is not None
    with pytest.raises(CacheSchemaMismatch):
        await cache.load(ref, 2)


@pytest.mark.asyncio
async def test_expected_type_must_match(make_manifest, cache) -> None:
    manifest = make_manifest("probe", provides=(PROBE,))
    key = cache_key_for([], manifest)
    await cache.store(key, result(), producer=manifest)
    assert await cache.lookup(key, 1, expected_type=PROBE) is not None
    assert await cache.lookup(key, 1, expected_type=FEATURES) is None


@pytest.mark.asyncio
async def test_ttl_expiry_and_refresh(make_manifest, fake_store) -> None:
    manifest = make_manifest("probe", provides=(PROBE,))
    cache = Cache(fake_store, clock=lambda: NOW)
    key = cache_key_for([], manifest)
    expired = StepResult(
        artifacts=(
            ArtifactDraft(
                type=PROBE,
                payload={"x": 1},
                expires_at=NOW - timedelta(seconds=1),
            ),
        )
    )
    await cache.store(key, expired, producer=manifest)
    assert await cache.lookup(key, 1) is None
    assert await cache.lookup(key, 1, refresh=True) is not None


@pytest.mark.asyncio
async def test_skipped_results_are_not_stored(make_manifest, cache) -> None:
    manifest = make_manifest("probe", provides=(PROBE,))
    key = cache_key_for([], manifest)
    refs = await cache.store(
        key,
        StepResult(status=StepResultStatus.SKIPPED),
        producer=manifest,
    )
    assert refs == []
    assert await cache.lookup(key, 1) is None


@pytest.mark.asyncio
async def test_multiple_artifacts_round_trip(make_manifest, cache) -> None:
    manifest = make_manifest("probe", provides=(PROBE, FEATURES))
    key = cache_key_for([], manifest)
    multi = StepResult(
        artifacts=(
            ArtifactDraft(type=PROBE, payload={"a": 1}),
            ArtifactDraft(type=FEATURES, payload={"b": 2}),
        ),
        warnings=(StepWarning(code="no_data", message="partial"),),
        status=StepResultStatus.DEGRADED,
    )
    refs = await cache.store(key, multi, producer=manifest)
    assert len(refs) == 2
    entry = await cache.load(refs[0], {PROBE.render(): 1, FEATURES.render(): 1})
    assert [artifact.type for artifact in entry.artifacts] == [PROBE, FEATURES]
    assert entry.result.status is StepResultStatus.DEGRADED
    assert entry.result.warnings[0].code == "no_data"


@pytest.mark.asyncio
async def test_corrupted_entry_is_a_miss(make_manifest, fake_store, cache) -> None:
    manifest = make_manifest("probe", provides=(PROBE,))
    key = cache_key_for([], manifest)
    await cache.store(key, result(), producer=manifest)
    stored = fake_store.entries[key]
    fake_store.entries[key] = (stored[0], stored[1] + b"x", stored[2])
    assert await cache.lookup(key, 1) is None


@pytest.mark.asyncio
async def test_missing_entry_is_a_miss(make_manifest, fake_store, cache) -> None:
    manifest = make_manifest("probe", provides=(PROBE,))
    key = cache_key_for([], manifest)
    await cache.store(key, result(), producer=manifest)
    fake_store.entries.pop(key)
    assert await cache.lookup(key, 1) is None


@pytest.mark.asyncio
async def test_cache_load_raises_cache_miss_for_bad_payload(make_manifest, fake_store) -> None:
    manifest = make_manifest("probe", provides=(PROBE,))
    cache = Cache(fake_store)
    key = cache_key_for([], manifest)
    await cache.store(key, result(), producer=manifest)
    ref = await cache.lookup(key, 1)
    assert ref is not None
    stored_manifest, _, fmt = fake_store.entries[key]
    bad = b"not json"
    fake_store.entries[key] = (stored_manifest, bad, fmt)
    assert await cache.lookup(key, 1) is None


@pytest.mark.asyncio
async def test_has_reports_hits(make_manifest, cache) -> None:
    manifest = make_manifest("probe", provides=(PROBE,))
    key = cache_key_for([], manifest)
    assert await cache.has(key, 1) is False
    await cache.store(key, result(), producer=manifest)
    assert await cache.has(key, 1) is True
    assert await cache.has(key, 2) is False


def test_cache_input_validates_hash() -> None:
    with pytest.raises(ValueError):
        CacheInput("media", "")


@pytest.mark.asyncio
async def test_forced_lookup_is_a_miss_but_keeps_the_artifact(
    make_manifest,
    fake_store,
) -> None:
    manifest = make_manifest("probe", provides=(PROBE,))
    key = cache_key_for([], manifest)
    cache = Cache(fake_store)
    await cache.store(key, result(), producer=manifest)

    assert await cache.lookup(key, 1, force=True) is None
    assert await cache.has(key, 1, force=True) is False
    assert key in fake_store.entries
    assert await cache.lookup(key, 1) is not None

    forced = Cache(fake_store, force=True)
    assert await forced.lookup(key, 1) is None
    key_forced = Cache(fake_store, force_keys=[key])
    assert await key_forced.lookup(key, 1) is None

"""Integration and contract tests for the filesystem artifact store."""

from __future__ import annotations

import math
import stat
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from trackforge.adapters.persistence.filesystem import artifacts as artifacts_module
from trackforge.adapters.persistence.filesystem.artifacts import FileSystemArtifactStore
from trackforge.adapters.persistence.filesystem.cas import FileSystemCas
from trackforge.application.dto.artifacts import (
    ArtifactInput,
    ArtifactManifest,
    ArtifactProducer,
)
from trackforge.application.ports.storage import (
    ArtifactConflict,
    ArtifactCorrupted,
    ArtifactRef,
    ArtifactStore,
    InvalidStorageKey,
)
from trackforge.application.services.canonical_json import canonical_json, sha256_hex
from trackforge.bootstrap import build_artifact_store, build_cas, build_context
from trackforge.config import TrackForgeConfig

pytestmark = pytest.mark.integration

FIXED_ID = "01930000-0000-7000-8000-000000000001"
FIXED_TIME = "2026-10-22T13:00:00Z"


def _key(label: str) -> str:
    return sha256_hex(label.encode("utf-8"))


def _manifest_data(**overrides: Any) -> dict[str, Any]:
    params = overrides.pop("params", {"b": 2, "a": "\u0451"})
    data: dict[str, Any] = {
        "artifact_id": FIXED_ID,
        "type": "audio.probe.v1",
        "schema_version": 1,
        "producer": {"id": "ffprobe", "version": "6.1"},
        "inputs": [],
        "params": params,
        "params_hash": sha256_hex(canonical_json(params)),
        "payload_hash": "",
        "warnings": [],
        "created_at": FIXED_TIME,
        "expires_at": None,
        "license": None,
    }
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_json_artifact_round_trip(
    store: FileSystemArtifactStore,
    manifest_factory: Any,
    json_payload: bytes,
) -> None:
    cache_key = _key("json-artifact")
    manifest = manifest_factory(params={"b": 2, "a": "\u0451"}, params_hash="", payload_hash="")

    ref = await store.put(cache_key, manifest, json_payload)

    assert ref.cache_key == cache_key
    assert ref.manifest_path.resolve() == (store.root / cache_key / "manifest.json").resolve()
    assert ref.payload_path.resolve() == (store.root / cache_key / "payload.json").resolve()
    assert ref.payload_path.read_bytes() == json_payload
    assert ref.payload_hash == sha256_hex(json_payload)

    manifest_bytes = ref.manifest_path.read_bytes()
    assert manifest_bytes.endswith(b"\n")
    stored = ArtifactManifest.model_validate_json(manifest_bytes)
    assert stored.payload_hash == sha256_hex(json_payload)
    assert stored.params_hash == sha256_hex(canonical_json({"b": 2, "a": "\u0451"}))
    assert manifest_bytes == canonical_json(stored.model_dump(mode="json")) + b"\n"

    found = await store.find(cache_key)
    assert found is not None
    assert found.manifest_path.resolve() == ref.manifest_path.resolve()
    assert found.payload_path.resolve() == ref.payload_path.resolve()
    assert found.payload_hash == ref.payload_hash

    loaded_manifest, loaded_payload = await store.load(ref)
    assert loaded_manifest.artifact_id == manifest.artifact_id
    assert loaded_payload == json_payload
    assert await store.verify(ref) is True
    with await store.open_payload(ref) as stream:
        assert stream.read() == json_payload

    assert stat.S_IMODE(ref.manifest_path.stat().st_mode) == 0o644
    assert stat.S_IMODE(ref.payload_path.stat().st_mode) == 0o644
    assert stat.S_IMODE((store.root / cache_key).stat().st_mode) == 0o700
    assert list(store.tmp_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_binary_artifact_round_trip(
    store: FileSystemArtifactStore,
    manifest_factory: Any,
    binary_payload: bytes,
) -> None:
    cache_key = _key("binary-artifact")
    manifest = manifest_factory(type="audio.features.v1", params={"sample_rate": 44100})

    ref = await store.put(cache_key, manifest, binary_payload, payload_format="bin")

    assert ref.payload_path.name == "payload.bin"
    assert ref.payload_path.read_bytes() == binary_payload
    loaded_manifest, loaded_payload = await store.load(ref)
    assert loaded_manifest.type == "audio.features.v1"
    assert loaded_payload == binary_payload
    with await store.open_payload(ref) as stream:
        assert stream.read() == binary_payload


@pytest.mark.asyncio
async def test_payload_can_be_read_from_a_path(
    store: FileSystemArtifactStore,
    manifest_factory: Any,
    tmp_path: Path,
) -> None:
    json_source = tmp_path / "payload-source.json"
    json_source.write_bytes(b'{"source":"file"}')
    json_ref = await store.put(
        _key("path-json"),
        manifest_factory(),
        json_source,
        payload_format="json",
    )
    assert json_ref.payload_path.read_bytes() == b'{"source":"file"}'

    binary_source = tmp_path / "payload-source.bin"
    binary_source.write_bytes(bytes(range(64)))
    binary_ref = await store.put(
        _key("path-bin"),
        manifest_factory(type="audio.fingerprint.v1"),
        binary_source,
        payload_format="bin",
    )
    assert binary_ref.payload_path.read_bytes() == bytes(range(64))


@pytest.mark.asyncio
async def test_same_cache_key_and_payload_returns_existing_artifact(
    store: FileSystemArtifactStore,
    manifest_factory: Any,
    json_payload: bytes,
) -> None:
    cache_key = _key("dedupe")
    first = await store.put(cache_key, manifest_factory(), json_payload)
    first_bytes = first.manifest_path.read_bytes()
    first_stat = first.manifest_path.stat()

    second = await store.put(
        cache_key,
        manifest_factory(
            artifact_id="01930000-0000-7000-8000-000000000002",
            created_at="2026-10-23T13:00:00Z",
        ),
        json_payload,
    )

    assert second.manifest_path == first.manifest_path
    assert second.payload_hash == first.payload_hash
    assert first.manifest_path.read_bytes() == first_bytes
    assert first.manifest_path.stat().st_ino == first_stat.st_ino
    assert first.manifest_path.stat().st_mtime_ns == first_stat.st_mtime_ns
    stored = ArtifactManifest.model_validate_json(first.manifest_path.read_bytes())
    assert stored.artifact_id == FIXED_ID


@pytest.mark.asyncio
async def test_same_cache_key_with_different_payload_conflicts(
    store: FileSystemArtifactStore,
    manifest_factory: Any,
) -> None:
    cache_key = _key("conflict")
    ref = await store.put(cache_key, manifest_factory(), b'{"version":1}')
    original_manifest = ref.manifest_path.read_bytes()
    original_payload = ref.payload_path.read_bytes()

    with pytest.raises(ArtifactConflict):
        await store.put(cache_key, manifest_factory(), b'{"version":2}')

    assert ref.manifest_path.read_bytes() == original_manifest
    assert ref.payload_path.read_bytes() == original_payload
    _, payload = await store.load(ref)
    assert payload == b'{"version":1}'
    assert list(store.tmp_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_same_cache_key_with_different_format_conflicts(
    store: FileSystemArtifactStore,
    manifest_factory: Any,
) -> None:
    cache_key = _key("format-conflict")
    await store.put(cache_key, manifest_factory(), b'{"version":1}', payload_format="json")

    with pytest.raises(ArtifactConflict):
        await store.put(cache_key, manifest_factory(), b'{"version":1}', payload_format="bin")


@pytest.mark.asyncio
async def test_find_returns_none_for_missing_artifact(store: FileSystemArtifactStore) -> None:
    assert await store.find(_key("missing")) is None


@pytest.mark.parametrize(
    "cache_key",
    ["", "abc", "A" * 64, "../" + "a" * 61, "a" * 63, "a" * 64 + "/"],
)
@pytest.mark.asyncio
async def test_invalid_cache_keys_are_rejected(
    store: FileSystemArtifactStore,
    manifest_factory: Any,
    cache_key: str,
) -> None:
    with pytest.raises(InvalidStorageKey):
        await store.find(cache_key)
    with pytest.raises(InvalidStorageKey):
        await store.put(cache_key, manifest_factory(), b"{}")


@pytest.mark.asyncio
async def test_json_payload_must_be_valid_json(
    store: FileSystemArtifactStore,
    manifest_factory: Any,
) -> None:
    cache_key = _key("bad-json")
    with pytest.raises(ValueError):
        await store.put(cache_key, manifest_factory(), b"{not json")
    with pytest.raises(ValueError):
        await store.put(cache_key, manifest_factory(), b'{"value":NaN}')

    assert await store.find(cache_key) is None
    assert list(store.tmp_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_invalid_payload_format_and_type_are_rejected(
    store: FileSystemArtifactStore,
    manifest_factory: Any,
) -> None:
    with pytest.raises(InvalidStorageKey):
        await store.put(_key("bad-format"), manifest_factory(), b"{}", payload_format="yaml")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await store.put(_key("bad-type"), manifest_factory(), 123)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_manifest_payload_hash_mismatch_is_rejected(
    store: FileSystemArtifactStore,
    manifest_factory: Any,
) -> None:
    cache_key = _key("payload-hash-mismatch")
    manifest = manifest_factory(payload_hash="0" * 64)

    with pytest.raises(ArtifactCorrupted):
        await store.put(cache_key, manifest, b'{"value":1}')

    assert await store.find(cache_key) is None
    assert list(store.tmp_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_manifest_params_hash_mismatch_is_rejected(
    store: FileSystemArtifactStore,
    manifest_factory: Any,
) -> None:
    cache_key = _key("params-hash-mismatch")
    manifest = manifest_factory(params_hash="0" * 64)

    with pytest.raises(ArtifactCorrupted):
        await store.put(cache_key, manifest, b"{}")

    assert await store.find(cache_key) is None


@pytest.mark.asyncio
async def test_load_detects_corrupted_payload(
    store: FileSystemArtifactStore,
    manifest_factory: Any,
) -> None:
    ref = await store.put(_key("corrupt-payload"), manifest_factory(), b'{"value":1}')
    ref.payload_path.write_bytes(b'{"value":2}')

    with pytest.raises(ArtifactCorrupted):
        await store.load(ref)
    with pytest.raises(ArtifactCorrupted):
        await store.verify(ref)


@pytest.mark.asyncio
async def test_load_detects_corrupted_manifest(
    store: FileSystemArtifactStore,
    manifest_factory: Any,
) -> None:
    ref = await store.put(_key("corrupt-manifest"), manifest_factory(), b"{}")
    ref.manifest_path.write_bytes(b"{not json")

    with pytest.raises(ArtifactCorrupted):
        await store.find(ref.cache_key)
    with pytest.raises(ArtifactCorrupted):
        await store.load(ref)


@pytest.mark.asyncio
async def test_load_detects_missing_payload(
    store: FileSystemArtifactStore,
    manifest_factory: Any,
) -> None:
    ref = await store.put(_key("missing-payload"), manifest_factory(), b"{}")
    ref.payload_path.unlink()

    with pytest.raises(ArtifactCorrupted):
        await store.load(ref)
    with pytest.raises(ArtifactCorrupted):
        await store.verify(ref)


@pytest.mark.asyncio
async def test_load_rejects_tampered_references(
    store: FileSystemArtifactStore,
    manifest_factory: Any,
    tmp_path: Path,
) -> None:
    ref = await store.put(_key("tampered-ref"), manifest_factory(), b"{}")
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"{}")

    outside_ref = ArtifactRef(
        cache_key=ref.cache_key,
        manifest_path=outside,
        payload_path=ref.payload_path,
        payload_hash=ref.payload_hash,
    )
    wrong_payload_ref = ArtifactRef(
        cache_key=ref.cache_key,
        manifest_path=ref.manifest_path,
        payload_path=store.root / _key("other") / "payload.json",
        payload_hash=ref.payload_hash,
    )

    with pytest.raises(ArtifactCorrupted):
        await store.load(outside_ref)
    with pytest.raises(ArtifactCorrupted):
        await store.load(wrong_payload_ref)


@pytest.mark.asyncio
async def test_symlinked_artifact_entry_outside_root_is_rejected(
    store: FileSystemArtifactStore,
    manifest_factory: Any,
    tmp_path: Path,
) -> None:
    store.root.mkdir(parents=True)
    outside = tmp_path / "outside-artifacts"
    outside.mkdir()
    cache_key = _key("symlink")
    (store.root / cache_key).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactCorrupted):
        await store.find(cache_key)
    with pytest.raises(ArtifactCorrupted):
        await store.put(cache_key, manifest_factory(), b"{}")
    assert list(store.tmp_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_interrupted_manifest_write_cleans_temp(
    store: FileSystemArtifactStore,
    manifest_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_key = _key("interrupted")
    store.root.mkdir(parents=True)

    def fail_manifest(*args: object, **kwargs: object) -> None:
        raise OSError("interrupted")

    monkeypatch.setattr(artifacts_module, "atomic_write_bytes", fail_manifest)

    with pytest.raises(OSError, match="interrupted"):
        await store.put(cache_key, manifest_factory(), b"{}")

    assert list(store.tmp_dir.iterdir()) == []
    assert await store.find(cache_key) is None


@pytest.mark.asyncio
async def test_manifest_bytes_are_stable(
    store: FileSystemArtifactStore,
    manifest_factory: Any,
    json_payload: bytes,
) -> None:
    manifest = manifest_factory(params_hash="", payload_hash="")
    first = await store.put(_key("stable-1"), manifest, json_payload)
    second = await store.put(_key("stable-2"), manifest, json_payload)

    assert first.manifest_path.read_bytes() == second.manifest_path.read_bytes()


def test_domain_and_application_do_not_import_adapters() -> None:
    # The contract suite enforces this globally; keep the local assertion so the
    # storage tests fail loudly if a future refactor leaks an adapter import.
    import ast

    root = Path(__file__).resolve().parents[2] / "src" / "trackforge"
    for package in ("domain", "application"):
        for path in (root / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert not node.module.startswith("trackforge.adapters")
                if isinstance(node, ast.Import):
                    assert all(
                        not alias.name.startswith("trackforge.adapters") for alias in node.names
                    )


def test_bootstrap_builds_stores_without_creating_directories(
    config: TrackForgeConfig,
) -> None:
    context = build_context(config)

    assert isinstance(context.cas, FileSystemCas)
    assert isinstance(context.artifact_store, FileSystemArtifactStore)
    assert context.cas.data_dir == config.data_dir
    assert context.artifact_store.cache_dir == config.cache_dir
    assert isinstance(build_cas(config), FileSystemCas)
    assert isinstance(build_artifact_store(config), FileSystemArtifactStore)
    assert not config.data_dir.exists()
    assert not config.cache_dir.exists()


def test_store_implements_the_port(store: FileSystemArtifactStore) -> None:
    assert isinstance(store, ArtifactStore)


class _FakeArtifactStore:
    async def put(self, *args: object, **kwargs: object) -> ArtifactRef:
        raise NotImplementedError

    async def find(self, cache_key: str) -> ArtifactRef | None:
        raise NotImplementedError

    async def load(self, ref: ArtifactRef) -> tuple[ArtifactManifest, bytes]:
        raise NotImplementedError

    async def verify(self, ref: ArtifactRef) -> bool:
        raise NotImplementedError

    async def open_payload(self, ref: ArtifactRef) -> object:
        raise NotImplementedError


def test_fake_object_satisfies_the_protocol() -> None:
    assert isinstance(_FakeArtifactStore(), ArtifactStore)


def test_manifest_validation_rejects_unknown_and_invalid_fields() -> None:
    assert ArtifactManifest.model_validate(_manifest_data()).type == "audio.probe.v1"
    assert ArtifactInput(type="media", hash="a" * 64).type == "media"
    assert ArtifactProducer(id="ffprobe", version="6.1").id == "ffprobe"

    with pytest.raises(ValidationError):
        ArtifactManifest.model_validate(_manifest_data(unknown=True))
    with pytest.raises(ValidationError):
        ArtifactManifest.model_validate(_manifest_data(type="audio.probe"))
    with pytest.raises(ValidationError):
        ArtifactManifest.model_validate(_manifest_data(created_at="2026-10-22T13:00:00+00:00"))
    with pytest.raises(ValidationError):
        ArtifactManifest.model_validate(_manifest_data(payload_hash="A" * 64))
    with pytest.raises(ValidationError):
        ArtifactManifest.model_validate(
            _manifest_data(producer={"id": "x", "version": "1", "x": 1})
        )


def test_manifest_validation_rejects_nan_params() -> None:
    data = _manifest_data(params_hash="0" * 64)
    data["params"] = {"value": math.nan}

    with pytest.raises(ValidationError):
        ArtifactManifest.model_validate(data)


def test_manifest_validation_requires_ids_and_timestamps() -> None:
    missing_id = _manifest_data()
    missing_id.pop("artifact_id")
    missing_time = _manifest_data()
    missing_time.pop("created_at")

    with pytest.raises(ValidationError):
        ArtifactManifest.model_validate(missing_id)
    with pytest.raises(ValidationError):
        ArtifactManifest.model_validate(missing_time)

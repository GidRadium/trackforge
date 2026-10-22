"""Integration tests for the filesystem content-addressed store."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest

from trackforge.adapters.persistence.filesystem import cas as cas_module
from trackforge.adapters.persistence.filesystem.cas import FileSystemCas
from trackforge.application.ports.storage import (
    ArtifactCorrupted,
    ArtifactNotFound,
    ContentAddressedStore,
    InvalidStorageKey,
)

pytestmark = pytest.mark.integration


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.mark.asyncio
async def test_put_bytes_uses_expected_layout_and_permissions(cas: FileSystemCas) -> None:
    data = b"immutable bytes"
    content_hash = await cas.put_bytes(data)

    target = cas.path_for(content_hash)
    assert content_hash == _sha(data)
    assert target == cas.data_dir / "cas" / content_hash[:2] / content_hash[2:4] / content_hash
    assert target.read_bytes() == data
    assert stat.S_IMODE(target.stat().st_mode) == 0o644

    for directory in (
        cas.data_dir,
        cas.cache_dir,
        cas.root,
        target.parent.parent,
        target.parent,
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.asyncio
async def test_put_bytes_deduplicates_without_a_second_file(cas: FileSystemCas) -> None:
    data = b"same bytes"
    first = await cas.put_bytes(data)
    target = cas.path_for(first)
    first_inode = target.stat().st_ino

    second = await cas.put_bytes(data)

    assert second == first
    assert target.stat().st_ino == first_inode
    assert [path for path in cas.root.rglob("*") if path.is_file()] == [target]
    assert list(cas.tmp_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_put_file_streams_and_preserves_the_original(
    cas: FileSystemCas,
    tmp_path: Path,
) -> None:
    source = tmp_path / "original.bin"
    source.write_bytes(b"original-content" * 1000)
    original_hash = _sha(source.read_bytes())
    original_mtime = source.stat().st_mtime_ns

    content_hash = await cas.put_file(source)

    assert content_hash == original_hash
    assert _sha(source.read_bytes()) == original_hash
    assert source.stat().st_mtime_ns == original_mtime
    assert await cas.verify(content_hash, expected_size=source.stat().st_size)
    with await cas.open(content_hash) as stream:
        assert stream.read() == source.read_bytes()
    assert list(cas.tmp_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_put_file_deduplicates_a_large_file(cas: FileSystemCas, large_file: Path) -> None:
    assert large_file.stat().st_size == 10 * 1024 * 1024

    first = await cas.put_file(large_file)
    target = cas.path_for(first)
    first_inode = target.stat().st_ino
    second = await cas.put_file(large_file)

    assert first == second
    assert target.stat().st_ino == first_inode
    assert target.stat().st_size == large_file.stat().st_size
    assert [path for path in cas.root.rglob("*") if path.is_file()] == [target]
    assert list(cas.tmp_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_corrupted_object_is_reported_and_not_overwritten(
    cas: FileSystemCas,
    tmp_path: Path,
) -> None:
    data = b"original"
    content_hash = await cas.put_bytes(data)
    target = cas.path_for(content_hash)
    target.write_bytes(b"corrupt")
    source = tmp_path / "original.bin"
    source.write_bytes(data)

    assert await cas.exists(content_hash) is True
    assert await cas.verify(content_hash) is False
    with pytest.raises(ArtifactCorrupted):
        await cas.put_bytes(data)
    with pytest.raises(ArtifactCorrupted):
        await cas.put_file(source)

    assert target.read_bytes() == b"corrupt"


@pytest.mark.asyncio
async def test_verify_checks_expected_size(cas: FileSystemCas) -> None:
    content_hash = await cas.put_bytes(b"1234")

    assert await cas.verify(content_hash, expected_size=4) is True
    assert await cas.verify(content_hash, expected_size=5) is False
    assert await cas.verify(content_hash, expected_size=-1) is False


@pytest.mark.asyncio
async def test_open_missing_object_raises_not_found(cas: FileSystemCas) -> None:
    missing = _sha(b"never stored")

    assert await cas.exists(missing) is False
    assert await cas.verify(missing) is False
    with pytest.raises(ArtifactNotFound):
        await cas.open(missing)


@pytest.mark.parametrize("bad_hash", ["", "abc", "A" * 64, "../" + "a" * 61])
@pytest.mark.asyncio
async def test_invalid_hashes_are_rejected(cas: FileSystemCas, bad_hash: str) -> None:
    with pytest.raises(InvalidStorageKey):
        cas.path_for(bad_hash)
    with pytest.raises(InvalidStorageKey):
        await cas.exists(bad_hash)
    with pytest.raises(InvalidStorageKey):
        await cas.open(bad_hash)


@pytest.mark.asyncio
async def test_symlinked_shard_outside_root_is_rejected(
    cas: FileSystemCas,
    tmp_path: Path,
) -> None:
    content_hash = await cas.put_bytes(b"symlink target")
    target = cas.path_for(content_hash)
    shard = target.parent.parent
    outside = tmp_path / "outside"
    outside.mkdir()
    shard.rename(outside / "shard")
    shard.symlink_to(outside / "shard", target_is_directory=True)

    assert await cas.exists(content_hash) is False
    assert await cas.verify(content_hash) is False
    with pytest.raises(ArtifactCorrupted):
        await cas.open(content_hash)


@pytest.mark.asyncio
async def test_interrupted_put_file_cleans_temp(
    cas: FileSystemCas,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"new bytes")

    def fail_commit(source_path: Path, target_path: Path, *, mode: int) -> None:
        raise OSError("interrupted")

    monkeypatch.setattr(cas_module, "commit_temp", fail_commit)

    with pytest.raises(OSError, match="interrupted"):
        await cas.put_file(source)

    assert list(cas.tmp_dir.iterdir()) == []
    assert [path for path in cas.root.rglob("*") if path.is_file()] == []


def test_cas_implements_the_port(cas: FileSystemCas) -> None:
    assert isinstance(cas, ContentAddressedStore)

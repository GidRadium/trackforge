"""Local-folder collection enumeration tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from trackforge.adapters.sources.local_folder import LocalFolderSource
from trackforge.application.ports.sources import CollectionResolver
from trackforge.domain.errors import UnsupportedSource
from trackforge.domain.model.source import LocalPath, Url

pytestmark = pytest.mark.unit


def test_local_folder_satisfies_collection_protocol() -> None:
    assert isinstance(LocalFolderSource(), CollectionResolver)


@pytest.mark.asyncio
async def test_enumerates_supported_files_in_name_order(tmp_path: Path) -> None:
    root = tmp_path / "2024 - Fixture Album"
    root.mkdir()
    (root / "b.mp3").write_bytes(b"b")
    (root / "A.flac").write_bytes(b"a")
    (root / "notes.txt").write_text("ignored", encoding="utf-8")
    (root / "cover.jpg").write_bytes(b"image")

    source = LocalFolderSource()
    collection = await source.resolve(LocalPath(str(root)))

    assert collection.kind.value == "folder"
    assert collection.title == "2024 - Fixture Album"
    assert collection.owner == tmp_path.name
    assert [item.position for item in collection.items] == [1, 2]
    assert [item.candidate.title for item in collection.items] == ["A", "b"]
    assert collection.items[0].status.value == "planned"
    assert collection.context_claims[0].field == "album"
    assert collection.context_claims[0].confidence == pytest.approx(0.2)
    assert any(claim.field == "date" for claim in collection.context_claims)
    assert collection.warnings


@pytest.mark.asyncio
async def test_symlink_outside_root_is_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "folder"
    root.mkdir()
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside")
    (root / "inside.mp3").write_bytes(b"inside")
    (root / "escape.mp3").symlink_to(outside)

    collection = await LocalFolderSource().resolve_collection(LocalPath(str(root)))

    assert [item.candidate.external_id for item in collection.items] == [
        str((root / "inside.mp3").absolute())
    ]
    assert any("unsupported_files_ignored" in warning for warning in collection.warnings)


@pytest.mark.asyncio
async def test_empty_folder_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    (root / "readme.txt").write_text("nothing", encoding="utf-8")

    with pytest.raises(UnsupportedSource, match="no supported audio"):
        await LocalFolderSource().resolve_collection(LocalPath(str(root)))


def test_supports_only_directories(tmp_path: Path) -> None:
    root = tmp_path / "folder"
    root.mkdir()
    source = LocalFolderSource()

    assert source.supports_collection(LocalPath(str(root))) is True
    assert source.supports_collection(LocalPath(str(tmp_path / "missing"))) is False
    assert source.supports_collection(Url("https://example.invalid/folder")) is False


@pytest.mark.asyncio
async def test_enumeration_is_bounded_for_large_folders(tmp_path: Path) -> None:
    root = tmp_path / "large"
    root.mkdir()
    for index in range(501):
        (root / f"{index:04d}.mp3").write_bytes(b"x")

    collection = await LocalFolderSource().resolve_collection(LocalPath(str(root)))

    assert len(collection.items) == 501
    assert any("truncated" in warning for warning in collection.warnings)

"""Path guard and atomic-write tests."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from trackforge.adapters.persistence.filesystem.paths import (
    PathEscapeError,
    atomic_write_bytes,
    atomic_writer,
    ensure_within,
    open_binary,
)

pytestmark = pytest.mark.integration


def test_ensure_within_accepts_nested_relative_and_absolute_paths(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    assert ensure_within(root, "nested/file.txt") == (root / "nested" / "file.txt").resolve()
    assert (
        ensure_within(root, root / "nested" / "file.txt")
        == (root / "nested" / "file.txt").resolve()
    )


def test_ensure_within_rejects_parent_traversal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(PathEscapeError):
        ensure_within(root, "../outside.txt")
    with pytest.raises(PathEscapeError):
        ensure_within(root, root / "nested" / ".." / ".." / "outside.txt")
    with pytest.raises(PathEscapeError):
        ensure_within(root, "nested/../file.txt")


def test_ensure_within_rejects_absolute_path_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(PathEscapeError):
        ensure_within(root, Path("/etc/passwd"))


def test_ensure_within_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathEscapeError):
        ensure_within(root, root / "link" / "file.txt")


def test_ensure_within_allows_symlink_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    inside = root / "inside"
    root.mkdir()
    inside.mkdir()
    (root / "link").symlink_to(inside, target_is_directory=True)

    assert ensure_within(root, root / "link" / "file.txt") == (inside / "file.txt").resolve()


def test_open_binary_refuses_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    link = tmp_path / "link.txt"
    link.symlink_to(outside)

    with pytest.raises(PathEscapeError):
        open_binary(link)


def test_atomic_write_bytes_is_exact_and_private(tmp_path: Path) -> None:
    target = tmp_path / "data" / "file.bin"
    temp_dir = tmp_path / "cache" / "tmp"

    atomic_write_bytes(target, b"exact-bytes", tmp_dir=temp_dir)

    assert target.read_bytes() == b"exact-bytes"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert list(temp_dir.iterdir()) == []


def test_atomic_writer_cleans_temp_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "data" / "file.bin"
    temp_dir = tmp_path / "cache" / "tmp"

    with pytest.raises(RuntimeError, match="boom"):
        with atomic_writer(target, tmp_dir=temp_dir) as stream:
            stream.write(b"partial")
            raise RuntimeError("boom")

    assert not target.exists()
    assert list(temp_dir.iterdir()) == []


def test_atomic_writer_cleans_temp_when_rename_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "data" / "file.bin"
    temp_dir = tmp_path / "cache" / "tmp"

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="rename failed"):
        atomic_write_bytes(target, b"partial", tmp_dir=temp_dir)

    assert not target.exists()
    assert list(temp_dir.iterdir()) == []

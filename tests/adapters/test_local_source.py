"""Unit tests for the local file source resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from trackforge.adapters.sources.local import LocalFileSource
from trackforge.application.ports.sources import SourceResolver
from trackforge.domain.errors import UnsupportedSource
from trackforge.domain.model.source import LocalPath, Url

pytestmark = pytest.mark.unit


def test_local_file_source_satisfies_protocol() -> None:
    assert isinstance(LocalFileSource(), SourceResolver)


@pytest.mark.asyncio
async def test_supports_and_describes_supported_file(tmp_path: Path) -> None:
    path = tmp_path / "Artist - Song.mp3"
    payload = b"not-real-audio-but-bytes"
    path.write_bytes(payload)
    source = LocalFileSource()
    reference = LocalPath(str(path))

    assert source.supports(reference) is True
    resolved = await source.resolve(reference)
    candidate = resolved.candidates[0]

    assert candidate.source_ref == LocalPath(str(path.absolute()))
    assert candidate.external_id == str(path.absolute())
    assert candidate.title == "Artist - Song"
    assert candidate.duration_seconds is None
    assert candidate.uploader is None
    assert candidate.formats[0].container == "mp3"
    assert candidate.formats[0].size_bytes == len(payload)
    assert candidate.extra["size_bytes"] == len(payload)


@pytest.mark.parametrize("suffix", [".mp3", ".m4a", ".flac", ".ogg", ".opus", ".MP3"])
def test_supported_extensions_are_case_insensitive(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"song{suffix}"
    path.write_bytes(b"x")
    assert LocalFileSource().supports(LocalPath(str(path))) is True


@pytest.mark.asyncio
async def test_unsupported_extension_raises_typed_error(tmp_path: Path) -> None:
    path = tmp_path / "song.wav"
    path.write_bytes(b"x")
    source = LocalFileSource()

    assert source.supports(LocalPath(str(path))) is False
    with pytest.raises(UnsupportedSource) as caught:
        await source.resolve(LocalPath(str(path)))
    assert caught.value.user_action is not None
    assert caught.value.context["suffix"] == ".wav"


@pytest.mark.asyncio
async def test_directory_is_not_supported(tmp_path: Path) -> None:
    directory = tmp_path / "album.mp3"
    directory.mkdir()
    source = LocalFileSource()

    assert source.supports(LocalPath(str(directory))) is False
    with pytest.raises(UnsupportedSource, match="not a regular file"):
        await source.resolve(LocalPath(str(directory)))


@pytest.mark.asyncio
async def test_missing_file_is_not_supported(tmp_path: Path) -> None:
    path = tmp_path / "missing.mp3"
    source = LocalFileSource()

    assert source.supports(LocalPath(str(path))) is False
    with pytest.raises(UnsupportedSource):
        await source.resolve(LocalPath(str(path)))


@pytest.mark.asyncio
async def test_symlink_to_regular_file_is_supported(tmp_path: Path) -> None:
    target = tmp_path / "real.flac"
    target.write_bytes(b"audio")
    link = tmp_path / "link.flac"
    link.symlink_to(target)
    source = LocalFileSource()

    assert source.supports(LocalPath(str(link))) is True
    resolved = await source.resolve(LocalPath(str(link)))
    assert resolved.candidates[0].external_id == str(link.absolute())
    assert resolved.candidates[0].extra["symlink"] is True
    # The explicit user path is preserved; the target is not substituted.
    assert target.exists()


def test_symlink_to_directory_is_not_supported(tmp_path: Path) -> None:
    target = tmp_path / "directory"
    target.mkdir()
    link = tmp_path / "link.mp3"
    link.symlink_to(target, target_is_directory=True)

    assert LocalFileSource().supports(LocalPath(str(link))) is False


def test_non_local_reference_is_not_supported() -> None:
    assert LocalFileSource().supports(Url("https://example.invalid/track")) is False

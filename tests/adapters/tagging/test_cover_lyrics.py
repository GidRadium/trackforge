"""Cover embedding and plain-lyrics behavior across tag formats."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.fixtures.media import LRC_TEXT, generate_media, generate_png, skip_without_media_tools

from trackforge.adapters.tagging.mutagen import MutagenTagReader, MutagenTagWriter
from trackforge.application.dto.rendering import CoverArt, RenderOptions, TagSet

pytestmark = pytest.mark.integration

FORMATS = ("mp3", "m4a", "flac", "ogg", "opus")


def _cover(tmp_path: Path) -> CoverArt:
    return CoverArt.from_path(generate_png(tmp_path / "media"))


@pytest.mark.parametrize("container", FORMATS)
def test_cover_and_plain_lyrics_are_embedded(tmp_path: Path, container: str) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", container, with_tags=False, duration=0.3)
    cover = _cover(tmp_path)
    expected = TagSet(title="Song", lyrics="line one\nline two")
    reader = MutagenTagReader()
    writer = MutagenTagWriter(reader=reader)

    writer.write(fixture.path, expected, cover, RenderOptions())

    actual = reader.read(fixture.path)
    assert actual.lyrics == expected.lyrics
    embedded = reader.read_cover(fixture.path)
    assert embedded is not None
    assert embedded.data == cover.data
    assert writer.verify(fixture.path, expected, cover) == []


@pytest.mark.parametrize("container", FORMATS)
def test_old_cover_is_removed_when_no_new_cover(tmp_path: Path, container: str) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", container, with_tags=False, duration=0.3)
    writer = MutagenTagWriter()
    # Make each format's embedded-artwork path differ: MP3 APIC, MP4 covr,
    # FLAC picture, Ogg/Opus METADATA_BLOCK_PICTURE.
    writer.write(fixture.path, TagSet(title="Song"), _cover(tmp_path), RenderOptions())
    assert MutagenTagReader().read_cover(fixture.path) is not None

    writer.write(fixture.path, TagSet(title="Song"), None, RenderOptions())
    assert MutagenTagReader().read_cover(fixture.path) is None


@pytest.mark.parametrize("container", ("mp3", "m4a", "flac", "ogg", "opus"))
def test_lrc_is_never_written_as_plain_lyrics(tmp_path: Path, container: str) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", container, with_tags=False, duration=0.3)
    writer = MutagenTagWriter()
    writer.write(fixture.path, TagSet(title="Song"), None, RenderOptions())

    # The canonical TagSet refuses LRC outright, so the adapter can never put a
    # timecode into USLT/©lyr/LYRICS.
    from trackforge.domain.errors import DomainValidationError

    with pytest.raises(DomainValidationError):
        TagSet(lyrics=LRC_TEXT)
    assert MutagenTagReader().read(fixture.path).lyrics is None


def test_cover_sidecar_name_constant_is_stable() -> None:
    from trackforge.application.dto.rendering import COVER_SIDECAR_NAME

    assert COVER_SIDECAR_NAME == "cover.jpg"

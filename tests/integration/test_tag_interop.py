"""Raw tag interoperability for the canonical-v2 mapping."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from tests.fixtures.media import generate_media, skip_without_media_tools

from trackforge.adapters.tagging.mutagen import MutagenTagReader, MutagenTagWriter
from trackforge.application.dto.rendering import RenderOptions, TagSet

pytestmark = pytest.mark.integration

FORMATS = ("mp3", "m4a", "flac", "ogg")


def _tagset() -> TagSet:
    return TagSet(
        title="Collide",
        artists=("Rock Goes Nightcore", "End Of The Dream"),
        albumartist="Rock Goes Nightcore",
        album="Collide",
        date="2024-01-01",
        originaldate="2019-02-02",
        remixer="Rock Goes Nightcore",
        originalartist="End Of The Dream",
        qualifier="Nightcore",
        genre=("Rock",),
        bpm=172,
        key="F#m",
        language="eng",
        label="Label",
        catalog_number="CAT-001",
        barcode="1234567890123",
        isrc="USABC1234567",
        comment="comment",
        description="portable description",
        source_url="https://example.com/source?v=1&token=secret",
        source_id="private-source-id",
        copyright="(c) 2024",
        license="CC-BY-4.0",
        trackforge={
            "TRACKFORGE_ITEM_ID": "item-private",
            "TRACKFORGE_DERIVATIVE": "nightcore",
        },
    )


def _write(tmp_path: Path, container: str) -> tuple[Path, TagSet]:
    fixture = generate_media(tmp_path / "media", container, with_tags=False, duration=0.3)
    expected = _tagset()
    writer = MutagenTagWriter()
    writer.write(fixture.path, expected, None, RenderOptions())
    assert writer.verify(fixture.path, expected, None) == []
    return fixture.path, expected


@pytest.mark.parametrize("container", FORMATS)
def test_no_trackforge_keys_in_audio(tmp_path: Path, container: str) -> None:
    skip_without_media_tools()
    path, _ = _write(tmp_path, container)

    import mutagen

    audio = mutagen.File(str(path))
    assert audio is not None
    keys = [str(key) for key in audio.tags.keys()]
    assert not any("TRACKFORGE_" in key.upper() for key in keys)
    actual = MutagenTagReader().read(path)
    assert "TRACKFORGE_DERIVATIVE" not in actual.trackforge


def test_mp3_uses_standard_frames(tmp_path: Path) -> None:
    skip_without_media_tools()
    path, _ = _write(tmp_path, "mp3")

    import mutagen

    audio = mutagen.File(str(path))
    assert audio is not None
    for frame in ("TPE4", "TOPE", "TIT1", "TLAN", "WOAS"):
        assert frame in audio.tags, frame
    assert not any(str(key).startswith("TXXX:TRACKFORGE") for key in audio.tags)


def test_m4a_uses_portable_atoms_not_freeform_language(tmp_path: Path) -> None:
    skip_without_media_tools()
    path, _ = _write(tmp_path, "m4a")

    import mutagen

    audio = mutagen.File(str(path))
    assert audio is not None
    for atom in ("©ope", "©lan", "©url", "©grp"):
        assert atom in audio.tags, atom
    for forbidden in (
        "----:com.apple.iTunes:LANGUAGE",
        "----:com.apple.iTunes:REMIXER",
        "----:com.apple.iTunes:ORIGINALARTIST",
    ):
        assert forbidden not in audio.tags
    assert not any("TRACKFORGE_" in str(key).upper() for key in audio.tags)


@pytest.mark.parametrize("container", ("flac", "ogg", "opus"))
def test_vorbis_uses_uppercase_comments(tmp_path: Path, container: str) -> None:
    skip_without_media_tools()
    path, _ = _write(tmp_path, container)

    import mutagen

    audio = mutagen.File(str(path))
    assert audio is not None
    for key in ("LANGUAGE", "REMIXER", "ORIGINALARTIST", "GROUPING", "SOURCE"):
        assert key in audio.tags, key
    assert not any("TRACKFORGE_" in str(key).upper() for key in audio.tags)


def test_ffprobe_sees_portable_m4a_fields(tmp_path: Path) -> None:
    skip_without_media_tools()
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        pytest.skip("ffprobe is not installed")
    path, _ = _write(tmp_path, "m4a")

    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format_tags",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    tags = json.loads(completed.stdout)["format"]["tags"]
    assert tags["title"] == "Collide"
    assert "Rock Goes Nightcore" in tags["artist"]
    assert tags["album"] == "Collide"
    assert tags["date"] == "2024-01-01"
    assert tags["grouping"] == "Nightcore"
    assert tags["original_artist"] == "End Of The Dream"
    assert tags["URL"] == "https://example.com/source"


@pytest.mark.parametrize("container", ("mp3", "m4a", "flac"))
def test_old_trackforge_tags_are_removed_on_rewrite(tmp_path: Path, container: str) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", container, with_tags=False, duration=0.3)

    import mutagen

    audio = mutagen.File(str(fixture.path))
    if audio.tags is None:
        audio.add_tags()
    if container == "mp3":
        from mutagen.id3 import TXXX

        audio.tags.add(TXXX(encoding=3, desc="TRACKFORGE_DERIVATIVE", text=["nightcore"]))
        audio.tags.add(TXXX(encoding=3, desc="TRACKFORGE_ITEM_ID", text=["old"]))
    elif container == "m4a":
        import mutagen.mp4

        audio.tags["----:com.apple.iTunes:TRACKFORGE_DERIVATIVE"] = [
            mutagen.mp4.MP4FreeForm(b"nightcore")
        ]
    else:
        audio.tags["TRACKFORGE_DERIVATIVE"] = ["nightcore"]
    audio.save()

    writer = MutagenTagWriter()
    writer.write(
        fixture.path,
        TagSet(title="New", source_url="https://example.com/new"),
        None,
        RenderOptions(preserve_unknown_tags=True),
    )
    audio = mutagen.File(str(fixture.path))
    assert audio is not None
    assert not any("TRACKFORGE_" in str(key).upper() for key in audio.tags)

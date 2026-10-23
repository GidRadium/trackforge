"""Round-trip canonical tags through all five MVP formats."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.fixtures.media import generate_media, generate_png, skip_without_media_tools

from trackforge.adapters.tagging.mutagen import MutagenTagReader, MutagenTagWriter
from trackforge.application.dto.rendering import (
    COVER_MAX_BYTES,
    CoverArt,
    RenderOptions,
    TagSet,
)
from trackforge.domain.errors import DomainValidationError, RenderFailed

pytestmark = pytest.mark.integration

FORMATS = ("mp3", "m4a", "flac", "ogg", "opus")

MBIDS = {
    "mb_recording_id": "11111111-1111-1111-1111-111111111111",
    "mb_release_id": "22222222-2222-2222-2222-222222222222",
    "mb_release_group_id": "33333333-3333-3333-3333-333333333333",
    "mb_artist_id": "44444444-4444-4444-4444-444444444444",
    "mb_albumartist_id": "55555555-5555-5555-5555-555555555555",
    "acoustid": "acoustid-value",
}


def full_tagset() -> TagSet:
    return TagSet(
        title="Ünïcode — Song",
        artists=("Artist One", "Artist Two"),
        albumartist="Album Artist",
        album="Album ✓",
        date="2024-05-01",
        originaldate="2019",
        track=3,
        tracktotal=12,
        disc=1,
        disctotal=2,
        genre=("Rock", "Pop"),
        bpm=172,
        key="F#m",
        language="eng",
        label="Label",
        catalog_number="CAT-001",
        barcode="1234567890123",
        isrc="USABC1234567",
        compilation=False,
        comment="short comment",
        description="portable description",
        lyrics="plain line one\nplain line two",
        source_url="https://example.com/source?id=1",
        source_id="source-id",
        copyright="(c) 2024",
        license="CC-BY-4.0",
        trackforge={"TRACKFORGE_DERIVATIVE": "nightcore"},
        **MBIDS,
    )


def cover_art(tmp_path: Path) -> CoverArt:
    return CoverArt.from_path(generate_png(tmp_path / "media"), description="front")


def _assert_tagset(actual: TagSet, expected: TagSet) -> None:
    for field_name in (
        "title",
        "artists",
        "albumartist",
        "album",
        "date",
        "originaldate",
        "track",
        "tracktotal",
        "disc",
        "disctotal",
        "genre",
        "bpm",
        "key",
        "language",
        "label",
        "catalog_number",
        "barcode",
        "isrc",
        "compilation",
        "comment",
        "description",
        "lyrics",
        "source_url",
        "source_id",
        "copyright",
        "license",
    ):
        assert getattr(actual, field_name) == getattr(expected, field_name), field_name
    for field_name in MBIDS:
        assert getattr(actual, field_name) == getattr(expected, field_name), field_name
    for key, value in expected.trackforge.items():
        assert actual.trackforge.get(key) == value, key


@pytest.mark.parametrize("container", FORMATS)
def test_canonical_roundtrip(tmp_path: Path, container: str) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", container, with_tags=False, duration=0.3)
    expected = full_tagset()
    cover = cover_art(tmp_path)
    reader = MutagenTagReader()
    writer = MutagenTagWriter(reader=reader)

    writer.write(fixture.path, expected, cover, RenderOptions())

    actual = reader.read(fixture.path)
    _assert_tagset(actual, expected)
    actual_cover = reader.read_cover(fixture.path)
    assert actual_cover is not None
    assert actual_cover.mime_type == "image/png"
    assert actual_cover.data == cover.data
    assert writer.verify(fixture.path, expected, cover) == []


@pytest.mark.parametrize("version", (3, 4))
def test_id3_versions(tmp_path: Path, version: int) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", "mp3", with_tags=False, duration=0.3)
    expected = full_tagset()
    reader = MutagenTagReader()
    writer = MutagenTagWriter(reader=reader)

    writer.write(fixture.path, expected, None, RenderOptions(id3_version=version))

    header = fixture.path.read_bytes()[:5]
    assert header[:3] == b"ID3"
    assert header[3] == version
    actual = reader.read(fixture.path)
    _assert_tagset(actual, expected)
    assert writer.verify(fixture.path, expected, None) == []


def test_mp4_freeform_mbid_is_readable(tmp_path: Path) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", "m4a", with_tags=False, duration=0.3)
    expected = full_tagset()
    writer = MutagenTagWriter()
    writer.write(fixture.path, expected, None, RenderOptions())

    import mutagen

    audio = mutagen.File(str(fixture.path))
    assert audio is not None
    key = "----:com.apple.iTunes:MusicBrainz Track Id"
    assert key in audio.tags
    assert bytes(audio.tags[key][0]).decode() == expected.mb_recording_id
    assert MutagenTagReader().read(fixture.path).mb_recording_id == expected.mb_recording_id


def test_vorbis_uses_repeated_uppercase_comments(tmp_path: Path) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", "flac", with_tags=False, duration=0.3)
    writer = MutagenTagWriter()
    writer.write(fixture.path, full_tagset(), None, RenderOptions())

    import mutagen

    audio = mutagen.File(str(fixture.path))
    assert audio is not None
    assert audio.tags["ARTIST"] == ["Artist One", "Artist Two"]
    assert audio.tags["GENRE"] == ["Rock", "Pop"]
    assert "TITLE" in audio.tags


def test_none_and_empty_values_are_not_written(tmp_path: Path) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", "mp3", with_tags=False, duration=0.3)
    writer = MutagenTagWriter()
    writer.write(
        fixture.path,
        TagSet(title="Only title", album=None, artists=(), comment=""),
        None,
        RenderOptions(),
    )
    actual = MutagenTagReader().read(fixture.path)
    assert actual.title == "Only title"
    assert actual.album is None
    assert actual.artists == ()
    assert actual.comment is None


def test_unknown_tags_are_removed_by_default(tmp_path: Path) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", "mp3", with_tags=False, duration=0.3)
    import mutagen
    from mutagen.id3 import TXXX

    audio = mutagen.File(str(fixture.path))
    if audio.tags is None:
        audio.add_tags()
    audio.tags.add(TXXX(encoding=3, desc="UNRELATED", text=["keep me"]))
    audio.save()

    writer = MutagenTagWriter()
    writer.write(fixture.path, TagSet(title="New"), None, RenderOptions())
    audio = mutagen.File(str(fixture.path))
    assert not any(str(key).startswith("TXXX:UNRELATED") for key in audio.tags.keys())


def test_unknown_tags_can_be_preserved(tmp_path: Path) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", "mp3", with_tags=False, duration=0.3)
    import mutagen
    from mutagen.id3 import TXXX

    audio = mutagen.File(str(fixture.path))
    if audio.tags is None:
        audio.add_tags()
    audio.tags.add(TXXX(encoding=3, desc="UNRELATED", text=["keep me"]))
    audio.save()

    writer = MutagenTagWriter()
    writer.write(
        fixture.path,
        TagSet(title="New"),
        None,
        RenderOptions(preserve_unknown_tags=True),
    )
    audio = mutagen.File(str(fixture.path))
    assert "TXXX:UNRELATED" in audio.tags


def test_lrc_cannot_be_plain_lyrics() -> None:
    from tests.fixtures.media import LRC_TEXT

    with pytest.raises(DomainValidationError):
        TagSet(lyrics=LRC_TEXT)


def test_oversized_cover_is_rejected() -> None:
    with pytest.raises(DomainValidationError):
        CoverArt(data=b"x" * (COVER_MAX_BYTES + 1), mime_type="image/jpeg")


def test_corrupt_audio_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.mp3"
    path.write_bytes(b"not an audio file")
    with pytest.raises(RenderFailed):
        MutagenTagWriter().write(path, TagSet(title="x"), None, RenderOptions())


def test_unsupported_format_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "track.wav"
    path.write_bytes(b"RIFF....WAVEfmt ")
    with pytest.raises(RenderFailed):
        MutagenTagWriter().write(path, TagSet(title="x"), None, RenderOptions())


def test_verify_reports_mismatch(tmp_path: Path) -> None:
    skip_without_media_tools()
    fixture = generate_media(tmp_path / "media", "mp3", with_tags=False, duration=0.3)
    writer = MutagenTagWriter()
    writer.write(fixture.path, TagSet(title="Correct"), None, RenderOptions())
    mismatches = writer.verify(fixture.path, TagSet(title="Wrong"), None)
    assert mismatches
    assert mismatches[0].field == "title"


def test_adapters_match_their_ports() -> None:
    from trackforge.adapters.rendering.ffmpeg import FfmpegRenderer
    from trackforge.application.ports.rendering import Renderer, TagReader, TagWriter

    assert isinstance(MutagenTagReader(), TagReader)
    assert isinstance(MutagenTagWriter(), TagWriter)
    assert isinstance(FfmpegRenderer(), Renderer)


def test_target_format_mapping() -> None:
    from trackforge.application.dto.rendering import (
        is_supported_container,
        target_format_for,
    )
    from trackforge.domain.model.outputs import TargetFormat

    assert target_format_for("mp3") is TargetFormat.MP3
    assert target_format_for("oga") is TargetFormat.OGG
    assert target_format_for("m4b") is TargetFormat.M4A
    assert target_format_for("wav") is TargetFormat.FLAC
    assert target_format_for("wav", "opus") is TargetFormat.OPUS
    assert is_supported_container("oga")
    assert not is_supported_container("wav")

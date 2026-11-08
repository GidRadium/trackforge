"""Tests for magic-byte container detection."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from trackforge.metadata import (
    ContainerFormat,
    DetectedFormat,
    TagSystem,
    expected_tag_systems,
    sniff,
    sniff_bytes,
)


def _id3v2(payload: bytes = b"") -> bytes:
    size = len(payload)
    encoded_size = bytes(
        [
            (size >> 21) & 0x7F,
            (size >> 14) & 0x7F,
            (size >> 7) & 0x7F,
            size & 0x7F,
        ]
    )
    return b"ID3\x04\x00\x00" + encoded_size + payload


def _ftyp(brand: bytes) -> bytes:
    body = b"ftyp" + brand + b"\x00\x00\x00\x00" + brand
    return struct.pack(">I", 8 + len(body)) + body


def _ts_packet() -> bytes:
    return b"\x47" + b"\x00" * 187


def _m2ts_packet() -> bytes:
    return b"\x00" * 4 + b"\x47" + b"\x00" * 187


@pytest.mark.parametrize(
    ("name", "data", "container"),
    [
        ("mp3-with-id3", _id3v2() + b"\xff\xfb\x90\x64", ContainerFormat.MP3),
        ("mp3-raw", b"\xff\xfb\x90\x64", ContainerFormat.MP3),
        ("mp3-tag-only", _id3v2(), ContainerFormat.MP3),
        ("aac-adts", b"\xff\xf1\x50\x80", ContainerFormat.AAC),
        ("flac", b"fLaC" + b"\x00" * 32, ContainerFormat.FLAC),
        ("flac-after-id3", _id3v2() + b"fLaC" + b"\x00" * 32, ContainerFormat.FLAC),
        ("ogg-vorbis", b"OggS" + b"\x00" * 20 + b"\x01vorbis", ContainerFormat.OGG),
        ("ogg-opus", b"OggS" + b"\x00" * 20 + b"OpusHead", ContainerFormat.OGG),
        ("ogg-theora", b"OggS" + b"\x00" * 20 + b"\x80theora", ContainerFormat.OGG),
        ("mp4-m4a", _ftyp(b"M4A "), ContainerFormat.MP4),
        ("mp4-generic", _ftyp(b"isom"), ContainerFormat.MP4),
        ("mov", _ftyp(b"qt  "), ContainerFormat.MP4),
        ("matroska", b"\x1aE\xdf\xa3" + b"\x00" * 16 + b"matroska", ContainerFormat.MATROSKA),
        ("webm", b"\x1aE\xdf\xa3" + b"\x00" * 16 + b"webm", ContainerFormat.MATROSKA),
        ("wav", b"RIFF" + struct.pack("<I", 36) + b"WAVE" + b"\x00" * 32, ContainerFormat.WAV),
        ("rf64", b"RF64" + struct.pack("<I", 36) + b"WAVE" + b"\x00" * 32, ContainerFormat.WAV),
        ("avi", b"RIFF" + struct.pack("<I", 36) + b"AVI " + b"\x00" * 32, ContainerFormat.AVI),
        ("aiff", b"FORM" + struct.pack(">I", 36) + b"AIFF" + b"\x00" * 32, ContainerFormat.AIFF),
        ("aifc", b"FORM" + struct.pack(">I", 36) + b"AIFC" + b"\x00" * 32, ContainerFormat.AIFF),
        (
            "asf",
            bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c") + b"\x00" * 32,
            ContainerFormat.ASF,
        ),
        ("flv", b"FLV\x01" + b"\x00" * 32, ContainerFormat.FLV),
        ("dsf", b"DSD " + b"\x00" * 32, ContainerFormat.DSF),
        ("dff", b"FRM8" + b"\x00" * 32, ContainerFormat.DFF),
        ("ape", b"MAC " + b"\x00" * 32, ContainerFormat.APE),
        ("wavpack", b"wvpk" + b"\x00" * 32, ContainerFormat.WAVPACK),
        ("musepack", b"MPCK" + b"\x00" * 32, ContainerFormat.MUSEPACK),
        ("tta", b"TTA1" + b"\x00" * 32, ContainerFormat.TTA),
        ("tak", b"tBaK" + b"\x00" * 32, ContainerFormat.TAK),
        ("mpeg-ts", _ts_packet() + _ts_packet() + b"\x00", ContainerFormat.MPEG_TS),
        ("m2ts", _m2ts_packet() + _m2ts_packet(), ContainerFormat.MPEG_TS),
        ("mpeg-ps", b"\x00\x00\x01\xba" + b"\x00" * 32, ContainerFormat.MPEG_PS),
    ],
)
def test_container_detection(
    name: str,
    data: bytes,
    container: ContainerFormat,
) -> None:
    assert sniff_bytes(data).container is container, name


def test_empty_is_unknown() -> None:
    detected = sniff_bytes(b"")
    assert detected.container is ContainerFormat.UNKNOWN
    assert detected.mime_type == "application/octet-stream"
    assert detected.tag_systems == (TagSystem.NONE,)


def test_unknown_is_unknown() -> None:
    detected = sniff_bytes(b"this is not a media file")
    assert detected.container is ContainerFormat.UNKNOWN
    assert detected.tag_systems == (TagSystem.NONE,)


def test_ogg_codec_details() -> None:
    vorbis = sniff_bytes(b"OggS" + b"\x00" * 20 + b"\x01vorbis")
    opus = sniff_bytes(b"OggS" + b"\x00" * 20 + b"OpusHead")
    theora = sniff_bytes(b"OggS" + b"\x00" * 20 + b"\x80theora")

    assert vorbis.codec == "vorbis"
    assert vorbis.mime_type == "audio/ogg"
    assert opus.codec == "opus"
    assert opus.mime_type == "audio/ogg"
    assert theora.codec == "theora"
    assert theora.mime_type == "video/ogg"


def test_mp4_brand_details() -> None:
    m4a = sniff_bytes(_ftyp(b"M4A "))
    m4v = sniff_bytes(_ftyp(b"M4V "))
    mov = sniff_bytes(_ftyp(b"qt  "))
    generic = sniff_bytes(_ftyp(b"isom"))

    assert (m4a.variant, m4a.mime_type) == ("m4a", "audio/mp4")
    assert (m4v.variant, m4v.mime_type) == ("m4v", "video/mp4")
    assert (mov.variant, mov.mime_type) == ("mov", "video/quicktime")
    assert (generic.variant, generic.mime_type) == ("mp4", "video/mp4")


def test_mp4_skips_leading_free_box() -> None:
    free = struct.pack(">I", 8) + b"free"
    detected = sniff_bytes(free + _ftyp(b"M4A "))
    assert detected.container is ContainerFormat.MP4
    assert detected.variant == "m4a"


def test_mp4_extended_size_ftyp_box() -> None:
    box = (
        struct.pack(">I", 1)
        + b"ftyp"
        + struct.pack(">Q", 28)
        + b"M4A "
        + b"\x00\x00\x00\x00"
        + b"M4A "
    )
    detected = sniff_bytes(box)
    assert detected.container is ContainerFormat.MP4
    assert detected.variant == "m4a"


def test_matroska_variant() -> None:
    assert sniff_bytes(b"\x1aE\xdf\xa3" + b"matroska").variant == "matroska"
    assert sniff_bytes(b"\x1aE\xdf\xa3" + b"webm").variant == "webm"


def test_expected_tag_systems_defaults() -> None:
    assert expected_tag_systems(ContainerFormat.FLAC) == (TagSystem.VORBIS_COMMENT,)
    assert expected_tag_systems(ContainerFormat.UNKNOWN) == (TagSystem.NONE,)
    assert expected_tag_systems(ContainerFormat.MP4) == (
        TagSystem.MP4_ILST,
        TagSystem.QUICKTIME_UDTA,
    )


def test_detected_format_fills_tag_systems() -> None:
    detected = DetectedFormat(ContainerFormat.MP3, "audio/mpeg")
    assert detected.tag_systems == (
        TagSystem.ID3V2,
        TagSystem.APEV2,
        TagSystem.ID3V1,
    )


def test_detected_format_keeps_explicit_tag_systems() -> None:
    detected = DetectedFormat(
        ContainerFormat.MP3,
        "audio/mpeg",
        tag_systems=(TagSystem.ID3V1,),
    )
    assert detected.tag_systems == (TagSystem.ID3V1,)


def test_sniff_reads_file_and_keeps_extension(tmp_path: Path) -> None:
    path = tmp_path / "track.bin"
    path.write_bytes(b"\xff\xfb\x90\x64")

    detected = sniff(path)

    assert detected.container is ContainerFormat.MP3
    assert detected.extension == "bin"


def test_sniff_m4a_extension(tmp_path: Path) -> None:
    path = tmp_path / "song.m4a"
    path.write_bytes(_ftyp(b"M4A "))

    detected = sniff(path)

    assert detected.container is ContainerFormat.MP4
    assert detected.variant == "m4a"
    assert detected.extension == "m4a"


def test_sniff_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        sniff(tmp_path / "missing.mp3")

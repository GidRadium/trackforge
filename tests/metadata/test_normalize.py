"""Tests for normalization helpers."""

from __future__ import annotations

import struct

import pytest

from trackforge.metadata.model import ArtworkKind
from trackforge.metadata.normalize import (
    clean_text,
    first_text,
    parse_bool,
    parse_flac_picture,
    parse_float,
    parse_gain,
    parse_genres,
    parse_int,
    parse_number_pair,
    parse_partial_date,
    picture_kind,
    split_values,
)


class TestText:
    def test_clean_text(self) -> None:
        assert clean_text("  hello  ") == "hello"
        assert clean_text("a\x00b") == "a b"
        assert clean_text("") is None
        assert clean_text(None) is None

    def test_split_values(self) -> None:
        assert split_values("one\x00two") == ["one", "two"]
        assert split_values(["one", "two"]) == ["one", "two"]
        assert split_values("AC/DC") == ["AC/DC"]
        assert split_values(None) == []
        assert first_text(["a", "b"]) == "a"


class TestDates:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2024", "2024"),
            ("2024-05", "2024-05"),
            ("2024-05-17", "2024-05-17"),
            ("2024-05-17T12:30:00", "2024-05-17"),
            ("2024/05/17", "2024-05-17"),
        ],
    )
    def test_valid(self, raw: str, expected: str) -> None:
        parsed = parse_partial_date(raw)
        assert parsed is not None
        assert str(parsed) == expected

    @pytest.mark.parametrize("raw", ["", "not a date", "2024-13", "2024-02-30"])
    def test_invalid(self, raw: str) -> None:
        assert parse_partial_date(raw) is None


class TestNumbers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("3/12", (3, 12)),
            ("3", (3, None)),
            ("3 of 12", (3, 12)),
            ("4/0", (4, None)),
            ("0", (None, None)),
            ((3, 12), (3, 12)),
            (None, (None, None)),
            ("", (None, None)),
        ],
    )
    def test_number_pair(self, raw: object, expected: tuple[int | None, int | None]) -> None:
        assert parse_number_pair(raw) == expected

    def test_int_and_float(self) -> None:
        assert parse_int("42") == 42
        assert parse_int("nope") is None
        assert parse_int(True) is None
        assert parse_float("1.5") == 1.5
        assert parse_float("-3") == -3.0
        assert parse_gain("-3.5 dB") == -3.5
        assert parse_gain("0.0 dB") == 0.0

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (True, True),
            (0, False),
            ("yes", True),
            ("off", False),
            ("maybe", None),
        ],
    )
    def test_bool(self, raw: object, expected: bool | None) -> None:
        assert parse_bool(raw) is expected


class TestGenres:
    def test_numeric_id3_genre(self) -> None:
        assert parse_genres("(17)") == ["Rock"]

    def test_numeric_with_text(self) -> None:
        assert parse_genres("(17)Alternative") == ["Alternative"]

    def test_semicolon(self) -> None:
        assert parse_genres(["Rock; Pop"]) == ["Rock", "Pop"]

    def test_unknown_numeric_is_preserved(self) -> None:
        assert parse_genres("(200)") == ["(200)"]

    def test_none(self) -> None:
        assert parse_genres(None) == []


def _picture_block(
    *,
    picture_type: int = 3,
    mime: bytes = b"image/jpeg",
    description: bytes = b"front",
    width: int = 10,
    height: int = 20,
    data: bytes = b"jpegdata",
) -> bytes:
    return b"".join(
        [
            struct.pack(">I", picture_type),
            struct.pack(">I", len(mime)),
            mime,
            struct.pack(">I", len(description)),
            description,
            struct.pack(">IIII", width, height, 24, 0),
            struct.pack(">I", len(data)),
            data,
        ]
    )


class TestFlacPicture:
    def test_parse(self) -> None:
        artwork = parse_flac_picture(_picture_block())
        assert artwork is not None
        assert artwork.mime == "image/jpeg"
        assert artwork.kind is ArtworkKind.FRONT
        assert artwork.description == "front"
        assert artwork.width == 10
        assert artwork.height == 20
        assert artwork.data == b"jpegdata"

    def test_truncated(self) -> None:
        assert parse_flac_picture(_picture_block()[:-2]) is None

    def test_empty_data(self) -> None:
        assert parse_flac_picture(_picture_block(data=b"")) is None

    @pytest.mark.parametrize(
        ("number", "expected"),
        [
            (3, ArtworkKind.FRONT),
            (4, ArtworkKind.BACK),
            (8, ArtworkKind.ARTIST),
            (19, ArtworkKind.LOGO),
            (99, ArtworkKind.OTHER),
            ("3", ArtworkKind.FRONT),
        ],
    )
    def test_picture_kind(self, number: object, expected: ArtworkKind) -> None:
        assert picture_kind(number) is expected

"""Helpers that turn container-specific values into the normalized model."""

from __future__ import annotations

import re
import struct
from collections.abc import Iterable, Sequence

from .model import Artwork, ArtworkKind, PartialDate

_DATE_RE = re.compile(r"^\s*(\d{4})(?:[-/.](\d{1,2})(?:[-/.](\d{1,2}))?)?")
_NUMBER_PAIR_RE = re.compile(r"^\s*(\d+)(?:\s*/\s*(\d+))?\s*$")
_NUMBER_OF_RE = re.compile(r"^\s*(\d+)\s+of\s+(\d+)\s*$", re.IGNORECASE)

# The original ID3v1 genre list (0..79).  Mutagen usually resolves numeric
# genres already, but raw tags written by other tools may still contain "(17)".
ID3_GENRES: tuple[str, ...] = (
    "Blues",
    "Classic Rock",
    "Country",
    "Dance",
    "Disco",
    "Funk",
    "Grunge",
    "Hip-Hop",
    "Jazz",
    "Metal",
    "New Age",
    "Oldies",
    "Other",
    "Pop",
    "R&B",
    "Rap",
    "Reggae",
    "Rock",
    "Techno",
    "Industrial",
    "Alternative",
    "Ska",
    "Death Metal",
    "Pranks",
    "Soundtrack",
    "Euro-Techno",
    "Ambient",
    "Trip-Hop",
    "Vocal",
    "Jazz+Funk",
    "Fusion",
    "Trance",
    "Classical",
    "Instrumental",
    "Acid",
    "House",
    "Game",
    "Sound Clip",
    "Gospel",
    "Noise",
    "Alternative Rock",
    "Bass",
    "Soul",
    "Punk",
    "Space",
    "Meditative",
    "Instrumental Pop",
    "Instrumental Rock",
    "Ethnic",
    "Gothic",
    "Darkwave",
    "Techno-Industrial",
    "Electronic",
    "Pop-Folk",
    "Eurodance",
    "Dream",
    "Southern Rock",
    "Comedy",
    "Cult",
    "Gangsta",
    "Top 40",
    "Christian Rap",
    "Pop/Funk",
    "Jungle",
    "Native US",
    "Cabaret",
    "New Wave",
    "Psychadelic",
    "Rave",
    "Showtunes",
    "Trailer",
    "Lo-Fi",
    "Tribal",
    "Acid Punk",
    "Acid Jazz",
    "Polka",
    "Retro",
    "Musical",
    "Rock & Roll",
    "Hard Rock",
)

_PICTURE_KINDS: dict[int, ArtworkKind] = {
    3: ArtworkKind.FRONT,
    4: ArtworkKind.BACK,
    7: ArtworkKind.ARTIST,
    8: ArtworkKind.ARTIST,
    19: ArtworkKind.LOGO,
    20: ArtworkKind.LOGO,
}


def clean_text(value: str | None) -> str | None:
    """Strip NULs and surrounding whitespace; empty strings become ``None``."""
    if value is None:
        return None
    result = value.replace("\x00", " ").strip()
    return result or None


def split_values(value: str | Iterable[str] | None) -> list[str]:
    """Flatten a tag value into a list of non-empty strings.

    Handles ID3v2.4's NUL-separated values and the lists returned by Vorbis
    comments and MP4 atoms.  It deliberately does not split on ``/`` or ``,``:
    artist names such as ``AC/DC`` must survive.
    """
    if value is None:
        return []
    if isinstance(value, str):
        raw: list[str] = [value]
    else:
        raw = [str(item) for item in value]

    result: list[str] = []
    for item in raw:
        for part in item.split("\x00"):
            text = clean_text(part)
            if text is not None:
                result.append(text)
    return result


def first_text(value: str | Iterable[str] | None) -> str | None:
    values = split_values(value)
    return values[0] if values else None


def parse_partial_date(value: str | None) -> PartialDate | None:
    """Parse ``YYYY``, ``YYYY-MM`` or ``YYYY-MM-DD`` (a time suffix is ignored)."""
    if not value:
        return None
    match = _DATE_RE.match(value)
    if match is None:
        return None
    year = int(match.group(1))
    month = int(match.group(2)) if match.group(2) else None
    day = int(match.group(3)) if match.group(3) else None
    try:
        return PartialDate(year, month, day)
    except ValueError:
        return None


def parse_number_pair(value: object) -> tuple[int | None, int | None]:
    """Parse ``3``, ``3/12`` or ``3 of 12`` into a number/total pair."""
    if value is None:
        return (None, None)
    if isinstance(value, bool):
        return (None, None)
    if isinstance(value, int):
        return (value, None)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) == 2:
            return (_parse_int_value(value[0]), _parse_int_value(value[1]))
        return (None, None)

    text = str(value).strip()
    if not text:
        return (None, None)
    match = _NUMBER_PAIR_RE.match(text)
    if match is not None:
        return (int(match.group(1)), int(match.group(2)) if match.group(2) else None)
    match = _NUMBER_OF_RE.match(text)
    if match is not None:
        return (int(match.group(1)), int(match.group(2)))
    return (None, None)


def _parse_int_value(value: object) -> int | None:
    return parse_int(value)


def parse_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def parse_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_gain(value: object) -> float | None:
    """Parse a ReplayGain value such as ``-3.5 dB`` into a float."""
    if value is None:
        return None
    text = str(value).strip()
    if text.lower().endswith("db"):
        text = text[:-2].strip()
    return parse_float(text)


def parse_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def parse_genres(value: str | Iterable[str] | None) -> list[str]:
    """Normalize ID3 genre text and numeric references into readable names."""
    result: list[str] = []
    for item in split_values(value):
        match = re.fullmatch(r"\((\d+)\)(.*)", item)
        if match is not None:
            index = int(match.group(1))
            remainder = clean_text(match.group(2))
            if remainder is not None:
                result.extend(_split_genre_text(remainder))
            elif 0 <= index < len(ID3_GENRES):
                result.append(ID3_GENRES[index])
            else:
                result.append(item)
            continue
        result.extend(_split_genre_text(item))
    return result


def _split_genre_text(value: str) -> list[str]:
    # Semicolon is the common multi-genre convention; commas and slashes show
    # up inside legitimate genre names, so they are left alone.
    parts = [clean_text(part) for part in value.split(";")]
    return [part for part in parts if part is not None]


def parse_flac_picture(data: bytes) -> Artwork | None:
    """Parse a FLAC ``PICTURE`` block into an :class:`Artwork`."""
    if len(data) < 8:
        return None
    picture_type, mime_length = struct.unpack_from(">II", data, 0)
    offset = 8

    if offset + mime_length + 4 > len(data):
        return None
    mime = data[offset : offset + mime_length].decode("utf-8", "replace")
    offset += mime_length

    description_length = struct.unpack_from(">I", data, offset)[0]
    offset += 4
    if offset + description_length + 16 + 4 > len(data):
        return None
    description = data[offset : offset + description_length].decode("utf-8", "replace")
    offset += description_length

    width, height, _depth, _colors = struct.unpack_from(">IIII", data, offset)
    offset += 16
    image_length = struct.unpack_from(">I", data, offset)[0]
    offset += 4
    if offset + image_length > len(data):
        return None
    image = data[offset : offset + image_length]
    if not image:
        return None

    return Artwork(
        data=image,
        mime=mime,
        kind=_PICTURE_KINDS.get(picture_type, ArtworkKind.OTHER),
        description=clean_text(description),
        width=width or None,
        height=height or None,
    )


def picture_kind(picture_type: object) -> ArtworkKind:
    """Map an ID3/FLAC picture type number to :class:`ArtworkKind`."""
    try:
        number = int(picture_type)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ArtworkKind.OTHER
    return _PICTURE_KINDS.get(number, ArtworkKind.OTHER)

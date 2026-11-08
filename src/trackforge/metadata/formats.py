"""Container detection by magic bytes.

This module is intentionally dependency-free: it is the cheap first pass that
decides which backend should look at a file.  It answers *what container is
this*; tag-system details are the backend's job.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

_ASF_GUID = bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")
_ID3_HEADER_SIZE = 10
_DEFAULT_READ_SIZE = 64 * 1024


class ContainerFormat(StrEnum):
    """Physical container families understood by the metadata layer."""

    MP3 = "mp3"
    AAC = "aac"
    FLAC = "flac"
    OGG = "ogg"
    MP4 = "mp4"
    MATROSKA = "matroska"
    WAV = "wav"
    AIFF = "aiff"
    ASF = "asf"
    APE = "ape"
    WAVPACK = "wavpack"
    MUSEPACK = "musepack"
    TTA = "tta"
    TAK = "tak"
    DSF = "dsf"
    DFF = "dff"
    AVI = "avi"
    FLV = "flv"
    MPEG_TS = "mpeg_ts"
    MPEG_PS = "mpeg_ps"
    UNKNOWN = "unknown"


class TagSystem(StrEnum):
    """Tag systems that may exist inside a container."""

    ID3V1 = "id3v1"
    ID3V2 = "id3v2"
    APEV2 = "apev2"
    VORBIS_COMMENT = "vorbis_comment"
    MP4_ILST = "mp4_ilst"
    QUICKTIME_UDTA = "quicktime_udta"
    RIFF_INFO = "riff_info"
    ASF = "asf"
    MATROSKA = "matroska"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class DetectedFormat:
    """Result of sniffing a file.

    ``tag_systems`` lists the systems that *may* be present in this container,
    in the order the backend should try them.  It is not a claim that every
    listed system is actually present.
    """

    container: ContainerFormat
    mime_type: str
    codec: str | None = None
    variant: str | None = None
    brand: str | None = None
    extension: str | None = None
    tag_systems: tuple[TagSystem, ...] = ()

    def __post_init__(self) -> None:
        if not self.tag_systems:
            object.__setattr__(
                self,
                "tag_systems",
                expected_tag_systems(self.container),
            )


def expected_tag_systems(container: ContainerFormat) -> tuple[TagSystem, ...]:
    """Return the tag systems a backend should attempt for a container."""
    return _TAG_SYSTEMS.get(container, (TagSystem.NONE,))


def sniff(path: str | Path, *, read_size: int = _DEFAULT_READ_SIZE) -> DetectedFormat:
    """Detect the container of ``path`` by reading its first bytes.

    The file extension is recorded as a hint but never used to decide the
    container: ``.mp4``/``.m4a``/``.m4b`` share the same format, and an
    extension can simply be wrong.
    """
    source = Path(path)
    with source.open("rb") as handle:
        head = handle.read(read_size)

    detected = sniff_bytes(head)
    extension = source.suffix[1:].lower() if source.suffix else None
    if extension == detected.extension:
        return detected
    return replace(detected, extension=extension)


def sniff_bytes(data: bytes) -> DetectedFormat:
    """Detect a container from an in-memory prefix."""
    if not data:
        return _unknown()

    # An ID3v2 tag may legally precede an MP3/AAC stream, and is sometimes
    # found on FLAC files written by careless taggers.
    payload = _strip_id3v2(data)

    if payload.startswith(_ASF_GUID):
        return DetectedFormat(
            ContainerFormat.ASF,
            "application/vnd.ms-asf",
            variant="asf",
        )
    if payload.startswith(b"FLV\x01"):
        return DetectedFormat(ContainerFormat.FLV, "video/x-flv", variant="flv")
    if payload.startswith(b"DSD "):
        return DetectedFormat(ContainerFormat.DSF, "audio/x-dsf", variant="dsf")
    if payload.startswith(b"FRM8"):
        return DetectedFormat(ContainerFormat.DFF, "audio/x-dff", variant="dff")
    if payload.startswith(b"MAC "):
        return DetectedFormat(ContainerFormat.APE, "audio/x-ape", variant="ape")
    if payload.startswith(b"wvpk"):
        return DetectedFormat(
            ContainerFormat.WAVPACK,
            "audio/x-wavpack",
            variant="wavpack",
        )
    if payload.startswith((b"MPCK", b"MP+")):
        return DetectedFormat(
            ContainerFormat.MUSEPACK,
            "audio/x-musepack",
            variant="musepack",
        )
    if payload.startswith(b"TTA1"):
        return DetectedFormat(ContainerFormat.TTA, "audio/x-tta", variant="tta")
    if payload.startswith(b"tBaK"):
        return DetectedFormat(ContainerFormat.TAK, "audio/x-tak", variant="tak")

    if payload.startswith(b"fLaC"):
        return DetectedFormat(ContainerFormat.FLAC, "audio/flac", variant="flac")

    if payload.startswith(b"OggS"):
        return _ogg(payload)

    if payload.startswith((b"RIFF", b"RF64", b"BW64")) and len(payload) >= 12:
        riff_type = payload[8:12]
        if riff_type == b"WAVE":
            return DetectedFormat(ContainerFormat.WAV, "audio/wav", variant="wav")
        if riff_type == b"AVI ":
            return DetectedFormat(
                ContainerFormat.AVI,
                "video/x-msvideo",
                variant="avi",
            )

    if payload.startswith(b"FORM") and len(payload) >= 12:
        form_type = payload[8:12]
        if form_type in (b"AIFF", b"AIFC"):
            return DetectedFormat(
                ContainerFormat.AIFF,
                "audio/aiff",
                variant=form_type.decode("ascii").lower(),
            )

    if payload.startswith(b"\x1aE\xdf\xa3"):
        return _matroska(payload)

    ftyp = _find_ftyp(payload)
    if ftyp is not None:
        return _mp4(ftyp)

    if _is_mpeg_ts(payload):
        return DetectedFormat(
            ContainerFormat.MPEG_TS,
            "video/mp2t",
            variant="ts",
        )
    if payload.startswith(b"\x00\x00\x01\xba"):
        return DetectedFormat(
            ContainerFormat.MPEG_PS,
            "video/mpeg",
            variant="ps",
        )

    if len(payload) >= 4 and payload[0] == 0xFF and (payload[1] & 0xE0) == 0xE0:
        layer = (payload[1] >> 1) & 0x03
        if layer == 0:
            return DetectedFormat(
                ContainerFormat.AAC,
                "audio/aac",
                variant="adts",
            )
        return DetectedFormat(ContainerFormat.MP3, "audio/mpeg", variant="mp3")

    # A tag-only file (or a stream whose first frame is not in the prefix).
    if data.startswith(b"ID3"):
        return DetectedFormat(ContainerFormat.MP3, "audio/mpeg", variant="mp3")

    return _unknown()


def _unknown() -> DetectedFormat:
    return DetectedFormat(
        ContainerFormat.UNKNOWN,
        "application/octet-stream",
        variant="unknown",
        tag_systems=(TagSystem.NONE,),
    )


def _strip_id3v2(data: bytes) -> bytes:
    """Skip a leading ID3v2 tag, returning the rest of the prefix.

    Returns ``b""`` for a tag-only prefix, and the original data for a
    malformed header so the normal detection path can still run.
    """
    if len(data) < _ID3_HEADER_SIZE or data[:3] != b"ID3":
        return data
    raw_size = data[6:10]
    if any(byte & 0x80 for byte in raw_size):
        return data
    size = (
        ((raw_size[0] & 0x7F) << 21)
        | ((raw_size[1] & 0x7F) << 14)
        | ((raw_size[2] & 0x7F) << 7)
        | (raw_size[3] & 0x7F)
    )
    footer = 10 if data[3] == 4 and data[5] & 0x10 else 0
    total = _ID3_HEADER_SIZE + size + footer
    if total > len(data):
        return b""
    return data[total:]


def _ogg(data: bytes) -> DetectedFormat:
    codec: str | None = None
    if b"OpusHead" in data[:512]:
        codec = "opus"
    elif b"\x01vorbis" in data[:512]:
        codec = "vorbis"
    elif b"fLaC" in data[:512] or b"\x7fFLAC" in data[:512]:
        codec = "flac"
    elif b"\x80theora" in data[:512]:
        codec = "theora"
    elif b"Speex   " in data[:512]:
        codec = "speex"

    mime = "video/ogg" if codec == "theora" else "audio/ogg"
    return DetectedFormat(
        ContainerFormat.OGG,
        mime,
        codec=codec,
        variant=codec or "ogg",
    )


def _matroska(data: bytes) -> DetectedFormat:
    window = data[:256]
    if b"webm" in window:
        return DetectedFormat(
            ContainerFormat.MATROSKA,
            "video/webm",
            variant="webm",
        )
    return DetectedFormat(
        ContainerFormat.MATROSKA,
        "video/x-matroska",
        variant="matroska",
    )


def _find_ftyp(data: bytes) -> bytes | None:
    """Return the major brand of the first ISO BMFF ``ftyp`` box."""
    offset = 0
    for _ in range(8):
        if offset + 8 > len(data):
            return None
        size = int.from_bytes(data[offset : offset + 4], "big")
        box_type = data[offset + 4 : offset + 8]
        header_size = 8
        if size == 1:
            if offset + 16 > len(data):
                return None
            size = int.from_bytes(data[offset + 8 : offset + 16], "big")
            header_size = 16
            if size < 16:
                return None
        elif size == 0:
            return None
        elif size < 8:
            return None
        if box_type == b"ftyp":
            start = offset + header_size
            return data[start : start + 4] or None
        offset += size
    return None


def _mp4(brand: bytes) -> DetectedFormat:
    brand_text = brand.decode("latin-1").strip() or None
    if brand in (b"M4A ", b"M4B ", b"M4P "):
        variant = brand_text.lower()
        return DetectedFormat(
            ContainerFormat.MP4,
            "audio/mp4",
            variant=variant,
            brand=brand_text,
        )
    if brand == b"M4V ":
        return DetectedFormat(
            ContainerFormat.MP4,
            "video/mp4",
            variant="m4v",
            brand=brand_text,
        )
    if brand == b"qt  ":
        return DetectedFormat(
            ContainerFormat.MP4,
            "video/quicktime",
            variant="mov",
            brand=brand_text,
        )
    if brand.startswith(b"3gp"):
        return DetectedFormat(
            ContainerFormat.MP4,
            "video/3gpp",
            variant=brand_text.lower(),
            brand=brand_text,
        )
    return DetectedFormat(
        ContainerFormat.MP4,
        "video/mp4",
        variant="mp4",
        brand=brand_text,
    )


def _is_mpeg_ts(data: bytes) -> bool:
    """Detect 188-byte TS and 192-byte M2TS packets."""
    if len(data) >= 189 and data[0] == 0x47 and data[188] == 0x47:
        return True
    if len(data) >= 197 and data[4] == 0x47 and data[196] == 0x47:
        return True
    return False


_TAG_SYSTEMS: dict[ContainerFormat, tuple[TagSystem, ...]] = {
    ContainerFormat.MP3: (TagSystem.ID3V2, TagSystem.APEV2, TagSystem.ID3V1),
    ContainerFormat.AAC: (TagSystem.ID3V2, TagSystem.APEV2, TagSystem.ID3V1),
    ContainerFormat.FLAC: (TagSystem.VORBIS_COMMENT,),
    ContainerFormat.OGG: (TagSystem.VORBIS_COMMENT,),
    ContainerFormat.MP4: (TagSystem.MP4_ILST, TagSystem.QUICKTIME_UDTA),
    ContainerFormat.MATROSKA: (TagSystem.MATROSKA,),
    ContainerFormat.WAV: (TagSystem.ID3V2, TagSystem.RIFF_INFO),
    ContainerFormat.AIFF: (TagSystem.ID3V2,),
    ContainerFormat.ASF: (TagSystem.ASF,),
    ContainerFormat.APE: (TagSystem.APEV2, TagSystem.ID3V1),
    ContainerFormat.WAVPACK: (TagSystem.APEV2, TagSystem.ID3V1),
    ContainerFormat.MUSEPACK: (TagSystem.APEV2, TagSystem.ID3V1),
    ContainerFormat.TTA: (TagSystem.APEV2, TagSystem.ID3V1),
    ContainerFormat.TAK: (TagSystem.APEV2, TagSystem.ID3V1),
    ContainerFormat.DSF: (TagSystem.ID3V2,),
    ContainerFormat.DFF: (TagSystem.ID3V2,),
    ContainerFormat.AVI: (TagSystem.ID3V2, TagSystem.RIFF_INFO),
    ContainerFormat.FLV: (TagSystem.NONE,),
    ContainerFormat.MPEG_TS: (TagSystem.NONE,),
    ContainerFormat.MPEG_PS: (TagSystem.NONE,),
    ContainerFormat.UNKNOWN: (TagSystem.NONE,),
}

__all__ = [
    "ContainerFormat",
    "DetectedFormat",
    "TagSystem",
    "expected_tag_systems",
    "sniff",
    "sniff_bytes",
]

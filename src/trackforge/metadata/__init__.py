"""Normalized metadata model, container detection and public facade.

Public entry point::

    from trackforge.metadata import MediaFile

    media = MediaFile.open("track.mp3")
    print(media.title, media.audio.album)
"""

from .batch import ScanResult, discover_media_files, read_many, scan
from .capabilities import (
    Capabilities,
    FieldCapability,
    UnsupportedPolicy,
    WritePolicy,
    WriteReport,
)
from .errors import (
    InvalidMetadataError,
    MetadataError,
    MetadataReadError,
    MetadataWriteError,
    NoAudioStreamError,
    NoVideoStreamError,
    TrackForgeError,
    UnsupportedFieldError,
    UnsupportedFormatError,
    WriteVerificationError,
)
from .facade import MediaFile
from .formats import (
    ContainerFormat,
    DetectedFormat,
    TagSystem,
    expected_tag_systems,
    sniff,
    sniff_bytes,
)
from .model import (
    Artwork,
    ArtworkCollection,
    ArtworkKind,
    AudioMetadata,
    CommonMetadata,
    Lyrics,
    MediaKind,
    MediaMetadata,
    PartialDate,
    VideoMetadata,
)

__all__ = [
    "Artwork",
    "ArtworkCollection",
    "ArtworkKind",
    "AudioMetadata",
    "Capabilities",
    "CommonMetadata",
    "ContainerFormat",
    "DetectedFormat",
    "FieldCapability",
    "InvalidMetadataError",
    "Lyrics",
    "MediaFile",
    "MediaKind",
    "MediaMetadata",
    "MetadataError",
    "MetadataReadError",
    "MetadataWriteError",
    "NoAudioStreamError",
    "NoVideoStreamError",
    "PartialDate",
    "ScanResult",
    "TagSystem",
    "TrackForgeError",
    "UnsupportedFieldError",
    "UnsupportedFormatError",
    "UnsupportedPolicy",
    "VideoMetadata",
    "WritePolicy",
    "WriteReport",
    "WriteVerificationError",
    "discover_media_files",
    "expected_tag_systems",
    "read_many",
    "scan",
    "sniff",
    "sniff_bytes",
]

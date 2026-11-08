"""Normalized metadata model, container detection and public facade.

Public entry point (once the facade lands)::

    from trackforge.metadata import MediaFile

    media = MediaFile.open("track.mp3")
    print(media.title, media.audio.album)
"""

from .errors import (
    InvalidMetadataError,
    MetadataError,
    NoAudioStreamError,
    NoVideoStreamError,
    TrackForgeError,
    UnsupportedFieldError,
    UnsupportedFormatError,
    WriteVerificationError,
)
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
    "CommonMetadata",
    "ContainerFormat",
    "DetectedFormat",
    "InvalidMetadataError",
    "Lyrics",
    "MediaKind",
    "MediaMetadata",
    "MetadataError",
    "NoAudioStreamError",
    "NoVideoStreamError",
    "PartialDate",
    "TagSystem",
    "TrackForgeError",
    "UnsupportedFieldError",
    "UnsupportedFormatError",
    "VideoMetadata",
    "WriteVerificationError",
    "expected_tag_systems",
    "sniff",
    "sniff_bytes",
]

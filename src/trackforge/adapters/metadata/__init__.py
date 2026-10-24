"""External metadata providers and pipeline observers."""

from trackforge.adapters.metadata.acoustid import AcoustIDProvider, AcoustIdProvider
from trackforge.adapters.metadata.coverart import (
    CAAProvider,
    CoverArtArchiveProvider,
    CoverArtProvider,
    DeezerProvider,
    ITunesProvider,
    ItunesProvider,
    image_dimensions,
)
from trackforge.adapters.metadata.lrclib import LRCLIBProvider, LrclibProvider, parse_lrc
from trackforge.adapters.metadata.musicbrainz import (
    MusicBrainzMetadataProvider,
    MusicBrainzProvider,
    build_search_query,
    parse_musicbrainz_recording,
)
from trackforge.adapters.metadata.observers import (
    AcoustIdObserver,
    ChromaprintObserver,
    CoverArtObserver,
    LrclibObserver,
    MusicBrainzObserver,
)

__all__ = [
    "CAAProvider",
    "AcoustIDProvider",
    "AcoustIdObserver",
    "AcoustIdProvider",
    "ChromaprintObserver",
    "CoverArtArchiveProvider",
    "CoverArtObserver",
    "CoverArtProvider",
    "DeezerProvider",
    "ITunesProvider",
    "ItunesProvider",
    "LRCLIBProvider",
    "LrclibObserver",
    "LrclibProvider",
    "MusicBrainzMetadataProvider",
    "MusicBrainzObserver",
    "MusicBrainzProvider",
    "build_search_query",
    "image_dimensions",
    "parse_lrc",
    "parse_musicbrainz_recording",
]

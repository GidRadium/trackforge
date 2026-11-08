"""Shared mutagen file selection.

Both the reader and the writer need the same container-to-class mapping and
the same notion of which containers are writable.  Keeping them here avoids a
static import cycle between :mod:`mutagen` and :mod:`mutagen_write`.

The concrete mutagen classes are deliberately treated as dynamic: mutagen
exposes some of them with only partial annotations, so the maps stay typed as
``type[Any]``.
"""

# pyright: standard

from __future__ import annotations

from pathlib import Path
from typing import Any

from mutagen import MutagenError
from mutagen._vorbis import VCommentDict
from mutagen.aac import AAC
from mutagen.aiff import AIFF
from mutagen.asf import ASF
from mutagen.dsdiff import DSDIFF
from mutagen.dsf import DSF
from mutagen.flac import FLAC
from mutagen.monkeysaudio import MonkeysAudio
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.musepack import Musepack
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggspeex import OggSpeex
from mutagen.oggtheora import OggTheora
from mutagen.oggvorbis import OggVorbis
from mutagen.trueaudio import TrueAudio
from mutagen.wave import WAVE
from mutagen.wavpack import WavPack

from ..errors import MetadataReadError, UnsupportedFormatError
from ..formats import ContainerFormat, DetectedFormat

FILE_CLASSES: dict[ContainerFormat, type[Any]] = {
    ContainerFormat.MP3: MP3,
    ContainerFormat.AAC: AAC,
    ContainerFormat.FLAC: FLAC,
    ContainerFormat.MP4: MP4,
    ContainerFormat.WAV: WAVE,
    ContainerFormat.AIFF: AIFF,
    ContainerFormat.ASF: ASF,
    ContainerFormat.APE: MonkeysAudio,
    ContainerFormat.WAVPACK: WavPack,
    ContainerFormat.MUSEPACK: Musepack,
    ContainerFormat.TTA: TrueAudio,
    ContainerFormat.DSF: DSF,
    ContainerFormat.DFF: DSDIFF,
}

_OGG_CLASSES: dict[str, type[Any]] = {
    "vorbis": OggVorbis,
    "opus": OggOpus,
    "flac": OggFLAC,
    "speex": OggSpeex,
    "theora": OggTheora,
}

WRITABLE_CONTAINERS = frozenset(
    {
        ContainerFormat.MP3,
        ContainerFormat.AAC,
        ContainerFormat.FLAC,
        ContainerFormat.OGG,
        ContainerFormat.MP4,
        ContainerFormat.WAV,
        ContainerFormat.AIFF,
        ContainerFormat.DSF,
        ContainerFormat.DFF,
        ContainerFormat.ASF,
        ContainerFormat.APE,
        ContainerFormat.WAVPACK,
        ContainerFormat.MUSEPACK,
        ContainerFormat.TTA,
    }
)


def open_audio_file(path: Path, detected: DetectedFormat) -> Any:
    """Open ``path`` with the mutagen class selected by ``detected``."""
    if detected.container == ContainerFormat.OGG:
        file_class = _OGG_CLASSES.get(detected.codec or "")
        if file_class is None:
            raise UnsupportedFormatError(str(path))
    else:
        file_class = FILE_CLASSES.get(detected.container)
        if file_class is None:
            raise UnsupportedFormatError(str(path))

    try:
        return file_class(path)
    except (MutagenError, OSError, ValueError) as exc:
        raise MetadataReadError(str(path), str(exc)) from exc


__all__ = [
    "WRITABLE_CONTAINERS",
    "VCommentDict",
    "open_audio_file",
]

"""Mutagen-based tag reading and writing.

Mutagen is GPL-2.0-or-later and is an optional extra.  It is imported lazily
inside this package so TrackForge can start and report a visible degradation
when the extra is not installed.
"""

from __future__ import annotations

from trackforge.adapters.tagging.mutagen.reader import MutagenTagReader
from trackforge.adapters.tagging.mutagen.writer import MutagenTagWriter

__all__ = ["MutagenTagReader", "MutagenTagWriter"]

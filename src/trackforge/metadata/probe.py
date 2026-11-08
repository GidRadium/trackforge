"""Optional stream probing with ffprobe.

Container sniffing cannot tell whether an MP4 has an audio track, and
mutagen's MP4 reader only exposes audio properties.  When ffprobe is
available we use it to correct the audio/video facets; when it is not, the
facade falls back to the format heuristic.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast


@dataclass(frozen=True, slots=True)
class StreamProbe:
    audio_streams: int
    video_streams: int

    @property
    def has_audio(self) -> bool:
        return self.audio_streams > 0

    @property
    def has_video(self) -> bool:
        return self.video_streams > 0


def probe_streams(path: str | Path, *, timeout: float = 10.0) -> StreamProbe | None:
    """Return stream counts, or ``None`` if ffprobe is unavailable/fails."""
    executable = shutil.which("ffprobe")
    if executable is None:
        return None
    command = [
        executable,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        parsed: object = json.loads(completed.stdout)  # pyright: ignore[reportAny]
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    data = cast("dict[str, object]", parsed)
    streams = data.get("streams")
    if not isinstance(streams, list):
        return None

    audio = 0
    video = 0
    for raw_stream in cast("list[object]", streams):
        if not isinstance(raw_stream, dict):
            continue
        stream = cast("dict[str, object]", raw_stream)
        codec_type = stream.get("codec_type")
        if codec_type == "audio":
            audio += 1
        elif codec_type == "video":
            raw_disposition = stream.get("disposition")
            disposition = (
                cast("dict[str, object]", raw_disposition)
                if isinstance(raw_disposition, dict)
                else {}
            )
            if not disposition.get("attached_pic"):
                video += 1
    return StreamProbe(audio_streams=audio, video_streams=video)


__all__ = ["StreamProbe", "probe_streams"]

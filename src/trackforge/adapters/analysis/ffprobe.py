"""ffprobe-backed media probe capability.

``ffprobe`` is invoked as an argument list (never through a shell) with a hard
timeout.  Tags are deliberately not promoted to observations: the probe
payload contains measured container/stream facts only.
"""

from __future__ import annotations

import asyncio
import json
import math
import shutil
import subprocess  # noqa: S404 - explicit argument list, no shell
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trackforge.application.dto.observations import (
    AudioProbePayload,
    ProbeChapter,
    ProbePicture,
    ProbeStream,
)
from trackforge.application.ports.execution import (
    ArtifactDraft,
    StepContext,
    StepResult,
)
from trackforge.application.ports.observations import (
    MEDIA_ARTIFACT,
    PROBE_ARTIFACT,
    media_input,
)
from trackforge.domain.errors import DomainValidationError, ProbeFailed, ProviderUnavailable
from trackforge.domain.model.capabilities import (
    CapabilityKind,
    CapabilityManifest,
    Cost,
    Determinism,
    SideEffect,
)

_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_STDERR_BYTES = 8192


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0.0 else None


def _integer(value: object) -> int | None:
    number = _number(value)
    if number is None:
        return None
    return int(number)


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> list[object]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _streams(document: Mapping[str, Any]) -> tuple[list[ProbeStream], list[ProbePicture]]:
    streams: list[ProbeStream] = []
    pictures: list[ProbePicture] = []
    for index, raw in enumerate(_sequence(document.get("streams"))):
        stream = _mapping(raw)
        disposition = _mapping(stream.get("disposition"))
        tags = _mapping(stream.get("tags"))
        attached = bool(disposition.get("attached_pic"))
        normalized = ProbeStream(
            index=_integer(stream.get("index")) if stream.get("index") is not None else index,
            codec_type=_text(stream.get("codec_type")),
            codec_name=_text(stream.get("codec_name")),
            sample_rate=_integer(stream.get("sample_rate")),
            channels=_integer(stream.get("channels")),
            bit_rate=_integer(stream.get("bit_rate")),
            duration_seconds=_number(stream.get("duration")),
            width=_integer(stream.get("width")),
            height=_integer(stream.get("height")),
            channel_layout=_text(stream.get("channel_layout")),
        )
        streams.append(normalized)
        if attached:
            pictures.append(
                ProbePicture(
                    index=normalized.index,
                    codec_name=normalized.codec_name,
                    mime_type=_text(tags.get("mimetype")),
                    width=normalized.width,
                    height=normalized.height,
                    description=_text(tags.get("title")) or _text(tags.get("comment")),
                )
            )
    return streams, pictures


def _chapters(document: Mapping[str, Any]) -> list[ProbeChapter]:
    result: list[ProbeChapter] = []
    for raw in _sequence(document.get("chapters")):
        chapter = _mapping(raw)
        tags = _mapping(chapter.get("tags"))
        result.append(
            ProbeChapter(
                id=_integer(chapter.get("id")),
                start_seconds=_number(chapter.get("start_time")),
                end_seconds=_number(chapter.get("end_time")),
                title=_text(tags.get("title")),
            )
        )
    return result


def parse_ffprobe_document(
    document: Mapping[str, Any],
    *,
    container: str | None = None,
) -> AudioProbePayload:
    """Normalize one parsed ``ffprobe -of json`` document.

    The function is pure so format quirks can be tested without spawning a
    process.
    """

    if not isinstance(document, Mapping):
        raise ProbeFailed(
            "ffprobe returned a non-object JSON document",
            user_action="Check that ffprobe is installed and the file is valid audio.",
        )
    raw_format = _mapping(document.get("format"))
    format_name = _text(raw_format.get("format_name"))
    if format_name is None:
        raise ProbeFailed(
            "ffprobe produced no format information",
            user_action="Check that the file is a valid, readable audio file.",
        )
    streams, pictures = _streams(document)
    return AudioProbePayload(
        container=container or format_name.split(",")[0],
        format_name=format_name,
        format_long_name=_text(raw_format.get("format_long_name")),
        duration_seconds=_number(raw_format.get("duration")),
        size_bytes=_integer(raw_format.get("size")),
        bit_rate=_integer(raw_format.get("bit_rate")),
        streams=streams,
        chapters=_chapters(document),
        attached_pictures=pictures,
    )


class FfprobeProbe:
    """Run ``ffprobe`` against the CAS copy of a media object."""

    manifest = CapabilityManifest(
        id="ffprobe",
        version="1",
        kind=CapabilityKind.OBSERVER,
        requires=(MEDIA_ARTIFACT,),
        provides=(PROBE_ARTIFACT,),
        cost=Cost(cpu_seconds=0.2, memory_mb=32.0),
        determinism=Determinism.DETERMINISTIC,
        cacheable=True,
        side_effects=SideEffect.NONE,
        description="Measure container and stream properties with ffprobe.",
    )

    def __init__(
        self,
        executable: str = "ffprobe",
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_stderr_bytes: int = _DEFAULT_MAX_STDERR_BYTES,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_stderr_bytes < 1:
            raise ValueError("max_stderr_bytes must be positive")
        self._executable = executable
        self._timeout_seconds = timeout_seconds
        self._max_stderr_bytes = max_stderr_bytes

    async def run(self, ctx: StepContext) -> StepResult:
        media_step = ctx.inputs.get(MEDIA_ARTIFACT)
        if media_step is None:
            raise DomainValidationError("ffprobe requires a media input artifact")
        media = media_input(media_step)
        path = Path(media.cas_path)
        if not path.is_file():
            raise ProbeFailed(
                "media bytes are missing from the content-addressed store",
                user_action="Re-acquire the source file.",
                context={"path": str(path)},
            )
        document = await asyncio.to_thread(self._invoke, path)
        payload = parse_ffprobe_document(document, container=media.container)
        return StepResult(
            artifacts=(
                ArtifactDraft(
                    type=PROBE_ARTIFACT,
                    payload=payload.model_dump(mode="json"),
                ),
            ),
            actual_cost=Cost(cpu_seconds=0.2, memory_mb=32.0),
        )

    def _resolve_executable(self) -> str:
        executable = self._executable
        if Path(executable).is_file():
            return executable
        found = shutil.which(executable)
        if found is None:
            raise ProviderUnavailable(
                "ffprobe executable was not found",
                retryable=False,
                user_action="Install ffmpeg/ffprobe and ensure ffprobe is on PATH.",
                context={"executable": executable},
            )
        return found

    def _invoke(self, path: Path) -> Mapping[str, Any]:
        executable = self._resolve_executable()
        command = [
            executable,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            "-of",
            "json",
            str(path),
        ]
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed argument list, shell=False
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise ProviderUnavailable(
                "ffprobe executable was not found",
                retryable=False,
                user_action="Install ffmpeg/ffprobe and ensure ffprobe is on PATH.",
                context={"executable": executable},
            ) from exc
        except OSError as exc:
            raise ProviderUnavailable(
                "ffprobe could not be started",
                retryable=False,
                user_action="Check the ffprobe installation and permissions.",
                context={"executable": executable, "error": str(exc)},
            ) from exc

        try:
            stdout, stderr = process.communicate(timeout=self._timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()
            raise ProviderUnavailable(
                f"ffprobe timed out after {self._timeout_seconds:g}s",
                retryable=True,
                user_action="Check the file and retry; very large files may need a longer timeout.",
                context={"path": str(path)},
            ) from exc

        stderr_text = stderr[: self._max_stderr_bytes].decode("utf-8", errors="replace")
        if len(stderr) > self._max_stderr_bytes:
            stderr_text += "\n[stderr truncated]"
        if process.returncode != 0:
            raise ProbeFailed(
                "ffprobe could not read the media file",
                retryable=False,
                user_action="Check that the file is a valid, readable audio file.",
                context={
                    "path": str(path),
                    "returncode": process.returncode,
                    "stderr": stderr_text,
                },
            )
        if not stdout:
            raise ProbeFailed(
                "ffprobe returned no data",
                user_action="Check that the file is a valid, readable audio file.",
                context={"path": str(path), "stderr": stderr_text},
            )
        try:
            document = json.loads(stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProbeFailed(
                "ffprobe returned invalid JSON",
                user_action="Check the ffprobe version and the source file.",
                context={"path": str(path), "error": str(exc)},
            ) from exc
        if not isinstance(document, Mapping):
            raise ProbeFailed(
                "ffprobe returned an unexpected JSON document",
                user_action="Check the ffprobe version and the source file.",
                context={"path": str(path)},
            )
        return document


__all__ = ["FfprobeProbe", "parse_ffprobe_document"]

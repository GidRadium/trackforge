"""ffmpeg-backed remux/transcode adapter.

The renderer only produces audio.  Tags, cover art, and lyrics are written by
the tagging adapter afterwards.  Output is staged in a sibling temporary file
and committed with ``fsync`` + ``rename`` so a failed transcode cannot leave a
half-written bundle.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # noqa: S404 - explicit argument list, no shell
import uuid
from pathlib import Path

from trackforge.application.dto.rendering import (
    RenderedAudio,
    RenderOptions,
    extension_for,
    is_supported_container,
    target_format_for,
)
from trackforge.domain.errors import RenderFailed
from trackforge.domain.model.media import MediaObject
from trackforge.domain.model.outputs import ResolvedMetadata, TargetFormat

_DEFAULT_TIMEOUT_SECONDS = 600.0
_MAX_STDERR_CHARS = 4_000
_OGG_FIXED_SERIAL = 1

_CODEC_NAMES: dict[TargetFormat, str] = {
    TargetFormat.MP3: "mp3",
    TargetFormat.M4A: "aac",
    TargetFormat.FLAC: "flac",
    TargetFormat.OGG: "vorbis",
    TargetFormat.OPUS: "opus",
}

_DEFAULT_BITRATES: dict[TargetFormat, int] = {
    TargetFormat.MP3: 192_000,
    TargetFormat.M4A: 192_000,
    TargetFormat.OPUS: 128_000,
}


class FfmpegRenderer:
    """Copy same-format audio or transcode into one of the five MVP formats."""

    def __init__(
        self,
        *,
        ffmpeg_binary: str = "ffmpeg",
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise RenderFailed("ffmpeg timeout must be positive")
        self._ffmpeg = ffmpeg_binary
        self._timeout = float(timeout_seconds)

    def render(
        self,
        media: MediaObject,
        decisions: ResolvedMetadata,
        options: RenderOptions,
        workdir: Path,
    ) -> RenderedAudio:
        """Materialize ``media`` in ``workdir`` without touching the CAS file."""

        del decisions  # resolution is already reflected in the tag write step
        if not isinstance(media, MediaObject):
            raise RenderFailed("renderer requires a MediaObject")
        binary = shutil.which(self._ffmpeg)
        if binary is None:
            raise RenderFailed(
                "ffmpeg is not installed or not on PATH",
                user_action="Install ffmpeg and ensure it is available on PATH.",
            )

        source = Path(media.cas_path)
        if not source.is_file():
            raise RenderFailed(
                "media bytes are missing from the content-addressed store",
                context={"path": str(source)},
            )

        target = target_format_for(media.container, options.target_format)
        directory = Path(workdir)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RenderFailed(
                "render workspace could not be created",
                user_action="Check workspace permissions and available space.",
                context={"workdir": str(directory), "error": str(exc)},
            ) from exc
        output = directory / f"track.{extension_for(target)}"
        if _same_file(source, output):
            raise RenderFailed(
                "refusing to render over the source media object",
                context={"source": str(source), "output": str(output)},
            )

        temporary = directory / f".track-{uuid.uuid4().hex}.{extension_for(target)}"
        command = self._command(
            binary,
            source=source,
            output=temporary,
            target=target,
            media=media,
            options=options,
        )
        try:
            completed = subprocess.run(  # noqa: S603 - explicit arg list, shell=False
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            temporary.unlink(missing_ok=True)
            raise RenderFailed(
                "ffmpeg timed out while rendering audio",
                retryable=True,
                context={"timeout_seconds": self._timeout, "target": target.value},
            ) from exc
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise RenderFailed(
                "ffmpeg could not be executed",
                context={"error": str(exc)},
            ) from exc

        if completed.returncode != 0:
            temporary.unlink(missing_ok=True)
            raise RenderFailed(
                "ffmpeg failed to render audio",
                user_action="Check that the source audio is valid and the format is supported.",
                context={
                    "target_format": target.value,
                    "returncode": completed.returncode,
                    "stderr": _redacted_stderr(
                        completed.stderr,
                        source=source,
                        workdir=directory,
                    ),
                },
            )
        if not temporary.is_file() or temporary.stat().st_size == 0:
            temporary.unlink(missing_ok=True)
            raise RenderFailed(
                "ffmpeg produced no audio output",
                context={"target_format": target.value},
            )
        try:
            if target in {TargetFormat.OGG, TargetFormat.OPUS}:
                _normalize_ogg_serial(temporary)
            _fsync_file(temporary)
            os.replace(temporary, output)
            _fsync_directory(directory)
        except RenderFailed:
            temporary.unlink(missing_ok=True)
            raise
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise RenderFailed(
                "rendered audio could not be committed to the workspace",
                user_action="Check workspace permissions and available space.",
                context={"workdir": str(directory), "error": str(exc)},
            ) from exc
        return RenderedAudio(
            path=output.resolve(),
            format=target,
            codec=_CODEC_NAMES[target],
            sample_rate=media.sample_rate,
            channels=media.channels,
            bitrate=(
                _target_bitrate(target, media.bitrate)
                if target in _DEFAULT_BITRATES
                else media.bitrate
            ),
        )

    def _command(
        self,
        binary: str,
        *,
        source: Path,
        output: Path,
        target: TargetFormat,
        media: MediaObject,
        options: RenderOptions,
    ) -> list[str]:
        source_target = target_format_for(media.container)
        command = [
            binary,
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
        ]
        command.extend(
            ["-map_metadata", "0"] if options.preserve_unknown_tags else ["-map_metadata", "-1"]
        )
        if is_supported_container(media.container) and target is source_target:
            command.extend(["-c:a", "copy"])
        else:
            command.extend(_transcode_arguments(target, media.bitrate))
        if target is TargetFormat.M4A:
            command.extend(["-movflags", "+faststart"])
        command.append(str(output))
        return command


def _transcode_arguments(target: TargetFormat, source_bitrate: int | None) -> list[str]:
    if target is TargetFormat.MP3:
        return ["-c:a", "libmp3lame", "-b:a", str(_target_bitrate(target, source_bitrate))]
    if target is TargetFormat.M4A:
        return ["-c:a", "aac", "-b:a", str(_target_bitrate(target, source_bitrate))]
    if target is TargetFormat.FLAC:
        return ["-c:a", "flac"]
    if target is TargetFormat.OGG:
        return ["-c:a", "libvorbis", "-q:a", "5"]
    if target is TargetFormat.OPUS:
        return ["-c:a", "libopus", "-b:a", str(_target_bitrate(target, source_bitrate))]
    raise RenderFailed(
        "unsupported render target",
        context={"target_format": target.value},
    )


def _target_bitrate(target: TargetFormat, source_bitrate: int | None) -> int:
    default = _DEFAULT_BITRATES[target]
    if source_bitrate is None or source_bitrate <= 0:
        return default
    # Never deliberately reduce a lossy source's bitrate.  Lossless sources
    # have a high nominal bitrate, so cap the result at the format default.
    if source_bitrate >= 512_000:
        return default
    return max(default, source_bitrate)


def _normalize_ogg_serial(path: Path, serial: int = _OGG_FIXED_SERIAL) -> None:
    """Give every Ogg page a fixed serial number and recompute its CRC.

    The Ogg muxer picks a random stream serial for every run.  Without this
    normalization two renders of identical decisions would produce different
    bytes (and therefore different bundle hashes) even though the audio is the
    same.
    """

    data = path.read_bytes()
    output = bytearray()
    offset = 0
    while offset < len(data):
        if data[offset : offset + 4] != b"OggS":
            raise RenderFailed(
                "ffmpeg produced a malformed Ogg stream",
                context={"offset": offset},
            )
        if offset + 27 > len(data):
            raise RenderFailed("ffmpeg produced a truncated Ogg header")
        segment_count = data[offset + 26]
        table_start = offset + 27
        table_end = table_start + segment_count
        if table_end > len(data):
            raise RenderFailed("ffmpeg produced a truncated Ogg segment table")
        body_length = sum(data[table_start:table_end])
        page_end = table_end + body_length
        if page_end > len(data):
            raise RenderFailed("ffmpeg produced a truncated Ogg page")
        page = bytearray(data[offset:page_end])
        page[14:18] = int(serial).to_bytes(4, "little")
        page[22:26] = b"\x00\x00\x00\x00"
        page[22:26] = _ogg_crc(bytes(page)).to_bytes(4, "little")
        output.extend(page)
        offset = page_end
    if not output:
        raise RenderFailed("ffmpeg produced an empty Ogg stream")
    temporary = path.with_name(f".{path.name}.serial")
    try:
        with temporary.open("wb") as stream:
            stream.write(output)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _ogg_crc(data: bytes) -> int:
    table = _OGG_CRC_TABLE
    checksum = 0
    for byte in data:
        checksum = ((checksum << 8) & 0xFFFFFFFF) ^ table[((checksum >> 24) & 0xFF) ^ byte]
    return checksum


def _build_ogg_crc_table() -> tuple[int, ...]:
    table: list[int] = []
    for value in range(256):
        current = value << 24
        for _ in range(8):
            if current & 0x80000000:
                current = ((current << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                current = (current << 1) & 0xFFFFFFFF
        table.append(current)
    return tuple(table)


_OGG_CRC_TABLE = _build_ogg_crc_table()


def _redacted_stderr(
    stderr: str | None,
    *,
    source: Path,
    workdir: Path,
) -> str:
    text = (stderr or "").strip()
    if not text:
        return ""
    text = text.replace(str(source), "<source>")
    text = text.replace(str(workdir), "<workdir>")
    return text[-_MAX_STDERR_CHARS:]


def _same_file(first: Path, second: Path) -> bool:
    try:
        return first.resolve() == second.resolve() or (
            second.exists() and os.path.samefile(first, second)
        )
    except OSError:
        return False


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


__all__ = ["FfmpegRenderer"]

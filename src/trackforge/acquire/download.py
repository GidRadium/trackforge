"""Low-level download helpers shared by the yt-dlp adapter.

This module keeps the mechanics that are easy to get subtly wrong in one
place: progress-hook mapping, postprocessor selection, ffmpeg/ffprobe
preflight, real output discovery and the Yandex preview heuristic.  It does
not import :mod:`yt_dlp` at module import time.
"""

# pyright: standard

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from .errors import DownloadFailed, PostprocessError
from .models import (
    DownloadOptions,
    DownloadPlan,
    DownloadVariant,
    ItemPlan,
    MediaKind,
)
from .planner import YtDlpPlanner
from .progress import CancellationToken, ProgressEvent, ProgressSink, ProgressStage

PREVIEW_MIN_SECONDS: Final = 13.0
PREVIEW_MAX_SECONDS: Final = 30.0

_MEDIA_EXTENSIONS: Final = frozenset(
    {
        "3gp",
        "aac",
        "aif",
        "aifc",
        "aiff",
        "alac",
        "ape",
        "asf",
        "avi",
        "caf",
        "flac",
        "flv",
        "gif",
        "m2ts",
        "m4a",
        "m4b",
        "m4v",
        "mka",
        "mkv",
        "mov",
        "mp2",
        "mp3",
        "mp4",
        "mpeg",
        "mpg",
        "mts",
        "mxf",
        "oga",
        "ogg",
        "ogm",
        "ogv",
        "opus",
        "ts",
        "vob",
        "wav",
        "webm",
        "webp",
        "wma",
        "wmv",
    }
)
_IMAGE_EXTENSIONS: Final = frozenset({"gif", "jpeg", "jpg", "png", "webp"})
_SIDECAR_EXTENSIONS: Final = frozenset(
    {
        "ass",
        "description",
        "json",
        "json3",
        "jpeg",
        "jpg",
        "lrc",
        "png",
        "srt",
        "ttml",
        "txt",
        "vtt",
        "webp",
    }
)
_TEMP_SUFFIXES: Final = frozenset({".part", ".ytdl", ".temp", ".tmp"})

_POSTPROCESSOR_STAGES: Final = {
    # yt-dlp reports short PEP8 class names through the hook, while the
    # architecture/tests often use the CLI-facing ``FFmpeg*`` names.
    "FFmpegMerger": ProgressStage.MERGE,
    "Merger": ProgressStage.MERGE,
    "FFmpegExtractAudio": ProgressStage.EXTRACT_AUDIO,
    "ExtractAudio": ProgressStage.EXTRACT_AUDIO,
    "FFmpegVideoRemuxer": ProgressStage.REMUX,
    "VideoRemuxer": ProgressStage.REMUX,
    "FFmpegVideoConvertor": ProgressStage.CONVERT,
    "VideoConvertor": ProgressStage.CONVERT,
    "FFmpegSubtitlesConvertor": ProgressStage.SUBTITLES,
    "SubtitlesConvertor": ProgressStage.SUBTITLES,
    "MoveFiles": ProgressStage.MOVE,
}


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class YtDlpProgressHook:
    """Translate yt-dlp progress dicts into :class:`ProgressEvent` objects.

    The hook also owns cooperative cancellation: raising yt-dlp's
    ``DownloadCancelled`` from a hook is the documented way to abort a
    download mid-flight, and the adapter converts it to ``CancelledError``.
    """

    def __init__(
        self,
        sink: ProgressSink,
        cancel: CancellationToken,
        plan: DownloadPlan,
        item_plan: ItemPlan,
        item_index: int,
    ) -> None:
        self._sink = sink
        self._cancel = cancel
        self._plan = plan
        self._item_plan = item_plan
        self._item_index = item_index
        self._item_count = len(plan.items)
        self.warnings: list[str] = []

    @property
    def item_index(self) -> int:
        return self._item_index

    @property
    def item_count(self) -> int:
        return self._item_count

    def check_cancelled(self) -> None:
        """Abort the current yt-dlp call when the token was cancelled."""
        if not self._cancel.cancelled:
            return
        from yt_dlp.utils import DownloadCancelled

        raise DownloadCancelled("download cancelled by user")

    def _event(self, stage: ProgressStage, **fields: Any) -> ProgressEvent:
        return ProgressEvent(
            stage=stage,
            job_id=self._plan.job_id,
            item_index=self._item_index,
            item_count=self._item_count,
            **fields,
        )

    def download(self, data: Mapping[str, Any]) -> None:
        """Handle one yt-dlp ``progress_hooks`` call."""
        self.check_cancelled()
        status = data.get("status")
        if status == "downloading":
            total = _int_or_none(data.get("total_bytes")) or _int_or_none(
                data.get("total_bytes_estimate")
            )
            self._sink.emit(
                self._event(
                    ProgressStage.DOWNLOAD,
                    filename=_text_or_none(data.get("filename")),
                    downloaded_bytes=_int_or_none(data.get("downloaded_bytes")),
                    total_bytes=total,
                    speed=_float_or_none(data.get("speed")),
                    eta=_float_or_none(data.get("eta")),
                    fragment_index=_int_or_none(data.get("fragment_index")),
                    fragment_count=_int_or_none(data.get("fragment_count")),
                )
            )
        elif status == "finished":
            # ``total_bytes`` may be unknown here; keep it ``None`` rather
            # than inventing a 100% completion.
            total = _int_or_none(data.get("total_bytes"))
            if total is None:
                total = _int_or_none(data.get("total_bytes_estimate"))
            self._sink.emit(
                self._event(
                    ProgressStage.DOWNLOAD,
                    filename=_text_or_none(data.get("filename")),
                    downloaded_bytes=_int_or_none(data.get("downloaded_bytes")),
                    total_bytes=total,
                )
            )
        elif status == "error":
            self._sink.emit(
                self._event(
                    ProgressStage.DOWNLOAD,
                    filename=_text_or_none(data.get("filename")),
                    message="download error",
                )
            )

    def postprocessor(self, data: Mapping[str, Any]) -> None:
        """Handle one yt-dlp ``postprocessor_hooks`` call."""
        self.check_cancelled()
        name = str(data.get("postprocessor") or "")
        stage = _POSTPROCESSOR_STAGES.get(name, ProgressStage.CONVERT)
        self._sink.emit(
            self._event(
                stage,
                message=_text_or_none(data.get("status")),
                filename=_text_or_none(data.get("filename")),
            )
        )


def _text_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def build_postprocessors(
    item_plan: ItemPlan,
    options: DownloadOptions,
) -> list[dict[str, object]]:
    """Return only the postprocessors L2 is allowed to use.

    Tagging and thumbnail embedding are deliberately never added: TrackForge
    owns metadata in the ingest stage.
    """
    variant = item_plan.variant
    container = _normalise_container(options.container)
    postprocessors: list[dict[str, object]] = []

    needs_audio_extract = options.audio_only or (
        variant.kind is MediaKind.AUDIO
        and container is not None
        and container != _normalise_container(variant.container)
    )
    if needs_audio_extract:
        postprocessors.append(
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": container or "best",
                "preferredquality": "0",
            }
        )
    elif (
        container is not None
        and variant.kind in {MediaKind.AUDIO_VIDEO, MediaKind.VIDEO}
        and not variant.requires_merge
        and container != _normalise_container(variant.container)
    ):
        postprocessors.append(
            {
                "key": "FFmpegVideoRemuxer",
                "preferedformat": container,
            }
        )

    if options.subtitles_to_lrc:
        postprocessors.append(
            {
                "key": "FFmpegSubtitlesConvertor",
                "format": "lrc",
            }
        )
    return postprocessors


def _normalise_container(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().lower().lstrip(".")
    return text or None


def requires_ffmpeg(item_plan: ItemPlan, options: DownloadOptions) -> bool:
    """Whether preparing this item cannot work without ffmpeg."""
    if item_plan.variant.requires_merge:
        return True
    if item_plan.variant.requires_ffmpeg:
        return True
    return bool(build_postprocessors(item_plan, options))


def ensure_ffmpeg() -> None:
    """Raise a typed error when ffmpeg is missing."""
    if shutil.which("ffmpeg") is None:
        raise PostprocessError(
            "ffmpeg is required to merge, remux or extract this variant",
            hint=(
                "install ffmpeg and make sure it is on PATH; "
                "run `tfg doctor` to verify"
            ),
        )


def ensure_ffprobe() -> None:
    """Raise a typed error when ffprobe is missing."""
    if shutil.which("ffprobe") is None:
        raise PostprocessError(
            "ffprobe is required to verify the downloaded duration",
            hint=(
                "install ffmpeg/ffprobe and make sure it is on PATH; "
                "run `tfg doctor` to verify"
            ),
        )


def probe_media_duration(
    path: Path,
    *,
    ffprobe: str | None = None,
    timeout: float = 20.0,
) -> float:
    """Return the container duration in seconds using ffprobe."""
    binary = ffprobe or shutil.which("ffprobe")
    if binary is None:
        raise PostprocessError(
            "ffprobe was not found",
            hint="install ffmpeg/ffprobe and ensure it is on PATH",
        )
    command = [
        binary,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
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
    except subprocess.TimeoutExpired as exc:
        raise PostprocessError(
            f"ffprobe timed out while checking {path.name}",
            hint="the downloaded file may be corrupt; retry the download",
        ) from exc
    except OSError as exc:
        raise PostprocessError(
            f"cannot run ffprobe: {exc}",
            hint="check the ffmpeg/ffprobe installation",
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "ffprobe failed"
        raise PostprocessError(
            f"cannot probe {path.name}: {detail}",
            hint="the downloaded file may be corrupt; retry the download",
        )
    raw = completed.stdout.strip().splitlines()
    if not raw:
        raise PostprocessError(f"ffprobe returned no duration for {path.name}")
    try:
        return float(raw[0])
    except ValueError as exc:
        raise PostprocessError(
            f"ffprobe returned an invalid duration for {path.name}: {raw[0]!r}"
        ) from exc


def looks_like_preview(actual: float | None, expected: float | None) -> bool:
    """Return whether ``actual`` is a short preview of a longer ``expected``."""
    if actual is None:
        return False
    if not (PREVIEW_MIN_SECONDS <= actual <= PREVIEW_MAX_SECONDS):
        return False
    if expected is None:
        return True
    if expected <= PREVIEW_MAX_SECONDS:
        # A genuinely short track is not a preview when it matches metadata.
        return False
    return expected > actual + max(10.0, actual * 0.15)


# Short alias used by callers that only care about the duration probe.
probe_duration = probe_media_duration


def _is_temp(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in _TEMP_SUFFIXES)


def _is_sidecar(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith(".info.json"):
        return True
    suffix = path.suffix.lower().lstrip(".")
    return suffix in _SIDECAR_EXTENSIONS


def _is_media(path: Path, variant: DownloadVariant) -> bool:
    suffix = path.suffix.lower().lstrip(".")
    if suffix not in _MEDIA_EXTENSIONS:
        return False
    if path.name.lower().endswith(".info.json"):
        return False
    if variant.kind in {MediaKind.IMAGE, MediaKind.ANIMATED_IMAGE}:
        return suffix in _IMAGE_EXTENSIONS
    if variant.kind is MediaKind.DOCUMENT:
        return True
    # For audio/video, an image next to the media is a thumbnail sidecar.
    return suffix not in _IMAGE_EXTENSIONS


def _is_reported_media(path: Path, variant: DownloadVariant) -> bool:
    """Accept a path yt-dlp itself reported, even for an unknown extension."""
    name = path.name.lower()
    if name.endswith(".info.json"):
        return False
    suffix = path.suffix.lower().lstrip(".")
    if variant.kind in {MediaKind.IMAGE, MediaKind.ANIMATED_IMAGE}:
        return suffix in _IMAGE_EXTENSIONS or suffix not in _SIDECAR_EXTENSIONS
    if variant.kind is MediaKind.DOCUMENT:
        return True
    return suffix not in _SIDECAR_EXTENSIONS


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _info_paths(info: Mapping[str, Any] | None, job_dir: Path) -> list[Path]:
    if not info:
        return []
    raw_values: list[object] = [
        info.get("filepath"),
        info.get("filename"),
        info.get("_filename"),
    ]
    requested = info.get("requested_downloads")
    if isinstance(requested, list):
        raw_values.extend(
            entry.get("filepath")
            for entry in requested
            if isinstance(entry, Mapping)
        )
    paths: list[Path] = []
    for value in raw_values:
        if not isinstance(value, (str, Path)):
            continue
        path = Path(value)
        if not path.is_absolute():
            path = job_dir / path
        if path not in paths:
            paths.append(path)
    return paths


def _scan_media(job_dir: Path, stem_name: str, variant: DownloadVariant) -> Path | None:
    candidates: list[Path] = []
    fallback: list[Path] = []
    for path in job_dir.rglob("*"):
        if not path.is_file() or _is_temp(path):
            continue
        if not path.name.startswith(f"{stem_name}."):
            continue
        if _is_media(path, variant):
            candidates.append(path)
        elif not _is_sidecar(path):
            # Unknown extensions are still real outputs when yt-dlp already
            # reported them; this branch mainly helps extractors serving
            # formats outside our common whitelist.
            fallback.append(path)
    if not candidates:
        candidates = fallback
    if not candidates:
        return None
    preferred = _normalise_container(variant.container)
    if preferred:
        for candidate in candidates:
            if _normalise_container(candidate.suffix) == preferred:
                return candidate
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _scan_sidecars(directory: Path, stem_name: str, media: Path) -> tuple[Path, ...]:
    sidecars: list[Path] = []
    if not directory.is_dir():
        return ()
    for path in directory.iterdir():
        if not path.is_file() or path == media:
            continue
        if not path.name.startswith(f"{stem_name}."):
            continue
        if not _is_sidecar(path):
            continue
        sidecars.append(path)
    return tuple(sorted(sidecars, key=lambda path: path.name))


def collect_download_outputs(
    item_plan: ItemPlan,
    job_dir: Path,
    info: Mapping[str, Any] | None,
) -> tuple[Path, tuple[Path, ...]]:
    """Find the real media file and its sidecars after yt-dlp finished.

    The template is never trusted: every returned path is checked on disk.
    """
    media: Path | None = None
    for candidate in _info_paths(info, job_dir):
        if (
            candidate.is_file()
            and not _is_temp(candidate)
            and _is_reported_media(candidate, item_plan.variant)
            and _within(candidate, job_dir)
        ):
            media = candidate
            break
    if media is None:
        media = _scan_media(job_dir, item_plan.stem_name, item_plan.variant)
    if media is None:
        raise DownloadFailed(
            f"yt-dlp reported success but no media file was found for "
            f"{item_plan.item.id!r}"
        )
    sidecars = _scan_sidecars(media.parent, item_plan.stem_name, media)
    return media, sidecars


__all__ = [
    "PREVIEW_MAX_SECONDS",
    "PREVIEW_MIN_SECONDS",
    "DownloadPlan",
    "ItemPlan",
    "YtDlpPlanner",
    "YtDlpProgressHook",
    "build_postprocessors",
    "collect_download_outputs",
    "ensure_ffmpeg",
    "ensure_ffprobe",
    "looks_like_preview",
    "probe_duration",
    "probe_media_duration",
    "requires_ffmpeg",
]

"""Subprocess adapter for the ``yt-dlp`` executable.

Every invocation uses an argument list, ``shell=False``, a timeout, and
bounded captured output.  JSON metadata is parsed defensively; downloader
stderr is redacted before it becomes an error context.  The adapter never
imports yt-dlp as a Python package, so a broken plugin cannot take TrackForge
down with it.
"""

from __future__ import annotations

import json
import os
import subprocess  # noqa: S404 - explicit argument list, no shell
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trackforge.adapters.persistence.filesystem.paths import ensure_private_directory, ensure_within
from trackforge.adapters.sources.ytdlp.cookies import cookie_file_matches
from trackforge.adapters.sources.ytdlp.errors import map_error, redact_stderr
from trackforge.domain.errors import (
    AcquisitionFailed,
    CookiesPermissionsError,
    DownloaderNotFound,
    DownloadTimeout,
    ExtractorBroken,
    TrackForgeError,
)

_DEFAULT_METADATA_TIMEOUT = 60.0
_DEFAULT_DOWNLOAD_TIMEOUT = 1800.0
_DEFAULT_VERSION_TIMEOUT = 15.0
_DEFAULT_MAX_STDERR_BYTES = 8_192
_DEFAULT_MAX_METADATA_BYTES = 16 * 1024 * 1024
_DEFAULT_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"
_PROGRESS_PREFIX = "TF_PROGRESS"
_IGNORED_SUFFIXES = (".info.json", ".part", ".ytdl", ".temp")


@dataclass(frozen=True, slots=True)
class DownloadProgress:
    """Progress parsed from a downloader line (metadata calls have none)."""

    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    percent: float | None = None


DumpProgress = DownloadProgress
ProgressCallback = Callable[[DownloadProgress], None]


@dataclass(frozen=True, slots=True)
class DumpOptions:
    """Options for a metadata-only ``--dump-single-json`` call."""

    timeout_seconds: float | None = None
    cookies_path: Path | None = None
    cookies: Path | None = None
    site_hint: str | None = None
    extra_args: tuple[str, ...] = ()
    max_metadata_bytes: int | None = None

    def __post_init__(self) -> None:
        _positive_optional(self.timeout_seconds, "timeout_seconds")
        _positive_optional(self.max_metadata_bytes, "max_metadata_bytes")
        object.__setattr__(self, "extra_args", tuple(str(item) for item in self.extra_args))
        if self.cookies is not None and self.cookies_path is None:
            object.__setattr__(self, "cookies_path", Path(self.cookies))


@dataclass(frozen=True, slots=True)
class DownloadOptions:
    """Options for one audio-only media download."""

    format_id: str | None = None
    timeout_seconds: float | None = None
    cookies_path: Path | None = None
    cookies: Path | None = None
    site_hint: str | None = None
    extra_args: tuple[str, ...] = ()
    max_file_bytes: int | None = None
    progress: ProgressCallback | None = None

    def __post_init__(self) -> None:
        _positive_optional(self.timeout_seconds, "timeout_seconds")
        _positive_optional(self.max_file_bytes, "max_file_bytes")
        if self.format_id is not None and not self.format_id.strip():
            raise ValueError("format_id must not be empty")
        object.__setattr__(self, "extra_args", tuple(str(item) for item in self.extra_args))
        if self.cookies is not None and self.cookies_path is None:
            object.__setattr__(self, "cookies_path", Path(self.cookies))


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    """One downloaded audio file and its raw metadata sidecar."""

    path: Path
    info_json: dict[str, Any] = field(default_factory=dict)
    container: str = ""
    size_bytes: int = 0
    duration: float | None = None

    def __post_init__(self) -> None:
        path = Path(self.path)
        object.__setattr__(self, "path", path)
        if not path.is_file():
            raise AcquisitionFailed(
                "downloader did not produce a regular file",
                context={"path": str(path)},
            )
        size = path.stat().st_size
        object.__setattr__(self, "size_bytes", size if self.size_bytes <= 0 else self.size_bytes)
        if self.size_bytes <= 0:
            raise AcquisitionFailed(
                "downloader produced an empty file",
                context={"path": str(path)},
            )
        if not self.container:
            object.__setattr__(self, "container", path.suffix.lstrip(".").lower())


class YtDlpClient:
    """Run ``yt-dlp`` with a fixed, non-shell command line."""

    def __init__(
        self,
        executable: str | os.PathLike[str] = "yt-dlp",
        *,
        binary: str | os.PathLike[str] | None = None,
        timeout_metadata: float = _DEFAULT_METADATA_TIMEOUT,
        timeout_download: float = _DEFAULT_DOWNLOAD_TIMEOUT,
        timeout_version: float = _DEFAULT_VERSION_TIMEOUT,
        max_stderr_bytes: int = _DEFAULT_MAX_STDERR_BYTES,
        max_metadata_bytes: int = _DEFAULT_MAX_METADATA_BYTES,
        cache_dir: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if binary is not None:
            executable = binary
        raw_executable = os.fspath(executable)
        if not raw_executable.strip():
            raise ValueError("executable must be a non-empty string")
        for name, value in (
            ("timeout_metadata", timeout_metadata),
            ("timeout_download", timeout_download),
            ("timeout_version", timeout_version),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be positive")
        for name, value in (
            ("max_stderr_bytes", max_stderr_bytes),
            ("max_metadata_bytes", max_metadata_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._executable = raw_executable.strip()
        self._timeout_metadata = float(timeout_metadata)
        self._timeout_download = float(timeout_download)
        self._timeout_version = float(timeout_version)
        self._max_stderr_bytes = max_stderr_bytes
        self._max_metadata_bytes = max_metadata_bytes
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._env = dict(env) if env is not None else None
        self._version: str | None = None

    @property
    def executable(self) -> str:
        return self._executable

    def version(self) -> str:
        """Return ``yt-dlp --version`` or a typed downloader error."""

        if self._version is not None:
            return self._version
        completed = self._run(
            [self._executable, "--version"],
            timeout=self._timeout_version,
            url=None,
        )
        text = (completed.stdout or "").strip().splitlines()
        if completed.returncode != 0 or not text:
            raise self._failure(completed.stderr, completed.returncode, None)
        value = text[0].strip()
        if not value:
            raise ExtractorBroken("yt-dlp --version returned no version")
        self._version = value
        return value

    def dump_json(
        self,
        url: str,
        opts: DumpOptions | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return one ``info.json`` object without downloading media."""

        options = _coerce_dump_options(opts)
        command = [
            self._executable,
            "--dump-single-json",
            "--no-download",
            "--no-warnings",
            "--no-playlist",
            "--no-progress",
        ]
        command.extend(self._cookie_arguments(url, options.cookies_path, options.site_hint))
        command.extend(self._common_arguments())
        command.extend(str(item) for item in options.extra_args)
        command.extend(["--", url])
        completed = self._run(
            command,
            timeout=options.timeout_seconds or self._timeout_metadata,
            url=url,
        )
        if completed.returncode != 0:
            raise self._failure(completed.stderr, completed.returncode, url)
        stdout = completed.stdout or ""
        limit = options.max_metadata_bytes or self._max_metadata_bytes
        if len(stdout.encode("utf-8", errors="replace")) > limit:
            raise ExtractorBroken(
                "yt-dlp metadata output exceeded the configured size limit",
                context={"url": _safe_url(url), "limit_bytes": limit},
            )
        if not stdout.strip():
            raise ExtractorBroken(
                "yt-dlp returned no metadata",
                context={
                    "url": _safe_url(url),
                    "stderr": redact_stderr(completed.stderr, max_chars=self._max_stderr_bytes),
                },
            )
        try:
            document = json.loads(stdout)
        except (json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise ExtractorBroken(
                "yt-dlp returned invalid JSON metadata",
                context={
                    "url": _safe_url(url),
                    "error": str(exc),
                    "stderr": redact_stderr(completed.stderr, max_chars=self._max_stderr_bytes),
                },
            ) from exc
        if not isinstance(document, Mapping):
            raise ExtractorBroken(
                "yt-dlp metadata is not a JSON object",
                context={"url": _safe_url(url)},
            )
        return dict(document)

    def download(
        self,
        url: str,
        candidate: Any,
        dest: Path,
        opts: DownloadOptions | Mapping[str, Any] | None = None,
    ) -> DownloadedFile:
        """Download one audio-only file into ``dest``.

        ``candidate`` is accepted for protocol parity and for a future
        candidate-specific selector; the format is taken from ``opts`` or the
        candidate's ``extra['selected_format_id']``.
        """

        options = _coerce_download_options(opts)
        directory = Path(dest)
        try:
            ensure_private_directory(directory)
        except OSError as exc:
            raise AcquisitionFailed(
                "download directory could not be created",
                user_action="Check cache directory permissions and free space.",
                context={"path": str(directory), "error": str(exc)},
            ) from exc
        format_id = _selected_format_id(candidate, options)
        selector = format_id or _DEFAULT_FORMAT
        template = directory / "media.%(ext)s"
        command = [
            self._executable,
            "--no-playlist",
            "--continue",
            "--newline",
            "--progress-template",
            (
                f"download:{_PROGRESS_PREFIX} %(progress.downloaded_bytes)s "
                "%(progress.total_bytes)s %(progress._percent_str)s"
            ),
            "--print",
            "after_move:filepath",
            "--no-simulate",
            "--write-info-json",
            "--no-clean-info-json",
            "-f",
            selector,
            "-o",
            str(template),
        ]
        command.extend(self._cookie_arguments(url, options.cookies_path, options.site_hint))
        command.extend(self._common_arguments())
        command.extend(str(item) for item in options.extra_args)
        command.extend(["--", url])
        timeout = options.timeout_seconds or self._timeout_download
        completed = self._run(command, timeout=timeout, url=url)
        progress = _parse_progress(completed.stdout or "")
        if options.progress is not None:
            for event in progress:
                try:
                    options.progress(event)
                except Exception:  # noqa: S110 - UI callbacks must not break acquisition
                    # A progress UI must not break a completed download.
                    pass
        if completed.returncode != 0:
            raise self._failure(completed.stderr, completed.returncode, url)
        media_path, info = _locate_result(directory, completed.stdout or "")
        if (
            options.max_file_bytes is not None
            and media_path.stat().st_size > options.max_file_bytes
        ):
            raise AcquisitionFailed(
                "downloaded file exceeds the configured size limit",
                context={
                    "path": str(media_path),
                    "size_bytes": media_path.stat().st_size,
                    "limit_bytes": options.max_file_bytes,
                },
            )
        duration = _duration(info)
        return DownloadedFile(
            path=media_path,
            info_json=info,
            container=media_path.suffix.lstrip(".").lower(),
            size_bytes=media_path.stat().st_size,
            duration=duration,
        )

    def _run(
        self,
        command: Sequence[str],
        *,
        timeout: float,
        url: str | None,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        if self._env is not None:
            env = {**env, **self._env}
        try:
            return subprocess.run(  # noqa: S603 - explicit arg list, shell=False
                list(command),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                shell=False,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise DownloadTimeout(
                "yt-dlp timed out",
                retryable=True,
                context={"timeout_seconds": timeout, "url": _safe_url(url)},
            ) from exc
        except FileNotFoundError as exc:
            raise DownloaderNotFound(
                "yt-dlp executable was not found",
                user_action="Install yt-dlp and ensure it is on PATH.",
                context={"executable": self._executable},
            ) from exc
        except OSError as exc:
            raise DownloaderNotFound(
                "yt-dlp could not be executed",
                context={"executable": self._executable, "error": str(exc)},
            ) from exc

    def _failure(
        self,
        stderr: str | None,
        returncode: int | None,
        url: str | None,
    ) -> TrackForgeError:
        return map_error(
            stderr,
            returncode=returncode,
            url=url,
            max_stderr_chars=self._max_stderr_bytes,
        )

    def _common_arguments(self) -> list[str]:
        result: list[str] = []
        if self._cache_dir is not None:
            # Do not create the directory during metadata-only planning;
            # yt-dlp creates it lazily when it actually needs the cache.
            result.extend(["--cache-dir", str(self._cache_dir)])
        return result

    def _cookie_arguments(
        self,
        url: str,
        cookie_path: Path | None,
        site_hint: str | None,
    ) -> list[str]:
        if cookie_path is None or not cookie_file_matches(cookie_path, url, site_hint=site_hint):
            return []
        try:
            metadata = cookie_path.stat()
        except OSError as exc:
            raise CookiesPermissionsError(
                "cookie file cannot be read",
                context={"path": str(cookie_path), "error": str(exc)},
            ) from exc
        if cookie_path.is_symlink() or (metadata.st_mode & 0o777) != 0o600:
            raise CookiesPermissionsError(
                "cookie file permissions must be 0600",
                user_action=f"Run: chmod 600 {cookie_path}",
                context={"path": str(cookie_path), "mode": f"{metadata.st_mode & 0o777:04o}"},
            )
        return ["--cookies", str(cookie_path)]


def _coerce_dump_options(value: DumpOptions | Mapping[str, Any] | None) -> DumpOptions:
    if value is None:
        return DumpOptions()
    if isinstance(value, DumpOptions):
        return value
    return DumpOptions(**dict(value))


def _coerce_download_options(
    value: DownloadOptions | Mapping[str, Any] | None,
) -> DownloadOptions:
    if value is None:
        return DownloadOptions()
    if isinstance(value, DownloadOptions):
        return value
    return DownloadOptions(**dict(value))


def _positive_optional(value: float | int | None, name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{name} must be positive")


def _selected_format_id(candidate: Any, options: DownloadOptions) -> str | None:
    if options.format_id is not None:
        return options.format_id.strip()
    if candidate is None:
        return None
    extra = getattr(candidate, "extra", None)
    if isinstance(extra, Mapping):
        value = extra.get("selected_format_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    formats = getattr(candidate, "formats", None)
    if formats:
        from trackforge.adapters.sources.ytdlp.source import select_best_audio

        return select_best_audio(list(formats)).format_id
    return None


def _safe_url(url: str | None) -> str | None:
    from trackforge.adapters.http.url_safety import sanitize_url

    return sanitize_url(url)


def _parse_progress(stdout: str) -> tuple[DumpProgress, ...]:
    events: list[DownloadProgress] = []
    for line in stdout.splitlines():
        text = line.strip()
        if not text.startswith(_PROGRESS_PREFIX):
            continue
        parts = text.split()
        downloaded = _int_or_none(parts[1] if len(parts) > 1 else None)
        total = _int_or_none(parts[2] if len(parts) > 2 else None)
        percent = _percent(parts[3] if len(parts) > 3 else None)
        events.append(
            DownloadProgress(
                downloaded_bytes=downloaded,
                total_bytes=total,
                percent=percent,
            )
        )
    return tuple(events)


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(float(value.strip()))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _percent(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip().rstrip("%")
    try:
        parsed = float(text)
    except ValueError:
        return None
    if not 0.0 <= parsed <= 100.0:
        return None
    return parsed / 100.0


def _locate_result(directory: Path, stdout: str) -> tuple[Path, dict[str, Any]]:
    candidates: list[Path] = []
    for line in stdout.splitlines():
        text = line.strip()
        if not text or text.startswith(_PROGRESS_PREFIX):
            continue
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = directory / candidate
        try:
            resolved = ensure_within(directory, candidate)
        except Exception:  # noqa: S112 - ignore stdout lines that are not paths
            continue
        if resolved.is_file() and not _is_metadata_or_partial(resolved):
            candidates.append(resolved)
    if not candidates:
        try:
            for entry in sorted(directory.iterdir()):
                if entry.is_file() and not _is_metadata_or_partial(entry):
                    candidates.append(ensure_within(directory, entry))
        except OSError as exc:
            raise AcquisitionFailed(
                "download directory cannot be inspected",
                context={"path": str(directory), "error": str(exc)},
            ) from exc
    if not candidates:
        raise AcquisitionFailed(
            "yt-dlp finished without an audio file",
            user_action="Check the selected format and the downloader output.",
            context={"path": str(directory)},
        )
    media_path = max(candidates, key=lambda path: path.stat().st_size)
    info = _load_info_json(directory, media_path)
    return media_path, info


def _is_metadata_or_partial(path: Path) -> bool:
    name = path.name.casefold()
    return any(name.endswith(suffix) for suffix in _IGNORED_SUFFIXES)


def _load_info_json(directory: Path, media_path: Path) -> dict[str, Any]:
    candidates = sorted(directory.glob("*.info.json"))
    preferred = media_path.with_suffix(".info.json")
    if preferred.is_file():
        candidates.insert(0, preferred)
    for path in candidates:
        try:
            raw = path.read_text(encoding="utf-8")
            document = json.loads(raw)
        except (OSError, json.JSONDecodeError, ValueError, RecursionError):
            continue
        if isinstance(document, Mapping):
            return dict(document)
    raise ExtractorBroken(
        "yt-dlp did not produce a valid info.json sidecar",
        context={"path": str(directory)},
    )


def _duration(info: Mapping[str, Any]) -> float | None:
    value = info.get("duration")
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number >= 0:
            return number
    return None


__all__ = [
    "DownloadOptions",
    "DownloadProgress",
    "DownloadedFile",
    "DumpOptions",
    "DumpProgress",
    "YtDlpClient",
]

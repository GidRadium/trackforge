"""Environment and configuration health checks for ``trackforge doctor``."""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import sqlite3
import subprocess  # noqa: S404 - explicit argument list, no shell
import sys
import tempfile
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Protocol

from trackforge.application.dto.results import (
    DoctorCheck,
    DoctorReport,
    DoctorStatus,
)

_CURRENT_SCHEMA_REVISION = "0002"
_REQUIRED_TABLES = frozenset(
    {
        "alembic_version",
        "items",
        "media_objects",
        "model_registry",
        "runs",
        "steps",
        "renders",
        "output_bundles",
        "library_entries",
        "feedback_events",
    }
)
_MIN_WARNING_FREE_BYTES = 200 * 1024 * 1024
_MIN_CRITICAL_FREE_BYTES = 20 * 1024 * 1024


class DoctorConfig(Protocol):
    """The configuration surface doctor is allowed to read."""

    @property
    def config_dir(self) -> Path: ...

    @property
    def data_dir(self) -> Path: ...

    @property
    def state_dir(self) -> Path: ...

    @property
    def library_dir(self) -> Path: ...

    @property
    def profile(self) -> str: ...

    @property
    def mode(self) -> str: ...

    @property
    def offline(self) -> bool: ...


class Doctor:
    """Check tools, permissions, catalog schema, and conflicting settings."""

    def __init__(
        self,
        config: DoctorConfig,
        *,
        which: Callable[[str], str | None] | None = None,
        module_available: Callable[[str], bool] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._which = which or shutil.which
        self._module_available = module_available or _module_available
        self._clock = clock

    async def run(self) -> DoctorReport:
        """Return all checks; the report status is the worst check."""

        checks: list[DoctorCheck] = [
            _python_check(),
            _binary_check("ffmpeg", self._which, status=DoctorStatus.CRITICAL),
            _binary_check("ffprobe", self._which, status=DoctorStatus.CRITICAL),
            _mutagen_check(self._module_available),
        ]
        checks.extend(self._permission_checks())
        checks.append(self._catalog_check())
        checks.append(self._disk_check())
        checks.append(_binary_check("fpcalc", self._which, status=DoctorStatus.WARNING))
        checks.append(_acoustid_key_check(self._config))
        checks.append(_musicbrainz_user_agent_check(self._config))
        checks.append(
            _tool_version_check(
                "yt-dlp",
                self._which,
                status=DoctorStatus.WARNING,
                executable=str(getattr(self._config, "ytdlp_binary", "yt-dlp")),
            )
        )
        checks.append(
            _tool_version_check(
                "deno",
                self._which,
                status=DoctorStatus.WARNING,
                minimum=(2, 3, 0),
                executable=str(getattr(self._config, "deno_binary", "deno")),
            )
        )
        checks.append(_ytdlp_ejs_check(self._module_available))
        checks.append(_llm_check(self._config))
        checks.append(_ranker_check(self._config, self._module_available))
        checks.append(_asr_check(self._config, self._module_available))
        checks.append(_stems_check(self._config, self._module_available))
        checks.append(_cookies_check(Path(self._config.config_dir) / "cookies"))
        checks.append(_settings_check(self._config))
        return DoctorReport(checks=tuple(checks), generated_at=self._clock(), version=_version())

    def _permission_checks(self) -> list[DoctorCheck]:
        checks: list[DoctorCheck] = []
        for label, path in (
            ("config_dir", self._config.config_dir),
            ("data_dir", self._config.data_dir),
            ("state_dir", self._config.state_dir),
            ("library_dir", self._config.library_dir),
        ):
            checks.append(_writable_directory_check(label, path))
        return checks

    def _catalog_check(self) -> DoctorCheck:
        database = Path(self._config.data_dir) / "catalog.sqlite"
        if not database.exists():
            parent = Path(self._config.data_dir)
            try:
                parent.mkdir(parents=True, exist_ok=True)
                if not os.access(parent, os.W_OK | os.X_OK):
                    raise PermissionError("data directory is not writable")
            except OSError as exc:
                return DoctorCheck(
                    name="catalog",
                    status=DoctorStatus.CRITICAL,
                    message="catalog directory is not writable",
                    details={"path": str(parent), "error": str(exc)},
                )
            return DoctorCheck(
                name="catalog",
                status=DoctorStatus.OK,
                message="catalog will be created on the first write",
                details={"path": str(database)},
            )
        try:
            uri = f"file:{database}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=2.0) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                missing = sorted(_REQUIRED_TABLES - tables)
                if missing:
                    return DoctorCheck(
                        name="catalog",
                        status=DoctorStatus.CRITICAL,
                        message="catalog schema is incomplete",
                        details={"path": str(database), "missing_tables": missing},
                    )
                revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
                current = str(revision[0]) if revision is not None else None
                if current != _CURRENT_SCHEMA_REVISION:
                    return DoctorCheck(
                        name="catalog",
                        status=DoctorStatus.CRITICAL,
                        message="catalog migration revision is not current",
                        details={
                            "path": str(database),
                            "revision": current,
                            "expected": _CURRENT_SCHEMA_REVISION,
                        },
                    )
        except sqlite3.Error as exc:
            return DoctorCheck(
                name="catalog",
                status=DoctorStatus.CRITICAL,
                message="catalog cannot be opened",
                details={"path": str(database), "error": str(exc)},
            )
        return DoctorCheck(
            name="catalog",
            status=DoctorStatus.OK,
            message="catalog schema and migration revision are current",
            details={"path": str(database), "revision": _CURRENT_SCHEMA_REVISION},
        )

    def _disk_check(self) -> DoctorCheck:
        path = Path(self._config.library_dir)
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            usage = shutil.disk_usage(probe)
        except OSError as exc:
            return DoctorCheck(
                name="disk_space",
                status=DoctorStatus.WARNING,
                message="free disk space could not be determined",
                details={"path": str(probe), "error": str(exc)},
            )
        if usage.free < _MIN_CRITICAL_FREE_BYTES:
            status = DoctorStatus.CRITICAL
            message = "less than 20 MiB free"
        elif usage.free < _MIN_WARNING_FREE_BYTES:
            status = DoctorStatus.WARNING
            message = "less than 200 MiB free"
        else:
            status = DoctorStatus.OK
            message = "enough free space for a small local library"
        return DoctorCheck(
            name="disk_space",
            status=status,
            message=message,
            details={"path": str(probe), "free_bytes": usage.free},
        )


def _python_check() -> DoctorCheck:
    info = sys.version_info
    supported = info >= (3, 12)
    return DoctorCheck(
        name="python",
        status=DoctorStatus.OK if supported else DoctorStatus.CRITICAL,
        message=(
            "Python version is supported" if supported else "Python 3.12 or newer is required"
        ),
        details={"version": f"{info.major}.{info.minor}.{info.micro}"},
    )


def _llm_check(config: object) -> DoctorCheck:
    base_url = bool(str(getattr(config, "llm_base_url", "") or "").strip())
    model = bool(str(getattr(config, "llm_model", "") or "").strip())
    key_env = str(getattr(config, "llm_api_key_env", "TRACKFORGE_LLM_API_KEY") or "")
    key_present = bool(key_env and os.environ.get(key_env, "").strip())
    if not key_present and key_env:
        config_dir = getattr(config, "config_dir", None)
        if config_dir is not None:
            key_present = _dotenv_key_present(Path(config_dir) / ".env", key_env)
    configured = base_url and model
    if configured and key_present:
        status = DoctorStatus.OK
        message = "LLM endpoint/model and API key are configured"
    elif configured:
        status = DoctorStatus.WARNING
        message = "LLM endpoint/model are configured but the API key is absent; LLM modes fall back"
    else:
        status = DoctorStatus.WARNING
        message = "LLM endpoint/model are not configured; policy mode is available"
    return DoctorCheck(
        name="llm",
        status=status,
        message=message,
        details={
            "base_url_configured": base_url,
            "model_configured": model,
            "api_key_env": key_env,
            "api_key_present": key_present,
        },
    )


def _dotenv_key_present(path: Path, name: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        if separator and key.strip() == name and value.strip():
            return True
    return False


def _ranker_check(
    config: object,
    module_available: Callable[[str], bool],
) -> DoctorCheck:
    strategy = str(getattr(config, "ranker_strategy", "lexical") or "lexical")
    model = bool(str(getattr(config, "ranker_model", "") or "").strip())
    path_value = str(getattr(config, "ranker_model_path", "") or "").strip()
    path = bool(path_value)
    model_path = Path(path_value).expanduser() if path else None
    models_dir = getattr(config, "models_dir", None)
    if model_path is not None and not model_path.is_absolute() and models_dir is not None:
        model_path = Path(models_dir) / model_path
    path_exists = model_path is not None and model_path.exists()
    license_name = str(getattr(config, "ranker_license", "") or "").strip()
    if strategy == "lexical" and not model and not path:
        return DoctorCheck(
            name="ranker",
            status=DoctorStatus.OK,
            message="lexical ranker is available; cross-encoder is opt-in",
            details={"strategy": strategy, "cross_encoder": False},
        )
    problems: list[str] = []
    if not model and not path:
        problems.append("model/path is not configured")
    if path and not path_exists:
        problems.append("configured model path does not exist")
    if not license_name:
        problems.append("model license is not configured")
    if not module_available("onnxruntime"):
        problems.append("install the [ranker] extra")
    status = DoctorStatus.WARNING if problems else DoctorStatus.OK
    return DoctorCheck(
        name="ranker",
        status=status,
        message=(
            "; ".join(problems)
            if problems
            else "cross-encoder ranker is configured with an ONNX runtime"
        ),
        details={
            "strategy": strategy,
            "model_configured": model,
            "path_configured": path,
            "path_exists": path_exists if path else None,
            "license_configured": bool(license_name),
            "onnxruntime": module_available("onnxruntime"),
        },
    )


def _asr_check(
    config: object,
    module_available: Callable[[str], bool],
) -> DoctorCheck:
    model = str(getattr(config, "asr_model", "") or "").strip()
    path_value = str(getattr(config, "asr_model_path", "") or "").strip()
    if not model and not path_value:
        return DoctorCheck(
            name="asr",
            status=DoctorStatus.OK,
            message="ASR is not configured; profiles without ASR run normally",
            details={"configured": False},
        )
    models_dir = getattr(config, "models_dir", None)
    if path_value:
        model_path = Path(path_value).expanduser()
        if not model_path.is_absolute() and models_dir is not None:
            model_path = Path(models_dir) / model_path
    else:
        directory = _asr_model_directory(model)
        model_path = Path(directory) if models_dir is None else Path(models_dir) / "asr" / directory
    path_exists = model_path is not None and model_path.exists()
    license_name = str(getattr(config, "asr_license", "") or "").strip()
    checksum = str(getattr(config, "asr_checksum", "") or "").strip()
    required_memory = int(getattr(config, "asr_memory_mb", 2048) or 2048)
    available_memory = _available_memory_mb()
    problems: list[str] = []
    if not path_exists:
        problems.append("model path does not exist; run `trackforge models download asr`")
    if not license_name:
        problems.append("model license is not configured")
    if not checksum:
        problems.append("model checksum is not configured")
    if not module_available("faster_whisper"):
        problems.append("install the [asr] extra")
    if available_memory is not None and available_memory < required_memory:
        problems.append(
            f"only {available_memory:.0f} MB available; the estimate is {required_memory} MB"
        )
    return DoctorCheck(
        name="asr",
        status=DoctorStatus.WARNING if problems else DoctorStatus.OK,
        message="; ".join(problems) if problems else "ASR model and runtime are ready",
        details={
            "configured": True,
            "model": model or Path(path_value).stem,
            "path_exists": path_exists,
            "license_configured": bool(license_name),
            "checksum_configured": bool(checksum),
            "faster_whisper": module_available("faster_whisper"),
            "available_memory_mb": (
                round(available_memory) if available_memory is not None else None
            ),
            "required_memory_mb": required_memory,
        },
    )


def _stems_check(
    config: object,
    module_available: Callable[[str], bool],
) -> DoctorCheck:
    model = str(getattr(config, "stems_model", "") or "").strip()
    path_value = str(getattr(config, "stems_model_path", "") or "").strip()
    if not model and not path_value:
        return DoctorCheck(
            name="stems",
            status=DoctorStatus.OK,
            message=(
                "Demucs is not configured; full reports a visible model_missing skip "
                "and derivative analysis uses metadata/duration evidence only"
            ),
            details={"configured": False},
        )
    models_dir = getattr(config, "models_dir", None)
    if path_value:
        model_path = Path(path_value).expanduser()
        if not model_path.is_absolute() and models_dir is not None:
            model_path = Path(models_dir) / model_path
    else:
        version = str(getattr(config, "stems_model_version", "") or "").strip()
        if not version and model == "htdemucs":
            version = "955717e8-8726e21a"
        file_name = str(getattr(config, "stems_model_file", "") or "").strip()
        if not file_name:
            file_name = f"{version or model or 'demucs-model'}.th"
        directory = _asr_model_directory(model)
        model_path = (
            Path("stems") / directory / file_name
            if models_dir is None
            else Path(models_dir) / "stems" / directory / file_name
        )
    path_exists = model_path is not None and model_path.exists()
    license_name = str(getattr(config, "stems_license", "") or "").strip()
    checksum = str(getattr(config, "stems_checksum", "") or "").strip()
    required_memory = int(getattr(config, "stems_memory_mb", 3072) or 3072)
    available_memory = _available_memory_mb()
    problems: list[str] = []
    if not path_exists:
        problems.append("model path does not exist; run `trackforge models download demucs`")
    if not license_name:
        problems.append("model license is not configured")
    if not checksum:
        problems.append("model checksum is not configured")
    if not module_available("torch") or not module_available("demucs"):
        problems.append("install the [stems] extra")
    if available_memory is not None and available_memory < required_memory:
        problems.append(
            f"only {available_memory:.0f} MB available; the estimate is {required_memory} MB"
        )
    return DoctorCheck(
        name="stems",
        status=DoctorStatus.WARNING if problems else DoctorStatus.OK,
        message="; ".join(problems) if problems else "Demucs model and runtime are ready",
        details={
            "configured": True,
            "model": model or Path(path_value).stem,
            "path_exists": path_exists,
            "license_configured": bool(license_name),
            "checksum_configured": bool(checksum),
            "torch": module_available("torch"),
            "demucs": module_available("demucs"),
            "memory_available_mb": (
                round(available_memory, 1) if available_memory is not None else None
            ),
            "required_memory_mb": required_memory,
        },
    )


def _asr_model_directory(model_id: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "--", model_id.strip()).strip(".-")
    return text or "asr-model"


def _available_memory_mb() -> float | None:
    try:
        with Path("/proc/meminfo").open(encoding="utf-8") as file:
            for line in file:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) / 1024.0
    except (OSError, ValueError):
        return None
    return None


def _acoustid_key_check(config: object) -> DoctorCheck:
    configured = bool(str(getattr(config, "acoustid_api_key", "") or "").strip())
    return DoctorCheck(
        name="acoustid_api_key",
        status=DoctorStatus.OK if configured else DoctorStatus.WARNING,
        message=(
            "AcoustID API key is configured"
            if configured
            else "AcoustID API key is not configured; fingerprint lookup is disabled"
        ),
        details={
            "configured": configured,
            "env": "TRACKFORGE_ACOUSTID_API_KEY",
        },
    )


def _musicbrainz_user_agent_check(config: object) -> DoctorCheck:
    value = str(getattr(config, "musicbrainz_user_agent", "") or "").strip()
    has_contact = bool(value) and (
        "@" in value or "://" in value or value.casefold().startswith("trackforge/")
    )
    if has_contact:
        return DoctorCheck(
            name="musicbrainz_user_agent",
            status=DoctorStatus.OK,
            message="MusicBrainz User-Agent contact is configured",
            details={"configured": True, "contact": True},
        )
    return DoctorCheck(
        name="musicbrainz_user_agent",
        status=DoctorStatus.WARNING,
        message=(
            "MusicBrainz User-Agent contact is missing; MusicBrainz search is disabled"
            if not value
            else "MusicBrainz User-Agent has no contact; add an email address or URL"
        ),
        details={"configured": bool(value), "contact": False},
    )


def _binary_check(
    binary: str,
    which: Callable[[str], str | None],
    *,
    status: DoctorStatus,
) -> DoctorCheck:
    try:
        location = which(binary)
    except OSError:
        location = None
    if location:
        return DoctorCheck(
            name=binary,
            status=DoctorStatus.OK,
            message=f"{binary} is available",
            details={"path": location},
        )
    return DoctorCheck(
        name=binary,
        status=status,
        message=f"{binary} is not installed or not on PATH",
        details={"binary": binary},
    )


def _tool_version_check(
    binary: str,
    which: Callable[[str], str | None],
    *,
    status: DoctorStatus,
    minimum: tuple[int, int, int] | None = None,
    executable: str | None = None,
) -> DoctorCheck:
    configured = executable or binary
    try:
        location = which(configured)
    except OSError:
        location = None
    if location is None and Path(configured).is_file():
        location = configured
    if not location:
        return DoctorCheck(
            name=binary,
            status=status,
            message=f"{binary} is not installed or not on PATH",
            details={"binary": binary},
        )
    try:
        completed = subprocess.run(  # noqa: S603 - explicit arg list, no shell
            [location, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return DoctorCheck(
            name=binary,
            status=status,
            message=f"{binary} is present but could not be executed",
            details={"path": location},
        )
    output = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0 or not output:
        return DoctorCheck(
            name=binary,
            status=status,
            message=f"{binary} did not report a version",
            details={"path": location},
        )
    first_line = output.splitlines()[0].strip()[:160]
    version_numbers = _version_tuple(first_line)
    if minimum is not None and version_numbers is not None:
        if version_numbers < minimum:
            expected = ".".join(str(part) for part in minimum)
            return DoctorCheck(
                name=binary,
                status=DoctorStatus.WARNING,
                message=f"{binary} is older than the supported minimum {expected}",
                details={"path": location, "version": first_line},
            )
    return DoctorCheck(
        name=binary,
        status=DoctorStatus.OK,
        message=f"{binary} is available",
        details={"path": location, "version": first_line},
    )


def _ytdlp_ejs_check(module_available: Callable[[str], bool]) -> DoctorCheck:
    try:
        available = module_available("yt_dlp_ejs")
    except Exception:
        available = False
    if available:
        return DoctorCheck(
            name="yt-dlp-ejs",
            status=DoctorStatus.OK,
            message="yt-dlp-ejs is available",
            details={},
        )
    return DoctorCheck(
        name="yt-dlp-ejs",
        status=DoctorStatus.WARNING,
        message="yt-dlp-ejs is not importable from this Python environment",
        details={"install": "Install yt-dlp[default] for the downloader environment."},
    )


def _cookies_check(directory: Path) -> DoctorCheck:
    if not directory.exists():
        return DoctorCheck(
            name="cookies",
            status=DoctorStatus.OK,
            message="no cookie directory is configured; public sources do not need cookies",
            details={"path": str(directory), "files": 0},
        )
    if not directory.is_dir():
        return DoctorCheck(
            name="cookies",
            status=DoctorStatus.WARNING,
            message="cookie path is not a directory",
            details={"path": str(directory)},
        )
    try:
        files = sorted(path for path in directory.iterdir() if path.suffix.lower() == ".txt")
    except OSError as exc:
        return DoctorCheck(
            name="cookies",
            status=DoctorStatus.WARNING,
            message="cookie directory cannot be inspected",
            details={"path": str(directory), "error": str(exc)},
        )
    if not files:
        return DoctorCheck(
            name="cookies",
            status=DoctorStatus.OK,
            message="no cookie files found; public sources do not need cookies",
            details={"path": str(directory), "files": 0},
        )
    warnings: list[str] = []
    for path in files:
        try:
            metadata = path.stat()
        except OSError:
            warnings.append("a cookie file cannot be inspected")
            continue
        if path.is_symlink():
            warnings.append("a cookie file is a symlink")
            continue
        mode = metadata.st_mode & 0o777
        if mode != 0o600:
            warnings.append("a cookie file does not have mode 0600")
            continue
        if _cookie_file_expired(path):
            warnings.append("a cookie file appears to be expired")
    if warnings:
        return DoctorCheck(
            name="cookies",
            status=DoctorStatus.WARNING,
            message="; ".join(dict.fromkeys(warnings)),
            details={"path": str(directory), "files": len(files)},
        )
    return DoctorCheck(
        name="cookies",
        status=DoctorStatus.OK,
        message="cookie files are present, private, and not obviously expired",
        details={"path": str(directory), "files": len(files)},
    )


def _cookie_file_expired(path: Path) -> bool:
    from datetime import UTC, datetime

    current = datetime.now(UTC).timestamp()
    expiries: list[int] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                columns = line.rstrip("\n").split("\t")
                if len(columns) < 6:
                    continue
                try:
                    expiry = int(columns[4])
                except ValueError:
                    continue
                if expiry > 0:
                    expiries.append(expiry)
    except OSError:
        return False
    return bool(expiries) and all(expiry <= current for expiry in expiries)


def _version_tuple(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if match is None:
        return None
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3) or 0),
    )


def _mutagen_check(module_available: Callable[[str], bool]) -> DoctorCheck:
    try:
        available = module_available("mutagen")
    except Exception:
        available = False
    if available:
        return DoctorCheck(
            name="mutagen",
            status=DoctorStatus.OK,
            message="tagging support is installed",
            details={},
        )
    return DoctorCheck(
        name="mutagen",
        status=DoctorStatus.CRITICAL,
        message="mutagen is not installed",
        details={"install": "uv sync --extra tagging"},
    )


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _writable_directory_check(label: str, path: Path) -> DoctorCheck:
    target = Path(path).expanduser()
    try:
        target.mkdir(parents=True, exist_ok=True)
        if not target.is_dir():
            raise NotADirectoryError(str(target))
        if not os.access(target, os.W_OK | os.X_OK):
            raise PermissionError(f"{label} is not writable")
        descriptor, name = tempfile.mkstemp(prefix=".tf-doctor-", dir=target)
        try:
            os.write(descriptor, b"ok")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
            Path(name).unlink(missing_ok=True)
    except OSError as exc:
        return DoctorCheck(
            name=label,
            status=DoctorStatus.CRITICAL,
            message=f"{label} is not writable",
            details={"path": str(target), "error": str(exc)},
        )
    return DoctorCheck(
        name=label,
        status=DoctorStatus.OK,
        message=f"{label} is writable",
        details={"path": str(target)},
    )


def _settings_check(config: DoctorConfig) -> DoctorCheck:
    warnings: list[str] = []
    if config.mode != "policy":
        warnings.append(
            f"mode {config.mode!r} is not implemented in this build; policy mode is used"
        )
    if config.profile == "full":
        warnings.append("profile 'full' may require optional analysis extras")
    if config.offline and config.mode.startswith("llm_"):
        warnings.append("offline mode conflicts with LLM-assisted resolution")
    if warnings:
        return DoctorCheck(
            name="settings",
            status=DoctorStatus.WARNING,
            message="; ".join(warnings),
            details={"profile": config.profile, "mode": config.mode},
        )
    return DoctorCheck(
        name="settings",
        status=DoctorStatus.OK,
        message="no conflicting settings detected",
        details={"profile": config.profile, "mode": config.mode},
    )


def _version() -> str:
    try:
        return version("trackforge")
    except PackageNotFoundError:
        return "0.1.0"


def _iter_warnings(checks: Iterable[DoctorCheck]) -> Iterable[DoctorCheck]:
    return (check for check in checks if check.status is DoctorStatus.WARNING)


__all__ = ["Doctor", "DoctorConfig"]

"""Environment and configuration health checks for ``trackforge doctor``."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
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

_CURRENT_SCHEMA_REVISION = "0001"
_REQUIRED_TABLES = frozenset(
    {
        "alembic_version",
        "items",
        "media_objects",
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
        checks.append(_binary_check("yt-dlp", self._which, status=DoctorStatus.WARNING))
        checks.append(_binary_check("deno", self._which, status=DoctorStatus.WARNING))
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

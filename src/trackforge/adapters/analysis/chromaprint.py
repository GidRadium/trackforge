"""Chromaprint ``fpcalc`` observer.

``fpcalc`` is an external LGPL binary invoked as a subprocess with an explicit
argument list and a hard timeout.  The observer emits only the fingerprint,
duration, and tool identity; audio bytes never enter the artifact payload.

A missing or broken ``fpcalc`` degrades visibly (``skipped`` with a warning)
instead of failing the whole observation run.  AcoustID then reports its own
``fingerprint_missing``/provider-unavailable degradation.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess  # noqa: S404 - explicit argv list, shell=False
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trackforge.application.dto.providers import (
    AudioFingerprintPayload,
    FingerprintTool,
)
from trackforge.application.ports.execution import (
    ArtifactDraft,
    StepContext,
    StepResult,
    StepWarning,
)
from trackforge.application.ports.observations import (
    FINGERPRINT_ARTIFACT,
    MEDIA_ARTIFACT,
    media_input,
)
from trackforge.domain.model.capabilities import (
    CapabilityKind,
    CapabilityManifest,
    Cost,
    Determinism,
    SideEffect,
)

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_STDOUT_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_STDERR_BYTES = 8192


def parse_fpcalc_output(stdout: str, stderr: str = "") -> tuple[str, float]:
    """Parse ``fpcalc -json`` output, with a fallback for older plain output."""

    text = stdout.strip()
    if not text:
        raise ValueError("fpcalc produced no output")
    if text.startswith("{"):
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"fpcalc returned invalid JSON: {exc}") from exc
        if not isinstance(document, Mapping):
            raise ValueError("fpcalc returned a non-object JSON document")
        fingerprint = document.get("fingerprint")
        duration = document.get("duration")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise ValueError("fpcalc output has no fingerprint")
        if not isinstance(duration, (int, float, str)):
            raise ValueError("fpcalc output has no duration")
        try:
            duration_value = float(duration)
        except (TypeError, ValueError) as exc:
            raise ValueError("fpcalc output has no duration") from exc
        if duration_value < 0:
            raise ValueError("fpcalc duration must not be negative")
        return fingerprint.strip(), duration_value

    fingerprint = None
    duration = None
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        if key.strip().upper() == "FINGERPRINT":
            fingerprint = value.strip()
        elif key.strip().upper() == "DURATION":
            try:
                duration = float(value.strip())
            except ValueError:
                duration = None
    if not fingerprint or duration is None or duration < 0:
        detail = stderr.strip()[:500]
        raise ValueError(f"fpcalc output could not be parsed{': ' + detail if detail else ''}")
    return fingerprint, duration


class ChromaprintObserver:
    """Run ``fpcalc`` and emit ``audio.fingerprint.v1``."""

    manifest = CapabilityManifest(
        id="chromaprint",
        version="1",
        kind=CapabilityKind.OBSERVER,
        requires=(MEDIA_ARTIFACT,),
        provides=(FINGERPRINT_ARTIFACT,),
        cost=Cost(cpu_seconds=0.5, memory_mb=32.0),
        determinism=Determinism.DETERMINISTIC,
        cacheable=True,
        side_effects=SideEffect.NONE,
        description="Fingerprint audio locally with Chromaprint's fpcalc.",
        license="LGPL-2.1-or-later (external fpcalc process)",
    )

    def __init__(
        self,
        executable: str = "fpcalc",
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_stdout_bytes: int = DEFAULT_MAX_STDOUT_BYTES,
        max_stderr_bytes: int = DEFAULT_MAX_STDERR_BYTES,
        version: str | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_stdout_bytes < 1 or max_stderr_bytes < 1:
            raise ValueError("output limits must be positive")
        self._executable = (
            os.environ.get("TRACKFORGE_FPCALC_BINARY", executable)
            if executable == "fpcalc"
            else executable
        )
        self._timeout_seconds = timeout_seconds
        self._max_stdout_bytes = max_stdout_bytes
        self._max_stderr_bytes = max_stderr_bytes
        self._version = version
        self._resolved_version: str | None = version

    async def run(self, ctx: StepContext) -> StepResult:
        media_step = ctx.inputs.get(MEDIA_ARTIFACT)
        if media_step is None:
            return StepResult.skipped(
                warnings=(
                    StepWarning(
                        code="fingerprint_missing_media",
                        message="fpcalc requires a media input artifact",
                        capability_id=self.manifest.id,
                    ),
                )
            )
        media = media_input(media_step)
        path = Path(media.cas_path)
        if not path.is_file():
            return StepResult.skipped(
                warnings=(
                    StepWarning(
                        code="fingerprint_missing_media",
                        message="media bytes are missing from the content-addressed store",
                        capability_id=self.manifest.id,
                        user_action="Re-acquire the source file.",
                    ),
                )
            )
        if self._resolve_executable() is None:
            return StepResult.skipped(
                warnings=(
                    StepWarning(
                        code="fpcalc_missing",
                        message="fpcalc is not installed; fingerprinting and AcoustID are skipped",
                        capability_id=self.manifest.id,
                        user_action=(
                            "Install Chromaprint's fpcalc and ensure it is on PATH "
                            "to enable fingerprint-based identification."
                        ),
                    ),
                )
            )
        try:
            fingerprint, duration = await asyncio.to_thread(self._invoke, path)
        except ValueError as exc:
            return StepResult.skipped(
                warnings=(
                    StepWarning(
                        code="fpcalc_failed",
                        message=str(exc),
                        capability_id=self.manifest.id,
                        user_action="Check the audio file and the fpcalc installation.",
                    ),
                )
            )
        version = await asyncio.to_thread(self._tool_version) or "unknown"
        payload = AudioFingerprintPayload(
            fingerprint=fingerprint,
            duration_seconds=duration,
            tool=FingerprintTool(id="fpcalc", version=version),
        )
        return StepResult(
            artifacts=(
                ArtifactDraft(
                    type=FINGERPRINT_ARTIFACT,
                    payload=payload.model_dump(mode="json"),
                ),
            ),
            actual_cost=Cost(cpu_seconds=0.5, memory_mb=32.0),
        )

    def _resolve_executable(self) -> str | None:
        if Path(self._executable).is_file():
            return self._executable
        return shutil.which(self._executable)

    def _tool_version(self) -> str | None:
        if self._resolved_version is not None:
            return self._resolved_version
        executable = self._resolve_executable()
        if executable is None:
            return None
        try:
            process = subprocess.run(  # noqa: S603 - fixed argv, shell=False
                [executable, "-version"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            self._resolved_version = "unknown"
            return self._resolved_version
        text = (process.stdout or process.stderr).decode("utf-8", errors="replace").strip()
        version = text.splitlines()[0].strip() if text else "unknown"
        if version.casefold().startswith("fpcalc version "):
            version = version[len("fpcalc version ") :].strip()
        self._resolved_version = version or "unknown"
        return self._resolved_version

    def _invoke(self, path: Path) -> tuple[str, float]:
        executable = self._resolve_executable()
        if executable is None:
            raise ValueError("fpcalc is not installed")
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed argv, shell=False
                [executable, "-json", str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except OSError as exc:
            raise ValueError(f"fpcalc could not be started: {exc}") from exc
        try:
            stdout, stderr = process.communicate(timeout=self._timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()
            raise ValueError("fpcalc timed out") from exc
        stdout_text = stdout[: self._max_stdout_bytes].decode("utf-8", errors="replace")
        stderr_text = stderr[: self._max_stderr_bytes].decode("utf-8", errors="replace")
        if process.returncode != 0:
            detail = stderr_text.strip() or f"exit code {process.returncode}"
            raise ValueError(f"fpcalc failed: {detail}")
        return parse_fpcalc_output(stdout_text, stderr_text)


# Compatibility alias matching the adapter module name.
# Compatibility aliases used by callers and plugin authors.
ChromaprintFingerprintObserver = ChromaprintObserver
FpcalcObserver = ChromaprintObserver
ChromaprintFingerprint = ChromaprintObserver


def fingerprint_tool_from_payload(value: Any) -> FingerprintTool:
    """Decode a tool description from an artifact payload mapping."""

    return FingerprintTool.model_validate(value)


__all__ = [
    "ChromaprintFingerprint",
    "ChromaprintFingerprintObserver",
    "ChromaprintObserver",
    "DEFAULT_TIMEOUT_SECONDS",
    "FpcalcObserver",
    "fingerprint_tool_from_payload",
    "parse_fpcalc_output",
]

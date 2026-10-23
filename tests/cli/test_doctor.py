"""CLI doctor tests, including critical failure and JSON output."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from tests.cli.conftest import cli_paths, invoke, parse_json


def test_doctor_reports_ok_or_warning_not_critical(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    data, library = cli_dirs

    result = invoke("doctor", *cli_paths(data, library), "--json")

    assert result.exit_code == 0, result.stderr
    payload = parse_json(result)
    assert payload["status"] in {"ok", "warning"}
    names = {check["name"] for check in payload["checks"]}
    assert {"python", "ffmpeg", "ffprobe", "mutagen", "catalog", "disk_space"} <= names


def test_doctor_missing_ffmpeg_is_critical_and_exits_two(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, library = cli_dirs
    real_which = shutil.which

    def fake_which(binary: str) -> str | None:
        if binary in {"ffmpeg", "ffprobe"}:
            return None
        return real_which(binary)

    monkeypatch.setattr(shutil, "which", fake_which)

    result = invoke("doctor", *cli_paths(data, library), "--json")

    assert result.exit_code == 2
    payload = parse_json(result)
    assert payload["status"] == "critical"
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["ffmpeg"]["status"] == "critical"
    assert checks["ffprobe"]["status"] == "critical"


def test_doctor_unwritable_library_is_critical(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    data, library = cli_dirs
    library.mkdir(parents=True)
    library.chmod(0o500)
    try:
        result = invoke("doctor", *cli_paths(data, library), "--json")
    finally:
        library.chmod(0o700)

    assert result.exit_code == 2
    payload = parse_json(result)
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["library_dir"]["status"] == "critical"


def test_doctor_does_not_print_environment_values(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, library = cli_dirs
    monkeypatch.setenv("TRACKFORGE_ACOUSTID_API_KEY", "super-secret-value")

    result = invoke("doctor", *cli_paths(data, library), "--json")

    assert "super-secret-value" not in result.stdout
    assert "super-secret-value" not in result.stderr

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


def test_doctor_reports_metadata_provider_checks_without_values(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, library = cli_dirs
    monkeypatch.setenv("TRACKFORGE_ACOUSTID_API_KEY", "super-secret-value")
    monkeypatch.setenv("TRACKFORGE_MUSICBRAINZ_USER_AGENT", "person@example.com")

    result = invoke("doctor", *cli_paths(data, library), "--json")

    assert result.exit_code == 0, result.stderr
    payload = parse_json(result)
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["acoustid_api_key"]["status"] == "ok"
    assert checks["musicbrainz_user_agent"]["status"] == "ok"
    assert checks["acoustid_api_key"]["details"] == {
        "configured": True,
        "env": "TRACKFORGE_ACOUSTID_API_KEY",
    }
    assert "super-secret-value" not in result.stdout
    assert "person@example.com" not in result.stdout


def test_doctor_reports_missing_asr_model_without_downloading(
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, library = cli_dirs
    monkeypatch.setenv("TRACKFORGE_ASR_MODEL", "Systran/faster-whisper-small")
    monkeypatch.setenv("TRACKFORGE_ASR_MODEL_PATH", str(data / "models" / "absent"))
    monkeypatch.setenv("TRACKFORGE_ASR_LICENSE", "MIT")

    result = invoke("doctor", *cli_paths(data, library), "--json")

    assert result.exit_code == 0, result.stderr
    payload = parse_json(result)
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["asr"]["status"] == "warning"
    assert checks["asr"]["details"]["configured"] is True
    assert checks["asr"]["details"]["path_exists"] is False
    assert isinstance(checks["asr"]["details"]["faster_whisper"], bool)
    assert not (data / "models" / "absent").exists()


def test_doctor_reports_missing_demucs_model_without_downloading(
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, library = cli_dirs
    monkeypatch.setenv("TRACKFORGE_STEMS_MODEL", "htdemucs")
    monkeypatch.setenv("TRACKFORGE_STEMS_LICENSE", "MIT")

    result = invoke("doctor", *cli_paths(data, library), "--json")

    assert result.exit_code == 0, result.stderr
    payload = parse_json(result)
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["stems"]["status"] == "warning"
    assert checks["stems"]["details"]["configured"] is True
    assert checks["stems"]["details"]["path_exists"] is False
    assert isinstance(checks["stems"]["details"]["torch"], bool)
    assert isinstance(checks["stems"]["details"]["demucs"], bool)
    assert not (data / "models" / "stems").exists()


def test_doctor_reports_proxy_routes_without_values(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, library = cli_dirs
    proxy_value = "socks5h://user:super-secret@proxy.example:1080"
    monkeypatch.setenv("TRACKFORGE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRACKFORGE_YTDLP_PROXY", proxy_value)
    monkeypatch.setenv(
        "TRACKFORGE_YTDLP_SITE_PROXIES",
        '{"youtube.com": "socks5h://user:super-secret@proxy.example:1080"}',
    )

    result = invoke("doctor", *cli_paths(data, library), "--json")

    assert result.exit_code == 0, result.stderr
    assert "super-secret" not in result.stdout
    payload = parse_json(result)
    checks = {check["name"]: check for check in payload["checks"]}
    check = checks["network_proxies"]
    assert check["status"] == "ok"
    assert check["details"]["ytdlp_default"] is True
    assert check["details"]["ytdlp_site_routes"] == ["youtube.com"]


def test_doctor_reports_configured_cookie_routes_without_contents(
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data, library = cli_dirs
    monkeypatch.setenv("TRACKFORGE_CONFIG_DIR", str(tmp_path / "config"))
    cookie = tmp_path / "youtube.txt"
    cookie.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    cookie.chmod(0o600)
    monkeypatch.setenv(
        "TRACKFORGE_YTDLP_COOKIE_FILES",
        '{"youtube.com": "' + str(cookie) + '"}',
    )

    result = invoke("doctor", *cli_paths(data, library), "--json")

    assert result.exit_code == 0, result.stderr
    assert "Netscape" not in result.stdout
    payload = parse_json(result)
    checks = {check["name"]: check for check in payload["checks"]}
    route = checks["cookie_routes"]
    assert route["status"] == "ok"
    assert route["details"]["hosts"] == ["youtube.com"]
    assert route["details"]["missing"] == []


def test_ejs_check_inspects_ytdlp_python_environment(tmp_path: Path) -> None:
    import sys

    from trackforge.application.use_cases.doctor import (
        DoctorStatus,
        _executable_has_python_module,
        _ytdlp_ejs_check,
    )

    wrapper = tmp_path / "yt-dlp-wrapper"
    wrapper.write_text(f"#!{sys.executable}\n", encoding="utf-8")
    wrapper.chmod(0o755)

    assert _executable_has_python_module(wrapper, "json") is True
    assert _executable_has_python_module(wrapper, "definitely_missing_module") is False

    check = _ytdlp_ejs_check(
        lambda _name: False,
        lambda _name: str(wrapper),
        "yt-dlp",
    )
    assert check.name == "yt-dlp-ejs"
    assert check.status in {DoctorStatus.OK, DoctorStatus.WARNING}

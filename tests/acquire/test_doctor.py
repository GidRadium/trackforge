"""L1.0 doctor tests: offline default, failures and opt-in network reports."""

# pyright: standard

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from trackforge.acquire.config import AppConfig, SourceProfile
from trackforge.acquire.doctor import (
    CheckStatus,
    Doctor,
    DoctorCheck,
    NetworkProbeResult,
    _cookies_accepted,
    run_doctor,
)


def write_cookie_file(path: Path, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tCOOKIE-SECRET\n",
        encoding="utf-8",
    )
    os.chmod(path, mode)
    return path


def youtube_config(tmp_path: Path, **kwargs) -> AppConfig:
    cookie_file = write_cookie_file(tmp_path / "youtube.txt")
    return AppConfig(
        sources={
            "youtube": SourceProfile(
                name="youtube",
                cookies_file=cookie_file,
                **kwargs,
            )
        }
    )


def stub_environment_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    def ok(name: str):
        return lambda self: DoctorCheck(name, CheckStatus.OK, "stub")

    for method, name in (
        ("_check_ytdlp", "yt-dlp"),
        ("_check_ejs", "yt-dlp-ejs"),
        ("_check_deno", "deno"),
        ("_check_ffmpeg", "ffmpeg"),
        ("_check_yandex_plugin", "yt-dlp-yandex-music"),
    ):
        monkeypatch.setattr(Doctor, method, ok(name))


def test_offline_doctor_does_not_perform_network_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_environment_checks(monkeypatch)
    config = youtube_config(tmp_path)

    def forbidden_probe(*args, **kwargs):
        raise AssertionError("offline doctor attempted a network probe")

    monkeypatch.setattr(Doctor, "_probe_network", forbidden_probe)
    report = run_doctor(config, "youtube")

    assert report.ok
    network = next(check for check in report.checks if check.name == "network")
    assert network.status is CheckStatus.SKIP
    assert "--check-network" in network.message


def test_missing_youtube_cookies_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_environment_checks(monkeypatch)
    config = AppConfig(sources={"youtube": SourceProfile(name="youtube")})
    report = run_doctor(config, "youtube", check_network=False)

    cookies = next(check for check in report.checks if check.name == "cookies")
    assert cookies.status is CheckStatus.FAIL
    assert not report.ok
    assert any(check.name == "cookies" for check in report.failed)


def test_world_readable_cookies_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_environment_checks(monkeypatch)
    cookie_file = write_cookie_file(tmp_path / "youtube.txt", 0o644)
    config = AppConfig(
        sources={
            "youtube": SourceProfile(name="youtube", cookies_file=cookie_file)
        }
    )
    report = run_doctor(config, "youtube")
    cookies = next(check for check in report.checks if check.name == "cookies")
    assert cookies.status is CheckStatus.FAIL
    assert "world-readable" in cookies.message


def test_proxy_env_without_environment_is_a_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_environment_checks(monkeypatch)
    config = youtube_config(tmp_path, proxy="env")
    report = run_doctor(config, "youtube", env={})

    proxy = next(check for check in report.checks if check.name == "proxy")
    assert proxy.status is CheckStatus.WARN
    assert report.ok
    assert report.warnings


def test_invalid_proxy_is_reported_without_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_environment_checks(monkeypatch)
    config = youtube_config(
        tmp_path,
        proxy="http://user:supersecret@",
    )
    report = run_doctor(config, "youtube", env={})
    proxy = next(check for check in report.checks if check.name == "proxy")
    assert proxy.status is CheckStatus.FAIL
    assert "supersecret" not in proxy.message
    assert "user" not in proxy.message


def test_proxy_none_is_explicit_and_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_environment_checks(monkeypatch)
    config = youtube_config(tmp_path, proxy="none")
    report = run_doctor(config, "youtube")
    proxy = next(check for check in report.checks if check.name == "proxy")
    assert proxy.status is CheckStatus.OK
    assert "none" in proxy.message


def test_network_probe_report_is_redacted_and_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_environment_checks(monkeypatch)
    config = youtube_config(
        tmp_path,
        proxy="http://user:supersecret@proxy.test:8080",
    )

    def fake_probe(profile, proxy, timeout):
        assert timeout == 3.0
        return NetworkProbeResult(
            proxy_ok=True,
            proxy_message="proxy works: http://user:supersecret@proxy.test:8080",
            cookies_status=CheckStatus.OK,
            cookies_message="cookies accepted by YouTube",
        )

    report = run_doctor(
        config,
        "youtube",
        check_network=True,
        timeout=3.0,
        probe=fake_probe,
    )
    payload = json.dumps(report.as_dict(), ensure_ascii=False)
    assert "cookies accepted by YouTube" in payload
    assert "supersecret" not in payload
    assert "user:supersecret" not in payload
    assert report.ok


def test_network_probe_failure_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_environment_checks(monkeypatch)
    config = youtube_config(tmp_path, proxy="none")

    def fake_probe(profile, proxy, timeout):
        return NetworkProbeResult(
            proxy_ok=False,
            proxy_message="connection failed",
            cookies_status=CheckStatus.SKIP,
            cookies_message="cookies not checked",
        )

    report = run_doctor(
        config,
        "youtube",
        check_network=True,
        probe=fake_probe,
    )
    network_proxy = next(
        check for check in report.checks if check.name == "network.proxy"
    )
    assert network_proxy.status is CheckStatus.FAIL
    assert not report.ok


def test_invalid_proxy_skips_network_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_environment_checks(monkeypatch)
    config = youtube_config(tmp_path, proxy="http://user:supersecret@")

    def forbidden_probe(*args, **kwargs):
        raise AssertionError("network probe must not run with an invalid proxy")

    report = run_doctor(
        config,
        "youtube",
        check_network=True,
        probe=forbidden_probe,
    )
    network = next(check for check in report.checks if check.name == "network")
    assert network.status is CheckStatus.FAIL
    assert "invalid" in network.message
    assert "supersecret" not in repr(report)


def test_cookie_acceptance_detection_uses_logged_in_marker() -> None:
    body = '... "LOGGED_IN":true, "PAGE_BUILD_LABEL":"youtube.desktop.web" ...'
    assert _cookies_accepted("https://www.youtube.com/feed/subscriptions", body)
    assert not _cookies_accepted(
        "https://accounts.google.com/ServiceLogin",
        "",
    )
    assert not _cookies_accepted(
        "https://www.youtube.com/feed/subscriptions",
        "Sign in to YouTube",
    )


def test_doctor_report_does_not_expose_cookie_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_environment_checks(monkeypatch)
    config = youtube_config(tmp_path)
    report = run_doctor(config, "youtube")
    rendered = repr(report) + json.dumps(report.as_dict())
    assert "COOKIE-SECRET" not in rendered

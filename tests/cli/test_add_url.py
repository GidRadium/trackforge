"""CLI URL ingest tests using the fake yt-dlp executable."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tests.adapters.ytdlp.conftest import FAKE_DIR, YOUTUBE_URL, read_log
from tests.cli.conftest import cli_paths, invoke, parse_json
from tests.fixtures.media import skip_without_media_tools

SECRET_COOKIE = "cli-secret-cookie-value"  # noqa: S105


@pytest.fixture
def fake_ytdlp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    monkeypatch.setenv("PATH", f"{FAKE_DIR}:{__import__('os').environ.get('PATH', '')}")
    log = tmp_path / "yt-dlp-log.jsonl"
    monkeypatch.setenv("TRACKFORGE_FAKE_YTDLP_LOG", str(log))
    return log


def _write_cookie(config_dir: Path, *, mode: int = 0o600) -> Path:
    cookies = config_dir / "cookies"
    cookies.mkdir(parents=True, exist_ok=True)
    path = cookies / "youtube.com.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        f".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\t{SECRET_COOKIE}\n",
        encoding="utf-8",
    )
    path.chmod(mode)
    return path


def _artifacts(data: Path) -> list[tuple[str, str]]:
    database = data / "catalog.sqlite"
    assert database.is_file()
    with sqlite3.connect(database) as connection:
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute("SELECT type, payload_path FROM artifacts")
        ]


def test_plan_url_is_side_effect_free(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    fake_ytdlp: Path,
) -> None:
    data, library = cli_dirs
    result = invoke("plan", YOUTUBE_URL, *cli_paths(data, library), "--json")

    assert result.exit_code == 0, result.stderr
    payload = parse_json(result)
    assert payload["blocked"] is False
    candidate = payload["candidates"][0]
    assert [fmt["format_id"] for fmt in candidate["formats"]] == ["140", "251"]
    assert candidate["extra"]["selected_format_id"] == "140"
    assert not data.exists()
    assert not library.exists() or not any(library.iterdir())
    calls = read_log(fake_ytdlp)
    assert calls
    assert any("--dump-single-json" in call for call in calls)
    assert not any("-o" in call for call in calls)


def test_add_url_dry_run_downloads_nothing(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    fake_ytdlp: Path,
) -> None:
    data, library = cli_dirs
    result = invoke(
        "add",
        YOUTUBE_URL,
        *cli_paths(data, library),
        "--auto",
        "--dry-run",
        "--json",
    )

    assert result.exit_code == 0, result.stderr
    payload = parse_json(result)
    assert payload["blocked"] is False
    assert payload["candidates"]
    assert not data.exists()
    assert not library.exists() or not any(library.iterdir())
    assert not any("-o" in call for call in read_log(fake_ytdlp))


def test_add_url_creates_library_intake_and_snapshot(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    fake_ytdlp: Path,
) -> None:
    skip_without_media_tools()
    data, library = cli_dirs
    cookie = _write_cookie(data / "config")

    result = invoke(
        "add",
        YOUTUBE_URL,
        *cli_paths(data, library),
        "--auto",
        "--json",
    )

    assert result.exit_code == 0, result.stderr
    payload = parse_json(result)
    assert payload["status"] == "completed"
    item = payload["items"][0]
    assert item["status"] == "published"
    output = Path(item["output_path"])
    assert output.is_file()
    assert output.parent == library
    assert output.suffix == ".m4a"
    sidecars = list(library.glob("trackforge*.json"))
    assert sidecars

    with sqlite3.connect(data / "catalog.sqlite") as connection:
        intake = connection.execute(
            "SELECT source_snapshot_artifact_id, downloader_id FROM intakes"
        ).fetchone()
        assert intake is not None
        assert intake[0]
        assert intake[1] == "ytdlp"
        media = connection.execute("SELECT content_hash FROM media_objects").fetchone()
        assert media is not None and len(media[0]) == 64

    artifacts = _artifacts(data)
    types = {artifact_type for artifact_type, _ in artifacts}
    assert "source.snapshot.v1" in types
    assert "source.metadata.v1" in types
    payloads = [Path(path).read_text(encoding="utf-8") for _, path in artifacts]
    assert not any(SECRET_COOKIE in text for text in payloads)

    args = read_log(fake_ytdlp)[-1]
    assert "--cookies" in args
    assert str(cookie.resolve()) in args
    assert SECRET_COOKIE not in json.dumps(args)
    assert not any(
        SECRET_COOKIE in path.read_text(encoding="utf-8") for path in library.glob("*.json")
    )


def test_repeat_add_uses_cached_intake_and_does_not_download(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    fake_ytdlp: Path,
) -> None:
    skip_without_media_tools()
    data, library = cli_dirs
    first = invoke("add", YOUTUBE_URL, *cli_paths(data, library), "--auto", "--json")
    assert first.exit_code == 0, first.stderr
    first_item = parse_json(first)["items"][0]["item_id"]
    files_before = sorted(path.name for path in library.iterdir())
    download_calls_before = sum("-o" in call for call in read_log(fake_ytdlp))

    second = invoke("add", YOUTUBE_URL, *cli_paths(data, library), "--auto", "--json")

    assert second.exit_code == 0, second.stderr
    second_payload = parse_json(second)
    assert second_payload["cache_hit"] is True
    assert second_payload["items"][0]["item_id"] == first_item
    assert sorted(path.name for path in library.iterdir()) == files_before
    assert sum("-o" in call for call in read_log(fake_ytdlp)) == download_calls_before


def test_offline_url_does_not_invoke_ytdlp(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    fake_ytdlp: Path,
) -> None:
    data, library = cli_dirs
    result = invoke(
        "plan",
        YOUTUBE_URL,
        *cli_paths(data, library),
        "--offline",
        "--json",
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["type"] == "OfflineModeError"
    assert read_log(fake_ytdlp) == []


def test_offline_add_does_not_invoke_ytdlp(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    fake_ytdlp: Path,
) -> None:
    data, library = cli_dirs
    result = invoke(
        "add",
        YOUTUBE_URL,
        *cli_paths(data, library),
        "--auto",
        "--offline",
        "--json",
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["type"] == "OfflineModeError"
    assert read_log(fake_ytdlp) == []


def test_rate_limit_is_typed_provider_error(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    fake_ytdlp: Path,
) -> None:
    data, library = cli_dirs
    result = invoke(
        "plan",
        "https://www.youtube.com/watch?v=429",
        *cli_paths(data, library),
        "--json",
    )

    assert result.exit_code == 3
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["type"] == "RateLimited"
    assert payload["retryable"] is True


def test_invalid_metadata_json_is_extractor_broken(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    fake_ytdlp: Path,
) -> None:
    data, library = cli_dirs
    result = invoke(
        "plan",
        "https://www.youtube.com/watch?v=invalidjson",
        *cli_paths(data, library),
        "--json",
    )

    assert result.exit_code == 3
    payload = json.loads(result.stderr)
    assert payload["type"] == "ExtractorBroken"


def test_cookie_with_wrong_mode_is_rejected(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    fake_ytdlp: Path,
) -> None:
    data, library = cli_dirs
    _write_cookie(data / "config", mode=0o644)
    result = invoke(
        "plan",
        YOUTUBE_URL,
        *cli_paths(data, library),
        "--json",
    )

    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["type"] == "CookiesPermissionsError"
    assert read_log(fake_ytdlp) == []


def test_doctor_sees_fake_ytdlp_version_without_secrets(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    fake_ytdlp: Path,
) -> None:
    data, library = cli_dirs
    _write_cookie(data / "config")
    result = invoke("doctor", *cli_paths(data, library), "--json")

    payload = parse_json(result)
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["yt-dlp"]["status"] == "ok"
    assert checks["yt-dlp"]["details"]["version"] == "2026.10.21"
    assert checks["cookies"]["status"] == "ok"
    assert SECRET_COOKIE not in result.stdout
    assert SECRET_COOKIE not in result.stderr

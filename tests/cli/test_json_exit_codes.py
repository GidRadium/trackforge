"""JSON output shape and exit-code mapping tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.cli.conftest import cli_paths, invoke, parse_json


def test_import_missing_path_json_error_exits_two(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    data, library = cli_dirs

    result = invoke(
        "import",
        str(tmp_path / "missing.mp3"),
        *cli_paths(data, library),
        "--auto",
        "--json",
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert len(result.stderr.strip().splitlines()) == 1
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["type"] == "UnsupportedSource"
    assert payload["error"]["type"] == "UnsupportedSource"


def test_unsupported_extension_json_error_exits_two(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    data, library = cli_dirs
    source = tmp_path / "input.txt"
    source.write_text("not audio", encoding="utf-8")

    result = invoke(
        "import",
        str(source),
        *cli_paths(data, library),
        "--auto",
        "--json",
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["type"] == "UnsupportedSource"


def test_resolve_invalid_item_id_exits_two_with_json(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    data, library = cli_dirs

    result = invoke(
        "resolve",
        "not-an-item-id",
        *cli_paths(data, library),
        "--json",
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["type"] == "RecordNotFound"


def test_invalid_config_json_error_exits_two(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRACKFORGE_CORE_PROFILE", "not-a-profile")

    result = invoke("config", "show", "--json")

    assert result.exit_code == 2
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["type"] == "ConfigError"
    assert "profile" in payload["message"]


def test_invalid_force_step_exits_two(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    data, library = cli_dirs

    result = invoke(
        "import",
        str(tmp_path / "missing.mp3"),
        *cli_paths(data, library),
        "--auto",
        "--force",
        "explode",
        "--json",
    )

    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["type"] == "DomainValidationError"


def test_invalid_target_format_exits_two(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    data, library = cli_dirs
    source = tmp_path / "input.mp3"
    source.write_bytes(b"not really audio")

    result = invoke(
        "import",
        str(source),
        *cli_paths(data, library),
        "--auto",
        "--target-format",
        "wav",
        "--json",
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr)["type"] == "DomainValidationError"


def test_doctor_json_is_one_object(
    tmp_path: Path,
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    data, library = cli_dirs

    result = invoke("doctor", *cli_paths(data, library), "--json")

    assert len(result.stdout.strip().splitlines()) == 1
    assert isinstance(parse_json(result), dict)

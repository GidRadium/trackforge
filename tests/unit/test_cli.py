"""Unit tests for the thin CLI adapter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from trackforge.interfaces.cli.main import app

pytestmark = pytest.mark.unit

runner = CliRunner()


def invoke(*args: str) -> Result:
    return runner.invoke(app, list(args))


def test_version_prints_name_and_version(clean_env: None) -> None:
    result = invoke("--version")

    assert result.exit_code == 0
    assert result.stdout == "trackforge 0.1.0\n"
    assert result.exception is None


def test_version_does_not_load_invalid_config(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.setenv("TRACKFORGE_CORE_PROFILE", "invalid")

    result = invoke("--version")

    assert result.exit_code == 0
    assert result.stdout == "trackforge 0.1.0\n"


def test_config_show_json_is_single_machine_readable_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.setenv("TRACKFORGE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRACKFORGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRACKFORGE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("TRACKFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("TRACKFORGE_LIBRARY_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("TRACKFORGE_CORE_PROFILE", "fast")
    monkeypatch.setenv("TRACKFORGE_CORE_MODE", "llm_assisted")
    monkeypatch.setenv("TRACKFORGE_CORE_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("TRACKFORGE_CORE_OFFLINE", "true")

    result = invoke("config", "show", "--json")

    assert result.exit_code == 0
    assert len(result.stdout.splitlines()) == 1
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "asr_backend",
        "asr_beam_size",
        "asr_checksum",
        "asr_compute_type",
        "asr_heavy",
        "asr_license",
        "asr_memory_mb",
        "asr_model",
        "asr_model_path",
        "asr_model_version",
        "asr_size_bytes",
        "asr_threads",
        "asr_vad_filter",
        "cache_dir",
        "config_dir",
        "data_dir",
        "library_dir",
        "llm_api_key_env",
        "llm_base_url",
        "llm_max_cost_usd_per_item",
        "llm_max_retries",
        "llm_max_tokens",
        "llm_model",
        "llm_price_in",
        "llm_price_out",
        "llm_provider",
        "llm_proxy",
        "llm_strict_schema",
        "llm_temperature",
        "llm_thinking",
        "llm_timeout_seconds",
        "log_level",
        "metadata_proxy",
        "mode",
        "models_max_heavy_models",
        "models_offline",
        "models_proxy",
        "offline",
        "profile",
        "ranker_backend",
        "ranker_batch_size",
        "ranker_checksum",
        "ranker_heavy",
        "ranker_license",
        "ranker_max_length",
        "ranker_model",
        "ranker_model_path",
        "ranker_model_version",
        "ranker_size_bytes",
        "ranker_strategy",
        "ranker_threads",
        "send_audio_to_cloud",
        "send_text_to_cloud",
        "state_dir",
        "stems_backend",
        "stems_checksum",
        "stems_heavy",
        "stems_jobs",
        "stems_license",
        "stems_memory_mb",
        "stems_model",
        "stems_model_file",
        "stems_model_path",
        "stems_model_url",
        "stems_model_version",
        "stems_overlap",
        "stems_segment",
        "stems_size_bytes",
        "stems_threads",
        "ytdlp_cookie_files",
        "ytdlp_proxy",
        "ytdlp_site_proxies",
    }
    assert payload["profile"] == "fast"
    assert payload["mode"] == "llm_assisted"
    assert payload["offline"] is True
    assert payload["data_dir"] == str(tmp_path / "data")


def test_config_show_human_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.setenv("TRACKFORGE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRACKFORGE_CORE_PROFILE", "full")

    result = invoke("config", "show")

    assert result.exit_code == 0
    assert "profile: full" in result.stdout
    assert "{" not in result.stdout


def test_config_show_reads_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('[core]\nprofile = "custom"\n', encoding="utf-8")
    monkeypatch.setenv("TRACKFORGE_CONFIG_DIR", str(config_dir))

    result = invoke("config", "show", "--json")

    assert result.exit_code == 0
    assert json.loads(result.stdout)["profile"] == "custom"


def test_config_validate_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    monkeypatch.setenv("TRACKFORGE_CONFIG_DIR", str(tmp_path / "config"))

    result = invoke("config", "validate")

    assert result.exit_code == 0
    assert "valid" in result.stdout.lower()


def test_config_validate_json_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    monkeypatch.setenv("TRACKFORGE_CONFIG_DIR", str(tmp_path / "config"))

    result = invoke("config", "validate", "--json")

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"valid": True}


def test_config_validate_invalid_config_exits_two(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.setenv("TRACKFORGE_CORE_PROFILE", "invalid")

    result = invoke("config", "validate")

    assert result.exit_code == 2
    assert "profile" in result.stderr
    assert "Traceback" not in result.stderr


def test_config_show_invalid_config_exits_two(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.setenv("TRACKFORGE_CORE_MODE", "invalid")

    result = invoke("config", "show", "--json")

    assert result.exit_code == 2
    assert "mode" in result.stderr
    assert "Traceback" not in result.stderr


def test_config_validate_unknown_key_exits_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('[core]\nunknown = "x"\n', encoding="utf-8")
    monkeypatch.setenv("TRACKFORGE_CONFIG_DIR", str(config_dir))

    result = invoke("config", "validate")

    assert result.exit_code == 2
    assert "unknown" in result.stderr


def test_config_show_redacts_proxy_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    proxy_value = "socks5h://user:super-secret@proxy.example:1080"
    monkeypatch.setenv("TRACKFORGE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRACKFORGE_YTDLP_PROXY", proxy_value)
    monkeypatch.setenv(
        "TRACKFORGE_YTDLP_SITE_PROXIES",
        '{"youtube.com": "socks5h://user:super-secret@proxy.example:1080"}',
    )

    result = invoke("config", "show", "--json")

    assert result.exit_code == 0
    assert "super-secret" not in result.stdout
    payload = json.loads(result.stdout)
    assert payload["ytdlp_proxy"] == "<configured>"
    assert payload["ytdlp_site_proxies"] == {"youtube.com": "<configured>"}

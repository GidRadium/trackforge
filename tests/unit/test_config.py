"""Unit tests for configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from trackforge.bootstrap import build_context
from trackforge.config import ConfigError, TrackForgeConfig

pytestmark = pytest.mark.unit


def test_defaults_use_platformdirs_and_music_library(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    home = tmp_path / "home"
    xdg_config = tmp_path / "xdg-config"
    xdg_data = tmp_path / "xdg-data"
    xdg_cache = tmp_path / "xdg-cache"
    xdg_state = tmp_path / "xdg-state"

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_cache))
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))

    config = TrackForgeConfig()

    assert config.config_dir == xdg_config / "trackforge"
    assert config.data_dir == xdg_data / "trackforge"
    assert config.cache_dir == xdg_cache / "trackforge"
    assert config.state_dir == xdg_state / "trackforge"
    assert config.library_dir == home / "Music" / "TrackForge"
    assert config.profile == "balanced"
    assert config.mode == "policy"
    assert config.log_level == "INFO"
    assert config.offline is False


def test_env_overrides_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.setenv("TRACKFORGE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRACKFORGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRACKFORGE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("TRACKFORGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("TRACKFORGE_LIBRARY_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("TRACKFORGE_CORE_PROFILE", "full")
    monkeypatch.setenv("TRACKFORGE_CORE_MODE", "manual")
    monkeypatch.setenv("TRACKFORGE_CORE_LOG_LEVEL", "debug")
    monkeypatch.setenv("TRACKFORGE_CORE_OFFLINE", "true")

    config = TrackForgeConfig()

    assert config.config_dir == tmp_path / "config"
    assert config.data_dir == tmp_path / "data"
    assert config.cache_dir == tmp_path / "cache"
    assert config.state_dir == tmp_path / "state"
    assert config.library_dir == tmp_path / "library"
    assert config.profile == "full"
    assert config.mode == "manual"
    assert config.log_level == "DEBUG"
    assert config.offline is True


def test_direct_env_name_is_supported(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.setenv("TRACKFORGE_PROFILE", "fast")

    assert TrackForgeConfig().profile == "fast"


def test_toml_overrides_defaults(
    toml_config_path: Path,
    tmp_path: Path,
    clean_env: None,
) -> None:
    library_dir = tmp_path / "music"
    with toml_config_path.open("a", encoding="utf-8") as file:
        file.write(f'library_dir = "{library_dir}"\n')

    config = TrackForgeConfig.from_file(toml_config_path)

    assert config.profile == "fast"
    assert config.library_dir == library_dir


def test_env_overrides_toml(
    toml_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.setenv("TRACKFORGE_CORE_PROFILE", "full")

    config = TrackForgeConfig.from_file(toml_config_path)

    assert config.profile == "full"


def test_explicit_argument_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.setenv("TRACKFORGE_CORE_PROFILE", "fast")

    config = TrackForgeConfig(profile="custom")

    assert config.profile == "custom"


def test_from_file_uses_default_config_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    config_dir = tmp_path / "config-dir"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('[core]\nprofile = "fast"\n', encoding="utf-8")
    monkeypatch.setenv("TRACKFORGE_CONFIG_DIR", str(config_dir))

    assert TrackForgeConfig.from_file().profile == "fast"


def test_missing_default_config_file_is_allowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.setenv("TRACKFORGE_CONFIG_DIR", str(tmp_path / "absent"))

    assert TrackForgeConfig.from_file().profile == "balanced"


def test_missing_explicit_config_file_raises_config_error(
    tmp_path: Path,
    clean_env: None,
) -> None:
    path = tmp_path / "missing.toml"

    with pytest.raises(ConfigError) as exc_info:
        TrackForgeConfig.from_file(path)

    assert exc_info.value.key == "config"
    assert "missing.toml" in str(exc_info.value)


@pytest.mark.parametrize(
    ("env_name", "value", "key"),
    [
        ("TRACKFORGE_CORE_PROFILE", "turbo", "profile"),
        ("TRACKFORGE_CORE_MODE", "autopilot", "mode"),
        ("TRACKFORGE_CORE_LOG_LEVEL", "verbose", "log_level"),
    ],
)
def test_invalid_enum_values_raise_config_error(
    env_name: str,
    value: str,
    key: str,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.setenv(env_name, value)

    with pytest.raises(ConfigError) as exc_info:
        TrackForgeConfig()

    assert exc_info.value.key == key
    assert key in str(exc_info.value)


def test_invalid_constructor_value_raises_config_error(clean_env: None) -> None:
    with pytest.raises(ConfigError) as exc_info:
        TrackForgeConfig(profile="turbo")  # type: ignore[arg-type]

    assert exc_info.value.key == "profile"


def test_unknown_toml_key_raises_config_error(
    tmp_path: Path,
    clean_env: None,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[core]\nmystery = "value"\n', encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        TrackForgeConfig.from_file(path)

    assert "core.mystery" in str(exc_info.value)
    assert exc_info.value.key == "core.mystery"


def test_unknown_toml_section_raises_config_error(
    tmp_path: Path,
    clean_env: None,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[library]\nlayout = "flat"\n', encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        TrackForgeConfig.from_file(path)

    assert "library" in str(exc_info.value)


def test_invalid_toml_raises_config_error(
    tmp_path: Path,
    clean_env: None,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[core\nprofile = 'fast'\n", encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        TrackForgeConfig.from_file(path)

    assert exc_info.value.key == "config"


def test_empty_env_path_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.setenv("TRACKFORGE_DATA_DIR", "")

    with pytest.raises(ConfigError) as exc_info:
        TrackForgeConfig()

    assert exc_info.value.key == "data_dir"
    assert "empty" in str(exc_info.value)


def test_path_values_expand_home(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("TRACKFORGE_LIBRARY_DIR", "~/Music/TrackForge")

    assert TrackForgeConfig().library_dir == Path("/home/tester/Music/TrackForge")


def test_config_load_does_not_create_directories_or_read_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data-home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    (tmp_path / ".env").write_text(
        "TRACKFORGE_CORE_PROFILE=fast\nTRACKFORGE_DATA_DIR=/wrong\n",
        encoding="utf-8",
    )

    config = TrackForgeConfig.from_file()

    assert config.profile == "balanced"
    assert not config.config_dir.exists()
    assert not config.data_dir.exists()
    assert not config.cache_dir.exists()
    assert not config.state_dir.exists()
    assert not config.library_dir.exists()


def test_fixture_config_is_wrapped_by_container(config: TrackForgeConfig) -> None:
    context = build_context(config)

    assert context.config is config


def test_llm_ranker_and_privacy_sections_are_loaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[llm]
provider = "openai-compatible"
base_url = "https://llm.example/v1"
model = "test-model"
api_key_env = "MY_LLM_KEY"
max_cost_usd_per_item = 0.25
price_in = 0.5
price_out = 2.0
strict_schema = false
thinking = "disabled"

[ranker]
strategy = "hybrid"
threads = 4

[models]
offline = true
max_heavy_models = 1

[models.ranker]
name = "BAAI/bge-reranker-base"
path = "ranker/model.onnx"
license = "MIT"
checksum = "abc"

[privacy]
send_text_to_cloud = true
send_audio_to_cloud = false
""",
        encoding="utf-8",
    )

    config = TrackForgeConfig.from_file(path)

    assert config.llm_base_url == "https://llm.example/v1"
    assert config.llm_model == "test-model"
    assert config.llm_api_key_env == "MY_LLM_KEY"
    assert config.llm_max_cost_usd_per_item == pytest.approx(0.25)
    assert config.llm_price_in == pytest.approx(0.5)
    assert config.llm_price_out == pytest.approx(2.0)
    assert config.llm_strict_schema is False
    assert config.llm_thinking == "disabled"
    assert config.ranker_strategy == "hybrid"
    assert config.ranker_threads == 4
    assert config.ranker_model == "BAAI/bge-reranker-base"
    assert config.ranker_model_path == "ranker/model.onnx"
    assert config.ranker_license == "MIT"
    assert config.send_text_to_cloud is True
    assert config.send_audio_to_cloud is False
    assert config.llm_configured is True


def test_llm_env_overrides_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[llm]\nbase_url = "https://toml.example/v1"\nmodel = "toml-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("TRACKFORGE_LLM_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("TRACKFORGE_LLM_MODEL", "env-model")

    config = TrackForgeConfig.from_file(path)

    assert config.llm_base_url == "https://env.example/v1"
    assert config.llm_model == "env-model"


def test_unknown_llm_key_raises_config_error(
    tmp_path: Path,
    clean_env: None,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[llm]\nmystery = "value"\n', encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        TrackForgeConfig.from_file(path)

    assert "llm.mystery" in str(caught.value)


def test_models_asr_table_is_loaded_and_projected(
    tmp_path: Path,
    clean_env: None,
) -> None:
    from trackforge.application.ports.models import ModelKind, model_specs_from_config

    path = tmp_path / "config.toml"
    path.write_text(
        """
[models.asr]
name = "Systran/faster-whisper-small"
path = "asr/small"
version = "536b0662742c02347bc0e980a01041f333bce120"
license = "MIT"
checksum = "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671"
compute_type = "int8"
threads = 8
vad_filter = false
beam_size = 3
memory_mb = 4096
""",
        encoding="utf-8",
    )

    config = TrackForgeConfig.from_file(path)
    specs = model_specs_from_config(config)
    asr = [spec for spec in specs if spec.kind is ModelKind.ASR]

    assert config.asr_model == "Systran/faster-whisper-small"
    assert config.asr_model_path == "asr/small"
    assert config.asr_compute_type == "int8"
    assert config.asr_threads == 8
    assert config.asr_vad_filter is False
    assert config.asr_beam_size == 3
    assert config.asr_memory_mb == 4096
    assert len(asr) == 1
    assert asr[0].license == "MIT"
    assert asr[0].checksum.startswith("3e3059")
    assert asr[0].metadata["repo"] == "Systran/faster-whisper-small"
    assert asr[0].metadata["compute_type"] == "int8"


def test_unknown_models_asr_key_raises_config_error(
    tmp_path: Path,
    clean_env: None,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[models.asr]\nmystery = true\n", encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        TrackForgeConfig.from_file(path)

    assert "models.asr.mystery" in str(caught.value)


def test_stems_table_is_parsed(tmp_path: Path, clean_env: None) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[models.stems]\n"
        'name = "htdemucs"\n'
        'license = "MIT"\n'
        'checksum = "abc"\n'
        "jobs = 1\n"
        "overlap = 0.25\n",
        encoding="utf-8",
    )

    config = TrackForgeConfig.from_file(path)

    assert config.stems_model == "htdemucs"
    assert config.stems_license == "MIT"
    assert config.stems_checksum == "abc"
    assert config.stems_jobs == 1
    assert config.stems_overlap == 0.25


def test_demucs_table_is_an_alias_for_stems(tmp_path: Path, clean_env: None) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[models.demucs]\nname = "htdemucs"\nlicense = "MIT"\n',
        encoding="utf-8",
    )

    config = TrackForgeConfig.from_file(path)

    assert config.stems_model == "htdemucs"
    assert config.stems_license == "MIT"


def test_stems_jobs_above_one_is_rejected(tmp_path: Path, clean_env: None) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[models.stems]\nname = "htdemucs"\njobs = 2\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc_info:
        TrackForgeConfig.from_file(path)

    assert exc_info.value.key == "stems_jobs"


def test_network_proxy_tables_are_parsed(tmp_path: Path, clean_env: None) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[network.ytdlp]\n"
        'proxy = "socks5h://user:pass@proxy.example:1080"\n'
        "[network.ytdlp.sites]\n"
        '"youtube.com" = "socks5h://user:pass@proxy.example:1080"\n'
        '"music.yandex.ru" = ""\n'
        "[network.metadata]\n"
        'proxy = "http://metadata-proxy.example:3128"\n'
        "[network.llm]\n"
        'proxy = ""\n'
        "[network.models]\n"
        'proxy = "http://models-proxy.example:3128"\n',
        encoding="utf-8",
    )

    config = TrackForgeConfig.from_file(path)

    assert config.ytdlp_proxy == "socks5h://user:pass@proxy.example:1080"
    assert config.ytdlp_site_proxies == {
        "youtube.com": "socks5h://user:pass@proxy.example:1080",
        "music.yandex.ru": "",
    }
    assert config.metadata_proxy == "http://metadata-proxy.example:3128"
    assert config.llm_proxy == ""
    assert config.models_proxy == "http://models-proxy.example:3128"


def test_network_proxy_env_override(
    tmp_path: Path,
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[network.ytdlp]\nproxy = "http://config-proxy.example:3128"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("TRACKFORGE_YTDLP_PROXY", "socks5://env-proxy.example:1080")
    monkeypatch.setenv(
        "TRACKFORGE_YTDLP_SITE_PROXIES",
        '{"youtube.com": "socks5://env-proxy.example:1080"}',
    )

    config = TrackForgeConfig.from_file(path)

    assert config.ytdlp_proxy == "socks5://env-proxy.example:1080"
    assert config.ytdlp_site_proxies == {"youtube.com": "socks5://env-proxy.example:1080"}


@pytest.mark.parametrize(
    "proxy",
    [
        "file:///tmp/proxy.sock",
        "http://proxy.example:3128/path",
        "http://proxy.example:3128?x=1",
        "proxy.example:3128",
        "http://",
    ],
)
def test_invalid_proxy_is_rejected(
    tmp_path: Path,
    clean_env: None,
    proxy: str,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[network.metadata]\nproxy = "{proxy}"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc_info:
        TrackForgeConfig.from_file(path)

    assert exc_info.value.key == "metadata_proxy"


def test_invalid_proxy_route_host_is_rejected(tmp_path: Path, clean_env: None) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[network.ytdlp.sites]\n"https://youtube.com" = "socks5://proxy.example:1080"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc_info:
        TrackForgeConfig.from_file(path)

    assert exc_info.value.key == "ytdlp_site_proxies"

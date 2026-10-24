"""Typed application configuration.

Configuration precedence is, from highest to lowest:

1. Explicit constructor arguments.
2. ``TRACKFORGE_*`` environment variables.
3. ``config.toml`` (the ``[core]`` table).
4. Built-in defaults.

The loader never creates directories and never reads ``.env`` files.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from platformdirs import (
    user_cache_dir,
    user_config_dir,
    user_data_dir,
    user_state_dir,
)
from pydantic import AliasChoices, Field, ValidationError, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    SettingsError,
)

from trackforge.config.errors import ConfigError

Profile = Literal["fast", "balanced", "full", "custom"]
Mode = Literal["policy", "llm_assisted", "llm_autopilot", "manual"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

_APP_NAME = "trackforge"
_CONFIG_DIRECTORY_FIELDS = (
    "config_dir",
    "data_dir",
    "cache_dir",
    "state_dir",
    "library_dir",
)


def _default_config_dir() -> Path:
    return Path(user_config_dir(_APP_NAME))


def _default_data_dir() -> Path:
    return Path(user_data_dir(_APP_NAME))


def _default_cache_dir() -> Path:
    return Path(user_cache_dir(_APP_NAME))


def _default_state_dir() -> Path:
    return Path(user_state_dir(_APP_NAME))


def _default_library_dir() -> Path:
    return Path.home() / "Music" / "TrackForge"


class _TomlSettingsSource(PydanticBaseSettingsSource):
    """Settings source for values already read from ``config.toml``."""

    def __init__(self, settings_cls: type[BaseSettings], values: Mapping[str, Any]) -> None:
        super().__init__(settings_cls)
        self._values = dict(values)

    def get_field_value(
        self,
        field: FieldInfo,
        field_name: str,
    ) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        # pydantic-settings keys each source by the field's preferred alias.
        # Normalizing TOML keys the same way prevents a TOML field name and an
        # environment alias from being merged as two separate inputs.
        return {_preferred_alias(field_name): value for field_name, value in self._values.items()}


def _preferred_alias(field_name: str) -> str:
    alias = TrackForgeConfig.model_fields[field_name].validation_alias
    if isinstance(alias, AliasChoices):
        for choice in alias.choices:
            if isinstance(choice, str):
                return choice
    return field_name


class TrackForgeConfig(BaseSettings):
    """Validated TrackForge configuration."""

    model_config = SettingsConfigDict(
        env_prefix="TRACKFORGE_",
        env_prefix_target="all",
        extra="forbid",
        validate_by_name=True,
        loc_by_alias=False,
        validate_default=True,
    )

    config_dir: Path = Field(
        default_factory=_default_config_dir,
        validation_alias=AliasChoices("CONFIG_DIR", "CORE_CONFIG_DIR"),
    )
    data_dir: Path = Field(
        default_factory=_default_data_dir,
        validation_alias=AliasChoices("DATA_DIR", "CORE_DATA_DIR"),
    )
    cache_dir: Path = Field(
        default_factory=_default_cache_dir,
        validation_alias=AliasChoices("CACHE_DIR", "CORE_CACHE_DIR"),
    )
    state_dir: Path = Field(
        default_factory=_default_state_dir,
        validation_alias=AliasChoices("STATE_DIR", "CORE_STATE_DIR"),
    )
    library_dir: Path = Field(
        default_factory=_default_library_dir,
        validation_alias=AliasChoices("LIBRARY_DIR", "CORE_LIBRARY_DIR"),
    )
    profile: Profile = Field(
        default="balanced",
        validation_alias=AliasChoices("CORE_PROFILE", "PROFILE"),
    )
    mode: Mode = Field(
        default="policy",
        validation_alias=AliasChoices("CORE_MODE", "MODE"),
    )
    log_level: LogLevel = Field(
        default="INFO",
        validation_alias=AliasChoices("CORE_LOG_LEVEL", "LOG_LEVEL"),
    )
    offline: bool = Field(
        default=False,
        validation_alias=AliasChoices("CORE_OFFLINE", "OFFLINE"),
    )
    # External metadata providers are opt-in.  Configuring either credential
    # enables the network observers automatically; ``metadata_enabled`` is an
    # explicit switch for deployments that want the providers without a key.
    metadata_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CORE_METADATA_ENABLED", "METADATA_ENABLED"),
    )
    acoustid_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("ACOUSTID_API_KEY"),
    )
    musicbrainz_user_agent: str = Field(
        default="",
        validation_alias=AliasChoices("MUSICBRAINZ_USER_AGENT"),
    )
    # Downloader settings are internal to the URL adapter.  ``config show``
    # filters them so the portable configuration surface stays stable.
    ytdlp_binary: str = Field(
        default="yt-dlp",
        validation_alias=AliasChoices("YTDLP_BINARY"),
    )
    deno_binary: str = Field(
        default="deno",
        validation_alias=AliasChoices("DENO_BINARY"),
    )
    ytdlp_timeout_metadata: float = Field(
        default=60.0,
        gt=0,
        validation_alias=AliasChoices("YTDLP_TIMEOUT_METADATA"),
    )
    ytdlp_timeout_download: float = Field(
        default=1800.0,
        gt=0,
        validation_alias=AliasChoices("YTDLP_TIMEOUT_DOWNLOAD"),
    )
    # Network proxies are opt-in per component.  Standard proxy environment
    # variables are intentionally ignored by the application; an empty value
    # means a direct connection.  ``ytdlp_site_proxies`` maps host suffixes to
    # proxies and is the only place a per-site exception belongs.
    ytdlp_proxy: str = Field(default="", validation_alias=AliasChoices("YTDLP_PROXY"))
    ytdlp_site_proxies: dict[str, str] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("YTDLP_SITE_PROXIES"),
    )
    metadata_proxy: str = Field(
        default="",
        validation_alias=AliasChoices("METADATA_PROXY"),
    )
    llm_proxy: str = Field(default="", validation_alias=AliasChoices("LLM_PROXY"))
    models_proxy: str = Field(default="", validation_alias=AliasChoices("MODELS_PROXY"))
    # LLM settings live in ``[llm]``.  The API key is never stored here: only
    # the environment variable name is configuration.
    llm_provider: Literal["openai-compatible"] = Field(
        default="openai-compatible",
        validation_alias=AliasChoices("LLM_PROVIDER"),
    )
    llm_base_url: str = Field(default="", validation_alias=AliasChoices("LLM_BASE_URL"))
    llm_model: str = Field(default="", validation_alias=AliasChoices("LLM_MODEL"))
    llm_api_key_env: str = Field(
        default="TRACKFORGE_LLM_API_KEY",
        validation_alias=AliasChoices("LLM_API_KEY_ENV"),
    )
    llm_max_cost_usd_per_item: float = Field(
        default=0.50,
        ge=0.0,
        validation_alias=AliasChoices("LLM_MAX_COST_USD_PER_ITEM"),
    )
    llm_max_tokens: int = Field(
        default=2000,
        gt=0,
        validation_alias=AliasChoices("LLM_MAX_TOKENS"),
    )
    llm_temperature: float = Field(
        default=0.1,
        ge=0.0,
        le=2.0,
        validation_alias=AliasChoices("LLM_TEMPERATURE"),
    )
    llm_timeout_seconds: float = Field(
        default=30.0,
        gt=0.0,
        validation_alias=AliasChoices("LLM_TIMEOUT_SECONDS"),
    )
    llm_max_retries: int = Field(
        default=1,
        ge=0,
        le=3,
        validation_alias=AliasChoices("LLM_MAX_RETRIES"),
    )
    llm_price_in: float | None = Field(
        default=None,
        ge=0.0,
        validation_alias=AliasChoices("LLM_PRICE_IN"),
    )
    llm_price_out: float | None = Field(
        default=None,
        ge=0.0,
        validation_alias=AliasChoices("LLM_PRICE_OUT"),
    )
    llm_strict_schema: bool = Field(
        default=True,
        validation_alias=AliasChoices("LLM_STRICT_SCHEMA"),
    )
    # DeepSeek enables thinking by default; metadata extraction wants it off.
    llm_thinking: Literal["default", "enabled", "disabled"] = Field(
        default="default",
        validation_alias=AliasChoices("LLM_THINKING"),
    )
    # Ranker settings live in ``[ranker]`` and ``[models.ranker]``.
    ranker_strategy: Literal["lexical", "cross_encoder", "hybrid"] = Field(
        default="lexical",
        validation_alias=AliasChoices("RANKER_STRATEGY"),
    )
    ranker_backend: str = Field(default="onnx", validation_alias=AliasChoices("RANKER_BACKEND"))
    ranker_model: str = Field(default="", validation_alias=AliasChoices("RANKER_MODEL"))
    ranker_model_path: str = Field(
        default="",
        validation_alias=AliasChoices("RANKER_MODEL_PATH"),
    )
    ranker_model_version: str = Field(
        default="",
        validation_alias=AliasChoices("RANKER_MODEL_VERSION"),
    )
    ranker_license: str = Field(default="", validation_alias=AliasChoices("RANKER_LICENSE"))
    ranker_checksum: str = Field(default="", validation_alias=AliasChoices("RANKER_CHECKSUM"))
    ranker_size_bytes: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("RANKER_SIZE_BYTES"),
    )
    ranker_heavy: bool = Field(default=True, validation_alias=AliasChoices("RANKER_HEAVY"))
    ranker_threads: int = Field(
        default=8,
        gt=0,
        validation_alias=AliasChoices("RANKER_THREADS"),
    )
    ranker_batch_size: int = Field(
        default=8,
        gt=0,
        validation_alias=AliasChoices("RANKER_BATCH_SIZE"),
    )
    ranker_max_length: int = Field(
        default=256,
        gt=0,
        validation_alias=AliasChoices("RANKER_MAX_LENGTH"),
    )
    # ASR settings live in ``[models.asr]``.  The model identifier is never
    # hardcoded: an empty value means ASR is unconfigured and degrades visibly.
    asr_backend: Literal["faster-whisper"] = Field(
        default="faster-whisper",
        validation_alias=AliasChoices("ASR_BACKEND"),
    )
    asr_model: str = Field(default="", validation_alias=AliasChoices("ASR_MODEL"))
    asr_model_path: str = Field(
        default="",
        validation_alias=AliasChoices("ASR_MODEL_PATH"),
    )
    asr_model_version: str = Field(
        default="",
        validation_alias=AliasChoices("ASR_MODEL_VERSION"),
    )
    asr_license: str = Field(default="", validation_alias=AliasChoices("ASR_LICENSE"))
    asr_checksum: str = Field(default="", validation_alias=AliasChoices("ASR_CHECKSUM"))
    asr_size_bytes: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("ASR_SIZE_BYTES"),
    )
    asr_heavy: bool = Field(default=True, validation_alias=AliasChoices("ASR_HEAVY"))
    asr_compute_type: Literal["int8", "int8_float32", "float32"] = Field(
        default="int8",
        validation_alias=AliasChoices("ASR_COMPUTE_TYPE"),
    )
    asr_threads: int = Field(
        default=8,
        ge=1,
        le=16,
        validation_alias=AliasChoices("ASR_THREADS"),
    )
    asr_vad_filter: bool = Field(
        # Plan 13/16 measured VAD cutting most sung vocals.  Speech users can
        # opt in; the music-first default keeps the full mix.
        default=False,
        validation_alias=AliasChoices("ASR_VAD_FILTER"),
    )
    asr_beam_size: int = Field(
        default=5,
        ge=1,
        le=10,
        validation_alias=AliasChoices("ASR_BEAM_SIZE"),
    )
    asr_memory_mb: int = Field(
        default=2048,
        ge=512,
        validation_alias=AliasChoices("ASR_MEMORY_MB"),
    )
    # Demucs settings live in ``[models.stems]`` (``[models.demucs]`` is
    # accepted as an alias).  An empty model id keeps stems opt-in; ``full``
    # then reports a visible model_missing skip instead of running Demucs.
    stems_backend: Literal["demucs"] = Field(
        default="demucs",
        validation_alias=AliasChoices("STEMS_BACKEND"),
    )
    stems_model: str = Field(default="", validation_alias=AliasChoices("STEMS_MODEL"))
    stems_model_path: str = Field(
        default="",
        validation_alias=AliasChoices("STEMS_MODEL_PATH"),
    )
    stems_model_version: str = Field(
        default="",
        validation_alias=AliasChoices("STEMS_MODEL_VERSION"),
    )
    stems_model_url: str = Field(
        default="",
        validation_alias=AliasChoices("STEMS_MODEL_URL"),
    )
    stems_model_file: str = Field(
        default="",
        validation_alias=AliasChoices("STEMS_MODEL_FILE"),
    )
    stems_license: str = Field(default="", validation_alias=AliasChoices("STEMS_LICENSE"))
    stems_checksum: str = Field(default="", validation_alias=AliasChoices("STEMS_CHECKSUM"))
    stems_size_bytes: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("STEMS_SIZE_BYTES"),
    )
    stems_heavy: bool = Field(default=True, validation_alias=AliasChoices("STEMS_HEAVY"))
    stems_segment: float = Field(
        default=7.8,
        gt=0.0,
        validation_alias=AliasChoices("STEMS_SEGMENT"),
    )
    stems_overlap: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("STEMS_OVERLAP"),
    )
    stems_jobs: int = Field(
        default=1,
        ge=1,
        le=1,
        validation_alias=AliasChoices("STEMS_JOBS"),
    )
    stems_threads: int = Field(
        default=8,
        ge=1,
        le=16,
        validation_alias=AliasChoices("STEMS_THREADS"),
    )
    stems_memory_mb: int = Field(
        default=3072,
        ge=1024,
        validation_alias=AliasChoices("STEMS_MEMORY_MB"),
    )
    models_offline: bool = Field(
        default=True,
        validation_alias=AliasChoices("MODELS_OFFLINE"),
    )
    models_max_heavy_models: int = Field(
        default=1,
        ge=1,
        le=1,
        validation_alias=AliasChoices("MODELS_MAX_HEAVY_MODELS"),
    )
    send_text_to_cloud: bool = Field(
        default=True,
        validation_alias=AliasChoices("PRIVACY_SEND_TEXT_TO_CLOUD"),
    )
    send_audio_to_cloud: bool = Field(
        default=False,
        validation_alias=AliasChoices("PRIVACY_SEND_AUDIO_TO_CLOUD"),
    )

    @property
    def cookies_dir(self) -> Path:
        """Per-site Netscape cookie files live under the config directory."""

        return Path(self.config_dir) / "cookies"

    @property
    def models_dir(self) -> Path:
        """Local model weights live under the data directory."""

        return Path(self.data_dir) / "models"

    @property
    def llm_configured(self) -> bool:
        """Whether an endpoint and model are configured (key presence is separate)."""

        return bool(self.llm_base_url.strip() and self.llm_model.strip())

    @property
    def llm_api_key_present(self) -> bool:
        """Whether the configured credential env variable is non-empty."""

        name = self.llm_api_key_env.strip()
        return bool(name and os.environ.get(name, "").strip())

    @property
    def ranker_extra_configured(self) -> bool:
        return self.ranker_strategy in {"cross_encoder", "hybrid"} or bool(
            self.ranker_model.strip() or self.ranker_model_path.strip()
        )

    @property
    def metadata_providers_enabled(self) -> bool:
        """Whether network metadata observers should be attached to observe runs."""

        return bool(
            self.metadata_enabled
            or self.acoustid_api_key.strip()
            or self.musicbrainz_user_agent.strip()
        )

    def __init__(self, **values: Any) -> None:
        try:
            super().__init__(**values)
        except (SettingsError, ValidationError) as exc:
            raise _to_config_error(exc) from None

    @field_validator(*_CONFIG_DIRECTORY_FIELDS, mode="before")
    @classmethod
    def _normalize_path(cls, value: object) -> object:
        if isinstance(value, Path):
            return value.expanduser()
        if isinstance(value, str) or isinstance(value, os.PathLike):
            raw = os.fspath(value)
            if isinstance(raw, bytes):
                raw = raw.decode()
            if not raw.strip():
                raise ValueError("must not be empty")
            return Path(raw).expanduser()
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            return value.upper()
        return value

    @field_validator(
        "ytdlp_proxy",
        "metadata_proxy",
        "llm_proxy",
        "models_proxy",
        mode="before",
    )
    @classmethod
    def _normalize_proxy(cls, value: object) -> str:
        return _validated_proxy(value)

    @field_validator("ytdlp_site_proxies", mode="before")
    @classmethod
    def _normalize_site_proxies(cls, value: object) -> dict[str, str]:
        if value is None or (isinstance(value, str) and not value.strip()):
            return {}
        if not isinstance(value, Mapping):
            raise ValueError("ytdlp site proxies must be a table of host = proxy")
        result: dict[str, str] = {}
        for raw_host, raw_proxy in value.items():
            host = _validated_route_host(raw_host)
            result[host] = _validated_proxy(raw_proxy)
        return result

    @classmethod
    def from_file(cls, path: str | Path | None = None) -> TrackForgeConfig:
        """Load configuration from TOML, environment, and defaults.

        When ``path`` is omitted, ``<config_dir>/config.toml`` is used if it
        exists. A missing default file is not an error; an explicitly provided
        missing file is.
        """

        explicit_path = path is not None
        if path is not None:
            config_path = Path(path).expanduser()
        else:
            config_path = _default_config_path()

        if not config_path.exists():
            if explicit_path:
                raise ConfigError(
                    f"configuration file not found: {config_path}",
                    key="config",
                )
            return cls()

        values = _load_toml_values(config_path)
        if not values:
            return cls()
        return _build_with_toml(values)


_PROXY_SCHEMES = frozenset({"http", "https", "socks4", "socks4a", "socks5", "socks5h"})


def _validated_proxy(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("proxy must be a string")
    text = value.strip()
    if not text:
        return ""
    if any(char.isspace() for char in text):
        raise ValueError("proxy must not contain whitespace")
    try:
        parsed = urlsplit(text)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("proxy is not a valid URL") from exc
    if parsed.scheme.casefold() not in _PROXY_SCHEMES:
        raise ValueError("proxy scheme must be http, https, socks4, socks4a, socks5, or socks5h")
    if not parsed.hostname:
        raise ValueError("proxy must include a hostname")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("proxy must not include a path, query, or fragment")
    return text


def _validated_route_host(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("proxy route host must be a string")
    text = value.strip().casefold().rstrip(".")
    if text.startswith("*."):
        text = text[2:]
    if (
        not text
        or "/" in text
        or ":" in text
        or "*" in text
        or any(char.isspace() for char in text)
    ):
        raise ValueError("proxy route host must be a bare hostname or suffix")
    return text


def _to_config_error(exc: SettingsError | ValidationError) -> ConfigError:
    if isinstance(exc, SettingsError):
        return ConfigError(str(exc), key="config")

    details: list[str] = []
    keys: list[str] = []
    for error in exc.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in error["loc"])
        if location:
            keys.append(location)
            details.append(f"{location}: {error['msg']}")
        else:
            details.append(f"config: {error['msg']}")

    key = keys[0] if len(keys) == 1 else None
    message = "; ".join(details) or "invalid configuration"
    return ConfigError(message, key=key)


def _default_config_path() -> Path:
    configured = os.environ.get("TRACKFORGE_CONFIG_DIR") or os.environ.get(
        "TRACKFORGE_CORE_CONFIG_DIR"
    )
    if configured and configured.strip():
        config_dir = Path(configured).expanduser()
    else:
        config_dir = _default_config_dir()
    return config_dir / "config.toml"


def _load_toml_values(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as file:
            document = tomllib.load(file)
    except FileNotFoundError:
        raise ConfigError(f"configuration file not found: {path}", key="config") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"invalid TOML in {path}: {exc}",
            key="config",
        ) from None
    except OSError as exc:
        raise ConfigError(
            f"cannot read configuration file {path}: {exc.strerror or exc}",
            key="config",
        ) from None

    supported = {"core", "llm", "ranker", "models", "privacy", "network"}
    unknown_sections = sorted(set(document) - supported)
    if unknown_sections:
        section = unknown_sections[0]
        raise ConfigError(
            f"unsupported configuration section {section!r}; "
            "expected [core], [llm], [ranker], [models], [privacy], or [network]",
            key=section,
        )

    values: dict[str, Any] = {}
    core = _table(document, "core")
    _reject_unknown(core, _CORE_KEYS, "core")
    values.update(core)

    llm = _table(document, "llm")
    _reject_unknown(llm, _LLM_KEYS, "llm")
    values.update({f"llm_{key}": value for key, value in llm.items()})

    ranker = _table(document, "ranker")
    _reject_unknown(ranker, _RANKER_KEYS, "ranker")
    values.update({f"ranker_{key}": value for key, value in ranker.items()})

    models = _table(document, "models")
    nested_ranker = models.pop("ranker", None)
    nested_asr = models.pop("asr", None)
    nested_stems = models.pop("stems", None)
    nested_demucs = models.pop("demucs", None)
    _reject_unknown(models, {"offline", "max_heavy_models"}, "models")
    values.update({f"models_{key}": value for key, value in models.items()})
    if nested_ranker is not None:
        if not isinstance(nested_ranker, dict):
            raise ConfigError(
                "configuration key 'models.ranker' must be a table",
                key="models.ranker",
            )
        _reject_unknown(nested_ranker, set(_RANKER_TABLE_KEYS), "models.ranker")
        values.update(
            {f"ranker_{_RANKER_TABLE_KEYS[key]}": value for key, value in nested_ranker.items()}
        )
    if nested_asr is not None:
        if not isinstance(nested_asr, dict):
            raise ConfigError(
                "configuration key 'models.asr' must be a table",
                key="models.asr",
            )
        _reject_unknown(nested_asr, set(_ASR_TABLE_KEYS), "models.asr")
        values.update({f"asr_{_ASR_TABLE_KEYS[key]}": value for key, value in nested_asr.items()})
    for table_name, nested in (("demucs", nested_demucs), ("stems", nested_stems)):
        if nested is None:
            continue
        if not isinstance(nested, dict):
            raise ConfigError(
                f"configuration key 'models.{table_name}' must be a table",
                key=f"models.{table_name}",
            )
        _reject_unknown(nested, set(_STEMS_TABLE_KEYS), f"models.{table_name}")
        values.update({f"stems_{_STEMS_TABLE_KEYS[key]}": value for key, value in nested.items()})

    network = _table(document, "network")
    nested_ytdlp = network.pop("ytdlp", None)
    nested_metadata = network.pop("metadata", None)
    nested_llm = network.pop("llm", None)
    nested_models = network.pop("models", None)
    _reject_unknown(network, set(), "network")
    values.update(_network_values("ytdlp", nested_ytdlp, {"proxy", "sites"}))
    for name, nested in (
        ("metadata", nested_metadata),
        ("llm", nested_llm),
        ("models", nested_models),
    ):
        values.update(_network_values(name, nested, {"proxy"}))

    privacy = _table(document, "privacy")
    _reject_unknown(privacy, {"send_text_to_cloud", "send_audio_to_cloud"}, "privacy")
    values.update(privacy)
    return values


def _network_values(
    name: str,
    nested: object,
    allowed: set[str],
) -> dict[str, Any]:
    if nested is None:
        return {}
    if not isinstance(nested, dict):
        raise ConfigError(
            f"configuration key 'network.{name}' must be a table",
            key=f"network.{name}",
        )
    _reject_unknown(nested, allowed, f"network.{name}")
    if name == "ytdlp":
        sites = nested.get("sites", {})
        if not isinstance(sites, dict):
            raise ConfigError(
                "configuration key 'network.ytdlp.sites' must be a table",
                key="network.ytdlp.sites",
            )
        return {
            "ytdlp_proxy": nested.get("proxy", ""),
            "ytdlp_site_proxies": dict(sites),
        }
    return {f"{name}_proxy": nested.get("proxy", "")}


def _table(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"configuration key {name!r} must be a table", key=name)
    return dict(value)


def _reject_unknown(table: dict[str, Any], allowed: set[str], section: str) -> None:
    unknown_keys = sorted(set(table) - allowed)
    if not unknown_keys:
        return
    unknown = unknown_keys[0]
    raise ConfigError(
        f"unknown configuration key '{section}.{unknown}'",
        key=f"{section}.{unknown}",
    )


_CORE_KEYS = {
    "config_dir",
    "data_dir",
    "cache_dir",
    "state_dir",
    "library_dir",
    "profile",
    "mode",
    "log_level",
    "offline",
    "metadata_enabled",
    "acoustid_api_key",
    "musicbrainz_user_agent",
    "ytdlp_binary",
    "deno_binary",
    "ytdlp_timeout_metadata",
    "ytdlp_timeout_download",
}
_LLM_KEYS = {
    "provider",
    "base_url",
    "model",
    "api_key_env",
    "max_cost_usd_per_item",
    "max_tokens",
    "temperature",
    "timeout_seconds",
    "max_retries",
    "price_in",
    "price_out",
    "strict_schema",
    "thinking",
}
_RANKER_KEYS = {
    "strategy",
    "backend",
    "model",
    "model_path",
    "model_version",
    "license",
    "checksum",
    "size_bytes",
    "heavy",
    "threads",
    "batch_size",
    "max_length",
}
_RANKER_TABLE_KEYS = {
    "name": "model",
    "path": "model_path",
    "version": "model_version",
    "license": "license",
    "checksum": "checksum",
    "size_bytes": "size_bytes",
    "heavy": "heavy",
    "threads": "threads",
    "batch_size": "batch_size",
    "max_length": "max_length",
    "backend": "backend",
}
_ASR_TABLE_KEYS = {
    "backend": "backend",
    "name": "model",
    "path": "model_path",
    "version": "model_version",
    "license": "license",
    "checksum": "checksum",
    "size_bytes": "size_bytes",
    "heavy": "heavy",
    "compute_type": "compute_type",
    "threads": "threads",
    "vad_filter": "vad_filter",
    "beam_size": "beam_size",
    "memory_mb": "memory_mb",
}
_STEMS_KEYS = {
    "backend",
    "model",
    "model_path",
    "model_version",
    "model_url",
    "model_file",
    "license",
    "checksum",
    "size_bytes",
    "heavy",
    "segment",
    "overlap",
    "jobs",
    "threads",
    "memory_mb",
}
_STEMS_TABLE_KEYS = {
    "backend": "backend",
    "name": "model",
    "path": "model_path",
    "version": "model_version",
    "url": "model_url",
    "file": "model_file",
    "license": "license",
    "checksum": "checksum",
    "size_bytes": "size_bytes",
    "heavy": "heavy",
    "segment": "segment",
    "overlap": "overlap",
    "jobs": "jobs",
    "threads": "threads",
    "memory_mb": "memory_mb",
}


def _build_with_toml(values: dict[str, Any]) -> TrackForgeConfig:
    class TomlBoundConfig(TrackForgeConfig):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            # Explicit values beat environment values, which beat config.toml.
            # Field defaults are appended by pydantic-settings automatically.
            return (
                init_settings,
                env_settings,
                _TomlSettingsSource(settings_cls, values),
            )

    return TomlBoundConfig()


__all__ = [
    "LogLevel",
    "Mode",
    "Profile",
    "TrackForgeConfig",
]

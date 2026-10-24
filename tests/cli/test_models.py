"""``trackforge models list`` smoke tests; no downloads are performed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.cli.conftest import cli_paths, invoke, parse_json


def test_models_list_is_empty_without_configuration(
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    data, library = cli_dirs

    result = invoke("models", "list", *cli_paths(data, library), "--json")

    assert result.exit_code == 0, result.stderr
    payload = parse_json(result)
    assert payload["models"] == []
    assert not (data / "models").exists()


def test_models_list_reports_missing_configured_model(
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, library = cli_dirs
    monkeypatch.setenv("TRACKFORGE_RANKER_MODEL", "BAAI/test-reranker")
    monkeypatch.setenv("TRACKFORGE_RANKER_MODEL_PATH", str(data / "models" / "absent.onnx"))
    monkeypatch.setenv("TRACKFORGE_RANKER_LICENSE", "MIT")
    monkeypatch.setenv("TRACKFORGE_RANKER_STRATEGY", "hybrid")

    result = invoke("models", "list", *cli_paths(data, library), "--json")

    assert result.exit_code == 0, result.stderr
    payload = parse_json(result)
    assert len(payload["models"]) == 1
    model = payload["models"][0]
    assert model["id"] == "BAAI/test-reranker"
    assert model["license"] == "MIT"
    assert model["status"] == "missing"
    assert "checksum" in model


def test_models_list_json_is_one_object(
    cli_dirs: tuple[Path, Path],
    clean_env: None,
) -> None:
    data, library = cli_dirs

    result = invoke("models", "list", *cli_paths(data, library), "--json")

    assert len(result.stdout.strip().splitlines()) == 1
    assert isinstance(json.loads(result.stdout), dict)


def test_models_download_invokes_configured_asr(
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, library = cli_dirs
    monkeypatch.setenv("TRACKFORGE_ASR_MODEL", "Systran/faster-whisper-small")
    monkeypatch.setenv("TRACKFORGE_ASR_LICENSE", "MIT")
    monkeypatch.setenv("TRACKFORGE_ASR_MODEL_VERSION", "test-rev")
    calls: list[tuple[str, bool]] = []

    from trackforge.adapters.models.local import LocalModelManager
    from trackforge.application.ports.models import ModelInfo, ModelKind, ModelStatus

    def fake_download(self: LocalModelManager, model_id: str, *, force: bool = False) -> ModelInfo:
        calls.append((model_id, force))
        spec = self.spec(model_id)
        return ModelInfo(
            id=spec.id,
            version=spec.version,
            kind=ModelKind.ASR,
            license=spec.license,
            status=ModelStatus.AVAILABLE,
            path=str(data / "models" / "asr" / "Systran--faster-whisper-small"),
        )

    monkeypatch.setattr(LocalModelManager, "download", fake_download)

    result = invoke("models", "download", "asr", *cli_paths(data, library), "--json")

    assert result.exit_code == 0, result.stderr
    payload = parse_json(result)
    assert payload["model"]["id"] == "Systran/faster-whisper-small"
    assert payload["model"]["status"] == "available"
    assert calls == [("Systran/faster-whisper-small", False)]


def test_models_download_offline_is_refused(
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, library = cli_dirs
    monkeypatch.setenv("TRACKFORGE_ASR_MODEL", "Systran/faster-whisper-small")
    monkeypatch.setenv("TRACKFORGE_ASR_LICENSE", "MIT")

    result = invoke(
        "models",
        "download",
        "asr",
        *cli_paths(data, library),
        "--offline",
        "--json",
    )

    assert result.exit_code == 2
    payload = parse_json(result, stream="stderr")
    assert payload["type"] == "OfflineModeError"
    assert not (data / "models").exists()


def test_models_download_invokes_configured_demucs(
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, library = cli_dirs
    monkeypatch.setenv("TRACKFORGE_STEMS_MODEL", "htdemucs")
    monkeypatch.setenv("TRACKFORGE_STEMS_LICENSE", "MIT")
    calls: list[tuple[str, bool]] = []

    from trackforge.adapters.models.local import LocalModelManager
    from trackforge.application.ports.models import ModelInfo, ModelKind, ModelStatus

    def fake_download(self: LocalModelManager, model_id: str, *, force: bool = False) -> ModelInfo:
        calls.append((model_id, force))
        spec = self.spec(model_id)
        return ModelInfo(
            id=spec.id,
            version=spec.version,
            kind=ModelKind.DEMUCS,
            license=spec.license,
            status=ModelStatus.AVAILABLE,
            path=str(data / "models" / "stems" / "htdemucs" / "955717e8-8726e21a.th"),
        )

    monkeypatch.setattr(LocalModelManager, "download", fake_download)

    result = invoke("models", "download", "demucs", *cli_paths(data, library), "--json")

    assert result.exit_code == 0, result.stderr
    payload = parse_json(result)
    assert payload["model"]["id"] == "htdemucs"
    assert payload["model"]["kind"] == "demucs"
    assert payload["model"]["status"] == "available"
    assert calls == [("htdemucs", False)]


def test_models_list_reports_configured_demucs(
    cli_dirs: tuple[Path, Path],
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, library = cli_dirs
    monkeypatch.setenv("TRACKFORGE_STEMS_MODEL", "htdemucs")
    monkeypatch.setenv("TRACKFORGE_STEMS_LICENSE", "MIT")

    result = invoke("models", "list", *cli_paths(data, library), "--json")

    assert result.exit_code == 0, result.stderr
    payload = parse_json(result)
    [model] = payload["models"]
    assert model["id"] == "htdemucs"
    assert model["kind"] == "demucs"
    assert model["license"] == "MIT"
    assert model["status"] == "missing"
    assert model["metadata"]["jobs"] == 1
    assert model["checksum"] == ("8726e21a993978c7ba086d3872e7608d7d5bfca646ca4aca459ffda844faa8b4")

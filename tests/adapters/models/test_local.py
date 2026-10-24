"""Local model manager checks: license, path, checksum, and one heavy model."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from trackforge.adapters.models.local import LocalModelManager
from trackforge.application.ports.models import ModelInfo, ModelKind, ModelStatus
from trackforge.domain.errors import ModelUnavailable, OfflineModeError

pytestmark = pytest.mark.unit


def _spec(path: Path, **overrides: object) -> ModelInfo:
    data: dict[str, object] = {
        "id": "ranker-test",
        "version": "1",
        "kind": ModelKind.RANKER,
        "license": "MIT",
        "path": str(path),
        "heavy": True,
    }
    data.update(overrides)
    return ModelInfo(**data)  # type: ignore[arg-type]


def test_missing_path_is_missing(tmp_path: Path) -> None:
    manager = LocalModelManager(
        [_spec(tmp_path / "absent.onnx")],
        runtime_available=lambda _name: True,
    )

    info = manager.resolve("ranker-test")

    assert info.status is ModelStatus.MISSING
    with pytest.raises(ModelUnavailable):
        manager.load_path("ranker-test")


def test_unlicensed_model_is_not_available(tmp_path: Path) -> None:
    path = tmp_path / "model.onnx"
    path.write_bytes(b"weights")
    manager = LocalModelManager(
        [_spec(path, license="")],
        runtime_available=lambda _name: True,
    )

    info = manager.resolve("ranker-test")

    assert info.status is ModelStatus.UNLICENSED
    assert "license" in str(info.metadata.get("error"))


def test_checksum_mismatch_is_visible(tmp_path: Path) -> None:
    path = tmp_path / "model.onnx"
    path.write_bytes(b"weights")
    manager = LocalModelManager(
        [_spec(path, checksum="0" * 64)],
        runtime_available=lambda _name: True,
    )

    info = manager.resolve("ranker-test")

    assert info.status is ModelStatus.CHECKSUM_MISMATCH


def test_available_model_and_one_heavy_policy(tmp_path: Path) -> None:
    path = tmp_path / "model.onnx"
    path.write_bytes(b"weights")
    digest = hashlib.sha256(b"weights").hexdigest()
    manager = LocalModelManager(
        [
            _spec(path, id="one", checksum=digest),
            _spec(path, id="two", checksum=digest),
        ],
        runtime_available=lambda _name: True,
    )

    info = manager.resolve("one")
    assert info.status is ModelStatus.AVAILABLE
    assert info.size_bytes == len(b"weights")
    assert manager.load_path("one") == path
    assert manager.active_heavy == "one"
    manager.load_path("two")
    assert manager.active_heavy == "two"


def test_runtime_missing_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "model.onnx"
    path.write_bytes(b"weights")
    manager = LocalModelManager(
        [_spec(path)],
        runtime_available=lambda _name: False,
    )

    assert manager.resolve("ranker-test").status is ModelStatus.RUNTIME_MISSING


def test_asr_directory_checksum_uses_model_bin(tmp_path: Path) -> None:
    model_dir = tmp_path / "asr-model"
    model_dir.mkdir()
    (model_dir / "model.bin").write_bytes(b"asr-weights")
    digest = hashlib.sha256(b"asr-weights").hexdigest()
    manager = LocalModelManager(
        [
            ModelInfo(
                id="Systran/faster-whisper-small",
                kind=ModelKind.ASR,
                license="MIT",
                path=str(model_dir),
                checksum=digest,
            )
        ],
        runtime_available=lambda name: name == "faster_whisper",
    )

    info = manager.resolve("Systran/faster-whisper-small")

    assert info.status is ModelStatus.AVAILABLE
    assert info.size_bytes == len(b"asr-weights")


def test_asr_directory_checksum_mismatch_is_visible(tmp_path: Path) -> None:
    model_dir = tmp_path / "asr-model"
    model_dir.mkdir()
    (model_dir / "model.bin").write_bytes(b"asr-weights")
    manager = LocalModelManager(
        [
            ModelInfo(
                id="Systran/faster-whisper-small",
                kind=ModelKind.ASR,
                license="MIT",
                path=str(model_dir),
                checksum="0" * 64,
            )
        ],
        runtime_available=lambda _name: True,
    )

    assert manager.resolve("Systran/faster-whisper-small").status is ModelStatus.CHECKSUM_MISMATCH


def test_asr_download_writes_model_and_verifies_checksum(tmp_path: Path) -> None:
    content = b"downloaded-weights"
    digest = hashlib.sha256(content).hexdigest()
    calls: list[tuple[str, str, Path]] = []

    def downloader(repository: str, revision: str, destination: Path) -> None:
        calls.append((repository, revision, destination))
        destination.mkdir(parents=True)
        (destination / "model.bin").write_bytes(content)

    manager = LocalModelManager(
        [
            ModelInfo(
                id="Systran/faster-whisper-small",
                version="commit-sha",
                kind=ModelKind.ASR,
                license="MIT",
                checksum=digest,
                metadata={"repo": "Systran/faster-whisper-small"},
            )
        ],
        models_dir=tmp_path / "models",
        offline=False,
        runtime_available=lambda name: name == "faster_whisper",
        downloader=downloader,
    )

    info = manager.download("Systran/faster-whisper-small")

    assert info.status is ModelStatus.AVAILABLE
    assert info.path is not None
    destination = tmp_path / "models" / "asr" / "Systran--faster-whisper-small"
    assert Path(info.path) == destination
    assert (destination / "model.bin").read_bytes() == content
    assert calls == [("Systran/faster-whisper-small", "commit-sha", destination)]


def test_asr_download_is_forbidden_offline(tmp_path: Path) -> None:
    manager = LocalModelManager(
        [
            ModelInfo(
                id="Systran/faster-whisper-small",
                kind=ModelKind.ASR,
                license="MIT",
                metadata={"repo": "Systran/faster-whisper-small"},
            )
        ],
        models_dir=tmp_path / "models",
        offline=True,
        runtime_available=lambda _name: True,
        downloader=lambda *_args: pytest.fail("offline download must not run"),
    )

    with pytest.raises(OfflineModeError):
        manager.download("Systran/faster-whisper-small")


def test_asr_download_requires_license(tmp_path: Path) -> None:
    manager = LocalModelManager(
        [
            ModelInfo(
                id="Systran/faster-whisper-small",
                kind=ModelKind.ASR,
                license="",
                metadata={"repo": "Systran/faster-whisper-small"},
            )
        ],
        models_dir=tmp_path / "models",
        offline=False,
        runtime_available=lambda _name: True,
        downloader=lambda *_args: pytest.fail("unlicensed download must not run"),
    )

    with pytest.raises(ModelUnavailable, match="license"):
        manager.download("Systran/faster-whisper-small")


def test_asr_download_checksum_failure_is_reported(tmp_path: Path) -> None:
    def downloader(_repository: str, _revision: str, destination: Path) -> None:
        destination.mkdir(parents=True)
        (destination / "model.bin").write_bytes(b"tampered")

    manager = LocalModelManager(
        [
            ModelInfo(
                id="Systran/faster-whisper-small",
                kind=ModelKind.ASR,
                license="MIT",
                checksum="0" * 64,
                metadata={"repo": "Systran/faster-whisper-small"},
            )
        ],
        models_dir=tmp_path / "models",
        offline=False,
        runtime_available=lambda _name: True,
        downloader=downloader,
    )

    with pytest.raises(ModelUnavailable, match="checksum"):
        manager.download("Systran/faster-whisper-small")


def test_demucs_runtime_requires_torch_and_demucs(tmp_path: Path) -> None:
    checkpoint = tmp_path / "955717e8-8726e21a.th"
    checkpoint.write_bytes(b"demucs-weights")
    spec = ModelInfo(
        id="htdemucs",
        version="955717e8-8726e21a",
        kind=ModelKind.DEMUCS,
        license="MIT",
        path=str(checkpoint),
    )

    missing_demucs = LocalModelManager(
        [spec],
        runtime_available=lambda name: name == "torch",
    )
    ready = LocalModelManager(
        [spec],
        runtime_available=lambda name: name in {"torch", "demucs"},
    )

    assert missing_demucs.resolve("htdemucs").status is ModelStatus.RUNTIME_MISSING
    assert ready.resolve("htdemucs").status is ModelStatus.AVAILABLE


def test_demucs_download_writes_default_checkpoint_and_verifies(tmp_path: Path) -> None:
    content = b"downloaded-demucs-weights"
    digest = hashlib.sha256(content).hexdigest()
    calls: list[tuple[str, Path]] = []

    def file_downloader(url: str, destination: Path) -> None:
        calls.append((url, destination))
        destination.write_bytes(content)

    manager = LocalModelManager(
        [
            ModelInfo(
                id="htdemucs",
                version="955717e8-8726e21a",
                kind=ModelKind.DEMUCS,
                license="MIT",
                checksum=digest,
                metadata={
                    "url": "https://example.invalid/htdemucs.th",
                    "file_name": "955717e8-8726e21a.th",
                },
            )
        ],
        models_dir=tmp_path / "models",
        offline=False,
        runtime_available=lambda name: name in {"torch", "demucs"},
        file_downloader=file_downloader,
    )

    info = manager.download("htdemucs")

    destination = tmp_path / "models" / "stems" / "htdemucs" / "955717e8-8726e21a.th"
    assert info.status is ModelStatus.AVAILABLE
    assert info.path == str(destination)
    assert destination.read_bytes() == content
    assert calls == [("https://example.invalid/htdemucs.th", destination)]


def test_demucs_download_requires_url_and_license(tmp_path: Path) -> None:
    unlicensed = LocalModelManager(
        [
            ModelInfo(
                id="htdemucs",
                kind=ModelKind.DEMUCS,
                license="",
                metadata={"url": "https://example.invalid/model.th"},
            )
        ],
        models_dir=tmp_path / "models",
        offline=False,
        runtime_available=lambda _name: True,
        file_downloader=lambda *_args: pytest.fail("unlicensed download must not run"),
    )
    with pytest.raises(ModelUnavailable, match="license"):
        unlicensed.download("htdemucs")

    no_url = LocalModelManager(
        [
            ModelInfo(
                id="htdemucs",
                kind=ModelKind.DEMUCS,
                license="MIT",
                metadata={},
            )
        ],
        models_dir=tmp_path / "models",
        offline=False,
        runtime_available=lambda _name: True,
        file_downloader=lambda *_args: pytest.fail("URL-less download must not run"),
    )
    with pytest.raises(ModelUnavailable, match="URL"):
        no_url.download("htdemucs")


def test_demucs_download_is_forbidden_offline(tmp_path: Path) -> None:
    manager = LocalModelManager(
        [
            ModelInfo(
                id="htdemucs",
                kind=ModelKind.DEMUCS,
                license="MIT",
                metadata={"url": "https://example.invalid/model.th"},
            )
        ],
        models_dir=tmp_path / "models",
        offline=True,
        runtime_available=lambda _name: True,
        file_downloader=lambda *_args: pytest.fail("offline download must not run"),
    )

    with pytest.raises(OfflineModeError):
        manager.download("htdemucs")


def test_demucs_directory_checksum_uses_single_checkpoint(tmp_path: Path) -> None:
    model_dir = tmp_path / "stems" / "htdemucs"
    model_dir.mkdir(parents=True)
    checkpoint = model_dir / "955717e8-8726e21a.th"
    checkpoint.write_bytes(b"demucs-weights")
    digest = hashlib.sha256(b"demucs-weights").hexdigest()
    manager = LocalModelManager(
        [
            ModelInfo(
                id="htdemucs",
                kind=ModelKind.DEMUCS,
                license="MIT",
                path=str(model_dir),
                checksum=digest,
            )
        ],
        runtime_available=lambda name: name in {"torch", "demucs"},
    )

    info = manager.resolve("htdemucs")

    assert info.status is ModelStatus.AVAILABLE
    assert info.size_bytes == len(b"demucs-weights")

"""Offline-first local model manager.

The manager validates configured paths, optional SHA-256 checksums, licenses,
and runtime availability, and enforces a single resident heavy model.  Only an
explicit ``download`` call performs network I/O; analysis never downloads
weights implicitly.  Runtime imports stay lazy so lexical-only installs need no
ONNX or CTranslate2 stack.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shutil
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx

from trackforge.application.ports.models import (
    ModelInfo,
    ModelKind,
    ModelStatus,
)
from trackforge.domain.errors import ModelUnavailable, OfflineModeError

Downloader = Callable[[str, str, Path], None]
FileDownloader = Callable[[str, Path], None]
_MODEL_FILE_NAME = "model.bin"
_SAFE_DIRECTORY_RE = re.compile(r"[^A-Za-z0-9._-]+")


class LocalModelManager:
    """Filesystem-backed model manager with a one-heavy-model policy."""

    def __init__(
        self,
        specs: Iterable[ModelInfo],
        *,
        models_dir: str | Path | None = None,
        offline: bool = True,
        max_heavy_models: int = 1,
        runtime_available: Callable[[str], bool] | None = None,
        downloader: Downloader | None = None,
        file_downloader: FileDownloader | None = None,
        proxy: str | None = None,
    ) -> None:
        self._specs = {spec.id: spec for spec in specs}
        self._models_dir = Path(models_dir) if models_dir is not None else None
        self.offline = bool(offline)
        self.max_heavy_models = max(1, int(max_heavy_models))
        self._runtime_available = runtime_available or _module_available
        self._downloader = downloader
        self._file_downloader = file_downloader
        self._proxy = proxy.strip() if isinstance(proxy, str) and proxy.strip() else None
        self._active_heavy: str | None = None
        self._registered: dict[str, ModelInfo] = {}

    def add_registered(self, models: Iterable[ModelInfo]) -> None:
        """Merge persisted registry entries; configured specs keep precedence."""

        for model in models:
            self._registered.setdefault(model.id, model)

    def list_models(self) -> tuple[ModelInfo, ...]:
        result: list[ModelInfo] = []
        seen: set[str] = set()
        for model_id in (*self._specs, *self._registered):
            if model_id in seen:
                continue
            seen.add(model_id)
            result.append(self.resolve(model_id))
        return tuple(result)

    def spec(self, model_id: str) -> ModelInfo:
        spec = self._specs.get(model_id) or self._registered.get(model_id)
        if spec is None:
            raise ModelUnavailable(
                "model is not configured",
                context={"model_id": model_id},
            )
        return spec

    def resolve(self, model_id: str) -> ModelInfo:
        spec = self.spec(model_id)
        path = self._path_for(spec)
        if not spec.license.strip():
            return _with_status(
                spec,
                ModelStatus.UNLICENSED,
                path=str(path) if path is not None else None,
                error="license is not configured",
            )
        if path is None:
            return _with_status(spec, ModelStatus.MISSING)
        if not path.exists():
            return _with_status(
                spec,
                ModelStatus.MISSING,
                path=str(path),
                error="model artifact does not exist locally",
            )
        if spec.kind is ModelKind.RANKER and not self._runtime_available("onnxruntime"):
            return _with_status(
                spec,
                ModelStatus.RUNTIME_MISSING,
                path=str(path),
                error="install the [ranker] extra for onnxruntime",
            )
        if spec.kind is ModelKind.ASR and not self._runtime_available("faster_whisper"):
            return _with_status(
                spec,
                ModelStatus.RUNTIME_MISSING,
                path=str(path),
                error="install the [asr] extra for faster-whisper",
            )
        if spec.kind is ModelKind.DEMUCS and not (
            self._runtime_available("torch") and self._runtime_available("demucs")
        ):
            return _with_status(
                spec,
                ModelStatus.RUNTIME_MISSING,
                path=str(path),
                error="install the [stems] extra for torch and demucs",
            )
        if spec.checksum:
            checksum_path = _checksum_path(path, spec.kind)
            if checksum_path is None:
                expected = "a .th checkpoint" if spec.kind is ModelKind.DEMUCS else "model.bin"
                return _with_status(
                    spec,
                    ModelStatus.CHECKSUM_MISMATCH,
                    path=str(path),
                    error=f"checksum verification requires {expected}",
                )
            actual = sha256_file(checksum_path)
            if actual.lower() != spec.checksum.lower():
                return _with_status(
                    spec,
                    ModelStatus.CHECKSUM_MISMATCH,
                    path=str(path),
                    error="SHA-256 does not match the configured checksum",
                )
        size = path.stat().st_size if path.is_file() else _directory_size(path)
        return _with_status(
            spec,
            ModelStatus.AVAILABLE,
            path=str(path),
            size_bytes=size,
        )

    def load_path(self, model_id: str) -> Path:
        info = self.resolve(model_id)
        if not info.available or info.path is None:
            raise ModelUnavailable(
                f"model {model_id!r} is not available ({info.status.value})",
                user_action=(
                    "Run `trackforge models download asr` or "
                    "`trackforge models download demucs`, or configure a local model path."
                ),
                context={"model_id": model_id, "status": info.status.value},
            )
        if info.heavy:
            self.acquire_heavy(model_id)
        return Path(info.path)

    def download(self, model_id: str, *, force: bool = False) -> ModelInfo:
        """Download one configured ASR or Demucs model and verify it.

        Downloading is never implicit.  ``models.offline`` intentionally does
        not block this explicit command; the core ``--offline`` flag does, and
        the CLI maps it to ``offline=True`` here.
        """

        if self.offline:
            raise OfflineModeError(
                "model download is disabled in offline mode",
                user_action="Remove --offline or disable core offline mode to download weights.",
                context={"model_id": model_id},
            )
        spec = self.spec(model_id)
        if spec.kind not in {ModelKind.ASR, ModelKind.DEMUCS}:
            raise ModelUnavailable(
                "automatic download is implemented only for ASR and Demucs models",
                user_action="Place the model file manually and configure its path/checksum.",
                context={"model_id": model_id, "kind": spec.kind.value},
            )
        if not spec.license.strip():
            table = "asr" if spec.kind is ModelKind.ASR else "stems"
            raise ModelUnavailable(
                f"the {spec.kind.value} model license is not configured",
                user_action=f"Set [models.{table}] license before downloading the weights.",
                context={"model_id": model_id},
            )
        destination = self._path_for(spec)
        if destination is None:
            raise ModelUnavailable(
                f"the {spec.kind.value} model has no local destination",
                user_action="Set a model path or configure models/data directories.",
                context={"model_id": model_id},
            )
        if spec.kind is ModelKind.DEMUCS:
            destination = _demucs_destination(destination, spec)
        if destination.exists():
            current = self.resolve(model_id)
            if current.status in {ModelStatus.AVAILABLE, ModelStatus.RUNTIME_MISSING} and not force:
                return current
            if not force:
                raise ModelUnavailable(
                    "the model destination already exists but is not usable",
                    user_action="Remove it or pass --force to download again.",
                    context={"model_id": model_id, "path": str(destination)},
                )
            if not self._safe_to_remove(destination):
                raise ModelUnavailable(
                    "refusing to remove a model outside the managed models directory",
                    user_action="Remove the configured path manually.",
                    context={"model_id": model_id, "path": str(destination)},
                )
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        destination.parent.mkdir(parents=True, exist_ok=True)

        try:
            if spec.kind is ModelKind.ASR:
                repository = str(spec.metadata.get("repo") or "").strip()
                if not repository:
                    raise ModelUnavailable(
                        "the ASR model name is not a repository identifier",
                        user_action="Set [models.asr] name to a Hugging Face repository id.",
                        context={"model_id": model_id},
                    )
                downloader = self._downloader or (
                    lambda repository, revision, target: _snapshot_download(
                        repository,
                        revision,
                        target,
                        proxy=self._proxy,
                    )
                )
                downloader(repository, spec.version or "main", destination)
            else:
                url = str(spec.metadata.get("url") or "").strip()
                if not url:
                    raise ModelUnavailable(
                        "the Demucs model URL is not configured",
                        user_action=(
                            "Set [models.stems] url (and checksum/file) for the "
                            "checkpoint you want to download."
                        ),
                        context={"model_id": model_id},
                    )
                file_downloader = self._file_downloader or (
                    lambda source, target: _download_file(
                        source,
                        target,
                        proxy=self._proxy,
                    )
                )
                file_downloader(url, destination)
        except ModelUnavailable:
            raise
        except Exception as exc:
            raise ModelUnavailable(
                f"model download failed: {type(exc).__name__}: {exc}",
                user_action="Check network access and the configured source/revision.",
                context={"model_id": model_id},
            ) from exc

        info = self.resolve(model_id)
        if info.status in {
            ModelStatus.MISSING,
            ModelStatus.UNLICENSED,
            ModelStatus.CHECKSUM_MISMATCH,
            ModelStatus.ERROR,
        }:
            error = info.metadata.get("error") or info.status.value
            raise ModelUnavailable(
                f"downloaded model did not verify: {error}",
                user_action="Check the configured checksum/license and runtime extra.",
                context={"model_id": model_id, "status": info.status.value},
            )
        return info

    def acquire_heavy(self, model_id: str) -> None:
        if self._active_heavy is not None and self._active_heavy != model_id:
            self.release(self._active_heavy)
        self._active_heavy = model_id

    def release(self, model_id: str | None = None) -> None:
        if model_id is None or model_id == self._active_heavy:
            self._active_heavy = None

    @property
    def active_heavy(self) -> str | None:
        return self._active_heavy

    def _path_for(self, spec: ModelInfo) -> Path | None:
        if spec.path:
            path = Path(spec.path).expanduser()
            if not path.is_absolute() and self._models_dir is not None:
                path = self._models_dir / path
            return path
        if spec.kind is ModelKind.ASR and self._models_dir is not None:
            return self._models_dir / "asr" / _model_directory(spec.id)
        if spec.kind is ModelKind.DEMUCS and self._models_dir is not None:
            return (
                self._models_dir
                / "stems"
                / _model_directory(spec.id, "demucs-model")
                / _demucs_filename(spec)
            )
        return None

    def _safe_to_remove(self, path: Path) -> bool:
        if self._models_dir is None:
            return False
        try:
            path.resolve().relative_to(self._models_dir.resolve())
        except ValueError:
            return False
        return path.resolve() != self._models_dir.resolve()


def _with_status(
    spec: ModelInfo,
    status: ModelStatus,
    *,
    path: str | None = None,
    size_bytes: int | None = None,
    error: str | None = None,
) -> ModelInfo:
    metadata = dict(spec.metadata)
    if error is not None:
        metadata["error"] = error
    return ModelInfo(
        id=spec.id,
        version=spec.version,
        kind=spec.kind,
        license=spec.license,
        status=status,
        path=path if path is not None else spec.path,
        checksum=spec.checksum,
        size_bytes=size_bytes if size_bytes is not None else spec.size_bytes,
        source=spec.source,
        heavy=spec.heavy,
        metadata=metadata,
        updated_at=spec.updated_at,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _checksum_path(path: Path, kind: ModelKind = ModelKind.ASR) -> Path | None:
    if path.is_dir():
        if kind is ModelKind.DEMUCS:
            checkpoints = sorted(path.glob("*.th"))
            return checkpoints[0] if len(checkpoints) == 1 else None
        candidate = path / _MODEL_FILE_NAME
        return candidate if candidate.is_file() else None
    return path if path.is_file() else None


def _directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def _model_directory(model_id: str, default: str = "asr-model") -> str:
    sanitized = _SAFE_DIRECTORY_RE.sub("--", model_id.strip()).strip(".-")
    return sanitized or default


def _demucs_filename(spec: ModelInfo) -> str:
    configured = str(spec.metadata.get("file_name") or "").strip()
    if configured:
        return configured
    return f"{spec.version or spec.id or 'demucs-model'}.th"


def _demucs_destination(path: Path, spec: ModelInfo) -> Path:
    if path.suffix == ".th":
        return path
    return path / _demucs_filename(spec)


def _download_file(url: str, destination: Path, *, proxy: str | None = None) -> None:
    temporary = destination.with_name(destination.name + ".part")
    try:
        with httpx.Client(
            proxy=proxy,
            trust_env=False,
            follow_redirects=True,
            timeout=httpx.Timeout(120.0, read=300.0),
            headers={"User-Agent": "TrackForge/0.1 model-download"},
        ) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                with temporary.open("wb") as file:
                    for chunk in response.iter_bytes(1024 * 1024):
                        file.write(chunk)
                    file.flush()
                    os.fsync(file.fileno())
        temporary.replace(destination)
    except ImportError as exc:
        temporary.unlink(missing_ok=True)
        raise ModelUnavailable(
            "the configured proxy scheme needs optional HTTP client support",
            user_action="Install httpx with SOCKS support or use an HTTP proxy.",
            context={"error": str(exc)},
        ) from exc
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _snapshot_download(
    repository: str,
    revision: str,
    destination: Path,
    *,
    proxy: str | None = None,
) -> None:
    try:
        from huggingface_hub import snapshot_download  # type: ignore[import-not-found,import-untyped,unused-ignore]
    except ImportError as exc:
        raise ModelUnavailable(
            "huggingface-hub is not installed",
            user_action="Install the [asr] extra or download the model manually.",
        ) from exc
    with _proxy_environment(proxy):
        snapshot_download(
            repo_id=repository,
            revision=revision,
            local_dir=str(destination),
        )


_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


@contextmanager
def _proxy_environment(proxy: str | None) -> Iterator[None]:
    """Localize proxy env for libraries that do not accept a proxy argument.

    Model download is an explicit, serialized command; the previous
    environment is always restored.  When no proxy is configured the
    standard variables are removed so an ambient global proxy cannot leak in.
    """

    previous = {key: os.environ.get(key) for key in _PROXY_ENV_KEYS}
    try:
        for key in _PROXY_ENV_KEYS:
            os.environ.pop(key, None)
        if proxy:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                os.environ[key] = proxy
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# The port is named ModelManager; the concrete class keeps its adapter prefix.
ModelManager = LocalModelManager

__all__ = [
    "Downloader",
    "FileDownloader",
    "LocalModelManager",
    "ModelManager",
    "sha256_file",
]

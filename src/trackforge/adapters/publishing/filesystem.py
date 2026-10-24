"""Filesystem publisher: safe naming, collisions, and atomic library copies.

Only this adapter knows how a rendered bundle is laid out on disk and how a
portable sidecar becomes a human-readable filename.  It never resolves names
through user-controlled paths: every component is sanitized and the final
destination is checked to stay inside ``library_root``.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trackforge.application.ports.publishing import (
    DEFAULT_NAMING_TEMPLATE,
    FALLBACK_NAMING_TEMPLATE,
    MAX_FILENAME_BYTES,
    PublishOptions,
)
from trackforge.application.services.sidecar import SIDECAR_FILENAME
from trackforge.domain.errors import (
    PathEscapeError,
    PublishFailed,
)
from trackforge.domain.model.ids import LibraryEntryId, new_uuid7
from trackforge.domain.model.outputs import LibraryEntry, LibraryStatus, OutputBundle

_FORBIDDEN_RE = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')
_WHITESPACE_RE = re.compile(r"\s+")
_AUDIO_SUFFIXES = frozenset({".mp3", ".m4a", ".flac", ".ogg", ".opus"})
_WINDOWS_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)
_QUALIFIER_LABELS = {
    "nightcore": "Nightcore",
    "remix": "Remix",
    "sped_up": "Sped Up",
    "spedup": "Sped Up",
    "slowed": "Slowed",
    "reverb": "Reverb",
    "8d": "8D",
    "instrumental": "Instrumental",
    "acapella": "Acapella",
    "cover": "Cover",
    "extended": "Extended",
}
_PLACEHOLDERS = frozenset(
    {
        "artist",
        "title",
        "qualifier",
        "collection",
        "year",
        "source_id",
        "short_id",
        "track",
        "disc",
    }
)


def sanitize_component(
    value: object,
    *,
    max_bytes: int = MAX_FILENAME_BYTES,
    fallback: str = "Unknown",
) -> str:
    """Return one safe filename component.

    Forbidden path and control characters are replaced, whitespace is
    collapsed, trailing dots/spaces are removed, Windows reserved device names
    are prefixed, and the result is truncated on a UTF-8 boundary and, where
    possible, a word boundary.
    """

    if value is None:
        value = fallback
    elif not isinstance(value, str):
        value = str(value)
    text = unicodedata.normalize("NFC", value)
    text = text.replace("\x00", "_")
    text = _FORBIDDEN_RE.sub("_", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    text = text.rstrip(" .")
    if text in {"", ".", ".."}:
        text = fallback
    if _is_windows_reserved(text):
        text = f"_{text}"
    return _truncate_utf8(text, max_bytes=max_bytes, fallback=fallback)


def _is_windows_reserved(text: str) -> bool:
    base = text.split(".", 1)[0].strip().upper()
    return base in _WINDOWS_RESERVED


def _truncate_utf8(text: str, *, max_bytes: int, fallback: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    cut = encoded[:max_bytes]
    while cut:
        try:
            decoded = cut.decode("utf-8")
            break
        except UnicodeDecodeError as exc:
            cut = cut[: exc.start]
    else:
        decoded = fallback
    decoded = decoded.rstrip(" .")
    if " " in decoded:
        candidate = decoded.rsplit(" ", 1)[0].rstrip(" .-_")
        if len(candidate.encode("utf-8")) >= max(1, max_bytes // 2):
            decoded = candidate
    decoded = decoded.rstrip(" .-_")
    return decoded or fallback


def qualifier_label(value: object) -> str:
    """Return the conventional display form of a derivative marker."""

    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    key = text.casefold().replace("-", "_").replace(" ", "_")
    return _QUALIFIER_LABELS.get(key, text.replace("_", " ").title())


def _strip_trailing_qualifier(title: str, qualifier: str) -> str:
    if not qualifier:
        return title
    pattern = re.compile(
        rf"\s*[\(\[]\s*{re.escape(qualifier)}\s*[\)\]]\s*$",
        re.IGNORECASE,
    )
    return pattern.sub("", title).strip()


def naming_values(
    metadata: Mapping[str, Any],
    *,
    short_id: str,
    derivative: Sequence[object] = (),
) -> dict[str, str]:
    """Normalize sidecar metadata into the placeholders used by naming."""

    raw_artists = metadata.get("artist", metadata.get("artists", ()))
    if isinstance(raw_artists, str):
        artists = [raw_artists]
    elif isinstance(raw_artists, (list, tuple)):
        artists = [str(item).strip() for item in raw_artists if str(item).strip()]
    else:
        artists = []
    artist = artists[0] if artists else "Unknown Artist"
    if len(artists) > 1:
        featured = [item for item in artists[1:] if item.casefold() != artist.casefold()]
        if featured and "feat." not in artist.casefold():
            artist = f"{artist} feat. {featured[0]}"

    raw_title = metadata.get("title")
    title = str(raw_title).strip() if isinstance(raw_title, str) else ""
    if not title:
        title = "Unknown Title"

    qualifier = ""
    for value in derivative:
        candidate = qualifier_label(value)
        if candidate:
            qualifier = candidate
            break
    if qualifier:
        title = _strip_trailing_qualifier(title, qualifier)
        if qualifier.casefold() in title.casefold():
            qualifier = ""

    date = metadata.get("date")
    year = str(date).strip()[:4] if isinstance(date, str) else ""
    source_id = metadata.get("source_id")
    track = metadata.get("track")
    disc = metadata.get("disc")

    return {
        "artist": artist,
        "title": title,
        "qualifier": qualifier,
        "short_id": short_id,
        "year": year,
        "source_id": str(source_id).strip() if source_id is not None else "",
        "track": "" if track is None else str(track),
        "disc": "" if disc is None else str(disc),
        "collection": "",
    }


def format_library_name(
    naming: str,
    metadata: Mapping[str, Any],
    *,
    short_id: str,
    derivative: Sequence[object] = (),
    extension: str,
) -> str:
    """Format and sanitize one flat library filename."""

    values = naming_values(metadata, short_id=short_id, derivative=derivative)
    template = naming if isinstance(naming, str) and naming.strip() else DEFAULT_NAMING_TEMPLATE
    if values["artist"] == "Unknown Artist" and values["title"] == "Unknown Title":
        template = FALLBACK_NAMING_TEMPLATE
    try:
        rendered = _format_template(template, values)
    except (KeyError, ValueError) as exc:
        raise PublishFailed(
            "naming template is invalid",
            user_action="Use the documented placeholders, for example "
            "'{artist} - {title} ({qualifier})'.",
            context={"naming": template, "error": str(exc)},
        ) from exc
    rendered = _collapse_optional_groups(rendered)
    suffix = extension if extension.startswith(".") else f".{extension}"
    suffix = sanitize_component(suffix.lstrip("."), max_bytes=16)
    suffix = f".{suffix.lower()}"
    max_stem = max(1, MAX_FILENAME_BYTES - len(suffix.encode("utf-8")))
    stem = sanitize_component(rendered, max_bytes=max_stem)
    return f"{stem}{suffix}"


def _format_template(template: str, values: Mapping[str, str]) -> str:
    for placeholder in re.findall(r"{([^{}]+)}", template):
        if placeholder not in _PLACEHOLDERS:
            raise KeyError(f"unknown placeholder: {placeholder}")
    return template.format_map(dict(values))


def _collapse_optional_groups(value: str) -> str:
    text = value
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\[\s*\]", "", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    text = re.sub(r"\s+-\s+-", " -", text)
    return text.strip(" -_.")


def _next_collision_name(stem: str, suffix: str, number: int) -> str:
    marker = "" if number == 0 else f" ({number})"
    return f"{stem}{marker}{suffix}"


@dataclass(slots=True)
class _BundleView:
    directory: Path
    audio: Path
    sidecar: Path | None
    lyrics: tuple[Path, ...]
    metadata: dict[str, Any]
    derivative: tuple[str, ...]
    short_id: str


class FilesystemPublisher:
    """Publish a rendered workspace bundle into ``library_root``."""

    def __init__(self, *, workspace_root: Path | None = None) -> None:
        self._workspace_root = Path(workspace_root) if workspace_root is not None else None

    async def publish(
        self,
        bundle: OutputBundle,
        library_root: Path,
        naming: str,
        options: PublishOptions,
        *,
        bundle_dir: Path | None = None,
    ) -> LibraryEntry:
        """Atomically copy the bundle, selecting collision suffixes as needed."""

        if not isinstance(bundle, OutputBundle):
            raise PublishFailed("bundle must be an OutputBundle")
        if not isinstance(options, PublishOptions):
            raise PublishFailed("options must be a PublishOptions instance")
        if not options.copy:
            raise PublishFailed(
                "only copy mode is implemented in v1",
                user_action="Use copy mode for managed-library publication.",
            )
        view = self._inspect_bundle(bundle, bundle_dir)
        library_base = self._prepare_root(library_root)
        root = _prepare_collection_subfolder(library_base, options.subfolder)
        temp_dir = _prepare_temp_dir(root)

        audio_name = format_library_name(
            naming,
            view.metadata,
            short_id=view.short_id,
            derivative=view.derivative,
            extension=view.audio.suffix,
        )
        stem = audio_name[: -len(view.audio.suffix)] if view.audio.suffix else audio_name
        created: list[Path] = []
        temporary: list[Path] = []
        try:
            staged_audio = _stage_file(view.audio, temp_dir)
            temporary.append(staged_audio)
            audio_target, suffix_number = _link_with_collision(
                staged_audio,
                root,
                stem,
                view.audio.suffix.lower(),
            )
            created.append(audio_target)
            final_stem = audio_target.name[: -len(audio_target.suffix)]

            sidecar_path: Path | None = None
            if options.sidecar and view.sidecar is not None:
                staged_sidecar = _stage_file(view.sidecar, temp_dir)
                temporary.append(staged_sidecar)
                sidecar_path = _link_sidecar(staged_sidecar, root, suffix_number)
                created.append(sidecar_path)

            if options.lrc:
                for lyrics in view.lyrics:
                    staged_lyrics = _stage_file(lyrics, temp_dir)
                    temporary.append(staged_lyrics)
                    target = _link_with_collision(
                        staged_lyrics,
                        root,
                        final_stem,
                        lyrics.suffix.lower(),
                    )[0]
                    created.append(target)

            _fsync_directory(root)
            return LibraryEntry(
                id=LibraryEntryId(new_uuid7()),
                item_id=bundle.item_id or _sidecar_item_id(view),
                bundle_id=bundle.id,
                path=str(audio_target),
                status=LibraryStatus.PUBLISHED,
                published_at=datetime.now(UTC),
            )
        except PublishFailed:
            _cleanup(created)
            raise
        except (OSError, ValueError, PathEscapeError) as exc:
            _cleanup(created)
            raise PublishFailed(
                "bundle could not be published to the managed library",
                user_action="Check library permissions and available space.",
                context={
                    "library_root": str(root),
                    "bundle_id": str(bundle.id),
                    "error": str(exc),
                },
            ) from exc
        finally:
            for path in temporary:
                path.unlink(missing_ok=True)
            try:
                temp_dir.rmdir()
            except OSError:
                pass
        # Unreachable: kept for type checkers that cannot see the except path.

    def _inspect_bundle(
        self,
        bundle: OutputBundle,
        bundle_dir: Path | None,
    ) -> _BundleView:
        directory = Path(bundle_dir) if bundle_dir is not None else self._derive_bundle_dir(bundle)
        if not directory.is_dir():
            raise PublishFailed(
                "output bundle directory is missing",
                user_action="Render the item again before publishing.",
                context={"bundle_dir": str(directory), "bundle_id": str(bundle.id)},
            )
        audio: Path | None = None
        sidecar: Path | None = None
        lyrics: list[Path] = []
        for relative in bundle.files:
            try:
                candidate = _safe_bundle_child(directory, relative)
            except PathEscapeError as exc:
                raise PublishFailed(
                    "bundle contains an unsafe file path",
                    context={"file": str(relative), "bundle_id": str(bundle.id)},
                ) from exc
            if not candidate.is_file():
                raise PublishFailed(
                    "bundle file is missing",
                    context={"file": str(candidate), "bundle_id": str(bundle.id)},
                )
            suffix = candidate.suffix.lower()
            if suffix in _AUDIO_SUFFIXES:
                if audio is not None:
                    raise PublishFailed(
                        "bundle contains more than one audio file",
                        context={"bundle_id": str(bundle.id)},
                    )
                audio = candidate
            elif candidate.name == SIDECAR_FILENAME:
                sidecar = candidate
            elif suffix in {".lrc", ".elrc"}:
                lyrics.append(candidate)
        if audio is None:
            raise PublishFailed(
                "bundle has no supported audio file",
                context={"bundle_id": str(bundle.id), "files": list(bundle.files)},
            )
        if sidecar is None:
            raise PublishFailed(
                "bundle is missing trackforge.json",
                user_action="Render the item again; the sidecar is required for publication.",
                context={"bundle_id": str(bundle.id)},
            )
        document = _load_sidecar(sidecar)
        trackforge = document.get("trackforge")
        if isinstance(trackforge, Mapping) and trackforge.get("bundle_hash"):
            if str(trackforge["bundle_hash"]) != bundle.bundle_hash:
                raise PublishFailed(
                    "bundle sidecar hash does not match the output bundle",
                    user_action="Render the item again before publishing.",
                    context={"bundle_id": str(bundle.id)},
                )
        metadata = document.get("metadata")
        if not isinstance(metadata, Mapping):
            metadata = {}
        identity = document.get("identity")
        derivative: tuple[str, ...] = ()
        if isinstance(identity, Mapping) and isinstance(identity.get("derivative"), list):
            derivative = tuple(str(item) for item in identity["derivative"] if str(item).strip())
        item_id = None
        if isinstance(trackforge, Mapping):
            item_id = trackforge.get("item_id")
        short_id = _short_id(str(item_id) if item_id else str(bundle.item_id or bundle.id))
        return _BundleView(
            directory=directory,
            audio=audio,
            sidecar=sidecar,
            lyrics=tuple(sorted(lyrics, key=lambda item: item.name)),
            metadata=dict(metadata),
            derivative=derivative,
            short_id=short_id,
        )

    def _derive_bundle_dir(self, bundle: OutputBundle) -> Path:
        if self._workspace_root is None or bundle.item_id is None:
            raise PublishFailed(
                "bundle directory is required for publication",
                context={"bundle_id": str(bundle.id)},
            )
        render_id = str(bundle.render_id)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", render_id):
            raise PublishFailed("render id is not a safe path component")
        return self._workspace_root / "items" / str(bundle.item_id) / "renders" / render_id

    @staticmethod
    def _prepare_root(library_root: Path) -> Path:
        root = Path(library_root).expanduser()
        if root.exists() and not root.is_dir():
            raise PublishFailed(
                "library root is not a directory",
                context={"library_root": str(root)},
            )
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PublishFailed(
                "library root cannot be created",
                user_action="Check library permissions.",
                context={"library_root": str(root), "error": str(exc)},
            ) from exc
        try:
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise PublishFailed(
                "library root cannot be resolved",
                context={"library_root": str(root), "error": str(exc)},
            ) from exc
        return resolved


def _sidecar_item_id(view: _BundleView) -> Any:
    from trackforge.domain.model.ids import ItemId

    document = _load_sidecar(view.sidecar) if view.sidecar is not None else {}
    trackforge = document.get("trackforge")
    item_id = trackforge.get("item_id") if isinstance(trackforge, Mapping) else None
    if not isinstance(item_id, str) or not item_id:
        raise PublishFailed(
            "bundle sidecar does not identify its item",
            context={"bundle_dir": str(view.directory)},
        )
    return ItemId(item_id)


def _short_id(identifier: str) -> str:
    compact = re.sub(r"[^A-Za-z0-9]", "", identifier)
    return (compact[-8:] if len(compact) >= 8 else compact) or "unknown"


def _load_sidecar(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublishFailed(
            "sidecar is not valid UTF-8 JSON",
            user_action="Render the item again.",
            context={"path": str(path), "error": str(exc)},
        ) from exc
    if not isinstance(document, dict):
        raise PublishFailed(
            "sidecar must contain a JSON object",
            context={"path": str(path)},
        )
    return document


def _safe_bundle_child(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise PublishFailed("bundle file path must be non-empty")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise PathEscapeError("bundle file escapes its workspace")
    resolved_root = root.resolve(strict=True)
    combined = (resolved_root / candidate).resolve(strict=False)
    try:
        combined.relative_to(resolved_root)
    except ValueError as exc:
        raise PathEscapeError("bundle file escapes its workspace") from exc
    return combined


def _prepare_temp_dir(root: Path) -> Path:
    temp = root / ".tmp"
    try:
        temp.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(temp, 0o700)
    except OSError as exc:
        raise PublishFailed(
            "library temporary directory cannot be created",
            user_action="Check library permissions.",
            context={"path": str(temp), "error": str(exc)},
        ) from exc
    return temp


def _prepare_collection_subfolder(root: Path, subfolder: str | None) -> Path:
    if subfolder is None:
        return root
    name = sanitize_component(subfolder, fallback="Collection")
    if name in {".", ".."}:
        raise PublishFailed("collection folder name is not safe")
    target = _safe_destination(root, name)
    if target.is_symlink():
        raise PublishFailed(
            "collection folder must not be a symlink",
            context={"path": str(target)},
        )
    try:
        target.mkdir(parents=False, exist_ok=True, mode=0o755)
        if not target.is_dir():
            raise PublishFailed(
                "collection folder path is not a directory",
                context={"path": str(target)},
            )
        resolved = target.resolve(strict=True)
        resolved.relative_to(root)
    except PublishFailed:
        raise
    except (OSError, ValueError) as exc:
        raise PublishFailed(
            "collection folder could not be created inside the library",
            context={"path": str(target), "error": str(exc)},
        ) from exc
    return resolved


def _stage_file(source: Path, temp_dir: Path) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=".publish-", suffix=".tmp", dir=temp_dir)
    target = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream, source.open("rb") as input_stream:
            shutil.copyfileobj(input_stream, stream, length=1024 * 1024)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o644)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def _link_with_collision(
    staged: Path,
    root: Path,
    stem: str,
    suffix: str,
    *,
    start: int = 0,
) -> tuple[Path, int]:
    for number in range(start, start + 10_000):
        name = _next_collision_name(stem, suffix, number)
        candidate = _safe_destination(root, name)
        if _link_no_overwrite(staged, candidate):
            return candidate, number
    raise PublishFailed(
        "no free collision suffix is available",
        context={"stem": stem, "suffix": suffix},
    )


def _link_sidecar(staged: Path, root: Path, preferred_number: int) -> Path:
    stem = Path(SIDECAR_FILENAME).stem
    suffix = Path(SIDECAR_FILENAME).suffix
    numbers = [preferred_number, *range(0, 10_000)]
    seen: set[int] = set()
    for number in numbers:
        if number in seen:
            continue
        seen.add(number)
        name = _next_collision_name(stem, suffix, number)
        candidate = _safe_destination(root, name)
        if _link_no_overwrite(staged, candidate):
            return candidate
    raise PublishFailed("no free sidecar filename is available")


def _safe_destination(root: Path, name: str) -> Path:
    candidate = root / name
    try:
        parent = candidate.parent.resolve(strict=True)
        parent.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PublishFailed(
            "publish destination escapes the library root",
            context={"root": str(root), "name": name},
        ) from exc
    # Never resolve the final component: an existing symlink is an occupied
    # destination, not a path to follow.  ``_link_no_overwrite`` then selects
    # the next collision suffix.
    return candidate


def _link_no_overwrite(source: Path, destination: Path) -> bool:
    try:
        os.link(source, destination)
    except FileExistsError:
        return False
    except OSError as exc:
        if exc.errno not in {errno.EPERM, errno.EOPNOTSUPP, errno.EXDEV, errno.ENOSYS}:
            raise
        return _exclusive_copy(source, destination)
    return True


def _exclusive_copy(source: Path, destination: Path) -> bool:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(destination, flags, 0o644)
    except FileExistsError:
        return False
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_stream:
            shutil.copyfileobj(input_stream, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return True


def _cleanup(created: Sequence[Path]) -> None:
    for path in reversed(created):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


__all__ = [
    "FilesystemPublisher",
    "format_library_name",
    "naming_values",
    "qualifier_label",
    "sanitize_component",
]

"""Local-folder collection resolver.

A directory is a folder collection.  Enumeration is shallow, deterministic,
and never follows a symlink outside the explicitly selected root.  Folder
names become low-confidence context hypotheses, not trusted metadata.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trackforge.adapters.sources.local import LocalFileSource
from trackforge.application.ports.sources import (
    absolute_local_path,
    local_audio_extension,
)
from trackforge.domain.errors import UnsupportedSource
from trackforge.domain.model.collection import (
    Collection,
    CollectionContextClaim,
    CollectionItem,
    CollectionKind,
)
from trackforge.domain.model.ids import CollectionId, new_uuid7
from trackforge.domain.model.source import LocalPath, SourceRef

_MAX_FOLDER_ENTRIES = 501
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


class LocalFolderSource:
    """Discover supported audio files in one local directory."""

    id = "local_folder"
    version = "1"

    def supports_collection(self, ref: SourceRef) -> bool:
        """Return true for an existing local directory."""

        if not isinstance(ref, LocalPath):
            return False
        path = absolute_local_path(ref.path)
        try:
            return path.is_dir()
        except OSError:
            return False

    def supports(self, ref: SourceRef) -> bool:
        """Alias for generic resolver callers."""

        return self.supports_collection(ref)

    async def resolve_collection(
        self,
        ref: SourceRef,
        opts: Mapping[str, Any] | None = None,
    ) -> Collection:
        """Return a folder collection without copying bytes."""

        del opts
        if not isinstance(ref, LocalPath):
            raise UnsupportedSource(
                "local_folder source only supports local paths",
                user_action="Pass the path to a folder containing audio files.",
                context={"source_type": type(ref).__name__},
            )
        root = absolute_local_path(ref.path)
        try:
            if not root.is_dir():
                raise UnsupportedSource(
                    "local folder path is not a directory",
                    user_action="Pass an existing folder.",
                    context={"path": str(root)},
                )
            resolved_root = root.resolve(strict=True)
            entries = sorted(root.iterdir(), key=lambda path: (path.name.casefold(), path.name))
        except OSError as exc:
            raise UnsupportedSource(
                "local folder cannot be inspected",
                user_action="Check that the folder exists and is readable.",
                context={"path": str(root), "error": str(exc)},
            ) from exc

        paths: list[Path] = []
        ignored = 0
        for entry in entries:
            if local_audio_extension(entry) is None:
                if entry.is_file():
                    ignored += 1
                continue
            if not entry.is_file():
                continue
            if entry.is_symlink():
                try:
                    entry.resolve(strict=True).relative_to(resolved_root)
                except (OSError, ValueError):
                    ignored += 1
                    continue
            paths.append(entry)
            if len(paths) >= _MAX_FOLDER_ENTRIES:
                break

        if not paths:
            raise UnsupportedSource(
                "local folder contains no supported audio files",
                user_action="Add .mp3, .m4a, .flac, .ogg, or .opus files to the folder.",
                context={"path": str(root)},
            )

        resolver = LocalFileSource()
        items: list[CollectionItem] = []
        for position, path in enumerate(paths, start=1):
            resolved = await resolver.resolve(LocalPath(str(absolute_local_path(path))))
            items.append(
                CollectionItem(
                    position=position,
                    candidate=resolved.candidates[0],
                )
            )

        owner = resolved_root.parent.name or None
        claims: list[CollectionContextClaim] = [
            CollectionContextClaim(
                field="album",
                value=resolved_root.name,
                source="folder_name",
                confidence=0.2,
            )
        ]
        year = _YEAR_RE.search(resolved_root.name)
        if year is not None:
            claims.append(
                CollectionContextClaim(
                    field="date",
                    value=year.group(1),
                    source="folder_name",
                    confidence=0.15,
                )
            )
        warnings = (f"folder_unsupported_files_ignored:{ignored}",) if ignored else ()
        if len(paths) >= _MAX_FOLDER_ENTRIES:
            warnings = (*warnings, "folder_enumeration_truncated:501")
        return Collection(
            id=CollectionId(new_uuid7()),
            kind=CollectionKind.FOLDER,
            source_ref=LocalPath(str(root)),
            title=resolved_root.name,
            owner=owner,
            context_claims=tuple(claims),
            items=tuple(items),
            warnings=warnings,
        )

    async def resolve(
        self,
        ref: SourceRef,
        opts: Mapping[str, Any] | None = None,
    ) -> Collection:
        """Alias for callers that use the generic resolver spelling."""

        return await self.resolve_collection(ref, opts)


__all__ = ["LocalFolderSource"]

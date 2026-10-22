"""Local-file source resolver.

The resolver only inspects the path.  It never copies bytes, follows a
symlink chain beyond the explicit user-provided target, or changes mtimes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trackforge.application.ports.sources import (
    CandidateList,
    absolute_local_path,
    local_audio_extension,
)
from trackforge.domain.errors import UnsupportedSource
from trackforge.domain.model.ids import CandidateId, new_uuid7
from trackforge.domain.model.source import Candidate, Format, LocalPath, SourceRef


class LocalFileSource:
    """Discover one local audio file as a source candidate."""

    id = "local_file"
    version = "1"

    def supports(self, ref: SourceRef) -> bool:
        """Return true for an existing regular file with an MVP extension."""

        if not isinstance(ref, LocalPath):
            return False
        path = absolute_local_path(ref.path)
        if local_audio_extension(path) is None:
            return False
        try:
            return path.is_file()
        except OSError:
            return False

    async def resolve(
        self,
        ref: SourceRef,
        opts: Mapping[str, Any] | None = None,
    ) -> CandidateList:
        """Describe one local file without copying it.

        ``opts`` is accepted for protocol parity with future sources and is
        currently unused.
        """

        del opts
        if not isinstance(ref, LocalPath):
            raise UnsupportedSource(
                "local_file source only supports local paths",
                user_action="Pass a path to a supported .mp3/.m4a/.flac/.ogg/.opus file.",
                context={"source_type": type(ref).__name__},
            )

        path = absolute_local_path(ref.path)
        extension = local_audio_extension(path)
        if extension is None:
            raise UnsupportedSource(
                "unsupported local audio extension",
                user_action="Use one of: .mp3, .m4a, .flac, .ogg, .opus.",
                context={"path": str(path), "suffix": path.suffix},
            )
        try:
            if not path.is_file():
                reason = "directory" if path.is_dir() else "missing or not a regular file"
                raise UnsupportedSource(
                    f"local path is not a regular file ({reason})",
                    user_action="Pass the path to an existing supported audio file.",
                    context={"path": str(path)},
                )
            stat = path.stat()
        except OSError as exc:
            raise UnsupportedSource(
                "local path cannot be inspected",
                user_action="Check that the file exists and is readable.",
                context={"path": str(path), "error": str(exc)},
            ) from exc

        candidate = Candidate(
            id=CandidateId(new_uuid7()),
            source_ref=LocalPath(str(path)),
            external_id=str(path),
            title=path.stem,
            duration_seconds=None,
            formats=(Format(container=extension.lstrip("."), size=stat.st_size),),
            extra={
                "size_bytes": stat.st_size,
                "mtime": stat.st_mtime,
                "symlink": path.is_symlink(),
            },
        )
        return CandidateList(source=LocalPath(str(path)), candidates=(candidate,))


__all__ = ["LocalFileSource"]

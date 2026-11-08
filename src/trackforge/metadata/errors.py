"""Exceptions raised by the metadata layer."""

from __future__ import annotations


class TrackForgeError(Exception):
    """Base class for all TrackForge errors."""


class MetadataError(TrackForgeError):
    """Base class for metadata read/write errors."""


class MetadataReadError(MetadataError):
    """A container was recognized but its tags could not be read."""

    def __init__(self, path: str, reason: str) -> None:
        self.path: str = path
        self.reason: str = reason
        super().__init__(f"cannot read metadata from {path}: {reason}")


class MetadataWriteError(MetadataError):
    """A container was recognized but its tags could not be written."""

    def __init__(self, path: str, reason: str) -> None:
        self.path: str = path
        self.reason: str = reason
        super().__init__(f"cannot write metadata to {path}: {reason}")


class UnsupportedFormatError(MetadataError):
    """The file's container is not supported by any registered backend."""

    def __init__(self, path: str) -> None:
        self.path: str = path
        super().__init__(f"unsupported media format: {path}")


class InvalidMetadataError(MetadataError):
    """A metadata value cannot be represented or parsed."""

    def __init__(self, field: str, value: object, reason: str) -> None:
        self.field: str = field
        self.value: object = value
        self.reason: str = reason
        super().__init__(f"invalid {field}={value!r}: {reason}")


class NoAudioStreamError(MetadataError):
    """The document has no audio stream."""

    def __init__(self, path: str) -> None:
        self.path: str = path
        super().__init__(f"no audio stream in {path}")


class NoVideoStreamError(MetadataError):
    """The document has no video stream."""

    def __init__(self, path: str) -> None:
        self.path: str = path
        super().__init__(f"no video stream in {path}")


class UnsupportedFieldError(MetadataError):
    """A field cannot be stored in the document's container."""

    def __init__(self, field: str, container: str) -> None:
        self.field: str = field
        self.container: str = container
        super().__init__(f"field {field!r} is not supported by {container}")


class WriteVerificationError(MetadataError):
    """Tags were written but reading them back did not match."""

"""Opaque, time-sortable identifiers.

Identifiers are UUIDv7 strings.  The domain does not depend on a particular
generator implementation: callers may supply their own IDs, while this module
provides a small RFC 9562 generator with no third-party dependency.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any, NewType

from trackforge.domain.errors import DomainValidationError, InvalidId

ItemId = NewType("ItemId", str)
MediaId = NewType("MediaId", str)
IntakeId = NewType("IntakeId", str)
ObservationId = NewType("ObservationId", str)
ClaimId = NewType("ClaimId", str)
DecisionRecordId = NewType("DecisionRecordId", str)
DecisionId = NewType("DecisionId", str)
RunId = NewType("RunId", str)
StepId = NewType("StepId", str)
RenderId = NewType("RenderId", str)
BundleId = NewType("BundleId", str)
CorrectionId = NewType("CorrectionId", str)

# Additional identifiers used by the aggregate references in this plan.
CandidateId = NewType("CandidateId", str)
CollectionId = NewType("CollectionId", str)
FileInstanceId = NewType("FileInstanceId", str)
ArtifactId = NewType("ArtifactId", str)
FeedbackEventId = NewType("FeedbackEventId", str)
ReviewSessionId = NewType("ReviewSessionId", str)
LibraryEntryId = NewType("LibraryEntryId", str)
PolicyId = NewType("PolicyId", str)

WorkId = NewType("WorkId", str)
RecordingId = NewType("RecordingId", str)
ReleaseId = NewType("ReleaseId", str)
ReleaseGroupId = NewType("ReleaseGroupId", str)
ArtistId = NewType("ArtistId", str)
DerivativeId = NewType("DerivativeId", str)
IdentityHypothesisId = NewType("IdentityHypothesisId", str)

_ID_KIND = type[Any] | Callable[[str], str]

_MAX_TIMESTAMP_MS = (1 << 48) - 1
_RANDOM_BITS = 74
_RANDOM_MASK = (1 << _RANDOM_BITS) - 1
_RAND_B_MASK = (1 << 62) - 1

_lock = threading.Lock()
_last_timestamp_ms = -1
_last_random = 0


def new_uuid7(now_ns: int | None = None) -> str:
    """Return an RFC 9562 UUIDv7 string.

    The timestamp occupies the first 48 bits, version is 7, and the variant is
    RFC 4122.  Within one millisecond the 74 random bits are used as a
    monotonically increasing suffix, so repeated calls remain sortable by
    string comparison.  Passing ``now_ns`` is useful for deterministic tests;
    production callers normally omit it.
    """

    global _last_random, _last_timestamp_ms

    if now_ns is None:
        timestamp_ns = time.time_ns()
    else:
        if isinstance(now_ns, bool) or not isinstance(now_ns, int):
            raise DomainValidationError(
                "now_ns must be an integer number of nanoseconds",
                context={"actual_type": type(now_ns).__name__},
            )
        if now_ns < 0:
            raise DomainValidationError(
                "now_ns must not be negative",
                context={"now_ns": now_ns},
            )
        timestamp_ns = now_ns

    timestamp_ms = timestamp_ns // 1_000_000
    if timestamp_ms > _MAX_TIMESTAMP_MS:
        raise DomainValidationError(
            "now_ns is outside the UUIDv7 timestamp range",
            context={"now_ns": now_ns},
        )

    with _lock:
        if timestamp_ms == _last_timestamp_ms:
            _last_random = (_last_random + 1) & _RANDOM_MASK
            if _last_random == 0:
                # A 74-bit overflow within one millisecond is practically
                # impossible, but advancing the clock keeps the prefix sorted.
                _last_timestamp_ms += 1
                timestamp_ms = _last_timestamp_ms
                _last_random = int.from_bytes(os.urandom(10), "big") & _RANDOM_MASK
        else:
            _last_timestamp_ms = timestamp_ms
            _last_random = int.from_bytes(os.urandom(10), "big") & _RANDOM_MASK

        timestamp_ms = _last_timestamp_ms
        random_bits = _last_random

    if timestamp_ms > _MAX_TIMESTAMP_MS:
        raise DomainValidationError(
            "UUIDv7 timestamp overflow",
            context={"timestamp_ms": timestamp_ms},
        )

    rand_a = (random_bits >> 62) & 0xFFF
    rand_b = random_bits & _RAND_B_MASK
    value = (timestamp_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return str(uuid.UUID(int=value))


def parse_id(value: str, kind: _ID_KIND) -> str:
    """Validate a UUID string at an input boundary and return canonical text.

    ``kind`` is used to make errors specific (for example ``ItemId``).  The
    model does not require version 7 here: IDs from fixtures or future
    generators remain valid, while the generator above always emits v7.
    """

    kind_name = getattr(kind, "__name__", str(kind))
    if not isinstance(value, str) or not value.strip():
        raise InvalidId(
            f"{kind_name} must be a non-empty UUID string",
            context={"kind": kind_name},
        )
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise InvalidId(
            f"{kind_name} is not a valid UUID",
            context={"kind": kind_name},
        ) from exc
    return str(parsed)


__all__ = [
    "ArtifactId",
    "ArtistId",
    "BundleId",
    "CandidateId",
    "ClaimId",
    "CollectionId",
    "CorrectionId",
    "DecisionId",
    "DecisionRecordId",
    "DerivativeId",
    "FeedbackEventId",
    "FileInstanceId",
    "IdentityHypothesisId",
    "IntakeId",
    "ItemId",
    "LibraryEntryId",
    "MediaId",
    "ObservationId",
    "PolicyId",
    "RecordingId",
    "ReleaseGroupId",
    "ReleaseId",
    "RenderId",
    "ReviewSessionId",
    "RunId",
    "StepId",
    "WorkId",
    "new_uuid7",
    "parse_id",
]

"""Field and cross-field validation for claims and decisions.

Validators return structured issues instead of raising.  The resolver uses the
hard issues to reject claims (and eventually choose ``unknown``) while soft
issues only become visible warnings.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model._validation import FieldValue
from trackforge.domain.policies.normalization import (
    ParsedDate,
    canonical_field,
    normalize_bpm,
    normalize_key,
    parse_date,
)

_MBID_FIELDS = frozenset(
    {
        "mb_recording_id",
        "mb_release_id",
        "mb_release_group_id",
        "mb_artist_id",
        "mb_albumartist_id",
    }
)
_ID_FIELDS = _MBID_FIELDS | {"isrc", "acoustid"}
_TEXT_FIELDS = frozenset(
    {
        "title",
        "artist",
        "album",
        "albumartist",
        "comment",
        "source",
        "lyrics",
        "artwork",
        "description",
        "label",
        "catalog_number",
        "barcode",
        "copyright",
        "license",
    }
)
_PATH_SEPARATORS = ("/", "\\", "\x00")
_ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")
_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(?:[-_][A-Za-z]{2,4})?$")


class Severity(StrEnum):
    """Whether an issue rejects a value or only warns about it."""

    HARD = "hard"
    SOFT = "soft"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One validation finding with a stable machine-readable code."""

    code: str
    message: str
    field: str
    severity: Severity = Severity.HARD

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code.strip():
            raise DomainValidationError("validation issue code must be non-empty")
        if not isinstance(self.message, str) or not self.message.strip():
            raise DomainValidationError("validation issue message must be non-empty")
        object.__setattr__(self, "code", self.code.strip())
        object.__setattr__(self, "field", canonical_field(self.field))


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Aggregated hard errors and soft warnings for one or more values."""

    issues: tuple[ValidationIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "issues", tuple(self.issues))

    @property
    def hard_issues(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity is Severity.HARD)

    @property
    def soft_issues(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity is Severity.SOFT)

    @property
    def valid(self) -> bool:
        return not self.hard_issues

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(issue.message for issue in self.soft_issues)

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(issue.message for issue in self.hard_issues)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

    def extend(self, other: ValidationResult) -> ValidationResult:
        return ValidationResult(self.issues + other.issues)

    def __bool__(self) -> bool:
        return self.valid


def merge_results(*results: ValidationResult) -> ValidationResult:
    """Merge results preserving order and de-duplicating identical issues."""

    merged: list[ValidationIssue] = []
    seen: set[tuple[str, str, str, str]] = set()
    for result in results:
        for issue in result.issues:
            key = (issue.code, issue.field, issue.severity.value, issue.message)
            if key not in seen:
                seen.add(key)
                merged.append(issue)
    return ValidationResult(tuple(merged))


def validate_confidence(value: object) -> ValidationResult:
    """Validate confidence in the inclusive ``[0, 1]`` range."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _hard("confidence_not_number", "confidence must be a number", "confidence")
    number = float(value)
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        return _hard(
            "confidence_out_of_range",
            "confidence must be between 0 and 1",
            "confidence",
        )
    return ValidationResult()


def validate_field(
    field: str,
    value: object,
    *,
    allow_future: bool = False,
    today: date | None = None,
) -> ValidationResult:
    """Validate one field value.

    ``None`` is a legal unknown value and always passes.  Unknown field names
    also pass: policies may use extra evidence fields that are not rendered.
    """

    canonical = canonical_field(field)
    if value is None:
        return ValidationResult()
    if canonical == "confidence":
        return validate_confidence(value)

    if canonical == "title":
        return _validate_text(
            canonical,
            value,
            max_length=300,
            soft_reason="suspiciously long title",
            reject_path_separators=True,
        )
    if canonical == "artist":
        return _validate_text_list(
            canonical,
            value,
            max_length=200,
            soft_reason="suspiciously long artist name",
            reject_path_separators=True,
        )
    if canonical == "genre":
        return _validate_text_list(
            canonical,
            value,
            max_length=80,
            soft_reason="suspiciously long genre",
            reject_path_separators=False,
        )
    if canonical in _TEXT_FIELDS:
        return _validate_text(canonical, value, max_length=10_000)
    if canonical in {"date", "originaldate"}:
        return _validate_date(
            canonical,
            value,
            allow_future=allow_future,
            today=today,
        )
    if canonical == "bpm":
        return _validate_bpm(value)
    if canonical == "key":
        return _validate_key(value)
    if canonical in {"track", "disc", "tracktotal", "disctotal"}:
        return _validate_positive_int(canonical, value)
    if canonical == "compilation":
        if not isinstance(value, bool):
            return _hard(
                "compilation_not_boolean",
                "compilation must be a boolean",
                canonical,
            )
        return ValidationResult()
    if canonical in _MBID_FIELDS:
        return _validate_uuid(canonical, value)
    if canonical == "isrc":
        return _validate_isrc(value)
    if canonical == "acoustid":
        return _validate_acoustid(value)
    if canonical == "language":
        return _validate_language(value)
    return ValidationResult()


def validate_claim(
    field: str,
    value: object,
    *,
    allow_future: bool = False,
    today: date | None = None,
) -> ValidationResult:
    """Alias for :func:`validate_field` phrased for claim call sites."""

    return validate_field(field, value, allow_future=allow_future, today=today)


def validate_cross_fields(
    values: Mapping[str, FieldValue],
    *,
    today: date | None = None,
) -> ValidationResult:
    """Validate relationships between resolved fields.

    Only genuinely impossible combinations are hard errors.  Missing
    ``albumartist`` for an album is a warning because it is a desired, not
    required, field.
    """

    issues: list[ValidationIssue] = []
    normalized = {canonical_field(key): value for key, value in values.items()}

    album = normalized.get("album")
    albumartist = normalized.get("albumartist")
    if _present(album) and not _present(albumartist):
        issues.append(
            _soft(
                "albumartist_missing",
                "album is set but albumartist is unknown",
                "albumartist",
            )
        )

    track = _as_positive_int(normalized.get("track"))
    tracktotal = _as_positive_int(normalized.get("tracktotal"))
    if track is not None and tracktotal is not None and track > tracktotal:
        issues.append(
            _hard_issue(
                "track_exceeds_total",
                f"track {track} exceeds tracktotal {tracktotal}",
                "track",
            )
        )

    disc = _as_positive_int(normalized.get("disc"))
    disctotal = _as_positive_int(normalized.get("disctotal"))
    if disc is not None and disctotal is not None and disc > disctotal:
        issues.append(
            _hard_issue(
                "disc_exceeds_total",
                f"disc {disc} exceeds disctotal {disctotal}",
                "disc",
            )
        )

    date_value = _as_string(normalized.get("date"))
    originaldate = _as_string(normalized.get("originaldate"))
    if date_value is not None and originaldate is not None:
        comparison = _compare_dates(date_value, originaldate)
        if comparison is not None and comparison < 0:
            issues.append(
                _hard_issue(
                    "date_before_originaldate",
                    "date is earlier than originaldate",
                    "date",
                )
            )
        elif comparison is None:
            issues.append(
                _soft(
                    "date_precision_mismatch",
                    "date and originaldate have incomparable precision",
                    "date",
                )
            )

    compilation = normalized.get("compilation")
    if compilation is True:
        if not _present(albumartist):
            issues.append(
                _soft(
                    "compilation_without_albumartist",
                    "compilation is set but albumartist is unknown",
                    "compilation",
                )
            )
        elif isinstance(albumartist, str):
            key = albumartist.strip().casefold()
            if key not in {"various artists", "various", "va"}:
                issues.append(
                    _soft(
                        "compilation_albumartist_mismatch",
                        "compilation is set but albumartist is not Various Artists",
                        "compilation",
                    )
                )

    return ValidationResult(tuple(issues))


def validate_values(
    values: Mapping[str, FieldValue],
    *,
    allow_future: bool = False,
    today: date | None = None,
) -> ValidationResult:
    """Validate every value in a mapping plus their cross-field relationships."""

    results = [
        validate_field(field, value, allow_future=allow_future, today=today)
        for field, value in values.items()
    ]
    results.append(validate_cross_fields(values, today=today))
    return merge_results(*results)


def _validate_text(
    field: str,
    value: object,
    *,
    max_length: int,
    soft_reason: str | None = None,
    reject_path_separators: bool = False,
) -> ValidationResult:
    values, issue = _string_values(field, value)
    if issue is not None or values is None:
        return issue if issue is not None else ValidationResult()
    issues: list[ValidationIssue] = []
    for text in values:
        if not text.strip():
            issues.append(_hard_issue("empty_value", f"{field} must not be empty", field))
            continue
        if reject_path_separators and any(separator in text for separator in _PATH_SEPARATORS):
            issues.append(
                _hard_issue(
                    "path_separator",
                    f"{field} must not contain path separators",
                    field,
                )
            )
        if len(text) > max_length and soft_reason is not None:
            issues.append(
                _soft(
                    "suspicious_length",
                    f"{field} is {soft_reason} ({len(text)} characters)",
                    field,
                )
            )
    return ValidationResult(tuple(issues))


def _validate_text_list(
    field: str,
    value: object,
    *,
    max_length: int,
    soft_reason: str,
    reject_path_separators: bool,
) -> ValidationResult:
    if isinstance(value, str):
        return _validate_text(
            field,
            value,
            max_length=max_length,
            soft_reason=soft_reason,
            reject_path_separators=reject_path_separators,
        )
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return _validate_text(
            field,
            list(value),
            max_length=max_length,
            soft_reason=soft_reason,
            reject_path_separators=reject_path_separators,
        )
    return _hard(
        "wrong_type",
        f"{field} must be a string or a list of strings",
        field,
    )


def _string_values(
    field: str,
    value: object,
) -> tuple[list[str] | None, ValidationResult | None]:
    if isinstance(value, str):
        return [value], None
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return list(value), None
    return None, _hard("wrong_type", f"{field} must be a string", field)


def _validate_date(
    field: str,
    value: object,
    *,
    allow_future: bool,
    today: date | None,
) -> ValidationResult:
    if not isinstance(value, str):
        return _hard("wrong_type", f"{field} must be an ISO date string", field)
    parsed = parse_date(value)
    if parsed is None:
        return _hard(
            "invalid_date",
            f"{field} must be YYYY, YYYY-MM, or YYYY-MM-DD",
            field,
        )
    issues: list[ValidationIssue] = []
    current = today if today is not None else date.today()
    if not allow_future and _is_future(parsed, current):
        issues.append(_hard_issue("future_date", f"{field} must not be in the future", field))
    if parsed.year < 1900:
        issues.append(
            _soft(
                "suspicious_date",
                f"{field} looks implausibly old",
                field,
            )
        )
    return ValidationResult(tuple(issues))


def _is_future(parsed: ParsedDate, today: date) -> bool:
    year = parsed.year
    month = parsed.month
    day = parsed.day
    if year > today.year:
        return True
    if year < today.year:
        return False
    if month is None:
        return False
    if month > today.month:
        return True
    if month < today.month:
        return False
    if day is None:
        return False
    return bool(day > today.day)


def _validate_bpm(value: object) -> ValidationResult:
    bpm = normalize_bpm(value)
    if bpm is None:
        return _hard("invalid_bpm", "bpm must be a finite positive number", "bpm")
    if bpm < 20.0 or bpm > 300.0:
        return _hard("bpm_out_of_range", "bpm must be between 20 and 300", "bpm")
    return ValidationResult()


def _validate_key(value: object) -> ValidationResult:
    if not isinstance(value, str):
        return _hard("wrong_type", "key must be a string", "key")
    if normalize_key(value) is None:
        return _hard(
            "invalid_key",
            "key must look like C, C#, Db, Am, or F#m",
            "key",
        )
    return ValidationResult()


def _validate_positive_int(field: str, value: object) -> ValidationResult:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return _hard(
            "invalid_positive_integer",
            f"{field} must be a positive integer",
            field,
        )
    return ValidationResult()


def _validate_uuid(field: str, value: object) -> ValidationResult:
    if not isinstance(value, str):
        return _hard("wrong_type", f"{field} must be a UUID string", field)
    try:
        uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        return _hard("invalid_mbid", f"{field} must be a valid UUID", field)
    return ValidationResult()


def _validate_isrc(value: object) -> ValidationResult:
    if not isinstance(value, str):
        return _hard("wrong_type", "isrc must be a string", "isrc")
    compact = re.sub(r"[\s-]", "", value).upper()
    if _ISRC_RE.fullmatch(compact) is None:
        return _hard(
            "invalid_isrc",
            "isrc must be 12 characters (CC-XXX-YY-NNNNN)",
            "isrc",
        )
    return ValidationResult()


def _validate_acoustid(value: object) -> ValidationResult:
    if not isinstance(value, str) or not value.strip():
        return _hard("invalid_acoustid", "acoustid must be a non-empty string", "acoustid")
    return ValidationResult()


def _validate_language(value: object) -> ValidationResult:
    if not isinstance(value, str) or _LANGUAGE_RE.fullmatch(value.strip()) is None:
        return _hard(
            "invalid_language",
            "language must be an ISO 639 code",
            "language",
        )
    return ValidationResult()


def _compare_dates(left: str, right: str) -> int | None:
    first = parse_date(left)
    second = parse_date(right)
    if first is None or second is None:
        return None
    if first.year != second.year:
        return 1 if first.year > second.year else -1
    if first.month is None or second.month is None:
        # A year-only value cannot prove it is earlier than a more precise
        # value inside the same year.
        return None
    if first.month != second.month:
        return 1 if first.month > second.month else -1
    if first.day is None or second.day is None:
        return None
    if first.day == second.day:
        return 0
    return 1 if first.day > second.day else -1


def _as_positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _as_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple)):
        return bool(value)
    return True


def _hard_issue(code: str, message: str, field: str) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, field=field, severity=Severity.HARD)


def _hard(code: str, message: str, field: str) -> ValidationResult:
    return ValidationResult((_hard_issue(code, message, field),))


def _soft(code: str, message: str, field: str) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, field=field, severity=Severity.SOFT)


validate_cross_field = validate_cross_fields


__all__ = [
    "Severity",
    "ValidationIssue",
    "ValidationResult",
    "merge_results",
    "validate_claim",
    "validate_confidence",
    "validate_cross_field",
    "validate_cross_fields",
    "validate_field",
    "validate_values",
]

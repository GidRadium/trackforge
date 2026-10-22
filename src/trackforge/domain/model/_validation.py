"""Shared validation helpers for domain value objects.

These helpers keep invariants in one place and always raise typed domain
errors rather than framework or implementation exceptions.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import Enum
from typing import Any, NoReturn

from trackforge.domain.errors import DomainValidationError

FieldValue = str | int | float | bool | list[str] | None


class FrozenDict(dict[str, Any]):
    """JSON object that cannot be mutated after construction."""

    def _immutable(self, *args: object, **kwargs: object) -> NoReturn:
        raise TypeError("domain mappings are immutable")

    def __setitem__(self, key: str, value: Any) -> None:
        self._immutable(key, value)

    def __delitem__(self, key: str) -> None:
        self._immutable(key)

    def clear(self) -> None:
        self._immutable()

    def pop(self, key: str, default: Any = None) -> Any:
        self._immutable(key, default)

    def popitem(self) -> tuple[str, Any]:
        self._immutable()

    def setdefault(self, key: str, default: Any = None) -> Any:
        self._immutable(key, default)

    def update(self, *args: object, **kwargs: object) -> None:
        self._immutable(*args, **kwargs)

    def __ior__(self, other: dict[str, Any]) -> FrozenDict:  # type: ignore[override,misc]
        self._immutable(other)


class Array(tuple[Any, ...]):
    """Immutable tuple that also compares equal to an equivalent list.

    Domain records expose arrays as tuples for immutability.  The additional
    list comparison keeps value objects ergonomic for callers that build
    expectations from JSON-like data.
    """

    def __eq__(self, other: object) -> bool:
        if isinstance(other, list):
            return tuple(self) == tuple(other)
        return tuple.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        return not self == other

    def __hash__(self) -> int:
        return tuple.__hash__(self)


def utc_now() -> datetime:
    """Return the current UTC time for default timestamps."""

    return datetime.now(UTC)


def require_text(value: object, field: str) -> str:
    """Require a non-empty string without normalizing its contents."""

    if not isinstance(value, str):
        raise DomainValidationError(
            f"{field} must be a string",
            context={"field": field, "actual_type": type(value).__name__},
        )
    if not value.strip():
        raise DomainValidationError(
            f"{field} must not be empty",
            context={"field": field},
        )
    return value


def optional_text(value: object | None, field: str) -> str | None:
    if value is None:
        return None
    return require_text(value, field)


def require_id(value: object, field: str) -> str:
    """Require an opaque identifier.

    The concrete UUID format is validated at input boundaries by
    :func:`trackforge.domain.model.ids.parse_id`; domain records trust the
    typed identifier once it has entered the model.
    """

    return require_text(value, field)


def optional_id(value: object | None, field: str) -> str | None:
    if value is None:
        return None
    return require_id(value, field)


def coerce_path(value: object, field: str) -> str:
    """Accept text or ``os.PathLike`` and return a non-empty string path."""

    if isinstance(value, str):
        text = value
    elif isinstance(value, os.PathLike):
        text = os.fspath(value)
        if isinstance(text, bytes):
            raise DomainValidationError(
                f"{field} must be a text path",
                context={"field": field},
            )
    else:
        raise DomainValidationError(
            f"{field} must be a path string",
            context={"field": field, "actual_type": type(value).__name__},
        )
    if not text.strip():
        raise DomainValidationError(
            f"{field} must not be empty",
            context={"field": field},
        )
    return text


def ensure_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise DomainValidationError(
            f"{field} must be a datetime",
            context={"field": field, "actual_type": type(value).__name__},
        )
    return value


def ensure_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise DomainValidationError(
            f"{field} must be a boolean",
            context={"field": field, "actual_type": type(value).__name__},
        )
    return value


def ensure_int(
    value: object,
    field: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainValidationError(
            f"{field} must be an integer",
            context={"field": field, "actual_type": type(value).__name__},
        )
    if minimum is not None and value < minimum:
        raise DomainValidationError(
            f"{field} must be >= {minimum}",
            context={"field": field, "value": value},
        )
    if maximum is not None and value > maximum:
        raise DomainValidationError(
            f"{field} must be <= {maximum}",
            context={"field": field, "value": value},
        )
    return value


def ensure_float(
    value: object,
    field: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DomainValidationError(
            f"{field} must be a number",
            context={"field": field, "actual_type": type(value).__name__},
        )
    number = float(value)
    if not math.isfinite(number):
        raise DomainValidationError(
            f"{field} must be finite",
            context={"field": field, "value": str(value)},
        )
    if minimum is not None and number < minimum:
        raise DomainValidationError(
            f"{field} must be >= {minimum}",
            context={"field": field, "value": number},
        )
    if maximum is not None and number > maximum:
        raise DomainValidationError(
            f"{field} must be <= {maximum}",
            context={"field": field, "value": number},
        )
    return number


def validate_confidence(value: object, field: str = "confidence") -> float:
    """Validate calibrated confidence in the inclusive ``[0, 1]`` range."""

    return ensure_float(value, field, minimum=0.0, maximum=1.0)


def validate_field_value(value: object, field: str = "value") -> FieldValue:
    """Validate the canonical scalar/list value shape used by claims."""

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DomainValidationError(
                f"{field} must be finite",
                context={"field": field, "value": str(value)},
            )
        return value
    if isinstance(value, (list, tuple)):
        items: list[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, str):
                raise DomainValidationError(
                    f"{field}[{index}] must be a string",
                    context={"field": field, "index": index},
                )
            items.append(item)
        return items
    raise DomainValidationError(
        f"{field} must be a string, number, boolean, list of strings, or null",
        context={"field": field, "actual_type": type(value).__name__},
    )


def coerce_enum[EnumT: Enum](value: object, enum_type: type[EnumT], field: str) -> EnumT:
    """Coerce a string/enum value to ``enum_type`` or raise a domain error."""

    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError:
            pass
    allowed = ", ".join(str(member.value) for member in enum_type)
    raise DomainValidationError(
        f"{field} must be one of: {allowed}",
        context={"field": field, "value": str(value)},
    )


def optional_enum[EnumT: Enum](
    value: object | None,
    enum_type: type[EnumT],
    field: str,
) -> EnumT | None:
    if value is None:
        return None
    return coerce_enum(value, enum_type, field)


def ensure_sequence(value: object, field: str) -> tuple[Any, ...]:
    """Return a tuple copy of a non-string sequence."""

    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise DomainValidationError(
            f"{field} must be a sequence",
            context={"field": field, "actual_type": type(value).__name__},
        )
    return Array(value)


def ensure_str_tuple(
    value: object,
    field: str,
    *,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    items = ensure_sequence(value, field)
    result: list[str] = []
    for index, item in enumerate(items):
        result.append(require_text(item, f"{field}[{index}]"))
    if not allow_empty and not result:
        raise DomainValidationError(
            f"{field} must not be empty",
            context={"field": field},
        )
    return Array(result)


def ensure_id_tuple(
    value: object,
    field: str,
    *,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    items = ensure_sequence(value, field)
    result: list[str] = []
    for index, item in enumerate(items):
        result.append(require_id(item, f"{field}[{index}]"))
    if not allow_empty and not result:
        raise DomainValidationError(
            f"{field} must not be empty",
            context={"field": field},
        )
    return Array(result)


def ensure_sha256(value: object, field: str = "content_hash") -> str:
    if not isinstance(value, str):
        raise DomainValidationError(
            f"{field} must be a string",
            context={"field": field, "actual_type": type(value).__name__},
        )
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise DomainValidationError(
            f"{field} must be 64 lowercase hexadecimal characters",
            context={"field": field},
        )
    return value


def json_mapping(value: object, field: str) -> dict[str, Any]:
    """Validate and deep-copy a JSON object (string keys only)."""

    if not isinstance(value, Mapping):
        raise DomainValidationError(
            f"{field} must be a JSON object",
            context={"field": field, "actual_type": type(value).__name__},
        )
    return _json_mapping(value, field, depth=0, seen=set())


def _json_mapping(
    value: Mapping[Any, Any],
    field: str,
    *,
    depth: int,
    seen: set[int],
) -> dict[str, Any]:
    identifier = id(value)
    if identifier in seen:
        raise DomainValidationError(
            f"{field} contains a circular reference",
            context={"field": field},
        )
    if depth > 25:
        raise DomainValidationError(
            f"{field} is nested too deeply",
            context={"field": field},
        )
    seen.add(identifier)
    result: dict[str, Any] = {}
    try:
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise DomainValidationError(
                    f"{field} keys must be strings",
                    context={"field": field, "actual_type": type(raw_key).__name__},
                )
            result[raw_key] = _json_value(
                raw_value,
                f"{field}.{raw_key}",
                depth=depth + 1,
                seen=seen,
            )
    finally:
        seen.discard(identifier)
    return FrozenDict(result)


def _json_value(value: Any, field: str, *, depth: int, seen: set[int]) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DomainValidationError(
                f"{field} must be finite",
                context={"field": field, "value": str(value)},
            )
        return value
    if isinstance(value, Enum):
        return _json_value(value.value, field, depth=depth + 1, seen=seen)
    if isinstance(value, Mapping):
        return _json_mapping(value, field, depth=depth, seen=seen)
    if isinstance(value, (list, tuple)):
        identifier = id(value)
        if identifier in seen:
            raise DomainValidationError(
                f"{field} contains a circular reference",
                context={"field": field},
            )
        if depth > 25:
            raise DomainValidationError(
                f"{field} is nested too deeply",
                context={"field": field},
            )
        seen.add(identifier)
        try:
            return Array(
                _json_value(item, f"{field}[{index}]", depth=depth + 1, seen=seen)
                for index, item in enumerate(value)
            )
        finally:
            seen.discard(identifier)
    raise DomainValidationError(
        f"{field} must be JSON-compatible",
        context={"field": field, "actual_type": type(value).__name__},
    )


def ensure_string_mapping(
    value: object,
    field: str,
) -> dict[str, str]:
    """Validate a shallow string-to-string mapping."""

    if not isinstance(value, Mapping):
        raise DomainValidationError(
            f"{field} must be a mapping",
            context={"field": field, "actual_type": type(value).__name__},
        )
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = require_text(raw_key, f"{field} key")
        result[key] = require_text(raw_value, f"{field}.{key}")
    return result

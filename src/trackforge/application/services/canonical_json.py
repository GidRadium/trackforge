"""Canonical JSON encoding and SHA-256 helpers.

Canonical JSON is the stable representation used for cache keys and artifact
manifests: UTF-8, sorted object keys, no insignificant whitespace, and no
``NaN``/``Infinity`` values.  Two equal JSON values always produce the same
bytes, independent of insertion order or locale.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["cache_key_for", "canonical_json", "sha256_hex"]


def _check_json_value(value: object, path: str, seen: set[int]) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must not contain NaN or Infinity")
        return
    if isinstance(value, (list, tuple)):
        identifier = id(value)
        if identifier in seen:
            raise ValueError(f"{path} contains a circular reference")
        seen.add(identifier)
        try:
            for index, item in enumerate(value):
                _check_json_value(item, f"{path}[{index}]", seen)
        finally:
            seen.discard(identifier)
        return
    if isinstance(value, Mapping):
        identifier = id(value)
        if identifier in seen:
            raise ValueError(f"{path} contains a circular reference")
        seen.add(identifier)
        try:
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError(f"{path} keys must be strings")
                _check_json_value(item, f"{path}.{key}", seen)
        finally:
            seen.discard(identifier)
        return
    raise TypeError(f"{path} is not JSON-serializable: {type(value).__name__}")


def canonical_json(value: object) -> bytes:
    """Encode ``value`` as canonical UTF-8 JSON bytes.

    The result has sorted keys, ``(",", ":")`` separators, no trailing newline,
    and non-ASCII text is emitted as UTF-8 rather than escaped.  ``NaN`` and
    infinities are rejected instead of being silently emitted as invalid JSON.
    """

    _check_json_value(value, "$", set())
    text = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return text.encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Return the lowercase hexadecimal SHA-256 digest of ``data``."""

    return hashlib.sha256(data).hexdigest()


def cache_key_for(
    inputs: Sequence[object],
    capability: Mapping[str, object] | str,
    params: Mapping[str, object] | None = None,
    config: Mapping[str, object] | None = None,
    model: Mapping[str, object] | None = None,
    prompt: Mapping[str, object] | None = None,
) -> str:
    """Build a deterministic cache key from the documented inputs.

    This is a pure helper, not cache policy: TTL, ``--force``, and capability
    version selection remain the caller's responsibility (Plan 05).
    """

    if isinstance(capability, str):
        capability_value: Any = {"id": capability}
    else:
        capability_value = capability
    document = {
        "inputs": list(inputs),
        "capability": capability_value,
        "params": {} if params is None else params,
        "config": {} if config is None else config,
        "model": model,
        "prompt": prompt,
    }
    return sha256_hex(canonical_json(document))

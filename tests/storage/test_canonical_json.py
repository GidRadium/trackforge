"""Unit tests for canonical JSON and hashing helpers."""

from __future__ import annotations

import math

import pytest

from trackforge.application.services.canonical_json import (
    cache_key_for,
    canonical_json,
    sha256_hex,
)

pytestmark = pytest.mark.unit


def test_canonical_json_sorts_keys_and_removes_whitespace() -> None:
    assert canonical_json({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_canonical_json_is_stable_for_nested_values() -> None:
    value = {"z": [3, {"b": False, "a": None}], "a": {"y": 2, "x": 1}}
    expected = b'{"a":{"x":1,"y":2},"z":[3,{"a":null,"b":false}]}'

    assert canonical_json(value) == expected
    assert canonical_json(value) == canonical_json(value)


def test_canonical_json_keeps_unicode_as_utf8() -> None:
    encoded = canonical_json(
        {"artist": "\u041c\u0443\u043c\u0438\u0439 \u0422\u0440\u043e\u043b\u043b\u044c"}
    )

    expected = '{"artist":"Мумий Тролль"}'.encode()
    assert encoded == expected
    assert b"\\u" not in encoded


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_canonical_json_rejects_non_finite_numbers(value: float) -> None:
    with pytest.raises(ValueError):
        canonical_json({"value": value})


def test_canonical_json_rejects_non_string_keys() -> None:
    with pytest.raises(TypeError):
        canonical_json({"a": 1, 2: "b"})


def test_canonical_json_rejects_non_json_values() -> None:
    with pytest.raises(TypeError):
        canonical_json({"value": object()})


def test_canonical_json_rejects_circular_values() -> None:
    value: list[object] = []
    value.append(value)

    with pytest.raises(ValueError):
        canonical_json(value)


def test_sha256_hex_known_vectors() -> None:
    assert sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert sha256_hex(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_cache_key_is_deterministic_and_order_independent() -> None:
    first = cache_key_for(
        inputs=["a" * 64],
        capability={"id": "probe", "version": "1"},
        params={"b": 2, "a": 1},
        config={"ffprobe": "6.1"},
        model=None,
        prompt=None,
    )
    second = cache_key_for(
        inputs=["a" * 64],
        capability={"version": "1", "id": "probe"},
        params={"a": 1, "b": 2},
        config={"ffprobe": "6.1"},
        model=None,
        prompt=None,
    )

    assert first == second
    assert len(first) == 64
    assert first == first.lower()


def test_cache_key_changes_with_inputs() -> None:
    base = cache_key_for(["a" * 64], {"id": "probe", "version": "1"})
    changed = cache_key_for(["b" * 64], {"id": "probe", "version": "1"})

    assert base != changed

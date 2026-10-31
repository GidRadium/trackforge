"""Build D11 derivative credits: remixer main, original artist featured.

The channel that transformed a track is the main artist for derivative
credits, while the original performer is retained as a featured artist and in
``originalartist``/``comment``.  The title stays clean; the transformation
qualifier lives in its own field so it can be appended once by the filename
template.  This module is pure: callers turn the result into claims.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from trackforge.application.services.source_identity import NormalizedSourceIdentity
from trackforge.domain.model.claims import Claim
from trackforge.domain.model.identity import Transformation
from trackforge.domain.policies.normalization import (
    comparison_key,
    display_qualifier,
    normalize_artist_name,
    normalize_text,
    rank_transformations,
    strip_qualifier,
)

_CREDIT_TRANSFORMATIONS = frozenset(
    {
        Transformation.NIGHTCORE,
        Transformation.SPED_UP,
        Transformation.SLOWED,
        Transformation.REMIX,
    }
)
_ARTIST_SOURCE_PRIORITY = {
    "llm": 0,
    "musicbrainz": 1,
    "source_title_parser": 2,
    "source_metadata": 3,
    "initial_tags": 4,
    "path": 5,
}


@dataclass(frozen=True, slots=True)
class DerivativeCredits:
    """Final credit interpretation for one derivative item."""

    remixer: str | None
    original_artist: str | None
    title: str | None
    qualifier: str | None
    comment: str | None
    confidence: float
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("remixer", "original_artist", "title", "qualifier", "comment"):
            value = getattr(self, name)
            if value is not None:
                text = text_value(value)
                object.__setattr__(self, name, text or None)
        object.__setattr__(
            self,
            "confidence",
            max(0.0, min(1.0, float(self.confidence))),
        )
        object.__setattr__(self, "warnings", tuple(dict.fromkeys(self.warnings)))


def build_derivative_credits(
    transformations: Iterable[Transformation | str | Claim] | Transformation | str | Claim,
    uploader: str | None,
    normalized_identity: NormalizedSourceIdentity,
    claims: Sequence[Claim],
    *,
    recording_artist: str | None = None,
    recording_title: str | None = None,
) -> DerivativeCredits:
    """Return credits for nightcore/sped-up/slowed/remix transformations."""

    ranked = _ranked_transformations(transformations)
    if not ranked:
        return DerivativeCredits(
            remixer=None,
            original_artist=None,
            title=normalized_identity.title,
            qualifier=None,
            comment=None,
            confidence=0.0,
        )
    qualifier = display_qualifier(ranked[0])
    warnings: list[str] = []
    remixer = _clean_artist(uploader)
    if remixer is None:
        warnings.append("derivative_remixer_unknown")

    original, same_artist = _choose_original(
        normalized_identity,
        claims,
        remixer=remixer,
        recording_artist=recording_artist,
    )
    if original is None:
        warnings.append(
            "derivative_credit_same_artist" if same_artist else "original_artist_unknown"
        )

    title = normalized_identity.title or _clean_title(recording_title)
    if title is None:
        title = _claim_title(claims)
    if title is None:
        warnings.append("derivative_title_unknown")

    comment: str | None
    if remixer and original:
        comment = f"{qualifier} by {remixer}; original by {original}"
    elif remixer:
        comment = f"{qualifier} by {remixer}; original artist unknown"
    elif original:
        comment = f"{qualifier}; original by {original}"
    else:
        comment = qualifier

    if remixer and original:
        confidence = 0.82
    elif remixer or original:
        confidence = 0.58
    else:
        confidence = 0.3
    if not title:
        confidence = min(confidence, 0.4)
    return DerivativeCredits(
        remixer=remixer,
        original_artist=original,
        title=title,
        qualifier=qualifier,
        comment=comment,
        confidence=confidence,
        warnings=tuple(warnings),
    )


def _ranked_transformations(
    values: Iterable[Transformation | str | Claim] | Transformation | str | Claim,
) -> tuple[Transformation, ...]:
    if isinstance(values, (Transformation, str, Claim)):
        items: tuple[object, ...] = (values,)
    else:
        try:
            items = tuple(values)
        except TypeError:
            return ()
    found: list[Transformation] = []
    for item in items:
        raw_values: list[object]
        if isinstance(item, Claim):
            raw_values = list(item.value) if isinstance(item.value, list) else [item.value]
        else:
            raw_values = [item]
        for raw in raw_values:
            if isinstance(raw, Transformation):
                found.append(raw)
            elif isinstance(raw, str):
                try:
                    found.append(Transformation(raw.strip().casefold()))
                except ValueError:
                    continue
    return tuple(item for item in rank_transformations(found) if item in _CREDIT_TRANSFORMATIONS)


def _choose_original(
    identity: NormalizedSourceIdentity,
    claims: Sequence[Claim],
    *,
    remixer: str | None,
    recording_artist: str | None,
) -> tuple[str | None, bool]:
    candidates: list[tuple[int, str]] = []
    if identity.artist:
        candidates.append((2, identity.artist))
    recording = _clean_artist(recording_artist)
    if recording:
        candidates.append((1, recording))
    for claim in claims:
        if claim.field not in {"artist", "artists"}:
            continue
        if claim.role not in {None, "main", "featured"}:
            continue
        values = claim.value if isinstance(claim.value, list) else [claim.value]
        for value in values:
            artist = _clean_artist(value)
            if artist is None:
                continue
            candidates.append((_ARTIST_SOURCE_PRIORITY.get(claim.source, 8), artist))
    candidates.sort(key=lambda item: item[0])
    saw_same = False
    for _, candidate in candidates:
        if remixer is not None and comparison_key(candidate) == comparison_key(remixer):
            saw_same = True
            continue
        return candidate, False
    return None, saw_same


def _claim_title(claims: Sequence[Claim]) -> str | None:
    for claim in reversed(claims):
        if claim.field != "title" or claim.source == "source_metadata":
            continue
        if isinstance(claim.value, str):
            title = _clean_title(claim.value)
            if title:
                return title
    return None


def _clean_artist(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = normalize_text(value)
    if not text:
        return None
    cleaned = normalize_artist_name(text)
    return cleaned or None


def _clean_title(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = normalize_text(value)
    if not text:
        return None
    # Remove a trailing derivative qualifier without changing the base title.
    for transformation in _CREDIT_TRANSFORMATIONS:
        text = strip_qualifier(text, transformation)
    return text or None


def text_value(value: object) -> str:
    """Small public helper used by the dataclass validation."""

    return normalize_text(str(value))


__all__ = ["DerivativeCredits", "build_derivative_credits"]

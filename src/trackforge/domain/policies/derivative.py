"""Evidence-backed derivative rules.

The detector is deliberately conservative: title markers, duration/tempo
ratios, vocal/spectral features, MusicBrainz relationships and optional LLM
claims are all *evidence*.  A hypothesis is emitted only when at least one
transformation crosses the evidence threshold, and contradictory speed or
vocal evidence yields ``ambiguous`` instead of an arbitrary winner.

This module is pure domain code.  It never persists claims, downloads models,
or decides what the final tags should be.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model.identity import Transformation
from trackforge.domain.policies.normalization import (
    detect_transformations,
    rank_transformations,
)

MIN_TRANSFORMATION_SCORE = 0.30
ACCELERATION_MIN_RATIO = 1.15
ACCELERATION_MAX_RATIO = 1.60
SLOWED_MIN_RATIO = 0.60
SLOWED_MAX_RATIO = 0.90
LOW_VOCAL_RATIO = 0.12
ACAPELLA_VOCAL_RATIO = 0.55
ACAPELLA_DRUM_RATIO = 0.12
ACAPELLA_BASS_RATIO = 0.15
ACAPELLA_OTHER_RATIO = 0.20
SPECTRAL_BRIGHT_RATIO = 1.12
SPECTRAL_DARK_RATIO = 0.89

_ACCELERATION = frozenset({Transformation.NIGHTCORE, Transformation.SPED_UP})
_DECELERATION = frozenset({Transformation.SLOWED})
_VOCAL_CONFLICTS = frozenset({Transformation.INSTRUMENTAL, Transformation.ACAPELLA})


class DerivativeSource(StrEnum):
    """Where one piece of derivative evidence came from."""

    METADATA = "metadata"
    TEMPO = "tempo"
    DURATION = "duration"
    VOCAL = "vocal"
    SPECTRAL = "spectral"
    MUSICBRAINZ = "musicbrainz"
    LLM = "llm"


class DerivativeStatus(StrEnum):
    """Lifecycle of one derivative hypothesis."""

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class TextEvidence:
    """One text field that may carry a derivative marker."""

    text: str
    origin: str = "title"
    confidence: float = 0.5
    artifact_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise DomainValidationError("text evidence must be a non-empty string")
        if not isinstance(self.origin, str) or not self.origin.strip():
            raise DomainValidationError("text evidence origin must be a non-empty string")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise DomainValidationError("text evidence confidence must be between 0 and 1")
        if self.artifact_ref is not None and not str(self.artifact_ref).strip():
            raise DomainValidationError("text evidence artifact_ref must not be empty")


@dataclass(frozen=True, slots=True)
class DerivativeEvidence:
    """One normalized evidence item attached to a hypothesis."""

    source: DerivativeSource
    value: str | float
    confidence: float
    artifact_ref: str | None = None

    def __post_init__(self) -> None:
        try:
            source = (
                self.source
                if isinstance(self.source, DerivativeSource)
                else DerivativeSource(str(self.source))
            )
        except ValueError as exc:
            raise DomainValidationError(
                "unknown derivative evidence source",
                context={"value": str(self.source)},
            ) from exc
        object.__setattr__(self, "source", source)
        value = self.value
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise DomainValidationError("derivative evidence value must be a string or number")
        if isinstance(value, float) and not math.isfinite(value):
            raise DomainValidationError("derivative evidence value must be finite")
        if isinstance(value, str) and not value.strip():
            raise DomainValidationError("derivative evidence value must not be empty")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise DomainValidationError("derivative evidence confidence must be between 0 and 1")
        if self.artifact_ref is not None and not str(self.artifact_ref).strip():
            raise DomainValidationError("derivative evidence artifact_ref must not be empty")


@dataclass(frozen=True, slots=True)
class RelationshipEvidence:
    """A MusicBrainz relationship that may establish a derivative or status."""

    type: str
    official: bool | None = None
    target_title: str | None = None
    artifact_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or not self.type.strip():
            raise DomainValidationError("relationship type must be a non-empty string")
        if self.official is not None and not isinstance(self.official, bool):
            raise DomainValidationError("relationship official must be a bool or None")
        if self.target_title is not None and not isinstance(self.target_title, str):
            raise DomainValidationError("relationship target_title must be a string or None")
        if self.artifact_ref is not None and not str(self.artifact_ref).strip():
            raise DomainValidationError("relationship artifact_ref must not be empty")


@dataclass(frozen=True, slots=True)
class DerivativeParameters:
    """Estimated transformation parameters; unknown values stay ``None``."""

    speed_factor: float | None = None
    pitch_semitones: float | None = None
    bpm_ratio: float | None = None
    reverb_amount: float | None = None

    def __post_init__(self) -> None:
        for name in ("speed_factor", "bpm_ratio"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or value <= 0.0):
                raise DomainValidationError(
                    f"{name} must be finite and positive",
                    context={"field": name},
                )
        if self.pitch_semitones is not None and not math.isfinite(self.pitch_semitones):
            raise DomainValidationError("pitch_semitones must be finite")
        if self.reverb_amount is not None and not 0.0 <= self.reverb_amount <= 1.0:
            raise DomainValidationError("reverb_amount must be between 0 and 1")

    @property
    def is_empty(self) -> bool:
        return (
            self.speed_factor is None
            and self.pitch_semitones is None
            and self.bpm_ratio is None
            and self.reverb_amount is None
        )

    def as_dict(self) -> dict[str, float]:
        return {
            name: float(value)
            for name in ("speed_factor", "pitch_semitones", "bpm_ratio", "reverb_amount")
            if (value := getattr(self, name)) is not None
        }


@dataclass(frozen=True, slots=True)
class DerivativeHypothesis:
    """One evidence-backed interpretation of a possible derivative."""

    transformations: tuple[Transformation, ...]
    score: float
    status: DerivativeStatus = DerivativeStatus.PROPOSED
    parameters: DerivativeParameters = field(default_factory=DerivativeParameters)
    derivative_title: str | None = None
    derivative_artist: str | None = None
    is_official: bool | None = None
    evidence: tuple[DerivativeEvidence, ...] = ()

    def __post_init__(self) -> None:
        try:
            transformations = tuple(
                item if isinstance(item, Transformation) else Transformation(str(item))
                for item in self.transformations
            )
        except ValueError as exc:
            raise DomainValidationError(
                "unknown derivative transformation",
                context={"value": str(self.transformations)},
            ) from exc
        if not transformations:
            raise DomainValidationError("a derivative hypothesis needs a transformation")
        if len(transformations) != len(set(transformations)):
            raise DomainValidationError("derivative transformations must be unique")
        if not 0.0 <= float(self.score) <= 1.0:
            raise DomainValidationError("derivative hypothesis score must be between 0 and 1")
        try:
            status = (
                self.status
                if isinstance(self.status, DerivativeStatus)
                else DerivativeStatus(str(self.status))
            )
        except ValueError as exc:
            raise DomainValidationError(
                "unknown derivative status",
                context={"value": str(self.status)},
            ) from exc
        if not isinstance(self.parameters, DerivativeParameters):
            raise DomainValidationError("parameters must be DerivativeParameters")
        if self.derivative_title is not None and not self.derivative_title.strip():
            raise DomainValidationError("derivative_title must not be empty")
        if self.derivative_artist is not None and not self.derivative_artist.strip():
            raise DomainValidationError("derivative_artist must not be empty")
        if self.is_official is not None and not isinstance(self.is_official, bool):
            raise DomainValidationError("is_official must be a bool or None")
        evidence = tuple(self.evidence)
        for item in evidence:
            if not isinstance(item, DerivativeEvidence):
                raise DomainValidationError("evidence entries must be DerivativeEvidence")
        object.__setattr__(self, "transformations", transformations)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "evidence", evidence)

    @property
    def primary(self) -> Transformation:
        return rank_transformations(self.transformations)[0]

    @property
    def is_ambiguous(self) -> bool:
        return self.status is DerivativeStatus.AMBIGUOUS


def evaluate_derivative(
    *,
    texts: Sequence[TextEvidence] = (),
    measured_duration: float | None = None,
    original_duration: float | None = None,
    measured_bpm: float | None = None,
    original_bpm: float | None = None,
    vocal_energy: float | None = None,
    drum_energy: float | None = None,
    bass_energy: float | None = None,
    other_energy: float | None = None,
    spectral_centroid: float | None = None,
    reference_spectral_centroid: float | None = None,
    relationships: Sequence[RelationshipEvidence] = (),
    llm_transformations: Sequence[tuple[Transformation | str, float]] = (),
    artifact_refs: Mapping[DerivativeSource | str, str | None] | None = None,
    derivative_title: str | None = None,
    derivative_artist: str | None = None,
) -> DerivativeHypothesis | None:
    """Combine independent evidence into one hypothesis, or return ``None``.

    ``None`` means “not enough evidence”, not “this is an original”.  The
    caller must not turn a missing hypothesis into a negative claim.
    """

    refs = _normalize_refs(artifact_refs)
    evidence: dict[Transformation, list[DerivativeEvidence]] = {}
    for text in texts:
        for transformation in detect_transformations(text.text):
            _add(
                evidence,
                transformation,
                DerivativeEvidence(
                    source=DerivativeSource.METADATA,
                    value=transformation.value,
                    confidence=text.confidence,
                    artifact_ref=text.artifact_ref,
                ),
            )

    duration_speed = _duration_rule(
        measured_duration,
        original_duration,
        artifact_ref=refs.get(DerivativeSource.DURATION),
        evidence=evidence,
    )
    bpm_ratio = _tempo_rule(
        measured_bpm,
        original_bpm,
        artifact_ref=refs.get(DerivativeSource.TEMPO),
        evidence=evidence,
    )
    _vocal_rule(
        vocal_energy,
        drum_energy=drum_energy,
        bass_energy=bass_energy,
        other_energy=other_energy,
        artifact_ref=refs.get(DerivativeSource.VOCAL),
        evidence=evidence,
    )
    _spectral_rule(
        spectral_centroid,
        reference_spectral_centroid,
        artifact_ref=refs.get(DerivativeSource.SPECTRAL),
        evidence=evidence,
    )
    official = _relationship_rule(
        relationships,
        artifact_ref=refs.get(DerivativeSource.MUSICBRAINZ),
        evidence=evidence,
    )
    _llm_rule(
        llm_transformations,
        artifact_ref=refs.get(DerivativeSource.LLM),
        evidence=evidence,
    )

    scores = {
        transformation: _noisy_or(item.confidence for item in items)
        for transformation, items in evidence.items()
    }
    supported = {
        transformation: score
        for transformation, score in scores.items()
        if score >= MIN_TRANSFORMATION_SCORE
    }
    if not supported:
        return None

    # Nightcore is a speed-up with a pitch shift in the common case.  Record
    # the implied speed-up explicitly instead of silently folding one
    # transformation into another.
    if Transformation.NIGHTCORE in supported and Transformation.SPED_UP not in supported:
        implied = min(0.75, supported[Transformation.NIGHTCORE] * 0.7)
        _add(
            evidence,
            Transformation.SPED_UP,
            DerivativeEvidence(
                source=DerivativeSource.METADATA,
                value="nightcore implies sped_up",
                confidence=implied,
                artifact_ref=refs.get(DerivativeSource.METADATA),
            ),
        )
        supported[Transformation.SPED_UP] = _noisy_or(
            item.confidence for item in evidence[Transformation.SPED_UP]
        )

    acceleration = any(item in supported for item in _ACCELERATION)
    deceleration = any(item in supported for item in _DECELERATION)
    vocal_conflict = _VOCAL_CONFLICTS.issubset(supported)
    ambiguous = (acceleration and deceleration) or vocal_conflict

    speed_factor: float | None = None
    direction = _direction(supported)
    if not ambiguous and direction in {"acceleration", "deceleration"}:
        if duration_speed is not None and _speed_direction(duration_speed) == direction:
            speed_factor = duration_speed
        elif bpm_ratio is not None and _speed_direction(bpm_ratio) == direction:
            speed_factor = bpm_ratio

    pitch_semitones: float | None = None
    if Transformation.NIGHTCORE in supported and speed_factor is not None:
        pitch_semitones = 12.0 * math.log2(speed_factor)
        _add(
            evidence,
            Transformation.NIGHTCORE,
            DerivativeEvidence(
                source=DerivativeSource.TEMPO,
                value=pitch_semitones,
                confidence=0.40,
                artifact_ref=refs.get(DerivativeSource.TEMPO),
            ),
        )

    parameters = DerivativeParameters(
        speed_factor=speed_factor,
        pitch_semitones=pitch_semitones,
        bpm_ratio=bpm_ratio,
        reverb_amount=None,
    )

    transformations = rank_transformations(tuple(supported))
    score = min(0.95, max(supported.values()))
    if ambiguous:
        score = min(0.50, score)
        status = DerivativeStatus.AMBIGUOUS
    else:
        status = DerivativeStatus.PROPOSED

    if official is None and Transformation.BOOTLEG in supported:
        official = False

    all_evidence = _dedupe_evidence(item for items in evidence.values() for item in items)
    return DerivativeHypothesis(
        transformations=transformations,
        score=score,
        status=status,
        parameters=parameters,
        derivative_title=_optional_text(derivative_title),
        derivative_artist=_optional_text(derivative_artist),
        is_official=official,
        evidence=all_evidence,
    )


def _duration_rule(
    measured: float | None,
    original: float | None,
    *,
    artifact_ref: str | None,
    evidence: dict[Transformation, list[DerivativeEvidence]],
) -> float | None:
    if measured is None or original is None or measured <= 0.0 or original <= 0.0:
        return None
    speed = original / measured
    if ACCELERATION_MIN_RATIO <= speed <= ACCELERATION_MAX_RATIO:
        confidence = _ramp(speed, ACCELERATION_MIN_RATIO, ACCELERATION_MAX_RATIO, 0.45, 0.75)
        _add(
            evidence,
            Transformation.SPED_UP,
            DerivativeEvidence(DerivativeSource.DURATION, speed, confidence, artifact_ref),
        )
        return speed
    if SLOWED_MIN_RATIO <= speed <= SLOWED_MAX_RATIO:
        confidence = _ramp(
            SLOWED_MAX_RATIO - speed,
            0.0,
            SLOWED_MAX_RATIO - SLOWED_MIN_RATIO,
            0.45,
            0.75,
        )
        _add(
            evidence,
            Transformation.SLOWED,
            DerivativeEvidence(DerivativeSource.DURATION, speed, confidence, artifact_ref),
        )
        return speed
    return None


def _tempo_rule(
    measured: float | None,
    original: float | None,
    *,
    artifact_ref: str | None,
    evidence: dict[Transformation, list[DerivativeEvidence]],
) -> float | None:
    if measured is None or original is None or measured <= 0.0 or original <= 0.0:
        return None
    ratio = measured / original
    if 1.18 <= ratio <= 1.40:
        confidence = _ramp(ratio, 1.18, 1.40, 0.50, 0.75)
        _add(
            evidence,
            Transformation.SPED_UP,
            DerivativeEvidence(DerivativeSource.TEMPO, ratio, confidence, artifact_ref),
        )
        return ratio
    if 0.70 <= ratio <= 0.93:
        confidence = _ramp(0.93 - ratio, 0.0, 0.23, 0.50, 0.75)
        _add(
            evidence,
            Transformation.SLOWED,
            DerivativeEvidence(DerivativeSource.TEMPO, ratio, confidence, artifact_ref),
        )
        return ratio
    return None


def _vocal_rule(
    vocal_energy: float | None,
    *,
    drum_energy: float | None,
    bass_energy: float | None,
    other_energy: float | None,
    artifact_ref: str | None,
    evidence: dict[Transformation, list[DerivativeEvidence]],
) -> None:
    if vocal_energy is None:
        return
    if not 0.0 <= vocal_energy <= 1.0:
        raise DomainValidationError("vocal_energy must be between 0 and 1")
    if vocal_energy <= LOW_VOCAL_RATIO:
        confidence = _ramp(
            LOW_VOCAL_RATIO - vocal_energy,
            0.0,
            LOW_VOCAL_RATIO,
            0.60,
            0.85,
        )
        _add(
            evidence,
            Transformation.INSTRUMENTAL,
            DerivativeEvidence(DerivativeSource.VOCAL, vocal_energy, confidence, artifact_ref),
        )
    if (
        vocal_energy >= ACAPELLA_VOCAL_RATIO
        and drum_energy is not None
        and drum_energy <= ACAPELLA_DRUM_RATIO
        and bass_energy is not None
        and bass_energy <= ACAPELLA_BASS_RATIO
        and other_energy is not None
        and other_energy <= ACAPELLA_OTHER_RATIO
    ):
        _add(
            evidence,
            Transformation.ACAPELLA,
            DerivativeEvidence(DerivativeSource.VOCAL, vocal_energy, 0.70, artifact_ref),
        )


def _spectral_rule(
    centroid: float | None,
    reference: float | None,
    *,
    artifact_ref: str | None,
    evidence: dict[Transformation, list[DerivativeEvidence]],
) -> None:
    if centroid is None or reference is None or centroid <= 0.0 or reference <= 0.0:
        return
    ratio = centroid / reference
    if ratio >= SPECTRAL_BRIGHT_RATIO:
        confidence = _ramp(ratio, SPECTRAL_BRIGHT_RATIO, 1.6, 0.25, 0.45)
        _add(
            evidence,
            Transformation.SPED_UP,
            DerivativeEvidence(DerivativeSource.SPECTRAL, ratio, confidence, artifact_ref),
        )
    elif ratio <= SPECTRAL_DARK_RATIO:
        confidence = _ramp(
            SPECTRAL_DARK_RATIO - ratio,
            0.0,
            SPECTRAL_DARK_RATIO - 0.65,
            0.25,
            0.45,
        )
        _add(
            evidence,
            Transformation.SLOWED,
            DerivativeEvidence(DerivativeSource.SPECTRAL, ratio, confidence, artifact_ref),
        )


def _relationship_rule(
    relationships: Sequence[RelationshipEvidence],
    *,
    artifact_ref: str | None,
    evidence: dict[Transformation, list[DerivativeEvidence]],
) -> bool | None:
    if not relationships:
        return None
    official_values: list[bool] = []
    for relationship in relationships:
        transformation = _relationship_transformation(relationship.type)
        if transformation is not None:
            confidence = 0.85 if relationship.official else 0.65
            _add(
                evidence,
                transformation,
                DerivativeEvidence(
                    DerivativeSource.MUSICBRAINZ,
                    relationship.type.strip().casefold(),
                    confidence,
                    relationship.artifact_ref or artifact_ref,
                ),
            )
        if relationship.official is not None:
            official_values.append(relationship.official)
    if not official_values:
        return None
    if any(official_values):
        return True
    return False


def _llm_rule(
    transformations: Sequence[tuple[Transformation | str, float]],
    *,
    artifact_ref: str | None,
    evidence: dict[Transformation, list[DerivativeEvidence]],
) -> None:
    for raw, confidence in transformations:
        try:
            transformation = raw if isinstance(raw, Transformation) else Transformation(str(raw))
        except ValueError:
            continue
        bounded = min(0.55, max(0.0, float(confidence)))
        if bounded < MIN_TRANSFORMATION_SCORE:
            continue
        _add(
            evidence,
            transformation,
            DerivativeEvidence(
                DerivativeSource.LLM,
                transformation.value,
                bounded,
                artifact_ref,
            ),
        )


def _relationship_transformation(value: str) -> Transformation | None:
    normalized = value.strip().casefold().replace("-", " ").replace("_", " ")
    if "bootleg" in normalized:
        return Transformation.BOOTLEG
    if "remix" in normalized:
        return Transformation.REMIX
    if "cover" in normalized:
        return Transformation.COVER
    if "karaoke" in normalized:
        return Transformation.KARAOKE
    if "instrumental" in normalized:
        return Transformation.INSTRUMENTAL
    if "acapella" in normalized or "a cappella" in normalized:
        return Transformation.ACAPELLA
    if "mashup" in normalized or "mash up" in normalized or "medley" in normalized:
        return Transformation.MASHUP
    if "live" in normalized:
        return Transformation.LIVE
    if "acoustic" in normalized:
        return Transformation.ACOUSTIC
    if "demo" in normalized:
        return Transformation.DEMO
    if "radio edit" in normalized:
        return Transformation.RADIO_EDIT
    if "extended" in normalized:
        return Transformation.EXTENDED
    return None


def _direction(supported: Mapping[Transformation, float]) -> str | None:
    accel = any(item in supported for item in _ACCELERATION)
    decel = any(item in supported for item in _DECELERATION)
    if accel and decel:
        return "conflict"
    if accel:
        return "acceleration"
    if decel:
        return "deceleration"
    return None


def _speed_direction(speed: float) -> str:
    if speed > 1.0:
        return "acceleration"
    if speed < 1.0:
        return "deceleration"
    return "neutral"


def _noisy_or(confidences: Iterable[float]) -> float:
    remaining = 1.0
    for confidence in confidences:
        remaining *= 1.0 - float(confidence)
    return min(0.95, 1.0 - remaining)


def _ramp(
    value: float,
    low: float,
    high: float,
    minimum: float,
    maximum: float,
) -> float:
    if high <= low:
        return maximum
    fraction = (value - low) / (high - low)
    return minimum + max(0.0, min(1.0, fraction)) * (maximum - minimum)


def _add(
    evidence: dict[Transformation, list[DerivativeEvidence]],
    transformation: Transformation,
    item: DerivativeEvidence,
) -> None:
    entries = evidence.setdefault(transformation, [])
    key = (item.source, item.value, item.artifact_ref)
    if all((entry.source, entry.value, entry.artifact_ref) != key for entry in entries):
        entries.append(item)


def _dedupe_evidence(
    items: Iterable[DerivativeEvidence],
) -> tuple[DerivativeEvidence, ...]:
    result: list[DerivativeEvidence] = []
    seen: set[tuple[DerivativeSource, str | float, str | None]] = set()
    for item in items:
        key = (item.source, item.value, item.artifact_ref)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return tuple(result)


def _normalize_refs(
    refs: Mapping[DerivativeSource | str, str | None] | None,
) -> dict[DerivativeSource, str | None]:
    result: dict[DerivativeSource, str | None] = {}
    for key, value in (refs or {}).items():
        try:
            source = key if isinstance(key, DerivativeSource) else DerivativeSource(str(key))
        except ValueError:
            continue
        if value is not None and not str(value).strip():
            continue
        result[source] = value
    return result


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "ACCELERATION_MAX_RATIO",
    "ACCELERATION_MIN_RATIO",
    "ACAPELLA_BASS_RATIO",
    "ACAPELLA_DRUM_RATIO",
    "ACAPELLA_OTHER_RATIO",
    "ACAPELLA_VOCAL_RATIO",
    "DerivativeEvidence",
    "DerivativeHypothesis",
    "DerivativeParameters",
    "DerivativeSource",
    "DerivativeStatus",
    "LOW_VOCAL_RATIO",
    "MIN_TRANSFORMATION_SCORE",
    "RelationshipEvidence",
    "SLOWED_MAX_RATIO",
    "SLOWED_MIN_RATIO",
    "SPECTRAL_BRIGHT_RATIO",
    "SPECTRAL_DARK_RATIO",
    "TextEvidence",
    "evaluate_derivative",
]

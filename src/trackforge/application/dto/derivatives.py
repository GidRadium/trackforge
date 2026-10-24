"""Schema for ``derivative.hypotheses.v1``.

The detector produces evidence-backed hypotheses; it never writes final tags
and never upgrades a guess into a derivative identity.  Claims are persisted
separately with provenance, while this artifact keeps the complete hypothesis
for explainability and later resolver/review steps.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trackforge.domain.model.identity import Transformation

DerivativeEvidenceSource = Literal[
    "metadata",
    "tempo",
    "duration",
    "vocal",
    "spectral",
    "musicbrainz",
    "llm",
]
DerivativeHypothesisStatus = Literal["proposed", "accepted", "rejected", "ambiguous"]
DerivativePayloadStatus = Literal["proposed", "ambiguous", "none"]


class DerivativeEvidencePayload(BaseModel):
    """One piece of evidence supporting or contradicting a hypothesis."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: DerivativeEvidenceSource
    value: str | float
    confidence: float = Field(ge=0.0, le=1.0)
    artifact_ref: str | None = None

    @field_validator("value")
    @classmethod
    def _value_is_scalar(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError("evidence value must be a string or a finite number")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("evidence value must be finite")
        if isinstance(value, str) and not value.strip():
            raise ValueError("evidence value must not be empty")
        return value


class DerivativeParametersPayload(BaseModel):
    """Estimated transformation parameters; every value stays optional."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    speed_factor: float | None = None
    pitch_semitones: float | None = None
    bpm_ratio: float | None = None
    reverb_amount: float | None = None

    @field_validator("speed_factor", "bpm_ratio")
    @classmethod
    def _positive_ratio(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError("ratio parameters must be finite and positive")
        return value

    @field_validator("pitch_semitones")
    @classmethod
    def _finite_pitch(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not math.isfinite(value):
            raise ValueError("pitch_semitones must be finite")
        return value

    @field_validator("reverb_amount")
    @classmethod
    def _reverb_range(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError("reverb_amount must be between 0 and 1")
        return value


class DerivativeHypothesisPayload(BaseModel):
    """One evidence-backed derivative hypothesis."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transformations: list[Transformation] = Field(min_length=1)
    parameters: DerivativeParametersPayload = Field(default_factory=DerivativeParametersPayload)
    derivative_title: str | None = Field(default=None, min_length=1)
    derivative_artist: str | None = Field(default=None, min_length=1)
    is_official: bool | None = None
    score: float = Field(ge=0.0, le=1.0)
    status: DerivativeHypothesisStatus = "proposed"
    evidence: list[DerivativeEvidencePayload] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_transformations(self) -> DerivativeHypothesisPayload:
        values = [item.value for item in self.transformations]
        if len(values) != len(set(values)):
            raise ValueError("transformations must be unique")
        return self


class DerivativeHypothesesPayload(BaseModel):
    """Schema for the ``derivative.hypotheses.v1`` artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    item_id: str = Field(min_length=1)
    status: DerivativePayloadStatus = "none"
    hypotheses: list[DerivativeHypothesisPayload] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


__all__ = [
    "DerivativeEvidencePayload",
    "DerivativeEvidenceSource",
    "DerivativeHypothesisPayload",
    "DerivativeHypothesisStatus",
    "DerivativeHypothesesPayload",
    "DerivativeParametersPayload",
    "DerivativePayloadStatus",
]

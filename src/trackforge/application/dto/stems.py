"""Schema for ``audio.stems.v1`` produced by the Demucs observer.

Stems are analysis evidence, not durable output: the payload keeps only
energy ratios and a small set of summary features.  Audio samples are never
part of an observation artifact and are not written to the library.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

_RATIO_TOLERANCE = 2e-3


class StemsModelInfo(BaseModel):
    """Identity and separation settings of the model that produced the stems."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    version: str = "unknown"
    backend: str = Field(default="demucs", min_length=1)
    segment: float = Field(default=7.8, gt=0.0)
    overlap: float = Field(default=0.25, ge=0.0, le=1.0)
    jobs: int = Field(default=1, ge=1)


class StemRatios(BaseModel):
    """Energy shares in ``[0, 1]`` relative to the sum of all four stems.

    ``instrumental`` is the sum of ``drums``, ``bass`` and ``other``.  For
    non-silent audio the four source shares sum to one, so ``vocals`` and
    ``instrumental`` also sum to one.  An all-zero payload is valid for
    silence and is reported with a warning by the observer.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    vocals: float = Field(ge=0.0, le=1.0)
    drums: float = Field(ge=0.0, le=1.0)
    bass: float = Field(ge=0.0, le=1.0)
    other: float = Field(ge=0.0, le=1.0)
    instrumental: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _consistent(self) -> StemRatios:
        total = self.vocals + self.drums + self.bass + self.other
        if total == 0.0:
            if self.instrumental != 0.0:
                raise ValueError("instrumental must be zero when every stem is silent")
            return self
        if abs(total - 1.0) > _RATIO_TOLERANCE:
            raise ValueError("stem ratios must sum to 1 for non-silent audio")
        if abs(self.instrumental - (self.drums + self.bass + self.other)) > _RATIO_TOLERANCE:
            raise ValueError("instrumental must equal drums + bass + other")
        if abs(self.vocals + self.instrumental - 1.0) > _RATIO_TOLERANCE:
            raise ValueError("vocals and instrumental must partition the stem energy")
        return self


class StemFeatures(BaseModel):
    """Optional low-cost features derived from the separated audio.

    ``spectral_centroid`` is in Hz.  ``rms`` is linear amplitude in ``[0, 1]``
    and ``vocal_energy`` is the vocal energy share in ``[0, 1]``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    vocal_energy: float | None = Field(default=None, ge=0.0, le=1.0)
    spectral_centroid: float | None = Field(default=None, ge=0.0)
    rms: float | None = Field(default=None, ge=0.0, le=1.0)


class StemsPayload(BaseModel):
    """Schema for the ``audio.stems.v1`` artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: StemsModelInfo
    duration_seconds: float = Field(ge=0.0)
    ratios: StemRatios
    features: StemFeatures = Field(default_factory=StemFeatures)
    warnings: list[str] = Field(default_factory=list)


__all__ = [
    "StemFeatures",
    "StemRatios",
    "StemsModelInfo",
    "StemsPayload",
]

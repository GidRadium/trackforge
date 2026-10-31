"""Pydantic request models for the local HTTP API.

Only transport concerns live here: field names, types, and mapping to the
existing application DTOs.  Business validation remains in the domain and use
cases, so HTTP and Python API callers see the same errors and rules.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trackforge.application.dto.results import RunOptions
from trackforge.application.ports.publishing import DEFAULT_NAMING_TEMPLATE, PublishOptions
from trackforge.config import TrackForgeConfig


class SourcePayload(BaseModel):
    """One source object in a request body.

    Extra keys are allowed so collection payloads can nest their inner source
    without duplicating every variant in the transport model.
    """

    model_config = ConfigDict(extra="allow")

    type: str | None = None
    kind: str | None = None
    path: str | None = None
    url: str | None = None
    site_hint: str | None = None
    text: str | None = None
    locale: str | None = None
    external_id: str | None = None
    source: Any | None = None


class PublishOptionsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    copy_mode: bool = Field(default=True, alias="copy")
    sidecar: bool = True
    lrc: bool = True
    collision: str = "suffix"
    subfolder: str | None = None

    def to_options(self) -> PublishOptions:
        return PublishOptions(
            copy=self.copy_mode,
            sidecar=self.sidecar,
            lrc=self.lrc,
            collision=self.collision,  # type: ignore[arg-type]
            subfolder=self.subfolder,
        )


class RunOptionsPayload(BaseModel):
    """Transport shape for ``RunOptions``; defaults come from config."""

    model_config = ConfigDict(extra="forbid")

    profile: str | None = None
    mode: str | None = None
    auto: bool | None = None
    review: bool = False
    force_steps: tuple[str, ...] | str = Field(default_factory=tuple)
    enabled: tuple[str, ...] | str = Field(default_factory=tuple)
    disabled: tuple[str, ...] | str = Field(default_factory=tuple)
    offline: bool | None = None
    refresh: bool = False
    dry_run: bool = False
    target_format: str | None = None
    format_id: str | None = None
    naming: str | None = None
    publish: PublishOptionsPayload | None = None

    def to_options(self, config: TrackForgeConfig) -> RunOptions:
        publish = self.publish.to_options() if self.publish is not None else PublishOptions()
        auto = (not self.review) if self.auto is None else self.auto
        return RunOptions(
            profile=self.profile or config.profile,
            mode=self.mode or config.mode,
            auto=auto,
            review=self.review,
            force_steps=self.force_steps,
            enabled=self.enabled,
            disabled=self.disabled,
            offline=config.offline if self.offline is None else self.offline,
            refresh=self.refresh,
            dry_run=self.dry_run,
            target_format=self.target_format,  # type: ignore[arg-type]
            format_id=self.format_id,
            naming=self.naming or DEFAULT_NAMING_TEMPLATE,
            publish=publish,
        )


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str | SourcePayload
    options: RunOptionsPayload | None = None


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str | SourcePayload | None = None
    plan: dict[str, Any] | None = None
    options: RunOptionsPayload | None = None

    @model_validator(mode="after")
    def _one_input(self) -> RunRequest:
        if (self.plan is None) == (self.source is None):
            raise ValueError("provide exactly one of plan or source")
        return self


class ReviewCorrectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    value: Any = None
    lock: bool = True
    scope: str = "item"
    reason_code: str | None = None
    reason_text: str | None = None


class ReviewRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    corrections: list[ReviewCorrectionPayload] | dict[str, Any] | None = None
    action: str = "show"
    field: str | None = None
    value: Any = None
    lock: bool = False
    reason: str | None = None
    reason_code: str | None = None
    reason_text: str | None = None
    scope: str = "item"
    mode: str | None = None
    auto: bool = False

    def correction_values(self) -> list[dict[str, Any]] | dict[str, Any] | None:
        if self.corrections is None:
            return None
        if isinstance(self.corrections, dict):
            return dict(self.corrections)
        return [correction.model_dump() for correction in self.corrections]


def options_for(
    payload: RunOptionsPayload | None,
    config: TrackForgeConfig,
) -> RunOptions:
    """Build run options using config defaults for omitted fields."""

    if payload is not None:
        return payload.to_options(config)
    return RunOptions(
        profile=config.profile,
        mode=config.mode,
        auto=True,
        offline=config.offline,
    )


__all__ = [
    "PlanRequest",
    "PublishOptionsPayload",
    "ReviewCorrectionPayload",
    "ReviewRequestPayload",
    "RunOptionsPayload",
    "RunRequest",
    "SourcePayload",
    "options_for",
]

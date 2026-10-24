"""Human and JSON presenters for the CLI.

The JSON shape mirrors the application DTOs and is always exactly one object
on stdout.  Error objects are written to stderr by the command wrappers.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

import typer

from trackforge.application.dto.collections import (
    CollectionPlanView,
    CollectionRunResult,
)
from trackforge.application.dto.decisions import ExplanationView
from trackforge.application.dto.plan import PlanView
from trackforge.application.dto.results import (
    DoctorReport,
    PublishResult,
    RenderView,
    RunResult,
    StatusView,
)
from trackforge.application.dto.views import ReviewView
from trackforge.domain.errors import TrackForgeError


def to_jsonable(value: Any) -> Any:
    """Recursively convert DTOs and domain primitives to JSON-safe values."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, (Path, UUID)):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(item) for item in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        document = {
            field.name: to_jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
        if isinstance(value, DoctorReport):
            document["status"] = to_jsonable(value.status)
            document["summary"] = value.summary
        elif isinstance(value, PlanView):
            document["blocked"] = value.blocked
        elif isinstance(value, CollectionPlanView):
            document["blocked"] = value.blocked
            document["tag_basis"] = to_jsonable(value.tag_basis)
            document["collection_id"] = to_jsonable(value.collection_id)
            document["selected_positions"] = [item.position for item in value.selected_items]
        elif isinstance(value, CollectionRunResult):
            document["succeeded"] = value.succeeded
        elif isinstance(value, RenderView):
            document["path"] = to_jsonable(value.audio_path)
            document["bundle"] = {
                "id": value.bundle_id,
                "hash": value.bundle_hash,
                "files": to_jsonable(value.files),
            }
        elif isinstance(value, PublishResult):
            document["path"] = value.path
            document["item_id"] = to_jsonable(value.item_id)
            document["bundle_id"] = value.bundle_id
            document["status"] = to_jsonable(value.status)
            document["published_at"] = to_jsonable(value.published_at)
        elif isinstance(value, ReviewView):
            document["decisions"] = to_jsonable(value.fields)
        return document
    if isinstance(value, TrackForgeError):
        return value.to_dict()
    return str(value)


def emit_json(value: Any) -> None:
    """Print one compact JSON object to stdout."""

    typer.echo(json.dumps(to_jsonable(value), ensure_ascii=False, allow_nan=False, sort_keys=True))


def emit_error(exc: TrackForgeError, *, as_json: bool) -> None:
    """Write a typed error to stderr in the requested format."""

    payload = exc.to_dict()
    if as_json:
        document = {"ok": False, **payload, "error": payload}
        typer.echo(
            json.dumps(to_jsonable(document), ensure_ascii=False, allow_nan=False, sort_keys=True),
            err=True,
        )
        return
    typer.echo(f"error: {exc.message}", err=True)
    if exc.user_action:
        typer.echo(f"action: {exc.user_action}", err=True)


def emit_internal_error(message: str, *, as_json: bool) -> None:
    payload = {
        "type": "InternalError",
        "message": message,
        "retryable": False,
        "user_action": None,
        "context": {},
    }
    if as_json:
        document = {"ok": False, **payload, "error": payload}
        typer.echo(
            json.dumps(document, ensure_ascii=False, allow_nan=False, sort_keys=True),
            err=True,
        )
    else:
        typer.echo(f"internal error: {message}", err=True)


def print_plan(plan: PlanView) -> None:
    typer.echo(f"Source: {_source_text(plan.source)}")
    if plan.resolver_id:
        typer.echo(f"Resolver: {plan.resolver_id}")
    if plan.candidates:
        typer.echo("Candidates:")
        for index, candidate in enumerate(plan.candidates, start=1):
            duration = (
                f" — {candidate.duration_seconds:.1f}s"
                if candidate.duration_seconds is not None
                else ""
            )
            formats = ", ".join(
                value.format_id or value.container or value.codec or "unknown"
                for value in candidate.formats
            )
            typer.echo(f"  {index}. {candidate.title}{duration} [{formats}]")
    if plan.steps:
        typer.echo("Steps:")
        for step in plan.steps:
            typer.echo(f"  - {step.node_id}: {step.description}")
    if plan.blocked_reason:
        typer.echo(f"Blocked: {plan.blocked_reason}", err=True)
    for warning in plan.warnings:
        typer.echo(f"warning: {warning}", err=True)
    if not plan.blocked_reason:
        typer.echo("Plan is ready.")


def print_collection_plan(plan: CollectionPlanView) -> None:
    collection = plan.collection
    typer.echo(f"Collection: {collection.title} ({collection.kind.value})")
    typer.echo(f"Id: {collection.id}")
    typer.echo(f"Tag basis: {collection.tag_basis.value}")
    if collection.owner:
        typer.echo(f"Owner: {collection.owner}")
    typer.echo(f"Items: {len(plan.selected_items)} selected of {len(collection.items)}")
    for item in plan.selected_items:
        typer.echo(f"  {item.position:>4}. {item.candidate.title}")
    typer.echo(
        f"Estimated cost: {plan.estimated_cost.network_calls} network call(s), "
        f"{plan.estimated_cost.cpu_seconds:.1f}s CPU"
    )
    for warning in plan.warnings:
        typer.echo(f"warning: {warning}", err=True)
    if plan.blocked_reason:
        typer.echo(f"Blocked: {plan.blocked_reason}", err=True)
    else:
        typer.echo("Plan is ready.")


def print_collection_run(result: CollectionRunResult) -> None:
    typer.echo(f"Collection {result.collection_id} — {result.status.value}")
    typer.echo(
        f"Processed: {result.processed}; failed: {result.failed}; "
        f"skipped: {result.skipped}; remaining: {result.remaining}"
    )
    for item in result.items:
        typer.echo(f"  {item.position:>4}. {item.status.value}")
        if item.output_path:
            typer.echo(f"       {item.output_path}")
        if item.error:
            typer.echo(f"       error: {item.error}", err=True)
    for warning in result.warnings:
        typer.echo(f"warning: {warning}", err=True)
    for error in result.errors:
        typer.echo(f"error: {error}", err=True)


def print_collection(collection: Any) -> None:
    typer.echo(f"Collection: {collection.title} ({collection.kind.value})")
    typer.echo(f"Id: {collection.id}")
    typer.echo(f"Tag basis: {collection.tag_basis.value}")
    if collection.owner:
        typer.echo(f"Owner: {collection.owner}")
    for item in collection.items:
        item_id = f" {item.item_id}" if item.item_id else ""
        typer.echo(f"  {item.position:>4}. {item.status.value:<9} {item.candidate.title}{item_id}")
        if item.error:
            typer.echo(f"       error: {item.error}", err=True)
    for warning in collection.warnings:
        typer.echo(f"warning: {warning}", err=True)


def print_run(result: RunResult) -> None:
    typer.echo(f"Run {result.run_id} — {result.status.value}")
    if result.cache_hit:
        typer.echo("Cache: reuse of existing artifacts/entries")
    for item in result.items:
        typer.echo(f"Item {item.item_id} — {item.status.value}")
        if item.output_path:
            typer.echo(f"Output: {item.output_path}")
        if item.cached_steps:
            typer.echo(f"Cached steps: {', '.join(item.cached_steps)}")
        for warning in item.warnings:
            typer.echo(f"warning: {warning}", err=True)
    for path in result.output_paths:
        if not any(item.output_path == path for item in result.items):
            typer.echo(f"Output: {path}")
    for warning in result.warnings:
        if not any(warning in item.warnings for item in result.items):
            typer.echo(f"warning: {warning}", err=True)
    for error in result.errors:
        typer.echo(f"error: {error}", err=True)


def print_publish(result: PublishResult) -> None:
    typer.echo(f"Published: {result.path}")
    typer.echo(f"Item: {result.item_id}")
    typer.echo(f"Bundle: {result.bundle_id}")
    if result.cache_hit:
        typer.echo("Cache: existing library entry reused")
    if result.sidecar_path:
        typer.echo(f"Sidecar: {result.sidecar_path}")
    if result.lrc_path:
        typer.echo(f"Lyrics: {result.lrc_path}")
    for warning in result.warnings:
        typer.echo(f"warning: {warning}", err=True)


def print_review(view: ReviewView) -> None:
    typer.echo(f"Item {view.item_id} — {view.status.value}")
    if view.decision_record_id:
        typer.echo(f"Decision record: {view.decision_record_id}")
    typer.echo(f"{'Field':<16} {'Current':<36} {'Confidence':>10}")
    for field in view.fields:
        lock = " [locked]" if field.locked else ""
        typer.echo(f"{field.field:<16} {_short(field.current):<36} {field.confidence:>10.2f}{lock}")
        for alternative in field.alternatives[:3]:
            typer.echo(
                f"{'':<16} alternative: {_short(alternative.value)} "
                f"({alternative.source}, {alternative.confidence:.2f})"
            )
    if view.locked_fields:
        typer.echo(f"Locked: {', '.join(view.locked_fields)}")
    if view.correction is not None:
        typer.echo(
            f"Correction: {view.correction.field} "
            f"{_short(view.correction.system_value)} → {_short(view.correction.user_value)} "
            f"({view.correction.reason_code.value})"
        )
    if view.session is not None:
        typer.echo(f"Review session: {view.session.id}")
    if view.requires_render:
        typer.echo("Next: run render/publish to apply the reviewed decisions.")
    if view.explanation is not None:
        print_explanation(view.explanation)
    for warning in view.warnings:
        typer.echo(f"warning: {warning}", err=True)


def print_explanation(view: ExplanationView) -> None:
    typer.echo(f"Field: {view.field}")
    typer.echo(f"Value: {_short(view.decision)}")
    typer.echo(f"Policy: {view.policy_id}@v{view.policy_version} ({view.mode.value})")
    typer.echo(f"Rationale: {view.rationale}")
    if view.chosen_claim is not None:
        typer.echo(
            f"Chosen claim: {view.chosen_claim.source}/{view.chosen_claim.method} "
            f"(confidence {view.chosen_claim.confidence:.2f})"
        )
    for alternative in view.alternatives:
        typer.echo(
            f"Alternative: {_short(alternative.value)} from {alternative.source} "
            f"— {alternative.reason}"
        )
    if view.locked:
        typer.echo("This decision is locked by the user.")
    for warning in view.warnings:
        typer.echo(f"warning: {warning}", err=True)


def print_doctor(report: DoctorReport) -> None:
    for check in report.checks:
        typer.echo(f"[{check.status.value}] {check.name}: {check.message}")
    typer.echo(f"Status: {report.status.value} ({report.summary})")


def print_status(view: StatusView) -> None:
    typer.echo(f"Run {view.run_id} — {view.status.value}")
    typer.echo(f"Profile: {view.profile}; mode: {view.mode}")
    for step in view.steps:
        typer.echo(f"  {step.capability_id:<12} {step.status.value} (attempts {step.attempts})")
        if step.error:
            typer.echo(f"    error: {step.error.get('message', step.error)}", err=True)
    for item_id, status in view.items:
        typer.echo(f"  item {item_id}: {status.value}")


def _source_text(source: Any) -> str:
    if isinstance(source, Path):
        return str(source)
    if dataclasses.is_dataclass(source):
        for field_name in ("path", "url", "text"):
            value = getattr(source, field_name, None)
            if isinstance(value, str):
                return value
        return type(source).__name__
    return str(source)


def _short(value: Any, limit: int = 34) -> str:
    text = "unknown" if value is None else str(value)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


__all__ = [
    "emit_error",
    "emit_internal_error",
    "emit_json",
    "print_collection",
    "print_collection_plan",
    "print_collection_run",
    "print_doctor",
    "print_explanation",
    "print_plan",
    "print_publish",
    "print_review",
    "print_run",
    "print_status",
    "to_jsonable",
]

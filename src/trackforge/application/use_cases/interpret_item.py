"""Turn observation artifacts into append-only claims and identity hypotheses.

Interpreters are pure, manifest-declared capabilities: they receive artifact
payloads and return a ``claims.v1`` (or identity) payload plus domain claims.
The use case owns persistence, de-duplication, item status, and warnings.
"""

from __future__ import annotations

import builtins
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ValidationError

from trackforge.application.dto.decisions import InterpretationView
from trackforge.application.dto.observations import (
    ArtworkInfo,
    AudioProbePayload,
    InitialTagsPayload,
    ObservationView,
    PathStructurePayload,
)
from trackforge.application.ports.observations import (
    INITIAL_TAGS_ARTIFACT,
    PATH_STRUCTURE_ARTIFACT,
    PROBE_ARTIFACT,
)
from trackforge.application.ports.repositories import UnitOfWork
from trackforge.application.ports.storage import ArtifactStore, StorageError
from trackforge.application.services.canonical_json import canonical_json, sha256_hex
from trackforge.domain.errors import (
    DomainValidationError,
    InvalidStateTransition,
    ObservationFailed,
    RecordNotFound,
)
from trackforge.domain.model._validation import FieldValue
from trackforge.domain.model.capabilities import (
    CapabilityKind,
    CapabilityManifest,
    Cost,
    Determinism,
    SideEffect,
)
from trackforge.domain.model.claims import Claim, Confidence, Subject
from trackforge.domain.model.identity import ContentKind, Transformation
from trackforge.domain.model.ids import ClaimId, ItemId, new_uuid7
from trackforge.domain.model.item import Item, ItemStatus
from trackforge.domain.policies.normalization import (
    detect_source_noise,
    detect_transformations,
    is_vevo,
    normalize_artist_name,
    parse_artists,
    rank_transformations,
    strip_topic_suffix,
)
from trackforge.domain.policies.validators import validate_field

CLAIMS_ARTIFACT = "claims.v1"
IDENTITY_ARTIFACT = "identity.hypotheses.v1"

INITIAL_TAGS_TYPE = str(INITIAL_TAGS_ARTIFACT)
PATH_STRUCTURE_TYPE = str(PATH_STRUCTURE_ARTIFACT)
PROBE_TYPE = str(PROBE_ARTIFACT)

SOURCE_INITIAL_TAGS = "initial_tags"
SOURCE_PATH = "path"
SOURCE_PROBE = "audio_probe"

METHOD_TAG_READER = "mutagen_tag_reader/v1"
METHOD_PATH = "path_structure/v1"
METHOD_KIND = "kind_classifier/v1"
METHOD_DERIVATIVE = "derivative_detector/v1"
METHOD_IDENTITY = "identity_resolver/v1"

_GENERIC_PARENTS = frozenset(
    {
        "music",
        "downloads",
        "download",
        "desktop",
        "documents",
        "tmp",
        "temp",
        "home",
        "users",
        "library",
        "audio",
        "songs",
        "media",
    }
)
_LEADING_TRACK_RE = re.compile(r"^\s*(\d{1,3})\s*(?:[-._)]\s*|\s+)(?P<rest>.+)$")
_ARTIST_TITLE_RE = re.compile(r"^(?P<artist>.+?)\s+[-–—]\s+(?P<title>.+)$")
_MBID_FIELD_ALIASES = {
    "recording": "mb_recording_id",
    "track": "mb_recording_id",
    "musicbrainz_trackid": "mb_recording_id",
    "mb_recording_id": "mb_recording_id",
    "release": "mb_release_id",
    "album": "mb_release_id",
    "mb_release_id": "mb_release_id",
    "releasegroup": "mb_release_group_id",
    "release_group": "mb_release_group_id",
    "mb_release_group_id": "mb_release_group_id",
    "artist": "mb_artist_id",
    "mb_artist_id": "mb_artist_id",
    "albumartist": "mb_albumartist_id",
    "album_artist": "mb_albumartist_id",
    "mb_albumartist_id": "mb_albumartist_id",
    "isrc": "isrc",
    "acoustid": "acoustid",
}


ModelT = TypeVar("ModelT", bound=BaseModel)


class MissingArtifactError(DomainValidationError):
    """Raised by an interpreter when a required artifact is absent."""


@dataclass(frozen=True, slots=True)
class ArtifactPayload:
    """One decoded-by-type artifact input for an interpreter."""

    type: str
    payload: bytes
    artifact_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or not self.type.strip():
            raise DomainValidationError("artifact type must be non-empty")
        if not isinstance(self.payload, (bytes, bytearray, memoryview)):
            raise DomainValidationError("artifact payload must be bytes")
        object.__setattr__(self, "type", self.type.strip())
        object.__setattr__(self, "payload", bytes(self.payload))
        if not isinstance(self.artifact_id, str) or not self.artifact_id.strip():
            raise DomainValidationError("artifact_id must be non-empty")

    def model(self, model_type: builtins.type[ModelT]) -> ModelT:
        try:
            return model_type.model_validate_json(self.payload)
        except ValidationError as exc:
            raise ObservationFailed(
                f"{self.type} payload does not match its schema",
                context={"artifact_id": self.artifact_id, "error": str(exc)},
            ) from exc


@dataclass(frozen=True, slots=True)
class InterpreterInput:
    """Artifacts and previously produced claims for one interpreter run."""

    item_id: ItemId
    artifacts: Mapping[str, ArtifactPayload]
    claims: tuple[Claim, ...] = ()

    def __post_init__(self) -> None:
        normalized = {str(key): value for key, value in self.artifacts.items()}
        object.__setattr__(self, "artifacts", normalized)
        object.__setattr__(self, "claims", tuple(self.claims))

    def require(self, artifact_type: str) -> ArtifactPayload:
        artifact = self.artifacts.get(artifact_type)
        if artifact is None:
            raise MissingArtifactError(
                "required artifact is missing",
                user_action="Run observation again or provide the missing artifact.",
                context={"artifact_type": artifact_type},
            )
        return artifact

    def optional(self, artifact_type: str) -> ArtifactPayload | None:
        return self.artifacts.get(artifact_type)


@dataclass(frozen=True, slots=True)
class InterpreterResult:
    """The artifact-shaped output of one interpreter."""

    artifact_type: str
    payload: dict[str, Any]
    claims: tuple[Claim, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.artifact_type not in {CLAIMS_ARTIFACT, IDENTITY_ARTIFACT}:
            raise DomainValidationError(
                "unsupported interpreter artifact type",
                context={"artifact_type": self.artifact_type},
            )
        canonical_json(self.payload)
        object.__setattr__(self, "claims", tuple(self.claims))
        object.__setattr__(self, "warnings", tuple(self.warnings))


@runtime_checkable
class Interpreter(Protocol):
    """A manifest-declared observation-to-claims capability."""

    @property
    def manifest(self) -> CapabilityManifest: ...

    def interpret(self, inputs: InterpreterInput) -> InterpreterResult: ...


@dataclass(frozen=True, slots=True)
class _PathMetadata:
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    track: int | None = None
    source: str | None = None


class InitialTagsInterpreter:
    """Create claims from existing tags and weak path structure evidence."""

    manifest = CapabilityManifest(
        id="initial_tags_interpreter",
        version="1",
        kind=CapabilityKind.INTERPRETER,
        requires=(INITIAL_TAGS_ARTIFACT, PATH_STRUCTURE_ARTIFACT),
        provides=(CLAIMS_ARTIFACT,),
        cost=Cost(cpu_seconds=0.01, memory_mb=8.0),
        determinism=Determinism.DETERMINISTIC,
        cacheable=True,
        side_effects=SideEffect.NONE,
        description="Turn initial tags and path structure into low-confidence claims.",
    )

    def interpret(self, inputs: InterpreterInput) -> InterpreterResult:
        tags_artifact = inputs.require(INITIAL_TAGS_TYPE)
        tags_payload = tags_artifact.model(InitialTagsPayload)
        path_artifact = inputs.optional(PATH_STRUCTURE_TYPE)
        claims: list[Claim] = []
        warnings: list[str] = []

        def add(
            field: str,
            value: FieldValue,
            confidence: float,
            *,
            source: str = SOURCE_INITIAL_TAGS,
            method: str = METHOD_TAG_READER,
            artifact: ArtifactPayload = tags_artifact,
            role: str | None = None,
            order: int | None = None,
            notes: str | None = None,
        ) -> None:
            if value is None:
                return
            claims.append(
                _make_claim(
                    inputs.item_id,
                    subject=Subject.ITEM,
                    field=field,
                    value=value,
                    confidence=confidence,
                    source=source,
                    method=method,
                    evidence=artifact.artifact_id,
                    role=role,
                    order=order,
                    notes=notes,
                )
            )

        tags = tags_payload.tags
        if tags.title is not None and tags.title.strip():
            title = tags.title.strip()
            add("title", title, 0.4)
            for marker in detect_source_noise(title):
                add(
                    "source_noise",
                    marker,
                    0.4,
                    notes=f"source-noise marker in initial title: {marker}",
                )

        artist_order = 0
        for raw_artist in tags.artist:
            credits = parse_artists(raw_artist)
            if not credits:
                if is_vevo(raw_artist):
                    add("source_noise", "vevo", 0.4, notes="VEVO is a source channel")
                continue
            if strip_topic_suffix(raw_artist) != raw_artist.strip():
                add(
                    "source_noise",
                    "topic_channel",
                    0.4,
                    notes="YouTube Topic suffix removed for comparison",
                )
            for credit in credits:
                add(
                    "artist",
                    credit.name,
                    0.35 if credit.role == "main" else 0.3,
                    role=credit.role,
                    order=artist_order,
                )
                artist_order += 1

        if tags.album is not None and tags.album.strip():
            add("album", tags.album.strip(), 0.3)
        albumartist_order = 0
        for raw_albumartist in tags.albumartist:
            for credit in parse_artists(raw_albumartist):
                add(
                    "albumartist",
                    credit.name,
                    0.3,
                    role="albumartist",
                    order=albumartist_order,
                )
                albumartist_order += 1
        if tags.date is not None and tags.date.strip():
            add("date", tags.date.strip(), 0.3)
        for genre in tags.genre:
            if genre.strip():
                add("genre", genre.strip(), 0.25)
        if tags.track is not None:
            add("track", tags.track, 0.3)
        if tags.track_total is not None:
            add("tracktotal", tags.track_total, 0.3)
        if tags.disc is not None:
            add("disc", tags.disc, 0.3)
        if tags.disc_total is not None:
            add("disctotal", tags.disc_total, 0.3)
        if tags.comment is not None and tags.comment.strip():
            add("comment", tags.comment.strip(), 0.25)
        if tags.lyrics is not None and tags.lyrics.strip():
            add("lyrics", tags.lyrics.strip(), 0.2)
        for artwork in tags.artwork:
            value = _artwork_value(artwork)
            if value is not None:
                add("artwork", value, 0.2, notes="embedded artwork metadata only")
        for raw_field, values in tags.mbids.items():
            canonical = _MBID_FIELD_ALIASES.get(raw_field.strip().lower())
            if canonical is None:
                continue
            for value in values:
                if validate_field(canonical, value).valid:
                    add(canonical, value, 0.3, notes="read from existing tag")
                else:
                    warnings.append(f"invalid_initial_tag:{canonical}")

        if path_artifact is not None:
            path_payload = path_artifact.model(PathStructurePayload)
            path_meta = _parse_path(path_payload)
            if path_meta.title:
                add(
                    "title",
                    path_meta.title,
                    0.2,
                    source=SOURCE_PATH,
                    method=METHOD_PATH,
                    artifact=path_artifact,
                    notes="weak path-derived title",
                )
                for marker in detect_source_noise(path_meta.title):
                    add(
                        "source_noise",
                        marker,
                        0.2,
                        source=SOURCE_PATH,
                        method=METHOD_PATH,
                        artifact=path_artifact,
                        notes=f"source-noise marker in path title: {marker}",
                    )
            if path_meta.artist:
                add(
                    "artist",
                    path_meta.artist,
                    0.15,
                    source=SOURCE_PATH,
                    method=METHOD_PATH,
                    artifact=path_artifact,
                    role="main",
                    order=0,
                    notes="weak path-derived artist",
                )
            if path_meta.album:
                add(
                    "album",
                    path_meta.album,
                    0.15,
                    source=SOURCE_PATH,
                    method=METHOD_PATH,
                    artifact=path_artifact,
                    notes="weak parent-directory album",
                )
            if path_meta.track is not None:
                add(
                    "track",
                    path_meta.track,
                    0.15,
                    source=SOURCE_PATH,
                    method=METHOD_PATH,
                    artifact=path_artifact,
                    notes="track number parsed from filename",
                )
            if path_meta.source:
                add(
                    "source",
                    path_meta.source,
                    0.3,
                    source=SOURCE_PATH,
                    method=METHOD_PATH,
                    artifact=path_artifact,
                    notes="local source path",
                )
        else:
            warnings.append("path_structure_missing")

        return InterpreterResult(
            artifact_type=CLAIMS_ARTIFACT,
            payload=_claims_payload(inputs.item_id, claims),
            claims=tuple(claims),
            warnings=tuple(dict.fromkeys(warnings)),
        )


class KindClassifier:
    """Classify audio as music/audiobook/podcast from explicit evidence."""

    manifest = CapabilityManifest(
        id="kind_classifier",
        version="1",
        kind=CapabilityKind.INTERPRETER,
        requires=(PROBE_ARTIFACT,),
        provides=(CLAIMS_ARTIFACT,),
        cost=Cost(cpu_seconds=0.01),
        determinism=Determinism.DETERMINISTIC,
        cacheable=True,
        side_effects=SideEffect.NONE,
    )

    def interpret(self, inputs: InterpreterInput) -> InterpreterResult:
        probe_artifact = inputs.require(PROBE_TYPE)
        probe = probe_artifact.model(AudioProbePayload)
        duration = probe.duration_seconds
        if duration is None:
            stream = probe.first_audio_stream()
            duration = stream.duration_seconds if stream is not None else None
        chapters = len(probe.chapters)
        kind, confidence, reason = _classify_kind(duration, chapters)
        claim = _make_claim(
            inputs.item_id,
            subject=Subject.ITEM,
            field="kind",
            value=kind.value,
            confidence=confidence,
            source=SOURCE_PROBE,
            method=METHOD_KIND,
            evidence=probe_artifact.artifact_id,
            notes=reason,
        )
        return InterpreterResult(
            artifact_type=CLAIMS_ARTIFACT,
            payload=_claims_payload(inputs.item_id, (claim,)),
            claims=(claim,),
        )


class DerivativeDetector:
    """Detect derivative markers in titles/comments/tags without tempo analysis."""

    manifest = CapabilityManifest(
        id="derivative_detector",
        version="1",
        kind=CapabilityKind.INTERPRETER,
        requires=(
            INITIAL_TAGS_ARTIFACT,
            PATH_STRUCTURE_ARTIFACT,
            PROBE_ARTIFACT,
        ),
        provides=(CLAIMS_ARTIFACT,),
        cost=Cost(cpu_seconds=0.01),
        determinism=Determinism.DETERMINISTIC,
        cacheable=True,
        side_effects=SideEffect.NONE,
    )

    def interpret(self, inputs: InterpreterInput) -> InterpreterResult:
        tags_artifact = inputs.require(INITIAL_TAGS_TYPE)
        path_artifact = inputs.optional(PATH_STRUCTURE_TYPE)
        tags = tags_artifact.model(InitialTagsPayload).tags
        path = path_artifact.model(PathStructurePayload) if path_artifact is not None else None

        text_sources: list[tuple[str, str, float, ArtifactPayload]] = []
        if tags.title:
            text_sources.append((tags.title, "title", 0.6, tags_artifact))
        if path is not None and path_artifact is not None and path.stem:
            text_sources.append((path.stem, "path", 0.45, path_artifact))
        if tags.comment:
            text_sources.append((tags.comment, "comment", 0.3, tags_artifact))
        for genre in tags.genre:
            text_sources.append((genre, "genre", 0.25, tags_artifact))
        if tags.album:
            text_sources.append((tags.album, "album", 0.2, tags_artifact))

        best: dict[Transformation, tuple[float, str, str, ArtifactPayload]] = {}
        for text, origin, confidence, artifact in text_sources:
            for transformation in detect_transformations(text):
                current = best.get(transformation)
                if current is None or confidence > current[0]:
                    best[transformation] = (confidence, origin, text, artifact)

        claims: list[Claim] = []
        for transformation in rank_transformations(tuple(best)):
            confidence, origin, text, artifact = best[transformation]
            claims.append(
                _make_claim(
                    inputs.item_id,
                    subject=Subject.DERIVATIVE,
                    field="transformation",
                    value=transformation.value,
                    confidence=min(0.7, confidence),
                    source=SOURCE_INITIAL_TAGS if artifact is tags_artifact else SOURCE_PATH,
                    method=METHOD_DERIVATIVE,
                    evidence=artifact.artifact_id,
                    notes=f"detected {transformation.value} in {origin}: {text[:120]}",
                )
            )

        if claims:
            derivative_title = tags.title or (path.stem if path is not None else None)
            if derivative_title:
                title_artifact = tags_artifact if tags.title else path_artifact
                if title_artifact is not None:
                    claims.append(
                        _make_claim(
                            inputs.item_id,
                            subject=Subject.DERIVATIVE,
                            field="title",
                            value=derivative_title,
                            confidence=0.45,
                            source=(
                                SOURCE_INITIAL_TAGS
                                if title_artifact is tags_artifact
                                else SOURCE_PATH
                            ),
                            method=METHOD_DERIVATIVE,
                            evidence=title_artifact.artifact_id,
                            notes="source title retained separately from item.title",
                        )
                    )
            artist = _first_artist(tags.artist)
            if artist:
                claims.append(
                    _make_claim(
                        inputs.item_id,
                        subject=Subject.DERIVATIVE,
                        field="artist",
                        value=artist,
                        confidence=0.35,
                        source=SOURCE_INITIAL_TAGS,
                        method=METHOD_DERIVATIVE,
                        evidence=tags_artifact.artifact_id,
                        role="main",
                        notes="source artist retained separately from item.artist",
                    )
                )

        return InterpreterResult(
            artifact_type=CLAIMS_ARTIFACT,
            payload=_claims_payload(inputs.item_id, claims),
            claims=tuple(claims),
        )


class IdentityResolver:
    """Build identity hypotheses without guessing external identifiers."""

    manifest = CapabilityManifest(
        id="identity_resolver",
        version="1",
        kind=CapabilityKind.INTERPRETER,
        requires=(CLAIMS_ARTIFACT, PROBE_ARTIFACT),
        provides=(IDENTITY_ARTIFACT,),
        cost=Cost(cpu_seconds=0.01),
        determinism=Determinism.DETERMINISTIC,
        cacheable=True,
        side_effects=SideEffect.NONE,
    )

    def interpret(self, inputs: InterpreterInput) -> InterpreterResult:
        inputs.optional(PROBE_TYPE)
        claims = tuple(inputs.claims)
        by_field = _claims_by_field(claims)
        recording = _first_value(by_field, "mb_recording_id")
        release = _first_value(by_field, "mb_release_id")
        release_group = _first_value(by_field, "mb_release_group_id")
        isrc = _first_value(by_field, "isrc")
        acoustid = _first_value(by_field, "acoustid")

        hypotheses: list[dict[str, Any]] = []
        warnings: list[str] = []
        if recording is not None:
            claim = by_field["mb_recording_id"][0]
            external = {"musicbrainz_recording": recording}
            if isrc is not None:
                external["isrc"] = isrc
            if acoustid is not None:
                external["acoustid"] = acoustid
            hypotheses.append(
                _hypothesis("recording", recording, float(claim.confidence), (claim.id,), external)
            )
        elif release is not None:
            claim = by_field["mb_release_id"][0]
            hypotheses.append(
                _hypothesis(
                    "release",
                    release,
                    float(claim.confidence),
                    (claim.id,),
                    {"musicbrainz_release": release},
                )
            )
        elif release_group is not None:
            claim = by_field["mb_release_group_id"][0]
            hypotheses.append(
                _hypothesis(
                    "release_group",
                    release_group,
                    float(claim.confidence),
                    (claim.id,),
                    {"musicbrainz_release_group": release_group},
                )
            )

        if not hypotheses:
            warnings.append("no_identity_evidence")
        status = "proposed" if hypotheses else "ambiguous"
        payload: dict[str, Any] = {
            "schema_version": 1,
            "item_id": str(inputs.item_id),
            "status": status,
            "hypotheses": hypotheses,
            "warnings": warnings,
        }
        return InterpreterResult(
            artifact_type=IDENTITY_ARTIFACT,
            payload=payload,
            warnings=tuple(warnings),
        )


initial_tags_interpreter = InitialTagsInterpreter()
kind_classifier = KindClassifier()
derivative_detector = DerivativeDetector()
identity_resolver = IdentityResolver()

DEFAULT_INTERPRETERS: tuple[Interpreter, ...] = (
    initial_tags_interpreter,
    kind_classifier,
    derivative_detector,
    identity_resolver,
)

ObservationSource = (
    Mapping[str, bytes | bytearray | str | ArtifactPayload | BaseModel]
    | Sequence[ArtifactPayload]
    | ObservationView
    | None
)


class InterpretItem:
    """Produce claims/identity hypotheses for one observed item."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], UnitOfWork],
        interpreters: Sequence[Interpreter] | None = None,
        *,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self._uow_factory = unit_of_work_factory
        self._interpreters = (
            tuple(interpreters) if interpreters is not None else DEFAULT_INTERPRETERS
        )
        self._artifact_store = artifact_store

    async def interpret(
        self,
        item_id: ItemId,
        observations: ObservationSource = None,
        *,
        force: bool = False,
        artifacts: ObservationSource = None,
    ) -> InterpretationView:
        """Interpret observations, append missing claims, and mark interpreted."""

        _ = force
        if observations is None and artifacts is not None:
            observations = artifacts
        async with self._uow_factory() as uow:
            item = await uow.items.get(item_id)
            if item is None:
                raise RecordNotFound("item not found", context={"item_id": item_id})
            if item.status not in {ItemStatus.OBSERVED, ItemStatus.INTERPRETED}:
                raise InvalidStateTransition(
                    f"cannot interpret an item in status {item.status.value}",
                    user_action="Acquire and observe the item before interpreting it.",
                    context={"item_id": item_id, "status": item.status.value},
                )

            artifacts = await self._load_artifacts(uow, item, observations)
            if INITIAL_TAGS_TYPE not in artifacts:
                raise ObservationFailed(
                    "initial.tags.v1 is required for interpretation",
                    user_action="Run observe first so tags and path artifacts exist.",
                    context={"item_id": item_id},
                )

            existing = await uow.claims.list_by_item(item.id)
            known = {_claim_key(claim): claim for claim in existing}
            produced: list[Claim] = []
            warnings: list[str] = []
            payloads: list[Mapping[str, Any]] = []
            accumulated = list(existing)
            identity_payload: Mapping[str, Any] | None = None

            for interpreter in self._interpreters:
                missing = _missing_requirements(interpreter, artifacts, accumulated)
                if missing:
                    warnings.append(f"missing_artifact:{missing}")
                    continue
                try:
                    result = interpreter.interpret(
                        InterpreterInput(
                            item_id=item.id,
                            artifacts=artifacts,
                            claims=tuple(accumulated),
                        )
                    )
                except MissingArtifactError as exc:
                    artifact_type = str(exc.context.get("artifact_type", "unknown"))
                    warnings.append(f"missing_artifact:{artifact_type}")
                    continue
                warnings.extend(result.warnings)
                payloads.append(result.payload)
                if result.artifact_type == IDENTITY_ARTIFACT:
                    identity_payload = result.payload
                for claim in result.claims:
                    key = _claim_key(claim)
                    if key in known:
                        continue
                    saved = await uow.claims.append(claim)
                    known[key] = saved
                    produced.append(saved)
                    accumulated.append(saved)

            _apply_kind(item, accumulated)
            if item.status is ItemStatus.OBSERVED:
                item.transition_to(ItemStatus.INTERPRETED)
            await uow.items.save(item)
            status = item.status

        if identity_payload is None:
            identity_payload = _empty_identity_payload(item_id)
        return InterpretationView(
            item_id=item_id,
            status=status,
            claims=tuple(produced) if produced else tuple(existing),
            identity=identity_payload,
            warnings=tuple(dict.fromkeys(warnings)),
            artifact_payloads=tuple(payloads),
        )

    async def _load_artifacts(
        self,
        uow: UnitOfWork,
        item: Item,
        observations: ObservationSource,
    ) -> dict[str, ArtifactPayload]:
        if observations is None:
            return await self._discover_artifacts(uow, item)
        if isinstance(observations, ObservationView):
            return await self._load_view(observations)
        if isinstance(observations, Mapping):
            return _mapping_artifacts(observations)
        if isinstance(observations, Sequence):
            return {
                artifact.type: artifact
                for artifact in observations
                if isinstance(artifact, ArtifactPayload)
            }
        raise DomainValidationError(
            "observations must be a mapping, sequence, ObservationView, or None"
        )

    async def _load_view(self, view: ObservationView) -> dict[str, ArtifactPayload]:
        if self._artifact_store is None:
            raise DomainValidationError("an artifact store is required to load an ObservationView")
        artifacts: dict[str, ArtifactPayload] = {}
        for summary in view.artifacts:
            reference = await self._artifact_store.find(summary.cache_key)
            if reference is None:
                continue
            manifest, payload = await self._artifact_store.load(reference)
            artifacts[manifest.type] = ArtifactPayload(
                type=manifest.type,
                payload=payload,
                artifact_id=summary.artifact_id,
            )
        return artifacts

    async def _discover_artifacts(
        self,
        uow: UnitOfWork,
        item: Item,
    ) -> dict[str, ArtifactPayload]:
        if self._artifact_store is None:
            raise DomainValidationError("an artifact store is required to discover observations")
        artifacts: dict[str, ArtifactPayload] = {}
        target_types = {
            INITIAL_TAGS_TYPE,
            PATH_STRUCTURE_TYPE,
            PROBE_TYPE,
        }
        steps = await uow.runs.list_steps_for_item(item.id)
        for step in reversed(steps):
            if not step.cache_key:
                continue
            record = await uow.artifacts.find_by_cache_key(step.cache_key)
            if record is None or record.artifact_type not in target_types:
                continue
            if record.artifact_type in artifacts:
                continue
            try:
                reference = await self._artifact_store.find(record.cache_key)
                if reference is None:
                    continue
                manifest, payload = await self._artifact_store.load(reference)
            except (StorageError, OSError) as exc:
                raise ObservationFailed(
                    f"could not load {record.artifact_type} artifact",
                    context={"item_id": item.id, "error": str(exc)},
                ) from exc
            artifacts[manifest.type] = ArtifactPayload(
                type=manifest.type,
                payload=payload,
                artifact_id=record.id,
            )

        if PROBE_TYPE not in artifacts and item.media_id is not None:
            media = await uow.media.get(item.media_id)
            if media is not None and media.probe_artifact_id is not None:
                record = await uow.artifacts.get(media.probe_artifact_id)
                if record is not None:
                    reference = await self._artifact_store.find(record.cache_key)
                    if reference is not None:
                        manifest, payload = await self._artifact_store.load(reference)
                        artifacts[manifest.type] = ArtifactPayload(
                            type=manifest.type,
                            payload=payload,
                            artifact_id=record.id,
                        )
        return artifacts


def _missing_requirements(
    interpreter: Interpreter,
    artifacts: Mapping[str, ArtifactPayload],
    claims: Sequence[Claim],
) -> str | None:
    for requirement in interpreter.manifest.requires:
        key = str(requirement)
        if key == CLAIMS_ARTIFACT:
            continue
        if interpreter.manifest.id in {
            "initial_tags_interpreter",
            "derivative_detector",
            "identity_resolver",
        } and key in {PATH_STRUCTURE_TYPE, PROBE_TYPE}:
            # These interpreters can degrade to text-only evidence.
            continue
        if key not in artifacts:
            return key
    return None


def _apply_kind(item: Item, claims: Sequence[Claim]) -> None:
    for claim in claims:
        if claim.subject is not Subject.ITEM or claim.field != "kind":
            continue
        if not isinstance(claim.value, str):
            continue
        try:
            kind = ContentKind(claim.value)
        except ValueError:
            continue
        item.kind = kind
        return


def _mapping_artifacts(
    observations: Mapping[str, bytes | bytearray | str | ArtifactPayload | BaseModel],
) -> dict[str, ArtifactPayload]:
    result: dict[str, ArtifactPayload] = {}
    for raw_type, raw_payload in observations.items():
        artifact_type = str(raw_type)
        if isinstance(raw_payload, ArtifactPayload):
            result[artifact_type] = raw_payload
        elif isinstance(raw_payload, (bytes, bytearray, memoryview)):
            payload = bytes(raw_payload)
            result[artifact_type] = ArtifactPayload(
                type=artifact_type,
                payload=payload,
                artifact_id=sha256_hex(payload),
            )
        elif isinstance(raw_payload, str):
            payload = raw_payload.encode("utf-8")
            result[artifact_type] = ArtifactPayload(
                type=artifact_type,
                payload=payload,
                artifact_id=sha256_hex(payload),
            )
        elif isinstance(raw_payload, BaseModel):
            payload = raw_payload.model_dump_json().encode("utf-8")
            result[artifact_type] = ArtifactPayload(
                type=artifact_type,
                payload=payload,
                artifact_id=sha256_hex(payload),
            )
        else:
            raise DomainValidationError(
                "observation payload must be bytes, text, or ArtifactPayload",
                context={"artifact_type": artifact_type},
            )
    return result


def _parse_path(payload: PathStructurePayload) -> _PathMetadata:
    stem = payload.stem.strip()
    title: str | None = stem or None
    artist: str | None = None
    album: str | None = None
    track: int | None = None

    leading = _LEADING_TRACK_RE.match(stem)
    remainder = stem
    if leading is not None:
        track = int(leading.group(1))
        remainder = leading.group("rest").strip()
        title = remainder or None

    artist_title = _ARTIST_TITLE_RE.match(remainder)
    if artist_title is not None:
        candidate_artist = normalize_artist_name(artist_title.group("artist"))
        candidate_title = artist_title.group("title").strip()
        if candidate_artist:
            artist = candidate_artist
            title = candidate_title or title

    parents = [parent.strip() for parent in payload.parent_names if parent.strip()]
    if parents:
        last = parents[-1]
        if last.casefold() not in _GENERIC_PARENTS:
            album = last
        if artist is None and len(parents) >= 2:
            candidate = normalize_artist_name(parents[-2])
            if candidate and parents[-2].casefold() not in _GENERIC_PARENTS:
                artist = candidate
        elif artist is None and len(parents) == 1:
            candidate = normalize_artist_name(parents[0])
            if candidate and parents[0].casefold() not in _GENERIC_PARENTS:
                artist = candidate

    return _PathMetadata(
        title=title,
        artist=artist,
        album=album,
        track=track,
        source=payload.path,
    )


def _classify_kind(
    duration: float | None,
    chapters: int,
) -> tuple[ContentKind, float, str]:
    if duration is None:
        return ContentKind.MUSIC, 0.4, "no duration evidence; music is the conservative default"
    if chapters >= 2 and duration >= 1800:
        return ContentKind.AUDIOBOOK, 0.65, f"{chapters} chapters and long duration"
    if chapters >= 2 and duration >= 600:
        return ContentKind.PODCAST, 0.6, f"{chapters} chapters and episode-like duration"
    if duration >= 5400 and chapters >= 1:
        return ContentKind.AUDIOBOOK, 0.6, "single long chapter"
    return ContentKind.MUSIC, 0.5, "audio without audiobook/podcast evidence"


def _first_artist(values: Sequence[str]) -> str | None:
    for value in values:
        credits = parse_artists(value)
        if credits:
            return credits[0].name
    return None


def _artwork_value(artwork: ArtworkInfo) -> str | None:
    if artwork.mime_type:
        return artwork.mime_type
    if artwork.picture_type:
        return artwork.picture_type
    if artwork.width and artwork.height:
        return f"{artwork.width}x{artwork.height}"
    return "embedded_artwork"


def _make_claim(
    item_id: ItemId,
    *,
    subject: Subject,
    field: str,
    value: FieldValue,
    confidence: float,
    source: str,
    method: str,
    evidence: str,
    role: str | None = None,
    order: int | None = None,
    notes: str | None = None,
) -> Claim:
    return Claim(
        id=ClaimId(new_uuid7()),
        item_id=item_id,
        subject=subject,
        field=field,
        value=value,
        role=role,
        order=order,
        source=source,
        method=method,
        confidence=Confidence(min(1.0, max(0.0, confidence))),
        evidence=(evidence,),
        notes=notes,
        created_at=datetime.now(UTC),
    )


def _claims_payload(item_id: ItemId, claims: Sequence[Claim]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "item_id": str(item_id),
        "claims": [_claim_document(claim) for claim in claims],
    }


def _claim_document(claim: Claim) -> dict[str, Any]:
    return {
        "id": claim.id,
        "item_id": claim.item_id,
        "subject": claim.subject.value,
        "subject_id": claim.subject_id,
        "field": claim.field,
        "value": claim.value,
        "role": claim.role,
        "order": claim.order,
        "locale": claim.locale,
        "source": claim.source,
        "method": claim.method,
        "confidence": float(claim.confidence),
        "evidence": list(claim.evidence),
        "derived_from": [str(item) for item in claim.derived_from],
        "status": claim.status.value,
        "notes": claim.notes,
        "created_at": claim.created_at.isoformat(),
    }


def _claim_key(claim: Claim) -> tuple[str, str, str, str | None, int | None, str, str]:
    value = canonical_json(claim.value).decode("utf-8")
    return (
        claim.subject.value,
        claim.field,
        value,
        claim.role,
        claim.order,
        claim.source,
        claim.method,
    )


def _claims_by_field(claims: Sequence[Claim]) -> dict[str, list[Claim]]:
    result: dict[str, list[Claim]] = {}
    for claim in claims:
        if claim.value is None:
            continue
        result.setdefault(claim.field, []).append(claim)
    return result


def _first_value(by_field: Mapping[str, Sequence[Claim]], field: str) -> str | None:
    for claim in by_field.get(field, ()):
        if isinstance(claim.value, str) and claim.value.strip():
            return claim.value.strip()
    return None


def _hypothesis(
    target_type: str,
    target_id: str,
    score: float,
    claim_ids: tuple[ClaimId, ...],
    external_ids: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "target_type": target_type,
        "target_id": target_id,
        "score": min(1.0, max(0.0, score)),
        "external_ids": dict(external_ids),
        "evidence": [str(item) for item in claim_ids],
        "status": "proposed",
    }


def _empty_identity_payload(item_id: ItemId) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "item_id": str(item_id),
        "status": "ambiguous",
        "hypotheses": [],
        "warnings": ["no_identity_evidence"],
    }


__all__ = [
    "CLAIMS_ARTIFACT",
    "DEFAULT_INTERPRETERS",
    "IDENTITY_ARTIFACT",
    "ArtifactPayload",
    "DerivativeDetector",
    "IdentityResolver",
    "InitialTagsInterpreter",
    "InterpretItem",
    "Interpreter",
    "InterpreterInput",
    "InterpreterResult",
    "KindClassifier",
    "MissingArtifactError",
    "ObservationSource",
    "derivative_detector",
    "identity_resolver",
    "initial_tags_interpreter",
    "kind_classifier",
]

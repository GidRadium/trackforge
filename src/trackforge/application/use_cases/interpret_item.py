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
from trackforge.application.dto.derivatives import (
    DerivativeEvidencePayload,
    DerivativeHypothesesPayload,
    DerivativeHypothesisPayload,
    DerivativeParametersPayload,
)
from trackforge.application.dto.lyrics import AsrTranscript
from trackforge.application.dto.observations import (
    ArtworkInfo,
    AudioProbePayload,
    InitialTagsPayload,
    NormalizedTags,
    ObservationView,
    PathStructurePayload,
    SourceIdentityPayload,
    SourceMetadataPayload,
)
from trackforge.application.dto.providers import (
    AcoustIdLookupPayload,
    CoverCandidate,
    CoverCandidatesPayload,
    LyricsCandidatesPayload,
    MusicBrainzRecording,
    MusicBrainzRelease,
    MusicBrainzSearchPayload,
)
from trackforge.application.dto.stems import StemsPayload
from trackforge.application.ports.observations import (
    ACOUSTID_ARTIFACT,
    ASR_TRANSCRIPT_ARTIFACT,
    COVER_ARTIFACT,
    FINGERPRINT_ARTIFACT,
    INITIAL_TAGS_ARTIFACT,
    LYRICS_ARTIFACT,
    MUSICBRAINZ_ARTIFACT,
    PATH_STRUCTURE_ARTIFACT,
    PROBE_ARTIFACT,
    SOURCE_IDENTITY_ARTIFACT,
    SOURCE_METADATA_ARTIFACT,
    STEMS_ARTIFACT,
)
from trackforge.application.ports.repositories import UnitOfWork
from trackforge.application.ports.storage import ArtifactStore, StorageError
from trackforge.application.services.canonical_json import canonical_json, sha256_hex
from trackforge.application.services.derivative_credits import build_derivative_credits
from trackforge.application.services.source_genres import normalize_source_genres
from trackforge.application.services.source_identity import (
    NormalizedSourceIdentity,
    looks_like_mix,
    normalize_source_identity,
)
from trackforge.application.services.source_lyrics import extract_description_lyrics
from trackforge.application.use_cases.lyrics_merge import LyricsMerger
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
from trackforge.domain.policies.derivative import (
    DerivativeEvidence,
    DerivativeHypothesis,
    DerivativeSource,
    RelationshipEvidence,
    TextEvidence,
    evaluate_derivative,
)
from trackforge.domain.policies.normalization import (
    comparison_key,
    detect_source_noise,
    detect_transformations,
    is_vevo,
    normalize_artist_name,
    parse_artists,
    strip_topic_suffix,
)
from trackforge.domain.policies.validators import validate_field

CLAIMS_ARTIFACT = "claims.v1"
IDENTITY_ARTIFACT = "identity.hypotheses.v1"
DERIVATIVE_HYPOTHESES_ARTIFACT = "derivative.hypotheses.v1"

INITIAL_TAGS_TYPE = str(INITIAL_TAGS_ARTIFACT)
PATH_STRUCTURE_TYPE = str(PATH_STRUCTURE_ARTIFACT)
PROBE_TYPE = str(PROBE_ARTIFACT)
SOURCE_METADATA_TYPE = str(SOURCE_METADATA_ARTIFACT)
ACOUSTID_TYPE = str(ACOUSTID_ARTIFACT)
MUSICBRAINZ_TYPE = str(MUSICBRAINZ_ARTIFACT)
COVER_TYPE = str(COVER_ARTIFACT)
LYRICS_TYPE = str(LYRICS_ARTIFACT)
ASR_TYPE = str(ASR_TRANSCRIPT_ARTIFACT)
STEMS_TYPE = str(STEMS_ARTIFACT)

SOURCE_INITIAL_TAGS = "initial_tags"
SOURCE_PATH = "path"
SOURCE_PROBE = "audio_probe"
SOURCE_SITE_METADATA = "source_metadata"
SOURCE_MUSICBRAINZ = "musicbrainz"
SOURCE_ACOUSTID = "acoustid"
SOURCE_COVER_ART_ARCHIVE = "cover_art_archive"
SOURCE_LRCLIB = "lrclib"
SOURCE_ASR = "asr"
SOURCE_LYRICS_MERGE = "lyrics_merge"
SOURCE_STEMS = "demucs"
SOURCE_TITLE_PARSER = "source_title_parser"
SOURCE_DESCRIPTION = "source_description"

METHOD_TAG_READER = "mutagen_tag_reader/v1"
METHOD_PATH = "path_structure/v1"
METHOD_KIND = "kind_classifier/v1"
METHOD_DERIVATIVE = "derivative_detector/v1"
METHOD_IDENTITY = "identity_resolver/v1"
METHOD_SOURCE_METADATA = "source_metadata/v1"
METHOD_SOURCE_TITLE_PARSER = "source_title_parser/v1"
METHOD_DESCRIPTION_LYRICS = "description_lyrics/v1"
METHOD_MUSICBRAINZ = "musicbrainz/v1"
METHOD_ACOUSTID = "acoustid/v1"
METHOD_COVER = "cover_selection/v1"
METHOD_LYRICS = "lrclib/v1"
METHOD_LYRICS_MERGE = "lyrics_merge/v1"
METHOD_ASR = "faster_whisper/v1"

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
_ATTRIBUTION_RE = re.compile(
    r"^\s*(?P<title>.+?)\s+by\s+(?P<artist>.+?)"
    r"(?:\s+from\s+(?P<album>.+?))?\.?\s*$",
    re.IGNORECASE,
)
_MBID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$")


def _valid_mbid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text if _MBID_RE.fullmatch(text) else None


def _valid_isrc(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().upper().replace("-", "")
    return text if _ISRC_RE.fullmatch(text) else None


def _similarity(left: str | None, right: str | None) -> float:
    if not left or not right:
        return 0.0
    from difflib import SequenceMatcher

    return SequenceMatcher(None, left.casefold(), right.casefold()).ratio()


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def _value_text(claims: Sequence[Claim], field: str) -> str | None:
    for claim in claims:
        if claim.field == field and isinstance(claim.value, str) and claim.value.strip():
            return claim.value.strip()
    return None


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
        if self.artifact_type not in {
            CLAIMS_ARTIFACT,
            IDENTITY_ARTIFACT,
            DERIVATIVE_HYPOTHESES_ARTIFACT,
        }:
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
        if tags.bpm is not None:
            add("bpm", float(tags.bpm), 0.35, notes="read from existing tag")
        if tags.key is not None and tags.key.strip():
            add("key", tags.key.strip(), 0.30, notes="read from existing tag")
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
    """Turn metadata, tempo/duration, vocal and MusicBrainz evidence into hypotheses."""

    manifest = CapabilityManifest(
        id="derivative_detector",
        version="1",
        kind=CapabilityKind.INTERPRETER,
        requires=(
            INITIAL_TAGS_ARTIFACT,
            PATH_STRUCTURE_ARTIFACT,
            PROBE_ARTIFACT,
        ),
        provides=(CLAIMS_ARTIFACT, DERIVATIVE_HYPOTHESES_ARTIFACT),
        cost=Cost(cpu_seconds=0.01),
        determinism=Determinism.DETERMINISTIC,
        cacheable=True,
        side_effects=SideEffect.NONE,
    )

    def interpret(self, inputs: InterpreterInput) -> InterpreterResult:
        tags_artifact = inputs.require(INITIAL_TAGS_TYPE)
        path_artifact = inputs.optional(PATH_STRUCTURE_TYPE)
        probe_artifact = inputs.optional(PROBE_TYPE)
        metadata_artifact = inputs.optional(SOURCE_METADATA_TYPE)
        mb_artifact = inputs.optional(MUSICBRAINZ_TYPE)
        stems_artifact = inputs.optional(STEMS_TYPE)

        tags = tags_artifact.model(InitialTagsPayload).tags
        path = path_artifact.model(PathStructurePayload) if path_artifact is not None else None
        metadata = (
            metadata_artifact.model(SourceMetadataPayload)
            if metadata_artifact is not None
            else None
        )
        warnings: list[str] = []
        duration = _probe_duration(inputs)
        if looks_like_mix(
            metadata.title if metadata is not None else tags.title,
            duration,
            uploader=metadata.uploader if metadata is not None else None,
            description=metadata.description if metadata is not None else None,
        ):
            return InterpreterResult(
                artifact_type=DERIVATIVE_HYPOTHESES_ARTIFACT,
                payload=_derivative_payload(inputs.item_id, None),
                warnings=("mix_track_detected",),
            )

        text_sources = _derivative_text_sources(
            tags,
            path.stem if path is not None else None,
            metadata,
            tags_artifact=tags_artifact,
            path_artifact=path_artifact,
            metadata_artifact=metadata_artifact,
        )
        recording = _derivative_recording(
            inputs,
            mb_artifact,
            title=metadata.title if metadata is not None else tags.title,
            artist=_first_artist(tags.artist),
            duration=duration,
            warnings=warnings,
        )
        relationships = tuple(
            RelationshipEvidence(
                type=item.type,
                official=item.official,
                target_title=item.target_title,
                artifact_ref=mb_artifact.artifact_id if mb_artifact is not None else None,
            )
            for item in (recording.relationships if recording is not None else ())
        )
        stems, stems_warning = _derivative_stems(inputs, stems_artifact)
        if stems_warning is not None:
            warnings.append(stems_warning)
        if stems is not None:
            vocal_energy = (
                stems.features.vocal_energy
                if stems.features.vocal_energy is not None
                else stems.ratios.vocals
            )
            drum_energy: float | None = stems.ratios.drums
            bass_energy: float | None = stems.ratios.bass
            other_energy: float | None = stems.ratios.other
        else:
            vocal_energy = None
            drum_energy = None
            bass_energy = None
            other_energy = None

        measured_bpm = _claim_number(inputs.claims, "bpm")
        hypothesis = evaluate_derivative(
            texts=text_sources,
            measured_duration=duration,
            original_duration=recording.duration if recording is not None else None,
            measured_bpm=measured_bpm,
            original_bpm=recording.bpm if recording is not None else None,
            vocal_energy=vocal_energy,
            drum_energy=drum_energy,
            bass_energy=bass_energy,
            other_energy=other_energy,
            relationships=relationships,
            artifact_refs={
                DerivativeSource.METADATA: (
                    metadata_artifact.artifact_id
                    if metadata_artifact is not None
                    else tags_artifact.artifact_id
                ),
                DerivativeSource.DURATION: (
                    mb_artifact.artifact_id
                    if mb_artifact is not None
                    else (probe_artifact.artifact_id if probe_artifact is not None else None)
                ),
                DerivativeSource.TEMPO: tags_artifact.artifact_id,
                DerivativeSource.VOCAL: (
                    stems_artifact.artifact_id if stems_artifact is not None else None
                ),
                DerivativeSource.SPECTRAL: (
                    stems_artifact.artifact_id if stems_artifact is not None else None
                ),
                DerivativeSource.MUSICBRAINZ: (
                    mb_artifact.artifact_id if mb_artifact is not None else None
                ),
            },
            derivative_title=(
                tags.title
                or (metadata.title if metadata is not None else None)
                or (path.stem if path is not None else None)
            ),
            derivative_artist=_first_artist(tags.artist),
        )

        claims: list[Claim] = []
        if hypothesis is not None:
            claims.extend(
                _derivative_hypothesis_claims(
                    inputs.item_id,
                    hypothesis,
                    tags_artifact=tags_artifact,
                    path_artifact=path_artifact,
                    probe_artifact=probe_artifact,
                    metadata_artifact=metadata_artifact,
                    mb_artifact=mb_artifact,
                    stems_artifact=stems_artifact,
                )
            )
            if not hypothesis.is_ambiguous:
                credits_claims, credits_warnings = _derivative_credits_claims(
                    inputs,
                    hypothesis.transformations,
                    tags,
                    metadata,
                    metadata_artifact or tags_artifact,
                    recording_title=recording.title if recording is not None else None,
                    recording_artist=(
                        recording.artists[0].name
                        if recording is not None and recording.artists
                        else None
                    ),
                )
                claims.extend(credits_claims)
                warnings.extend(credits_warnings)
        payload = _derivative_payload(inputs.item_id, hypothesis)
        return InterpreterResult(
            artifact_type=DERIVATIVE_HYPOTHESES_ARTIFACT,
            payload=payload,
            claims=tuple(claims),
            warnings=tuple(warnings),
        )


def _source_identity(
    inputs: InterpreterInput,
    metadata: SourceMetadataPayload | None,
    tags: NormalizedTags | None,
) -> tuple[NormalizedSourceIdentity, str]:
    """Prefer the LLM source identity artifact, then the heuristic parser."""

    artifact = inputs.optional(str(SOURCE_IDENTITY_ARTIFACT))
    if artifact is not None:
        try:
            payload = artifact.model(SourceIdentityPayload)
        except ObservationFailed:
            payload = None
        if payload is not None:
            source_title = (
                payload.source_title
                or (metadata.title if metadata is not None else None)
                or (tags.title if tags is not None else None)
            )
            return (
                NormalizedSourceIdentity(
                    title=payload.title,
                    artist=payload.artist or payload.originalartist,
                    qualifier=payload.qualifier,
                    source_title=source_title or "",
                    confidence=payload.confidence,
                    warnings=tuple(payload.warnings),
                ),
                payload.method,
            )
    return (
        normalize_source_identity(
            metadata.title if metadata is not None else (tags.title if tags is not None else None),
            ((metadata.artist or metadata.uploader) if metadata is not None else None),
            metadata.description if metadata is not None else None,
        ),
        "heuristic",
    )


def _derivative_credits_claims(
    inputs: InterpreterInput,
    transformations: Sequence[Transformation],
    tags: NormalizedTags,
    metadata: SourceMetadataPayload | None,
    artifact: ArtifactPayload,
    *,
    recording_title: str | None,
    recording_artist: str | None,
) -> tuple[list[Claim], list[str]]:
    identity, _identity_method = _source_identity(inputs, metadata, tags)
    credits = build_derivative_credits(
        transformations,
        metadata.uploader if metadata is not None else None,
        identity,
        inputs.claims,
        recording_artist=recording_artist,
        recording_title=recording_title,
    )
    if credits.qualifier is None:
        return [], list(credits.warnings)
    claims: list[Claim] = []

    def add(
        field: str,
        value: FieldValue,
        confidence: float,
        *,
        role: str | None = None,
        order: int | None = None,
        notes: str | None = None,
    ) -> None:
        if value is None:
            return
        if field in {"title", "qualifier", "originalartist"} and isinstance(value, str):
            for existing in (*inputs.claims, *claims):
                if (
                    existing.field == field
                    and isinstance(existing.value, str)
                    and comparison_key(existing.value) == comparison_key(value)
                ):
                    return
        claims.append(
            _make_claim(
                inputs.item_id,
                subject=Subject.ITEM,
                field=field,
                value=value,
                confidence=confidence,
                source="derivative_credits",
                method="derivative_credits/v1",
                evidence=artifact.artifact_id,
                role=role,
                order=order,
                notes=notes,
            )
        )

    add(
        "title",
        credits.title,
        min(0.85, credits.confidence),
        notes="clean original title; derivative qualifier kept separately",
    )
    add(
        "qualifier",
        credits.qualifier,
        min(0.9, credits.confidence),
        notes="derivative qualifier used by filename/tag policy",
    )
    add(
        "remixer",
        credits.remixer,
        min(0.85, credits.confidence),
        notes="uploader/channel that produced the derivative",
    )
    add(
        "originalartist",
        credits.original_artist,
        min(0.8, credits.confidence),
        notes="original performer retained as featured artist",
    )
    if credits.comment:
        add(
            "comment",
            credits.comment,
            min(0.8, credits.confidence),
            notes="short derivative provenance comment",
        )
    if credits.remixer and credits.original_artist:
        add(
            "artist",
            credits.remixer,
            min(0.85, credits.confidence),
            role="main",
            order=0,
            notes="D11: remixer is the main artist for derivative credits",
        )
        add(
            "artist",
            credits.original_artist,
            min(0.75, credits.confidence),
            role="featured",
            order=1,
            notes="D11: original artist is featured",
        )
    elif credits.remixer:
        add(
            "artist",
            credits.remixer,
            min(0.7, credits.confidence),
            role="main",
            order=0,
            notes="original artist unknown; remixer remains main",
        )
    elif credits.original_artist:
        add(
            "artist",
            credits.original_artist,
            min(0.7, credits.confidence),
            role="main",
            order=0,
            notes="remixer unknown; original artist is the safe main artist",
        )
    return claims, list(credits.warnings)


def _derivative_text_sources(
    tags: NormalizedTags,
    path_stem: str | None,
    metadata: SourceMetadataPayload | None,
    *,
    tags_artifact: ArtifactPayload,
    path_artifact: ArtifactPayload | None,
    metadata_artifact: ArtifactPayload | None,
) -> tuple[TextEvidence, ...]:
    entries: list[tuple[str, str, float, str]] = []
    if tags.title:
        entries.append((tags.title, "title", 0.75, tags_artifact.artifact_id))
    if path_stem:
        entries.append(
            (
                path_stem,
                "path",
                0.55,
                (
                    path_artifact.artifact_id
                    if path_artifact is not None
                    else tags_artifact.artifact_id
                ),
            )
        )
    if tags.comment:
        entries.append((tags.comment, "comment", 0.40, tags_artifact.artifact_id))
    for genre in tags.genre:
        entries.append((genre, "genre", 0.30, tags_artifact.artifact_id))
    if tags.album:
        entries.append((tags.album, "album", 0.20, tags_artifact.artifact_id))
    if metadata is not None and metadata_artifact is not None:
        if metadata.title:
            entries.append((metadata.title, "source_title", 0.70, metadata_artifact.artifact_id))
        if metadata.description:
            entries.append(
                (metadata.description, "description", 0.50, metadata_artifact.artifact_id)
            )
        for tag in metadata.tags:
            entries.append((tag, "source_tag", 0.55, metadata_artifact.artifact_id))
        if metadata.uploader:
            entries.append((metadata.uploader, "uploader", 0.35, metadata_artifact.artifact_id))
        if metadata.album:
            entries.append((metadata.album, "source_album", 0.20, metadata_artifact.artifact_id))

    best: dict[str, tuple[str, str, float, str]] = {}
    for text, origin, confidence, artifact_ref in entries:
        key = text.casefold().strip()
        if not key:
            continue
        current = best.get(key)
        if current is None or confidence > current[2]:
            best[key] = (text, origin, confidence, artifact_ref)
    return tuple(
        TextEvidence(
            text=text,
            origin=origin,
            confidence=confidence,
            artifact_ref=artifact_ref,
        )
        for text, origin, confidence, artifact_ref in best.values()
    )


def _derivative_recording(
    inputs: InterpreterInput,
    artifact: ArtifactPayload | None,
    *,
    title: str | None,
    artist: str | None,
    duration: float | None,
    warnings: list[str],
) -> MusicBrainzRecording | None:
    if artifact is None:
        return None
    try:
        payload = artifact.model(MusicBrainzSearchPayload)
    except Exception:
        warnings.append("invalid_artifact:musicbrainz.search.v1")
        return None
    return _best_recording_for_query(payload, title, artist, duration)


def _derivative_stems(
    inputs: InterpreterInput,
    artifact: ArtifactPayload | None,
) -> tuple[StemsPayload | None, str | None]:
    if artifact is None:
        return None, None
    try:
        return artifact.model(StemsPayload), None
    except Exception:
        return None, "invalid_artifact:audio.stems.v1"


def _claim_number(claims: Sequence[Claim], field: str) -> float | None:
    for claim in reversed(claims):
        if claim.field != field or claim.subject is not Subject.ITEM:
            continue
        value = claim.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if number > 0.0:
            return number
    return None


def _derivative_hypothesis_claims(
    item_id: ItemId,
    hypothesis: DerivativeHypothesis,
    *,
    tags_artifact: ArtifactPayload,
    path_artifact: ArtifactPayload | None,
    probe_artifact: ArtifactPayload | None,
    metadata_artifact: ArtifactPayload | None,
    mb_artifact: ArtifactPayload | None,
    stems_artifact: ArtifactPayload | None,
) -> list[Claim]:
    claims: list[Claim] = []
    context = {
        "tags": tags_artifact,
        "path": path_artifact,
        "probe": probe_artifact,
        "metadata": metadata_artifact,
        "musicbrainz": mb_artifact,
        "stems": stems_artifact,
    }
    for transformation in hypothesis.transformations:
        evidence = _best_evidence(hypothesis, transformation)
        confidence = _transformation_confidence(hypothesis, transformation)
        source, artifact_id = _derivative_claim_provenance(evidence, context, tags_artifact)
        claims.append(
            _make_claim(
                item_id,
                subject=Subject.DERIVATIVE,
                field="transformation",
                value=transformation.value,
                confidence=confidence,
                source=source,
                method=METHOD_DERIVATIVE,
                evidence=artifact_id,
                notes=(
                    f"{hypothesis.status.value} derivative hypothesis "
                    f"(score {hypothesis.score:.2f}); "
                    f"{len(hypothesis.evidence)} evidence item(s)"
                ),
            )
        )

    best = max(hypothesis.evidence, key=lambda item: item.confidence, default=None)
    source, artifact_id = _derivative_claim_provenance(best, context, tags_artifact)
    claims.append(
        _make_claim(
            item_id,
            subject=Subject.DERIVATIVE,
            field="status",
            value=hypothesis.status.value,
            confidence=hypothesis.score,
            source=source,
            method=METHOD_DERIVATIVE,
            evidence=artifact_id,
            notes=(
                "ambiguous evidence retained for review; resolver must not pick a winner"
                if hypothesis.is_ambiguous
                else "proposed hypothesis; resolver decides whether to apply it"
            ),
        )
    )
    if hypothesis.is_official is not None:
        official_evidence = next(
            (item for item in hypothesis.evidence if item.source is DerivativeSource.MUSICBRAINZ),
            best,
        )
        source, artifact_id = _derivative_claim_provenance(
            official_evidence, context, tags_artifact
        )
        claims.append(
            _make_claim(
                item_id,
                subject=Subject.DERIVATIVE,
                field="is_official",
                value=hypothesis.is_official,
                confidence=0.85 if hypothesis.is_official else 0.65,
                source=source,
                method=METHOD_DERIVATIVE,
                evidence=artifact_id,
                notes="official status comes only from MusicBrainz/explicit source evidence",
            )
        )

    for field, value in hypothesis.parameters.as_dict().items():
        claims.append(
            _make_claim(
                item_id,
                subject=Subject.DERIVATIVE,
                field=field,
                value=value,
                confidence=min(0.75, hypothesis.score),
                source=source,
                method=METHOD_DERIVATIVE,
                evidence=artifact_id,
                notes="estimated derivative parameter; not a measured tag value",
            )
        )

    if hypothesis.derivative_title:
        title_artifact = tags_artifact if tags_artifact is not None else path_artifact
        claims.append(
            _make_claim(
                item_id,
                subject=Subject.DERIVATIVE,
                field="title",
                value=hypothesis.derivative_title,
                confidence=0.45,
                source=SOURCE_INITIAL_TAGS if title_artifact is tags_artifact else SOURCE_PATH,
                method=METHOD_DERIVATIVE,
                evidence=(
                    title_artifact.artifact_id
                    if title_artifact is not None
                    else tags_artifact.artifact_id
                ),
                notes="source title retained separately from item.title",
            )
        )
    if hypothesis.derivative_artist:
        claims.append(
            _make_claim(
                item_id,
                subject=Subject.DERIVATIVE,
                field="artist",
                value=hypothesis.derivative_artist,
                confidence=0.35,
                source=SOURCE_INITIAL_TAGS,
                method=METHOD_DERIVATIVE,
                evidence=tags_artifact.artifact_id,
                role="main",
                notes="source artist retained separately from item.artist",
            )
        )
    return claims


def _derivative_claim_provenance(
    evidence: DerivativeEvidence | None,
    context: Mapping[str, ArtifactPayload | None],
    fallback: ArtifactPayload,
) -> tuple[str, str]:
    if evidence is None:
        return SOURCE_INITIAL_TAGS, fallback.artifact_id
    artifact_id = evidence.artifact_ref or fallback.artifact_id
    if evidence.source is DerivativeSource.MUSICBRAINZ:
        return SOURCE_MUSICBRAINZ, artifact_id
    if evidence.source in {DerivativeSource.VOCAL, DerivativeSource.SPECTRAL}:
        return SOURCE_STEMS, artifact_id
    if evidence.source is DerivativeSource.LLM:
        return "llm", artifact_id
    if evidence.artifact_ref is not None:
        metadata = context["metadata"]
        if metadata is not None and evidence.artifact_ref == metadata.artifact_id:
            return SOURCE_SITE_METADATA, artifact_id
        if context["path"] is not None and evidence.artifact_ref == context["path"].artifact_id:
            return SOURCE_PATH, artifact_id
        if context["probe"] is not None and evidence.artifact_ref == context["probe"].artifact_id:
            return SOURCE_PROBE, artifact_id
        if context["stems"] is not None and evidence.artifact_ref == context["stems"].artifact_id:
            return SOURCE_STEMS, artifact_id
    return SOURCE_INITIAL_TAGS, artifact_id


def _evidence_supports(
    evidence: DerivativeEvidence,
    transformation: Transformation,
) -> bool:
    if evidence.source is DerivativeSource.METADATA:
        if evidence.value == transformation.value:
            return True
        return (
            transformation is Transformation.SPED_UP
            and evidence.value == "nightcore implies sped_up"
        )
    if evidence.source in {
        DerivativeSource.DURATION,
        DerivativeSource.TEMPO,
        DerivativeSource.SPECTRAL,
    }:
        return transformation in {
            Transformation.NIGHTCORE,
            Transformation.SPED_UP,
            Transformation.SLOWED,
        }
    if evidence.source is DerivativeSource.VOCAL:
        return transformation in {Transformation.INSTRUMENTAL, Transformation.ACAPELLA}
    if evidence.source is DerivativeSource.MUSICBRAINZ:
        return str(transformation.value) in str(evidence.value)
    if evidence.source is DerivativeSource.LLM:
        return evidence.value == transformation.value
    return False


def _best_evidence(
    hypothesis: DerivativeHypothesis,
    transformation: Transformation,
) -> DerivativeEvidence | None:
    items = [item for item in hypothesis.evidence if _evidence_supports(item, transformation)]
    return max(items, key=lambda item: item.confidence, default=None)


def _transformation_confidence(
    hypothesis: DerivativeHypothesis,
    transformation: Transformation,
) -> float:
    evidence = _best_evidence(hypothesis, transformation)
    if evidence is None:
        return hypothesis.score
    return min(0.95, max(evidence.confidence, hypothesis.score * 0.85))


def _derivative_payload(
    item_id: ItemId,
    hypothesis: DerivativeHypothesis | None,
) -> dict[str, Any]:
    if hypothesis is None:
        return DerivativeHypothesesPayload(
            item_id=str(item_id),
            status="none",
            warnings=["no_derivative_evidence"],
        ).model_dump(mode="json")
    return DerivativeHypothesesPayload(
        item_id=str(item_id),
        status="ambiguous" if hypothesis.is_ambiguous else "proposed",
        hypotheses=[
            DerivativeHypothesisPayload(
                transformations=list(hypothesis.transformations),
                parameters=DerivativeParametersPayload(**hypothesis.parameters.as_dict()),
                derivative_title=hypothesis.derivative_title,
                derivative_artist=hypothesis.derivative_artist,
                is_official=hypothesis.is_official,
                score=hypothesis.score,
                status=hypothesis.status.value,
                evidence=[
                    DerivativeEvidencePayload(
                        source=item.source.value,
                        value=item.value,
                        confidence=item.confidence,
                        artifact_ref=item.artifact_ref,
                    )
                    for item in hypothesis.evidence
                ],
            )
        ],
    ).model_dump(mode="json")


class MetadataInterpreter:
    """Turn MusicBrainz/AcoustID/cover/lyrics observations into claims."""

    manifest = CapabilityManifest(
        id="metadata_interpreter",
        version="1",
        kind=CapabilityKind.INTERPRETER,
        requires=(),
        provides=(CLAIMS_ARTIFACT,),
        cost=Cost(cpu_seconds=0.05, memory_mb=16.0),
        determinism=Determinism.DETERMINISTIC,
        cacheable=True,
        side_effects=SideEffect.NONE,
        description="Create provenance-rich claims from external metadata observations.",
    )

    def interpret(self, inputs: InterpreterInput) -> InterpreterResult:
        claims: list[Claim] = []
        warnings: list[str] = []
        mb_artifact = inputs.optional(MUSICBRAINZ_TYPE)
        if mb_artifact is not None:
            try:
                mb = mb_artifact.model(MusicBrainzSearchPayload)
            except ObservationFailed:
                warnings.append("invalid_artifact:musicbrainz.search.v1")
            else:
                claims.extend(_musicbrainz_claims(inputs, mb_artifact, mb))
        acoustid_artifact = inputs.optional(ACOUSTID_TYPE)
        if acoustid_artifact is not None:
            try:
                acoustid = acoustid_artifact.model(AcoustIdLookupPayload)
            except ObservationFailed:
                warnings.append("invalid_artifact:acoustid.lookup.v1")
            else:
                claims.extend(_acoustid_claims(inputs, acoustid_artifact, acoustid, inputs.claims))
        cover_artifact = inputs.optional(COVER_TYPE)
        if cover_artifact is not None:
            try:
                cover = cover_artifact.model(CoverCandidatesPayload)
            except ObservationFailed:
                warnings.append("invalid_artifact:cover.candidates.v1")
            else:
                claims.extend(_cover_claims(inputs, cover_artifact, cover))
        lyrics_artifact = inputs.optional(LYRICS_TYPE)
        lyrics_payload: LyricsCandidatesPayload | None = None
        if lyrics_artifact is not None:
            try:
                lyrics_payload = lyrics_artifact.model(LyricsCandidatesPayload)
            except ObservationFailed:
                warnings.append("invalid_artifact:lyrics.candidates.v1")
        asr_artifact = inputs.optional(ASR_TYPE)
        transcript: AsrTranscript | None = None
        if asr_artifact is not None:
            try:
                transcript = asr_artifact.model(AsrTranscript)
            except ObservationFailed:
                warnings.append("invalid_artifact:asr.transcript.v1")
        if lyrics_payload is not None or transcript is not None:
            artifact = lyrics_artifact or asr_artifact
            if artifact is not None:
                lyrics_claims, lyrics_warnings = _merged_lyrics_claims(
                    inputs,
                    lyrics_payload,
                    transcript,
                    artifact,
                )
                claims.extend(lyrics_claims)
                warnings.extend(lyrics_warnings)
        return InterpreterResult(
            artifact_type=CLAIMS_ARTIFACT,
            payload=_claims_payload(inputs.item_id, claims),
            claims=tuple(claims),
            warnings=tuple(dict.fromkeys(warnings)),
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
        recording = _valid_mbid(_first_value(by_field, "mb_recording_id"))
        release = _valid_mbid(_first_value(by_field, "mb_release_id"))
        release_group = _valid_mbid(_first_value(by_field, "mb_release_group_id"))
        isrc = _valid_isrc(_first_value(by_field, "isrc"))
        acoustid = _first_value(by_field, "acoustid")
        warnings: list[str] = []
        candidates = _identity_candidates(inputs, claims)
        hypotheses: list[dict[str, Any]] = []
        for candidate in candidates[:3]:
            external = dict(candidate["external_ids"])
            if isrc is not None:
                external.setdefault("isrc", isrc)
            if acoustid is not None:
                external.setdefault("acoustid", acoustid)
            hypotheses.append(
                _hypothesis(
                    "recording",
                    candidate["target_id"],
                    candidate["score"],
                    tuple(candidate["claim_ids"]),
                    external,
                    evidence=tuple(candidate["evidence"]),
                )
            )
        if not hypotheses and recording is not None:
            claim = by_field["mb_recording_id"][0]
            external = {"musicbrainz_recording": recording}
            if isrc is not None:
                external["isrc"] = isrc
            if acoustid is not None:
                external["acoustid"] = acoustid
            hypotheses.append(
                _hypothesis("recording", recording, float(claim.confidence), (claim.id,), external)
            )
        elif not hypotheses and release is not None:
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
        elif not hypotheses and release_group is not None:
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

        status = "proposed"
        if not hypotheses:
            warnings.append("no_identity_evidence")
            status = "ambiguous"
        elif len(hypotheses) > 1:
            second = float(hypotheses[1]["score"])
            if second >= 0.55:
                status = "ambiguous"
                warnings.append("ambiguous_identity")
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


class SourceMetadataInterpreter:
    """Create claims from the normalized yt-dlp source metadata artifact."""

    manifest = CapabilityManifest(
        id="source_metadata_interpreter",
        version="1",
        kind=CapabilityKind.INTERPRETER,
        requires=(),
        provides=(CLAIMS_ARTIFACT,),
        cost=Cost(cpu_seconds=0.01, memory_mb=8.0),
        determinism=Determinism.DETERMINISTIC,
        cacheable=True,
        side_effects=SideEffect.NONE,
        description="Turn source.metadata.v1 into source-backed claims.",
    )

    def interpret(self, inputs: InterpreterInput) -> InterpreterResult:
        artifact = inputs.optional(str(SOURCE_METADATA_ARTIFACT))
        if artifact is None:
            return InterpreterResult(
                artifact_type=CLAIMS_ARTIFACT,
                payload={
                    "schema_version": 1,
                    "item_id": str(inputs.item_id),
                    "claims": [],
                },
            )
        payload = artifact.model(SourceMetadataPayload)
        claims: list[Claim] = []
        warnings: list[str] = []
        identity, identity_method = _source_identity(
            inputs,
            payload,
            None,
        )
        if identity_method != "mix" and looks_like_mix(
            payload.title,
            payload.duration_seconds or payload.duration,
            uploader=payload.uploader,
            description=payload.description,
        ):
            identity = NormalizedSourceIdentity(
                title=payload.title,
                artist=payload.uploader or payload.artist,
                qualifier=None,
                source_title=payload.title,
                confidence=0.9,
                warnings=("mix_track_detected",),
            )
            identity_method = "mix"
        warnings.extend(identity.warnings)
        is_mix = identity_method == "mix"
        derivative = (
            False
            if is_mix
            else bool(
                identity.qualifier
                or detect_transformations(payload.title or "")
                or detect_transformations(payload.uploader or "")
            )
        )

        identity_source = "llm" if identity_method == "llm" else SOURCE_TITLE_PARSER
        identity_method_name = (
            "source_identity/v1" if identity_method == "llm" else METHOD_SOURCE_TITLE_PARSER
        )

        def add(
            field: str,
            value: FieldValue,
            confidence: float,
            *,
            source: str = SOURCE_SITE_METADATA,
            method: str = METHOD_SOURCE_METADATA,
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

        # The raw upload title is provenance, never the display title.
        add(
            "originaltitle",
            payload.title,
            0.8,
            notes="raw source title retained for sidecar provenance",
        )
        if is_mix:
            add(
                "is_mix",
                True,
                0.9,
                notes="mix/compilation keeps its raw uploader title and channel name",
            )
        if identity.title and identity.confidence >= 0.7:
            add(
                "title",
                identity.title,
                0.8,
                source=identity_source,
                method=identity_method_name,
                notes=(
                    "normalized source title; "
                    f"raw={payload.title!r}; confidence={identity.confidence:.2f}"
                ),
            )
        elif identity.title is None:
            warnings.append("source_title_unusable")
        else:
            warnings.append("source_title_low_confidence")
        if identity.qualifier:
            add(
                "qualifier",
                identity.qualifier,
                0.75,
                source=identity_source,
                method=identity_method_name,
                notes="derivative qualifier stored separately from the title",
            )
        for marker in detect_source_noise(payload.title):
            add(
                "source_noise",
                marker,
                0.7,
                notes=f"source-noise marker in source title: {marker}",
            )

        raw_artist = payload.artist or payload.uploader
        if identity.artist:
            add(
                "artist",
                identity.artist,
                0.7,
                source=identity_source,
                method=identity_method_name,
                role="featured" if derivative else "main",
                order=1 if derivative else 0,
                notes=(
                    "original artist parsed from source title/description"
                    if derivative
                    else "artist parsed from source metadata"
                ),
            )
            if derivative and not (
                payload.uploader
                and comparison_key(identity.artist) == comparison_key(payload.uploader)
            ):
                add(
                    "originalartist",
                    identity.artist,
                    0.65,
                    source=identity_source,
                    method=identity_method_name,
                    notes="original performer retained for derivative credits",
                )
        elif raw_artist and not derivative:
            # A non-derivative channel name is a legitimate artist fallback.
            # For nightcore/remix channels it is provenance, not the original.
            cleaned = strip_topic_suffix(raw_artist)
            if cleaned != raw_artist.strip():
                add(
                    "source_noise",
                    "topic_channel",
                    0.65,
                    notes="YouTube Topic suffix removed for comparison",
                )
            for index, credit in enumerate(parse_artists(cleaned)):
                add(
                    "artist",
                    credit.name,
                    0.65 if credit.role == "main" else 0.55,
                    role=credit.role,
                    order=index,
                )
        attribution = _description_attribution(payload.description, payload.title)
        if attribution is not None:
            parsed_artist, parsed_album = attribution
            if identity.artist is None:
                add(
                    "artist",
                    parsed_artist,
                    0.60,
                    notes="parsed from source description attribution",
                )
            if parsed_album:
                add(
                    "album",
                    parsed_album,
                    0.45,
                    notes="parsed from source description attribution",
                )
        if payload.album:
            add("album", payload.album, 0.5)
        date_value = _normalize_upload_date(payload.upload_date)
        if date_value is None and payload.release_year is not None:
            date_value = str(payload.release_year)
        add("date", date_value, 0.5)
        for index, genre in enumerate(
            normalize_source_genres(payload.tags, payload.title, payload.uploader)
        ):
            add("genre", genre, 0.35, order=index)
        add("language", payload.language, 0.4)
        if payload.track and payload.track.strip().isdigit():
            add("track", int(payload.track.strip()), 0.4)
        add(
            "description",
            payload.description[:10_000] if payload.description else None,
            0.3,
            notes="untrusted source description retained as LLM evidence",
        )
        description_lyrics = extract_description_lyrics(payload.description)
        if description_lyrics is not None:
            warnings.extend(description_lyrics.warnings)
            if description_lyrics.text:
                add(
                    "lyrics",
                    description_lyrics.text,
                    description_lyrics.confidence,
                    source=SOURCE_DESCRIPTION,
                    method=METHOD_DESCRIPTION_LYRICS,
                    notes="plain lyrics from an explicit description section",
                )
        add("source", payload.webpage_url, 0.8, notes="canonical source page")
        return InterpreterResult(
            artifact_type=CLAIMS_ARTIFACT,
            payload=_claims_payload(inputs.item_id, claims),
            claims=tuple(claims),
            warnings=tuple(dict.fromkeys(warnings)),
        )


def _musicbrainz_claims(
    inputs: InterpreterInput,
    artifact: ArtifactPayload,
    mb: MusicBrainzSearchPayload,
) -> list[Claim]:
    claims: list[Claim] = []
    query_title = mb.query.title or _value_text(inputs.claims, "title")
    query_artist = mb.query.artist or _value_text(inputs.claims, "artist")
    recording = _best_recording_for_query(mb, query_title, query_artist, mb.query.duration)
    if recording is not None:
        confidence = _recording_confidence(
            recording,
            query_title,
            query_artist,
            mb.query.duration,
        )
        if confidence >= 0.45:
            claims.append(
                _metadata_claim(
                    inputs.item_id,
                    artifact,
                    field="title",
                    value=recording.title,
                    confidence=confidence,
                    source=SOURCE_MUSICBRAINZ,
                    method=METHOD_MUSICBRAINZ,
                    notes="MusicBrainz recording title",
                )
            )
        artist_confidence = max(0.35, confidence - 0.05)
        for order, artist in enumerate(recording.artists):
            claims.append(
                _metadata_claim(
                    inputs.item_id,
                    artifact,
                    field="artist",
                    value=artist.name,
                    confidence=artist_confidence,
                    source=SOURCE_MUSICBRAINZ,
                    method=METHOD_MUSICBRAINZ,
                    role=artist.role or "main",
                    order=order,
                    notes="MusicBrainz artist credit",
                )
            )
            mbid = _valid_mbid(artist.mbid)
            if mbid is not None:
                claims.append(
                    _metadata_claim(
                        inputs.item_id,
                        artifact,
                        field="mb_artist_id",
                        value=mbid,
                        confidence=min(0.9, confidence + 0.05),
                        source=SOURCE_MUSICBRAINZ,
                        method=METHOD_MUSICBRAINZ,
                        role=artist.role or "main",
                        order=order,
                        notes="MusicBrainz artist MBID from artist credit",
                    )
                )
        recording_mbid = _valid_mbid(recording.mbid)
        if recording_mbid is not None:
            claims.append(
                _metadata_claim(
                    inputs.item_id,
                    artifact,
                    field="mb_recording_id",
                    value=recording_mbid,
                    confidence=min(0.95, confidence + 0.05),
                    source=SOURCE_MUSICBRAINZ,
                    method=METHOD_MUSICBRAINZ,
                    notes="MusicBrainz recording MBID",
                )
            )
        for isrc in recording.isrcs:
            normalized = _valid_isrc(isrc)
            if normalized is not None:
                claims.append(
                    _metadata_claim(
                        inputs.item_id,
                        artifact,
                        field="isrc",
                        value=normalized,
                        confidence=min(0.9, confidence + 0.05),
                        source=SOURCE_MUSICBRAINZ,
                        method=METHOD_MUSICBRAINZ,
                        notes="ISRC from MusicBrainz recording",
                    )
                )
        for order, genre in enumerate(recording.genres[:5]):
            claims.append(
                _metadata_claim(
                    inputs.item_id,
                    artifact,
                    field="genre",
                    value=genre,
                    confidence=0.35,
                    source=SOURCE_MUSICBRAINZ,
                    method=METHOD_MUSICBRAINZ,
                    order=order,
                    notes="MusicBrainz genre/tag",
                )
            )
        release = _choose_release(recording, mb.releases)
        if release is not None:
            claims.extend(
                _release_claims(
                    inputs,
                    artifact,
                    release,
                    confidence=max(0.4, confidence - 0.08),
                )
            )
        claims.extend(
            _musicbrainz_subject_claims(
                inputs,
                artifact,
                recording=recording,
                release=release,
                confidence=confidence,
            )
        )
    elif mb.releases:
        claims.extend(
            _release_claims(
                inputs,
                artifact,
                mb.releases[0],
                confidence=0.55,
            )
        )
        claims.extend(
            _musicbrainz_subject_claims(
                inputs,
                artifact,
                recording=None,
                release=mb.releases[0],
                confidence=0.55,
            )
        )
    return claims


def _release_claims(
    inputs: InterpreterInput,
    artifact: ArtifactPayload,
    release: MusicBrainzRelease | Any,
    *,
    confidence: float,
) -> list[Claim]:
    claims: list[Claim] = []
    title = getattr(release, "title", None)
    if isinstance(title, str) and title.strip():
        claims.append(
            _metadata_claim(
                inputs.item_id,
                artifact,
                field="album",
                value=title.strip(),
                confidence=min(0.85, confidence),
                source=SOURCE_MUSICBRAINZ,
                method=METHOD_MUSICBRAINZ,
                notes="MusicBrainz release title",
            )
        )
    for field, value, field_confidence in (
        ("date", getattr(release, "date", None), confidence),
        ("country", getattr(release, "country", None), 0.55),
        ("label", getattr(release, "label", None), 0.55),
        ("barcode", getattr(release, "barcode", None), 0.55),
    ):
        if isinstance(value, str) and value.strip():
            claims.append(
                _metadata_claim(
                    inputs.item_id,
                    artifact,
                    field=field,
                    value=value.strip(),
                    confidence=min(0.85, field_confidence),
                    source=SOURCE_MUSICBRAINZ,
                    method=METHOD_MUSICBRAINZ,
                    notes=f"MusicBrainz release {field}",
                )
            )
    release_mbid = _valid_mbid(getattr(release, "mbid", None))
    if release_mbid is not None:
        claims.append(
            _metadata_claim(
                inputs.item_id,
                artifact,
                field="mb_release_id",
                value=release_mbid,
                confidence=min(0.9, confidence + 0.05),
                source=SOURCE_MUSICBRAINZ,
                method=METHOD_MUSICBRAINZ,
                notes="MusicBrainz release MBID",
            )
        )
    group_mbid = _valid_mbid(getattr(release, "release_group_mbid", None))
    if group_mbid is not None:
        claims.append(
            _metadata_claim(
                inputs.item_id,
                artifact,
                field="mb_release_group_id",
                value=group_mbid,
                confidence=min(0.88, confidence),
                source=SOURCE_MUSICBRAINZ,
                method=METHOD_MUSICBRAINZ,
                notes="MusicBrainz release-group MBID",
            )
        )
    for order, artist in enumerate(getattr(release, "artists", []) or []):
        name = getattr(artist, "name", None)
        if isinstance(name, str) and name.strip():
            claims.append(
                _metadata_claim(
                    inputs.item_id,
                    artifact,
                    field="albumartist",
                    value=name.strip(),
                    confidence=max(0.35, confidence - 0.05),
                    source=SOURCE_MUSICBRAINZ,
                    method=METHOD_MUSICBRAINZ,
                    role="albumartist",
                    order=order,
                    notes="MusicBrainz release artist",
                )
            )
        mbid = _valid_mbid(getattr(artist, "mbid", None))
        if mbid is not None:
            claims.append(
                _metadata_claim(
                    inputs.item_id,
                    artifact,
                    field="mb_albumartist_id",
                    value=mbid,
                    confidence=max(0.4, confidence),
                    source=SOURCE_MUSICBRAINZ,
                    method=METHOD_MUSICBRAINZ,
                    role="albumartist",
                    order=order,
                    notes="MusicBrainz release artist MBID",
                )
            )
    return claims


def _musicbrainz_subject_claims(
    inputs: InterpreterInput,
    artifact: ArtifactPayload,
    *,
    recording: MusicBrainzRecording | None,
    release: MusicBrainzRelease | Any | None,
    confidence: float,
) -> list[Claim]:
    """Emit recording/release/artist claims in addition to item-facing claims.

    The resolver consumes item claims, while these subject-specific claims keep
    the provenance graph faithful and let later plans correlate identities
    without flattening recording, release, and artist evidence into one row.
    """

    claims: list[Claim] = []

    def add(
        subject: Subject,
        subject_id: str | None,
        field: str,
        value: FieldValue,
        claim_confidence: float,
        *,
        role: str | None = None,
        order: int | None = None,
        notes: str | None = None,
    ) -> None:
        if value is None:
            return
        claims.append(
            _make_claim(
                inputs.item_id,
                subject=subject,
                subject_id=subject_id,
                field=field,
                value=value,
                confidence=claim_confidence,
                source=SOURCE_MUSICBRAINZ,
                method=METHOD_MUSICBRAINZ,
                evidence=artifact.artifact_id,
                role=role,
                order=order,
                notes=notes,
            )
        )

    if recording is not None:
        recording_id = _valid_mbid(recording.mbid)
        add(
            Subject.RECORDING,
            recording_id,
            "title",
            recording.title,
            confidence,
            notes="MusicBrainz recording title",
        )
        if recording_id is not None:
            add(
                Subject.RECORDING,
                recording_id,
                "mbid",
                recording_id,
                min(0.95, confidence + 0.05),
                notes="MusicBrainz recording MBID",
            )
        for order, artist in enumerate(recording.artists):
            artist_id = _valid_mbid(artist.mbid)
            add(
                Subject.ARTIST,
                artist_id,
                "name",
                artist.name,
                max(0.35, confidence - 0.05),
                role=artist.role or "main",
                order=order,
                notes="MusicBrainz artist credit",
            )
            if artist_id is not None:
                add(
                    Subject.ARTIST,
                    artist_id,
                    "mbid",
                    artist_id,
                    min(0.9, confidence + 0.05),
                    role=artist.role or "main",
                    order=order,
                    notes="MusicBrainz artist MBID",
                )
            add(
                Subject.RECORDING,
                recording_id,
                "artist",
                artist.name,
                max(0.35, confidence - 0.05),
                role=artist.role or "main",
                order=order,
                notes="MusicBrainz recording artist credit",
            )
        for isrc in recording.isrcs:
            normalized = _valid_isrc(isrc)
            if normalized is not None:
                add(
                    Subject.RECORDING,
                    recording_id,
                    "isrc",
                    normalized,
                    min(0.9, confidence + 0.05),
                    notes="ISRC from MusicBrainz recording",
                )
    if release is not None:
        release_mbid = _valid_mbid(getattr(release, "mbid", None))
        title = getattr(release, "title", None)
        if isinstance(title, str) and title.strip():
            add(
                Subject.RELEASE,
                release_mbid,
                "title",
                title.strip(),
                min(0.85, confidence),
                notes="MusicBrainz release title",
            )
        for field in ("date", "country", "label", "barcode"):
            value = getattr(release, field, None)
            if isinstance(value, str) and value.strip():
                add(
                    Subject.RELEASE,
                    release_mbid,
                    field,
                    value.strip(),
                    min(0.8, confidence),
                    notes=f"MusicBrainz release {field}",
                )
        if release_mbid is not None:
            add(
                Subject.RELEASE,
                release_mbid,
                "mbid",
                release_mbid,
                min(0.9, confidence + 0.05),
                notes="MusicBrainz release MBID",
            )
    return claims


def _acoustid_claims(
    inputs: InterpreterInput,
    artifact: ArtifactPayload,
    payload: AcoustIdLookupPayload,
    existing: Sequence[Claim],
) -> list[Claim]:
    if not payload.candidates:
        return []
    candidate = max(payload.candidates, key=lambda item: item.score)
    claims: list[Claim] = []
    confidence = min(0.85, 0.25 + 0.6 * candidate.score)
    claims.append(
        _metadata_claim(
            inputs.item_id,
            artifact,
            field="acoustid",
            value=candidate.acoustid_id,
            confidence=confidence,
            source=SOURCE_ACOUSTID,
            method=METHOD_ACOUSTID,
            notes="AcoustID identifier",
        )
    )
    for mbid in candidate.recording_mbids:
        normalized = _valid_mbid(mbid)
        if normalized is None:
            continue
        claims.append(
            _metadata_claim(
                inputs.item_id,
                artifact,
                field="mb_recording_id",
                value=normalized,
                confidence=min(0.8, 0.2 + 0.65 * candidate.score),
                source=SOURCE_ACOUSTID,
                method=METHOD_ACOUSTID,
                notes="recording MBID from AcoustID candidate",
            )
        )
    has_mb_title = _value_text(existing, "title") is not None
    if not has_mb_title and candidate.title:
        claims.append(
            _metadata_claim(
                inputs.item_id,
                artifact,
                field="title",
                value=candidate.title,
                confidence=min(0.6, 0.2 + 0.5 * candidate.score),
                source=SOURCE_ACOUSTID,
                method=METHOD_ACOUSTID,
                notes="title from AcoustID candidate",
            )
        )
    if _value_text(existing, "artist") is None and candidate.artist:
        for order, credit in enumerate(parse_artists(candidate.artist)):
            claims.append(
                _metadata_claim(
                    inputs.item_id,
                    artifact,
                    field="artist",
                    value=credit.name,
                    confidence=min(0.55, 0.18 + 0.45 * candidate.score),
                    source=SOURCE_ACOUSTID,
                    method=METHOD_ACOUSTID,
                    role=credit.role,
                    order=order,
                    notes="artist from AcoustID candidate",
                )
            )
    return claims


def _cover_claims(
    inputs: InterpreterInput,
    artifact: ArtifactPayload,
    payload: CoverCandidatesPayload,
) -> list[Claim]:
    claims: list[Claim] = []
    ranked = sorted(
        payload.candidates,
        key=lambda item: (
            item.score,
            item.resolution,
            _cover_source_rank(item.source),
        ),
        reverse=True,
    )
    for candidate in ranked[:3]:
        value = candidate.artifact_ref or candidate.url
        if not value:
            continue
        confidence = min(0.9, 0.35 + 0.5 * candidate.score)
        if candidate.resolution >= 1000:
            confidence = min(0.92, confidence + 0.05)
        notes = f"cover candidate from {candidate.source}"
        if candidate.width and candidate.height:
            notes += f" ({candidate.width}x{candidate.height})"
        claims.append(
            _metadata_claim(
                inputs.item_id,
                artifact,
                field="artwork",
                value=value,
                confidence=confidence,
                source=_cover_source(candidate),
                method=METHOD_COVER,
                notes=notes,
            )
        )
        if candidate.release_mbid:
            claims.append(
                _make_claim(
                    inputs.item_id,
                    subject=Subject.RELEASE,
                    subject_id=candidate.release_mbid,
                    field="artwork",
                    value=value,
                    confidence=confidence,
                    source=_cover_source(candidate),
                    method=METHOD_COVER,
                    evidence=artifact.artifact_id,
                    notes=notes,
                )
            )
    return claims


def _merged_lyrics_claims(
    inputs: InterpreterInput,
    lyrics: LyricsCandidatesPayload | None,
    transcript: AsrTranscript | None,
    artifact: ArtifactPayload,
) -> tuple[list[Claim], list[str]]:
    """Merge provider lyrics with ASR once, then expose separate fields.

    ``plain`` and ``synced_lyrics`` are independent resolution fields; LRC is
    never packed into plain lyrics.
    """

    warnings: list[str] = list(lyrics.warnings) if lyrics is not None else []
    result = LyricsMerger().merge(
        lyrics.candidates if lyrics is not None else [],
        transcript,
        inputs.claims,
        _probe_duration(inputs),
        title=_claim_text(inputs.claims, "title"),
        artist=_claim_text(inputs.claims, "artist"),
    )
    warnings.extend(result.warnings)
    if result.plain is None and result.lrc is None:
        return [], list(dict.fromkeys(warnings))

    if result.source == "asr":
        source = SOURCE_ASR
        method = METHOD_ASR
    elif result.source == "lrclib":
        source = SOURCE_LRCLIB
        method = METHOD_LYRICS_MERGE
    else:
        source = SOURCE_LYRICS_MERGE
        method = METHOD_LYRICS_MERGE
    confidence = max(0.0, min(0.9, result.confidence))
    notes = (
        f"merged lyrics ({result.source}); timing={result.timing_source or 'none'}; "
        f"language={result.language or 'unknown'}"
    )
    claims: list[Claim] = []
    if result.plain:
        claims.append(
            _metadata_claim(
                inputs.item_id,
                artifact,
                field="lyrics",
                value=result.plain,
                confidence=confidence,
                source=source,
                method=method,
                notes=notes,
            )
        )
    if result.lrc:
        claims.append(
            _metadata_claim(
                inputs.item_id,
                artifact,
                field="synced_lyrics",
                value=result.lrc,
                confidence=confidence,
                source=source,
                method=method,
                notes=f"{notes}; LRC sidecar only",
            )
        )
    if result.elrc:
        claims.append(
            _metadata_claim(
                inputs.item_id,
                artifact,
                field="elrc",
                value=result.elrc,
                confidence=confidence * 0.9,
                source=source,
                method=method,
                notes=f"{notes}; ELRC sidecar only",
            )
        )
    if result.language:
        claims.append(
            _metadata_claim(
                inputs.item_id,
                artifact,
                field="language",
                value=result.language,
                confidence=max(0.35, min(0.8, confidence)),
                source=source,
                method=method,
                notes="language evidence from merged lyrics/ASR",
            )
        )
    return claims, list(dict.fromkeys(warnings))


def _claim_text(claims: Sequence[Claim], field: str) -> str | None:
    for claim in claims:
        if claim.field == field and isinstance(claim.value, str) and claim.value.strip():
            return claim.value.strip()
    return None


def _description_attribution(
    description: str | None,
    title: str | None,
) -> tuple[str, str | None] | None:
    if not description or not title:
        return None
    first_line = next((line.strip() for line in description.splitlines() if line.strip()), "")
    match = _ATTRIBUTION_RE.fullmatch(first_line)
    if match is None:
        return None
    parsed_title = match.group("title").strip(" .")
    if not parsed_title or parsed_title.casefold() not in title.casefold():
        return None
    artist = strip_topic_suffix(match.group("artist")).strip(" .")
    album = (match.group("album") or "").strip(" .") or None
    return (artist, album) if artist else None


def _probe_duration(inputs: InterpreterInput) -> float | None:
    probe = inputs.optional(PROBE_TYPE)
    if probe is None:
        return None
    try:
        payload = AudioProbePayload.model_validate_json(probe.payload)
    except Exception:
        return None
    return payload.duration_seconds


def _metadata_claim(
    item_id: ItemId,
    artifact: ArtifactPayload,
    *,
    field: str,
    value: FieldValue,
    confidence: float,
    source: str,
    method: str,
    role: str | None = None,
    order: int | None = None,
    notes: str | None = None,
) -> Claim:
    return _make_claim(
        item_id,
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


def _best_recording_for_query(
    mb: MusicBrainzSearchPayload,
    title: str | None,
    artist: str | None,
    duration: float | None,
) -> MusicBrainzRecording | None:
    if not mb.recordings:
        return None
    return max(
        mb.recordings,
        key=lambda item: (
            _recording_confidence(item, title, artist, duration),
            item.score or 0.0,
        ),
    )


def _recording_confidence(
    recording: MusicBrainzRecording,
    title: str | None,
    artist: str | None,
    duration: float | None,
) -> float:
    provider_score = recording.score if recording.score is not None else 0.6
    title_score = _similarity(title, recording.title) if title else 0.5
    artist_score = (
        max((_similarity(artist, item.name) for item in recording.artists), default=0.0)
        if artist
        else 0.5
    )
    duration_score = 0.5
    if duration is not None and recording.duration is not None:
        delta = abs(duration - recording.duration)
        duration_score = max(0.0, 1.0 - delta / 15.0)
    return _clamp(
        0.45 * provider_score + 0.25 * title_score + 0.15 * artist_score + 0.15 * duration_score
    )


def _choose_release(
    recording: MusicBrainzRecording,
    releases: Sequence[MusicBrainzRelease],
) -> MusicBrainzRelease | Any | None:
    by_mbid = {release.mbid: release for release in releases}
    for ref in recording.releases:
        if ref.mbid and ref.mbid in by_mbid:
            return by_mbid[ref.mbid]
    if recording.releases:
        return recording.releases[0]
    if releases:
        official = [item for item in releases if not item.status or item.status == "Official"]
        return (official or list(releases))[0]
    return None


def _identity_candidates(
    inputs: InterpreterInput,
    claims: Sequence[Claim],
) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    sources: dict[str, set[str]] = {}

    def add(
        target_id: str,
        *,
        score: float,
        source: str,
        claim_ids: Sequence[ClaimId] = (),
        evidence: Sequence[str] = (),
        external: Mapping[str, str] | None = None,
    ) -> None:
        target = _valid_mbid(target_id)
        if target is None:
            return
        current = candidates.get(target)
        if current is None:
            candidates[target] = {
                "target_id": target,
                "score": _clamp(score),
                "claim_ids": list(claim_ids),
                "evidence": list(evidence),
                "external_ids": dict(external or {}),
            }
            sources[target] = {source}
            return
        current["score"] = max(float(current["score"]), _clamp(score))
        current["claim_ids"] = list(dict.fromkeys([*current["claim_ids"], *claim_ids]))
        current["evidence"] = list(dict.fromkeys([*current["evidence"], *evidence]))
        if external:
            current["external_ids"].update(external)
        sources[target].add(source)

    for claim in claims:
        if claim.field in {"mb_recording_id", "mb_recording"}:
            mbid = _valid_mbid(claim.value)
            if mbid is not None:
                add(
                    mbid,
                    score=float(claim.confidence),
                    source=claim.source,
                    claim_ids=(claim.id,),
                    evidence=(claim.evidence[0] if claim.evidence else str(claim.id),),
                    external={"musicbrainz_recording": mbid},
                )
    mb_artifact = inputs.optional(MUSICBRAINZ_TYPE)
    if mb_artifact is not None:
        try:
            mb = mb_artifact.model(MusicBrainzSearchPayload)
        except ObservationFailed:
            mb = None
        if mb is not None:
            title = mb.query.title or _value_text(claims, "title")
            artist = mb.query.artist or _value_text(claims, "artist")
            for recording in mb.recordings:
                confidence = _recording_confidence(
                    recording,
                    title,
                    artist,
                    mb.query.duration,
                )
                external = {"musicbrainz_recording": recording.mbid}
                if recording.isrcs:
                    isrc = _valid_isrc(recording.isrcs[0])
                    if isrc is not None:
                        external["isrc"] = isrc
                add(
                    recording.mbid,
                    score=confidence,
                    source=SOURCE_MUSICBRAINZ,
                    evidence=(mb_artifact.artifact_id,),
                    external=external,
                )
    acoustid_artifact = inputs.optional(ACOUSTID_TYPE)
    if acoustid_artifact is not None:
        try:
            acoustid = acoustid_artifact.model(AcoustIdLookupPayload)
        except ObservationFailed:
            acoustid = None
        if acoustid is not None:
            for candidate in acoustid.candidates:
                for mbid in candidate.recording_mbids:
                    add(
                        mbid,
                        score=candidate.score * 0.85,
                        source=SOURCE_ACOUSTID,
                        evidence=(acoustid_artifact.artifact_id,),
                        external={"acoustid": candidate.acoustid_id},
                    )
    for target, source_set in sources.items():
        if len(source_set) > 1:
            candidates[target]["score"] = _clamp(
                float(candidates[target]["score"]) + 0.05 * (len(source_set) - 1),
                maximum=0.95,
            )
    return sorted(
        candidates.values(),
        key=lambda item: (float(item["score"]), item["target_id"]),
        reverse=True,
    )


def _cover_source(candidate: CoverCandidate) -> str:
    if candidate.source in {"cover_art_archive", "itunes", "deezer"}:
        return candidate.source
    if candidate.source in {"source_thumbnail", "thumbnail"}:
        return "source_thumbnail"
    return SOURCE_COVER_ART_ARCHIVE


def _cover_source_rank(source: str) -> int:
    return {
        "cover_art_archive": 4,
        "itunes": 3,
        "deezer": 2,
        "source_thumbnail": 1,
        "thumbnail": 1,
    }.get(source, 0)


def _normalize_upload_date(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    if len(text) == 6 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}"
    if len(text) == 4 and text.isdigit():
        return text
    return text or None


initial_tags_interpreter = InitialTagsInterpreter()
source_metadata_interpreter = SourceMetadataInterpreter()
kind_classifier = KindClassifier()
derivative_detector = DerivativeDetector()
metadata_interpreter = MetadataInterpreter()
identity_resolver = IdentityResolver()

DEFAULT_INTERPRETERS: tuple[Interpreter, ...] = (
    initial_tags_interpreter,
    source_metadata_interpreter,
    kind_classifier,
    derivative_detector,
    metadata_interpreter,
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
            if item.status not in {
                ItemStatus.OBSERVED,
                ItemStatus.INTERPRETED,
                ItemStatus.NEEDS_REVIEW,
                ItemStatus.RESOLVED,
                ItemStatus.RENDERED,
                ItemStatus.PUBLISHED,
            }:
                raise InvalidStateTransition(
                    f"cannot interpret an item in status {item.status.value}",
                    user_action="Acquire and observe the item before interpreting it.",
                    context={"item_id": item_id, "status": item.status.value},
                )

            artifacts = await self._load_artifacts(uow, item, observations)
            if INITIAL_TAGS_TYPE not in artifacts:
                if SOURCE_METADATA_TYPE in artifacts:
                    # URL sources can lack a tag-readable container (for
                    # example WebM/Opus); normalized source metadata is a
                    # sufficient fallback and the degradation stays visible.
                    pass
                else:
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
            if INITIAL_TAGS_TYPE not in artifacts:
                warnings.append("missing_artifact:initial.tags.v1")
            accumulated = list(existing)
            identity_payload: Mapping[str, Any] | None = None
            derivative_payload: Mapping[str, Any] | None = None

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
                elif result.artifact_type == DERIVATIVE_HYPOTHESES_ARTIFACT:
                    derivative_payload = result.payload
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
        if derivative_payload is None:
            derivative_payload = _empty_derivative_payload(item_id)
        return InterpretationView(
            item_id=item_id,
            status=status,
            claims=tuple(produced) if produced else tuple(existing),
            identity=identity_payload,
            derivative=derivative_payload,
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
            SOURCE_METADATA_TYPE,
            str(FINGERPRINT_ARTIFACT),
            str(ACOUSTID_ARTIFACT),
            str(MUSICBRAINZ_ARTIFACT),
            str(COVER_ARTIFACT),
            str(LYRICS_ARTIFACT),
            str(ASR_TRANSCRIPT_ARTIFACT),
            STEMS_TYPE,
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
    subject_id: str | None = None,
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
        subject_id=subject_id,
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
    *,
    evidence: tuple[str, ...] = (),
) -> dict[str, Any]:
    combined = list(dict.fromkeys([*evidence, *(str(item) for item in claim_ids)]))
    return {
        "target_type": target_type,
        "target_id": target_id,
        "score": min(1.0, max(0.0, score)),
        "external_ids": dict(external_ids),
        "evidence": combined,
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


def _empty_derivative_payload(item_id: ItemId) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "item_id": str(item_id),
        "status": "none",
        "hypotheses": [],
        "warnings": ["no_derivative_evidence"],
    }


__all__ = [
    "CLAIMS_ARTIFACT",
    "DEFAULT_INTERPRETERS",
    "DERIVATIVE_HYPOTHESES_ARTIFACT",
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
    "MetadataInterpreter",
    "MissingArtifactError",
    "SourceMetadataInterpreter",
    "ObservationSource",
    "derivative_detector",
    "identity_resolver",
    "initial_tags_interpreter",
    "kind_classifier",
    "metadata_interpreter",
    "source_metadata_interpreter",
]

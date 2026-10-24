"""Capability adapters that wrap metadata providers for the pipeline.

Providers are pure network/search adapters.  These observers decode the input
artifacts, call one provider, and emit a normalized observation artifact with
an ``expires_at`` timestamp.  Network failures become structured ``skipped``
or ``degraded`` results; they never fail the whole observation run.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from trackforge.adapters.analysis.chromaprint import ChromaprintObserver
from trackforge.adapters.http.client import HttpClient
from trackforge.adapters.metadata.acoustid import AcoustIdProvider
from trackforge.adapters.metadata.coverart import (
    CoverArtArchiveProvider,
    DeezerProvider,
    ItunesProvider,
)
from trackforge.adapters.metadata.lrclib import LrclibProvider
from trackforge.adapters.metadata.musicbrainz import MusicBrainzProvider
from trackforge.application.dto.observations import (
    AudioProbePayload,
    InitialTagsPayload,
    SourceMetadataPayload,
)
from trackforge.application.dto.providers import (
    AudioFingerprintPayload,
    CoverCandidate,
    CoverCandidatesPayload,
    LyricsCandidatesPayload,
    MetadataQuery,
    MusicBrainzRecording,
    MusicBrainzSearchPayload,
    ProviderObservation,
    ttl_expiry,
)
from trackforge.application.ports.execution import (
    ArtifactDraft,
    StepContext,
    StepResult,
    StepWarning,
)
from trackforge.application.ports.observations import (
    ACOUSTID_ARTIFACT,
    COVER_ARTIFACT,
    FINGERPRINT_ARTIFACT,
    INITIAL_TAGS_ARTIFACT,
    LYRICS_ARTIFACT,
    MUSICBRAINZ_ARTIFACT,
    PROBE_ARTIFACT,
    SOURCE_METADATA_ARTIFACT,
)
from trackforge.application.ports.storage import ContentAddressedStore
from trackforge.domain.errors import (
    OfflineModeError,
    ProviderUnavailable,
    RateLimited,
    TrackForgeError,
    UnsafeUrl,
)
from trackforge.domain.model.capabilities import (
    CapabilityKind,
    CapabilityManifest,
    Cost,
    Determinism,
    SideEffect,
)

_OFFLINE = "offline"
_COVER_TTL = timedelta(days=7)


def _config(ctx: StepContext, key: str) -> Any:
    if key in ctx.config_subset:
        return ctx.config_subset[key]
    return ctx.params.get(key)


def _offline(ctx: StepContext) -> bool:
    return bool(_config(ctx, "offline"))


def _warning(
    code: str,
    message: str,
    *,
    capability_id: str,
    provider: str | None = None,
    retryable: bool = False,
    user_action: str | None = None,
) -> StepWarning:
    return StepWarning(
        code=code,
        message=message,
        capability_id=capability_id,
        provider=provider,
        retryable=retryable,
        user_action=user_action,
    )


def _offline_result(capability_id: str, provider: str) -> StepResult:
    return StepResult.skipped(
        warnings=(
            _warning(
                _OFFLINE,
                f"{provider} was skipped because offline mode is enabled",
                capability_id=capability_id,
                provider=provider,
                user_action="Remove --offline or disable offline mode to use network providers.",
            ),
        )
    )


def _provider_failure(
    error: TrackForgeError,
    *,
    capability_id: str,
    provider: str,
) -> StepResult:
    if isinstance(error, RateLimited):
        code = "rate_limited"
    elif isinstance(error, OfflineModeError):
        code = _OFFLINE
    elif isinstance(error, UnsafeUrl):
        code = "unsafe_url"
    else:
        code = "provider_unavailable"
    return StepResult.skipped(
        warnings=(
            _warning(
                code,
                str(error),
                capability_id=capability_id,
                provider=provider,
                retryable=error.retryable,
                user_action=error.user_action,
            ),
        )
    )


def _decode(payload: bytes, model: Any) -> Any:
    return model.model_validate_json(payload)


def _draft(
    artifact_type: Any,
    observation: ProviderObservation,
    *,
    warnings: Sequence[str] = (),
    expires_at: Any = None,
    degraded: bool = False,
) -> tuple[ArtifactDraft, StepResult]:
    codes = list(dict.fromkeys([*warnings, *observation.warnings]))
    warning_objects = tuple(StepWarning(code=code, message=code) for code in codes)
    draft = ArtifactDraft(
        type=artifact_type,
        payload=observation.payload,
        params={"provider": observation.source},
        warnings=warning_objects,
        expires_at=expires_at or observation.expires_at,
    )
    actual_cost = Cost(cpu_seconds=0.05, memory_mb=16.0, network_calls=1)
    if warning_objects or degraded:
        return draft, StepResult.degraded(
            artifacts=(draft,),
            warnings=warning_objects,
            actual_cost=actual_cost,
        )
    return draft, StepResult(artifacts=(draft,), actual_cost=actual_cost)


class _MetadataObserver:
    """Shared manifest/error plumbing for network metadata observers."""

    manifest: CapabilityManifest

    def _skip(
        self,
        code: str,
        message: str,
        *,
        provider: str | None = None,
        user_action: str | None = None,
        retryable: bool = False,
    ) -> StepResult:
        return StepResult.skipped(
            warnings=(
                _warning(
                    code,
                    message,
                    capability_id=self.manifest.id,
                    provider=provider,
                    retryable=retryable,
                    user_action=user_action,
                ),
            )
        )


class MusicBrainzObserver(_MetadataObserver):
    """Search MusicBrainz using existing tags and measured duration."""

    manifest = CapabilityManifest(
        id="musicbrainz_search",
        version="1",
        kind=CapabilityKind.OBSERVER,
        requires=(PROBE_ARTIFACT, INITIAL_TAGS_ARTIFACT),
        provides=(MUSICBRAINZ_ARTIFACT,),
        cost=Cost(cpu_seconds=0.2, memory_mb=32.0, network_calls=1),
        determinism=Determinism.NON_DETERMINISTIC,
        cacheable=True,
        side_effects=SideEffect.NETWORK,
        rate_limit="musicbrainz:1/s",
        config_schema={
            "type": "object",
            "properties": {"offline": {"type": "boolean"}},
        },
        description="Search MusicBrainz for a recording matching tags and duration.",
    )

    def __init__(
        self,
        provider: MusicBrainzProvider | None = None,
        *,
        client: HttpClient | None = None,
        contact: str | None = None,
        owns_provider: bool | None = None,
    ) -> None:
        self._provider = provider or MusicBrainzProvider(client=client, contact=contact)
        self._owns_provider = owns_provider if owns_provider is not None else provider is None

    async def run(self, ctx: StepContext) -> StepResult:
        if _offline(ctx):
            return _offline_result(self.manifest.id, "musicbrainz")
        query = self._query(ctx)
        if query is None:
            return self._skip(
                "no_query",
                "MusicBrainz needs at least a title or artist to search",
                provider="musicbrainz",
                user_action="Provide tags or source metadata with a usable title/artist.",
            )
        try:
            observations = await self._provider.search(query)
        except (ProviderUnavailable, RateLimited, OfflineModeError, UnsafeUrl) as exc:
            return _provider_failure(exc, capability_id=self.manifest.id, provider="musicbrainz")
        except ValueError as exc:
            return self._skip(
                "invalid_output",
                f"MusicBrainz response could not be parsed: {exc}",
                provider="musicbrainz",
            )
        finally:
            if self._owns_provider:
                await self._provider.aclose()
        if not observations:
            return self._skip("no_data", "MusicBrainz returned no data", provider="musicbrainz")
        return _draft(MUSICBRAINZ_ARTIFACT, observations[0])[1]

    def _query(self, ctx: StepContext) -> MetadataQuery | None:
        title: str | None = None
        artist: str | None = None
        album: str | None = None
        duration: float | None = None
        tags_step = ctx.inputs.get(INITIAL_TAGS_ARTIFACT)
        if tags_step is not None:
            try:
                tags = _decode(tags_step.payload, InitialTagsPayload).tags
            except ValueError:
                tags = None
            if tags is not None:
                title = _clean(tags.title)
                artist = _clean(", ".join(tags.artist)) if tags.artist else None
                album = _clean(tags.album)
        if title is None or artist is None:
            source_step = ctx.inputs.get(SOURCE_METADATA_ARTIFACT)
            if source_step is not None:
                try:
                    source = _decode(source_step.payload, SourceMetadataPayload)
                except ValueError:
                    source = None
                if source is not None:
                    title = title or _clean(source.title)
                    artist = artist or _clean(source.artist) or _clean(source.uploader)
                    album = album or _clean(source.album)
                    duration = source.duration_seconds or source.duration
        probe_step = ctx.inputs.get(PROBE_ARTIFACT)
        if probe_step is not None and duration is None:
            try:
                probe = _decode(probe_step.payload, AudioProbePayload)
            except ValueError:
                probe = None
            if probe is not None:
                duration = probe.duration_seconds
                if duration is None and probe.first_audio_stream() is not None:
                    stream = probe.first_audio_stream()
                    duration = stream.duration_seconds if stream is not None else None
        if title is None and artist is None:
            return None
        return MetadataQuery(title=title, artist=artist, album=album, duration=duration)


class AcoustIdObserver(_MetadataObserver):
    """Look up a Chromaprint fingerprint through AcoustID."""

    manifest = CapabilityManifest(
        id="acoustid_lookup",
        version="1",
        kind=CapabilityKind.OBSERVER,
        requires=(FINGERPRINT_ARTIFACT,),
        provides=(ACOUSTID_ARTIFACT,),
        cost=Cost(cpu_seconds=0.2, memory_mb=32.0, network_calls=1),
        determinism=Determinism.NON_DETERMINISTIC,
        cacheable=True,
        side_effects=SideEffect.NETWORK,
        rate_limit="acoustid:3/s",
        config_schema={
            "type": "object",
            "properties": {"offline": {"type": "boolean"}},
        },
        description="Resolve a Chromaprint fingerprint to recording MBIDs with AcoustID.",
    )

    def __init__(
        self,
        provider: AcoustIdProvider | None = None,
        *,
        client: HttpClient | None = None,
        api_key: str | None = None,
        owns_provider: bool | None = None,
    ) -> None:
        self._provider = provider or AcoustIdProvider(client=client, api_key=api_key)
        self._owns_provider = owns_provider if owns_provider is not None else provider is None

    async def run(self, ctx: StepContext) -> StepResult:
        if _offline(ctx):
            return _offline_result(self.manifest.id, "acoustid")
        if not self._provider.api_key_configured:
            return self._skip(
                "missing_api_key",
                "AcoustID API key is not configured; fingerprint lookup is skipped",
                provider="acoustid",
                user_action="Set TRACKFORGE_ACOUSTID_API_KEY to enable AcoustID.",
            )
        fingerprint_step = ctx.inputs.get(FINGERPRINT_ARTIFACT)
        if fingerprint_step is None:
            return self._skip(
                "missing_fingerprint",
                "AcoustID requires audio.fingerprint.v1",
                provider="acoustid",
            )
        try:
            payload = _decode(fingerprint_step.payload, AudioFingerprintPayload)
        except ValueError:
            return self._skip(
                "invalid_fingerprint",
                "audio.fingerprint.v1 payload is invalid",
                provider="acoustid",
            )
        try:
            observations = await self._provider.search(
                MetadataQuery(
                    fingerprint=payload.fingerprint,
                    duration=payload.duration_seconds,
                )
            )
        except (ProviderUnavailable, RateLimited, OfflineModeError, UnsafeUrl) as exc:
            return _provider_failure(exc, capability_id=self.manifest.id, provider="acoustid")
        except ValueError as exc:
            return self._skip(
                "invalid_output",
                f"AcoustID response could not be parsed: {exc}",
                provider="acoustid",
            )
        finally:
            if self._owns_provider:
                await self._provider.aclose()
        if not observations:
            return self._skip("no_data", "AcoustID returned no candidates", provider="acoustid")
        return _draft(ACOUSTID_ARTIFACT, observations[0])[1]


class CoverArtObserver(_MetadataObserver):
    """Collect cover candidates from CAA, iTunes, Deezer, and source metadata."""

    manifest = CapabilityManifest(
        id="cover_search",
        version="1",
        kind=CapabilityKind.OBSERVER,
        requires=(MUSICBRAINZ_ARTIFACT,),
        provides=(COVER_ARTIFACT,),
        cost=Cost(cpu_seconds=0.2, memory_mb=32.0, network_calls=1),
        determinism=Determinism.NON_DETERMINISTIC,
        cacheable=True,
        side_effects=SideEffect.NETWORK,
        rate_limit="coverart:2/s",
        config_schema={
            "type": "object",
            "properties": {"offline": {"type": "boolean"}},
        },
        description="Find front-cover candidates, preferring the Cover Art Archive.",
    )

    def __init__(
        self,
        *,
        client: HttpClient | None = None,
        cas: ContentAddressedStore | None = None,
        fetch_images: bool = False,
        providers: Sequence[Any] | None = None,
    ) -> None:
        if providers is not None:
            self._providers = list(providers)
        else:
            self._providers = [
                CoverArtArchiveProvider(client, cas=cas, fetch_images=fetch_images),
                ItunesProvider(client, cas=cas, fetch_images=fetch_images),
                DeezerProvider(client, cas=cas, fetch_images=fetch_images),
            ]

    async def run(self, ctx: StepContext) -> StepResult:
        if _offline(ctx):
            return _offline_result(self.manifest.id, "cover_art_archive")
        mb_step = ctx.inputs.get(MUSICBRAINZ_ARTIFACT)
        mb: MusicBrainzSearchPayload | None = None
        warnings: list[str] = []
        if mb_step is not None:
            try:
                mb = _decode(mb_step.payload, MusicBrainzSearchPayload)
            except ValueError:
                warnings.append("invalid_metadata")
        recording = _best_recording(mb) if mb is not None else None
        release = _best_release(mb) if mb is not None else None
        title = recording.title if recording is not None else None
        artist = (
            ", ".join(credit.name for credit in recording.artists)
            if recording is not None
            else None
        )
        album = release.title if release is not None else None
        duration = recording.duration if recording is not None else None
        tags_step = ctx.inputs.get(INITIAL_TAGS_ARTIFACT)
        if tags_step is not None:
            try:
                tags = _decode(tags_step.payload, InitialTagsPayload).tags
            except ValueError:
                tags = None
            if tags is not None:
                title = title or _clean(tags.title)
                artist = artist or (_clean(", ".join(tags.artist)) if tags.artist else None)
                album = album or _clean(tags.album)
        source_step = ctx.inputs.get(SOURCE_METADATA_ARTIFACT)
        source: SourceMetadataPayload | None = None
        if source_step is not None:
            try:
                source = _decode(source_step.payload, SourceMetadataPayload)
            except ValueError:
                source = None
            if source is not None:
                title = title or _clean(source.title)
                artist = artist or _clean(source.artist) or _clean(source.uploader)
                album = album or _clean(source.album)
                duration = duration or source.duration_seconds or source.duration
        query = MetadataQuery(
            title=title,
            artist=artist,
            album=album,
            duration=duration,
            recording_mbid=recording.mbid if recording is not None else None,
            release_mbid=(release.mbid if release is not None else None)
            or _first_release_mbid(recording),
            release_group_mbid=(release.release_group_mbid if release is not None else None),
        )
        candidates: list[CoverCandidate] = []
        for provider in self._providers:
            try:
                observations = await provider.search(query)
            except (ProviderUnavailable, RateLimited, OfflineModeError, UnsafeUrl, ValueError):
                warnings.append(
                    f"cover_provider_unavailable:{getattr(provider, 'source', 'unknown')}"
                )
                continue
            for observation in observations:
                warnings.extend(observation.warnings)
                try:
                    payload = CoverCandidatesPayload.model_validate(observation.payload)
                except ValueError:
                    continue
                candidates.extend(payload.candidates)
            if candidates:
                break
        # Source thumbnails are handled by ``SourceCoverObserver`` so their
        # bytes are stored in CAS.  Keeping URL-only candidates here would
        # prevent rendering when no CAA/iTunes/Deezer cover exists.
        if not candidates:
            return self._skip(
                "no_data",
                "no cover candidates were found",
                provider="cover_art_archive",
                user_action="Provide a release MBID or source artwork, or try again later.",
            )
        payload = CoverCandidatesPayload(
            candidates=candidates,
            warnings=list(dict.fromkeys(warnings)),
        )
        observation = ProviderObservation(
            type=str(COVER_ARTIFACT),
            payload=payload.model_dump(mode="json"),
            source="cover_art_archive",
            provider_version="1",
            warnings=tuple(dict.fromkeys(warnings)),
            expires_at=ttl_expiry(_COVER_TTL),
        )
        return _draft(COVER_ARTIFACT, observation, degraded=bool(warnings))[1]


class LrclibObserver(_MetadataObserver):
    """Fetch plain/LRC lyrics candidates from LRCLIB."""

    manifest = CapabilityManifest(
        id="lrclib_search",
        version="1",
        kind=CapabilityKind.OBSERVER,
        requires=(PROBE_ARTIFACT, INITIAL_TAGS_ARTIFACT),
        provides=(LYRICS_ARTIFACT,),
        cost=Cost(cpu_seconds=0.2, memory_mb=32.0, network_calls=1),
        determinism=Determinism.NON_DETERMINISTIC,
        cacheable=True,
        side_effects=SideEffect.NETWORK,
        rate_limit="lrclib:2/s",
        config_schema={
            "type": "object",
            "properties": {"offline": {"type": "boolean"}},
        },
        description="Find plain and synced lyrics candidates on LRCLIB.",
    )

    def __init__(
        self,
        provider: LrclibProvider | None = None,
        *,
        client: HttpClient | None = None,
        owns_provider: bool | None = None,
    ) -> None:
        self._provider = provider or LrclibProvider(client=client)
        self._owns_provider = owns_provider if owns_provider is not None else provider is None

    async def run(self, ctx: StepContext) -> StepResult:
        if _offline(ctx):
            return _offline_result(self.manifest.id, "lrclib")
        query = self._query(ctx)
        if query is None:
            return self._skip(
                "no_query",
                "LRCLIB needs a title and artist to search",
                provider="lrclib",
                user_action="Provide tags or source metadata with a usable title/artist.",
            )
        try:
            observations = await self._provider.search(query)
        except (ProviderUnavailable, RateLimited, OfflineModeError, UnsafeUrl) as exc:
            return _provider_failure(exc, capability_id=self.manifest.id, provider="lrclib")
        except ValueError as exc:
            return self._skip(
                "invalid_output",
                f"LRCLIB response could not be parsed: {exc}",
                provider="lrclib",
            )
        finally:
            if self._owns_provider:
                await self._provider.aclose()
        if not observations:
            return self._skip("no_data", "LRCLIB returned no lyrics", provider="lrclib")
        observation = observations[0]
        try:
            payload = LyricsCandidatesPayload.model_validate(observation.payload)
        except ValueError:
            return self._skip(
                "invalid_output",
                "LRCLIB returned an invalid lyrics payload",
                provider="lrclib",
            )
        if not payload.candidates:
            return self._skip(
                "no_match",
                "LRCLIB has no matching lyrics",
                provider="lrclib",
                user_action="Try another source or enable ASR in a later plan.",
            )
        return _draft(
            LYRICS_ARTIFACT,
            observation,
            warnings=payload.warnings,
            degraded=bool(observation.warnings or payload.warnings),
        )[1]

    def _query(self, ctx: StepContext) -> MetadataQuery | None:
        title: str | None = None
        artist: str | None = None
        album: str | None = None
        tags_step = ctx.inputs.get(INITIAL_TAGS_ARTIFACT)
        if tags_step is not None:
            try:
                tags = _decode(tags_step.payload, InitialTagsPayload).tags
            except ValueError:
                tags = None
            if tags is not None:
                title = _clean(tags.title)
                artist = _clean(", ".join(tags.artist)) if tags.artist else None
                album = _clean(tags.album)
        if not title or not artist:
            source_step = ctx.inputs.get(SOURCE_METADATA_ARTIFACT)
            if source_step is not None:
                try:
                    source = _decode(source_step.payload, SourceMetadataPayload)
                except ValueError:
                    source = None
                if source is not None:
                    title = title or _clean(source.title)
                    artist = artist or _clean(source.artist) or _clean(source.uploader)
                    album = album or _clean(source.album)
        if not title or not artist:
            return None
        duration: float | None = None
        probe_step = ctx.inputs.get(PROBE_ARTIFACT)
        if probe_step is not None:
            try:
                probe = _decode(probe_step.payload, AudioProbePayload)
            except ValueError:
                probe = None
            if probe is not None:
                duration = probe.duration_seconds
        if duration is None:
            fingerprint_step = ctx.inputs.get(FINGERPRINT_ARTIFACT)
            if fingerprint_step is not None:
                try:
                    duration = _decode(
                        fingerprint_step.payload, AudioFingerprintPayload
                    ).duration_seconds
                except ValueError:
                    duration = None
        return MetadataQuery(title=title, artist=artist, album=album, duration=duration)


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _best_recording(mb: MusicBrainzSearchPayload) -> Any | None:
    if not mb.recordings:
        return None
    return max(
        mb.recordings,
        key=lambda item: (item.score if item.score is not None else 0.0, item.duration or 0.0),
    )


def _best_release(mb: MusicBrainzSearchPayload) -> Any | None:
    if not mb.releases:
        return None
    official = [item for item in mb.releases if not item.status or item.status == "Official"]
    return (official or list(mb.releases))[0]


def _first_release_mbid(recording: MusicBrainzRecording | None) -> str | None:
    if recording is None:
        return None
    for release in recording.releases:
        if release.mbid:
            return release.mbid
    return None


# Compatibility names used by composition roots and tests.
CoverArtCapability = CoverArtObserver
LyricsCapability = LrclibObserver


__all__ = [
    "AcoustIdObserver",
    "ChromaprintObserver",
    "CoverArtCapability",
    "CoverArtObserver",
    "LrclibObserver",
    "LyricsCapability",
    "MusicBrainzObserver",
]

"""MusicBrainz search and lookup provider (HTTP, no ``musicbrainzngs``).

The provider talks to the public MusicBrainz web service through the shared
:class:`~trackforge.adapters.http.client.HttpClient`.  MusicBrainz requires a
descriptive ``User-Agent`` with a contact; when none is configured the
provider fails closed with a non-retryable :class:`ProviderUnavailable`.

Only normalized, useful fields enter the observation payload.  Invalid MBIDs
and ISRCs are dropped from that payload and surfaced as warnings.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from trackforge.adapters.http.client import HttpClient
from trackforge.adapters.metadata.common import (
    clean_text,
    duration_from_millis,
    mapping,
    safe_int,
    score_fraction,
    sequence,
    valid_isrcs,
    valid_mbid,
)
from trackforge.application.dto.providers import (
    MetadataQuery,
    MusicBrainzArtist,
    MusicBrainzArtistResult,
    MusicBrainzQuery,
    MusicBrainzRecording,
    MusicBrainzRelease,
    MusicBrainzReleaseRef,
    MusicBrainzSearchPayload,
    ProviderObservation,
    ttl_expiry,
)
from trackforge.domain.errors import ProviderUnavailable

DEFAULT_BASE_URL = "https://musicbrainz.org/ws/2"
DEFAULT_TTL = timedelta(days=7)
PROVIDER_VERSION = "1"

_LUCENE_SPECIAL = re.compile(r'([+\-!(){}\[\]^"~*?:\\/])')
_ENTITY_PATHS = {
    "recording": "recording",
    "release": "release",
    "release-group": "release-group",
    "release_group": "release-group",
    "artist": "artist",
    "work": "work",
}


def escape_lucene(value: str) -> str:
    """Escape a user value for a MusicBrainz Lucene query."""

    return _LUCENE_SPECIAL.sub(r"\\\1", value.strip())


def build_search_query(query: MetadataQuery) -> str:
    """Build a conservative MusicBrainz Lucene query."""

    parts: list[str] = []
    if query.title:
        parts.append(f'recording:"{escape_lucene(query.title)}"')
    if query.artist:
        parts.append(f'artist:"{escape_lucene(query.artist)}"')
    if query.album:
        parts.append(f'release:"{escape_lucene(query.album)}"')
    if query.duration is not None:
        millis = max(0, int(round(query.duration * 1000)))
        parts.append(f"dur:[{max(0, millis - 2000)} TO {millis + 2000}]")
    if query.recording_mbid:
        parts.append(f"rid:{escape_lucene(query.recording_mbid)}")
    return " AND ".join(parts) if parts else "*"


def _artist_credit(value: object) -> list[MusicBrainzArtist]:
    credits = sequence(value)
    result: list[MusicBrainzArtist] = []
    for raw in credits:
        item = mapping(raw)
        artist = mapping(item.get("artist"))
        name = clean_text(artist.get("name")) or clean_text(item.get("name"))
        if name is None:
            continue
        mbid = valid_mbid(artist.get("id"))
        result.append(
            MusicBrainzArtist(
                mbid=mbid,
                name=name,
                sort_name=clean_text(artist.get("sort-name")),
                join_phrase=clean_text(item.get("joinphrase")),
                role="main" if not item.get("joinphrase") else "featured",
            )
        )
    return result


def _genres(document: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("genres", "tags"):
        for raw in sequence(document.get(key)):
            item = mapping(raw)
            name = clean_text(item.get("name"))
            if name is not None and name not in values:
                values.append(name)
    return values


def _release_ref(document: Mapping[str, Any]) -> MusicBrainzReleaseRef | None:
    item = mapping(document)
    title = clean_text(item.get("title"))
    if title is None:
        return None
    group = mapping(item.get("release-group"))
    label = None
    for raw_info in sequence(item.get("label-info")):
        info = mapping(raw_info)
        candidate = clean_text(mapping(info.get("label")).get("name")) or clean_text(
            info.get("label")
        )
        if candidate is not None:
            label = candidate
            break
    media = sequence(item.get("media"))
    track_count = None
    if media:
        tracks = sequence(mapping(media[0]).get("tracks"))
        track_count = len(tracks) or safe_int(mapping(media[0]).get("track-count"))
    return MusicBrainzReleaseRef(
        mbid=valid_mbid(item.get("id")),
        title=title,
        date=clean_text(item.get("date")),
        country=clean_text(item.get("country")),
        label=label,
        barcode=clean_text(item.get("barcode")),
        release_group=clean_text(group.get("title")),
        release_group_mbid=valid_mbid(group.get("id")),
        status=clean_text(item.get("status")),
        track_count=track_count,
    )


def _recording(document: Mapping[str, Any]) -> MusicBrainzRecording | None:
    item = mapping(document)
    mbid = valid_mbid(item.get("id"))
    title = clean_text(item.get("title"))
    if mbid is None or title is None:
        return None
    releases: list[MusicBrainzReleaseRef] = []
    for raw in sequence(item.get("releases")):
        release = _release_ref(mapping(raw))
        if release is not None:
            releases.append(release)
    return MusicBrainzRecording(
        mbid=mbid,
        title=title,
        artists=_artist_credit(item.get("artist-credit")),
        duration=duration_from_millis(item.get("length")),
        isrcs=valid_isrcs(sequence(item.get("isrcs"))),
        releases=releases,
        genres=_genres(item),
        score=score_fraction(item.get("score")) if item.get("score") is not None else None,
    )


def _release(document: Mapping[str, Any]) -> MusicBrainzRelease | None:
    item = mapping(document)
    mbid = valid_mbid(item.get("id"))
    title = clean_text(item.get("title"))
    if mbid is None or title is None:
        return None
    ref = _release_ref(item)
    recordings: list[MusicBrainzRecording] = []
    for medium in sequence(item.get("media")):
        for track in sequence(mapping(medium).get("tracks")):
            recording = _recording(mapping(mapping(track).get("recording")))
            if recording is not None:
                recordings.append(recording)
    return MusicBrainzRelease(
        mbid=mbid,
        title=title,
        date=clean_text(item.get("date")),
        country=clean_text(item.get("country")),
        label=ref.label if ref is not None else None,
        barcode=clean_text(item.get("barcode")),
        release_group=ref.release_group if ref is not None else None,
        release_group_mbid=ref.release_group_mbid if ref is not None else None,
        artists=_artist_credit(item.get("artist-credit")),
        recordings=recordings,
        track_count=ref.track_count if ref is not None else None,
        status=clean_text(item.get("status")),
    )


def _artist_result(document: Mapping[str, Any]) -> MusicBrainzArtistResult | None:
    item = mapping(document)
    mbid = valid_mbid(item.get("id"))
    name = clean_text(item.get("name"))
    if mbid is None or name is None:
        return None
    return MusicBrainzArtistResult(
        mbid=mbid,
        name=name,
        sort_name=clean_text(item.get("sort-name")),
        score=score_fraction(item.get("score")) if item.get("score") is not None else None,
    )


def parse_musicbrainz_recording(
    document: Mapping[str, Any],
) -> tuple[MusicBrainzRecording | None, tuple[str, ...]]:
    """Parse one recording and report invalid identifiers dropped."""

    warnings: list[str] = []
    raw = mapping(document)
    recording = _recording(raw)
    if recording is None:
        warnings.append("invalid_recording")
        return None, tuple(warnings)
    for raw_isrc in sequence(raw.get("isrcs")):
        if valid_isrcs([raw_isrc]):
            continue
        warnings.append("invalid_isrc")
    for raw_release in sequence(raw.get("releases")):
        release_id = mapping(raw_release).get("id")
        if release_id is not None and valid_mbid(release_id) is None:
            warnings.append("invalid_release_mbid")
    for credit in sequence(raw.get("artist-credit")):
        artist_id = mapping(mapping(credit).get("artist")).get("id")
        if artist_id is not None and valid_mbid(artist_id) is None:
            warnings.append("invalid_artist_mbid")
    return recording, tuple(dict.fromkeys(warnings))


class MusicBrainzProvider:
    """Search and lookup over the MusicBrainz web service."""

    id = "musicbrainz"
    name = "MusicBrainz"
    version = PROVIDER_VERSION
    source = "musicbrainz"

    def __init__(
        self,
        client: HttpClient | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        contact: str | None = None,
        clock: Any = None,
        ttl: timedelta = DEFAULT_TTL,
        limit: int = 8,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._base_url = base_url.rstrip("/")
        self._contact = contact
        self._clock = clock
        self._ttl = ttl
        self._limit = max(1, min(100, int(limit)))

    @property
    def contact_configured(self) -> bool:
        if self._contact is not None:
            return bool(self._contact.strip())
        return self._effective_client().contact_configured

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _effective_client(self) -> HttpClient:
        if self._client is None:
            self._client = HttpClient(contact=self._contact)
        return self._client

    @property
    def user_agent(self) -> str:
        return self._effective_client().user_agent

    def _check_contact(self) -> None:
        if not self.contact_configured:
            raise ProviderUnavailable(
                "MusicBrainz requires a User-Agent contact",
                retryable=False,
                user_action=(
                    "Set TRACKFORGE_MUSICBRAINZ_USER_AGENT to a contact "
                    "such as an email address or project URL."
                ),
            )

    def _expires(self) -> datetime:
        from trackforge.domain.model._validation import utc_now

        return ttl_expiry(self._ttl, now=self._clock() if self._clock else utc_now())

    def _observation(
        self,
        payload: MusicBrainzSearchPayload,
        warnings: Sequence[str] = (),
    ) -> ProviderObservation:
        return ProviderObservation(
            type="musicbrainz.search.v1",
            payload=payload.model_dump(mode="json"),
            source=self.source,
            provider_version=self.version,
            warnings=tuple(warnings),
            expires_at=self._expires(),
        )

    async def search(
        self,
        query: MetadataQuery | Mapping[str, Any],
    ) -> Sequence[ProviderObservation]:
        normalized = MetadataQuery.coerce(query)
        self._check_contact()
        entity = (normalized.entity or "recording").casefold().replace("_", "-")
        if normalized.recording_mbid and entity == "recording":
            return [await self.lookup(normalized.recording_mbid)]
        if normalized.release_mbid and entity == "release":
            return [await self.lookup(f"release:{normalized.release_mbid}")]
        path = _ENTITY_PATHS.get(entity, "recording")
        params: dict[str, str] = {
            "query": build_search_query(normalized),
            "fmt": "json",
            "limit": str(normalized.limit or self._limit),
        }
        if path == "release":
            params["query"] = (
                " AND ".join(
                    part
                    for part in (
                        (
                            f'release:"{escape_lucene(normalized.album)}"'
                            if normalized.album
                            else ""
                        ),
                        (
                            f'artist:"{escape_lucene(normalized.artist)}"'
                            if normalized.artist
                            else ""
                        ),
                    )
                    if part
                )
                or "*"
            )
        elif path == "artist":
            artist_query = normalized.artist or normalized.title or ""
            params["query"] = f'artist:"{escape_lucene(artist_query)}"'
            params["query"] = params["query"] if params["query"] != 'artist:""' else "*"
        response = await self._effective_client().get(
            f"{self._base_url}/{path}",
            params=params,
            headers={"User-Agent": self._effective_client().user_agent},
        )
        if response.status_code == 404:
            return [self._observation(MusicBrainzSearchPayload(query=_query_document(normalized)))]
        if not response.ok:
            response.raise_for_status()
        document = response.json()
        if not isinstance(document, Mapping):
            raise ProviderUnavailable(
                "MusicBrainz returned a non-object response",
                retryable=False,
                context={"url": response.url},
            )
        if "error" in document:
            raise ProviderUnavailable(
                f"MusicBrainz error: {clean_text(document.get('error')) or 'unknown error'}",
                retryable=False,
                context={"url": response.url},
            )
        payload, warnings = _search_payload(normalized, path, document)
        return [self._observation(payload, warnings)]

    async def lookup(
        self,
        identifier: str,
        entity: str | None = None,
    ) -> ProviderObservation:
        self._check_contact()
        lookup_value = f"{entity}:{identifier}" if entity else identifier
        entity, mbid = _split_identifier(lookup_value)
        if mbid is None:
            raise ProviderUnavailable(
                "invalid MusicBrainz identifier",
                retryable=False,
                context={"identifier": identifier},
            )
        path = _ENTITY_PATHS.get(entity, "recording")
        inc = {
            "recording": "artist-credits+releases+release-groups+isrcs+genres+tags",
            "release": "artist-credits+release-groups+labels+recordings+genres+tags",
            "release-group": "artist-credits+releases+genres+tags",
            "artist": "artist-credits+genres+tags",
            "work": "artist-relations+genres+tags",
        }.get(path, "artist-credits")
        response = await self._effective_client().get(
            f"{self._base_url}/{path}/{mbid}",
            params={"fmt": "json", "inc": inc},
            headers={"User-Agent": self._effective_client().user_agent},
        )
        if response.status_code == 404:
            raise ProviderUnavailable(
                "MusicBrainz entity was not found",
                retryable=False,
                user_action="Check the MBID.",
                context={"url": response.url, "identifier": identifier},
            )
        if not response.ok:
            response.raise_for_status()
        document = response.json()
        if not isinstance(document, Mapping):
            raise ProviderUnavailable(
                "MusicBrainz returned a non-object response",
                retryable=False,
                context={"url": response.url},
            )
        payload, warnings = _lookup_payload(entity, mbid, document)
        return self._observation(payload, warnings)


def _split_identifier(identifier: str) -> tuple[str, str | None]:
    text = str(identifier).strip()
    if ":" in text:
        prefix, _, value = text.partition(":")
        entity = prefix.casefold().replace("_", "-")
        return entity, valid_mbid(value)
    return "recording", valid_mbid(text)


def _query_document(query: MetadataQuery) -> MusicBrainzQuery:
    return MusicBrainzQuery(
        title=query.title,
        artist=query.artist,
        album=query.album,
        duration=query.duration,
        recording_mbid=query.recording_mbid,
        release_mbid=query.release_mbid,
        release_group_mbid=query.release_group_mbid,
        entity=query.entity,
    )


def _search_payload(
    query: MetadataQuery,
    path: str,
    document: Mapping[str, Any],
) -> tuple[MusicBrainzSearchPayload, tuple[str, ...]]:
    warnings: list[str] = []
    recordings: list[MusicBrainzRecording] = []
    releases: list[MusicBrainzRelease] = []
    artists: list[MusicBrainzArtistResult] = []

    if path == "recording":
        for raw in sequence(document.get("recordings")):
            recording, recording_warnings = parse_musicbrainz_recording(mapping(raw))
            warnings.extend(recording_warnings)
            if recording is not None:
                recordings.append(recording)
    elif path == "release":
        for raw in sequence(document.get("releases")):
            release = _release(mapping(raw))
            if release is None:
                warnings.append("invalid_release_mbid")
            else:
                releases.append(release)
    elif path == "artist":
        for raw in sequence(document.get("artists")):
            artist = _artist_result(mapping(raw))
            if artist is None:
                warnings.append("invalid_artist_mbid")
            else:
                artists.append(artist)

    # Derive compact releases and artists from recording search results so the
    # payload shape is useful even when MusicBrainz only returns recordings.
    for recording in recordings:
        for release_ref in recording.releases:
            if release_ref.mbid and all(item.mbid != release_ref.mbid for item in releases):
                releases.append(
                    MusicBrainzRelease(
                        mbid=release_ref.mbid,
                        title=release_ref.title,
                        date=release_ref.date,
                        country=release_ref.country,
                        label=release_ref.label,
                        barcode=release_ref.barcode,
                        release_group=release_ref.release_group,
                        release_group_mbid=release_ref.release_group_mbid,
                        status=release_ref.status,
                        track_count=release_ref.track_count,
                    )
                )
        for credit_artist in recording.artists:
            if credit_artist.mbid and all(item.mbid != credit_artist.mbid for item in artists):
                artists.append(
                    MusicBrainzArtistResult(
                        mbid=credit_artist.mbid,
                        name=credit_artist.name,
                        sort_name=credit_artist.sort_name,
                    )
                )

    return (
        MusicBrainzSearchPayload(
            query=_query_document(query),
            recordings=recordings,
            releases=releases,
            artists=artists,
        ),
        tuple(dict.fromkeys(warnings)),
    )


def _lookup_payload(
    entity: str,
    mbid: str,
    document: Mapping[str, Any],
) -> tuple[MusicBrainzSearchPayload, tuple[str, ...]]:
    warnings: list[str] = []
    recordings: list[MusicBrainzRecording] = []
    releases: list[MusicBrainzRelease] = []
    artists: list[MusicBrainzArtistResult] = []
    recording_mbid: str | None = None
    release_mbid: str | None = None
    if entity == "recording":
        recording, recording_warnings = parse_musicbrainz_recording(document)
        warnings.extend(recording_warnings)
        if recording is not None:
            recordings.append(recording)
            recording_mbid = recording.mbid
            for release in recording.releases:
                if release.mbid:
                    releases.append(
                        MusicBrainzRelease(
                            mbid=release.mbid,
                            title=release.title,
                            date=release.date,
                            country=release.country,
                            label=release.label,
                            barcode=release.barcode,
                            release_group=release.release_group,
                            release_group_mbid=release.release_group_mbid,
                            status=release.status,
                            track_count=release.track_count,
                        )
                    )
        else:
            recording_mbid = mbid
    elif entity == "release":
        release_result = _release(document)
        if release_result is None:
            warnings.append("invalid_release_mbid")
        else:
            releases.append(release_result)
            release_mbid = release_result.mbid
            recordings.extend(release_result.recordings)
    elif entity == "artist":
        artist_result = _artist_result(document)
        if artist_result is None:
            warnings.append("invalid_artist_mbid")
        else:
            artists.append(artist_result)
    else:
        # Release groups and works are not yet modeled separately; retain the
        # identifier in the query and normalize any nested artists.
        for credit in sequence(document.get("artist-credit")):
            artist_doc = mapping(mapping(credit).get("artist"))
            candidate = valid_mbid(artist_doc.get("id"))
            name = clean_text(artist_doc.get("name"))
            if candidate and name:
                artists.append(MusicBrainzArtistResult(mbid=candidate, name=name))
    return (
        MusicBrainzSearchPayload(
            query=MusicBrainzQuery(
                entity=entity,
                recording_mbid=recording_mbid,
                release_mbid=release_mbid,
            ),
            recordings=recordings,
            releases=releases,
            artists=artists,
        ),
        tuple(dict.fromkeys(warnings)),
    )


# A readable alias used by callers and documentation.
MusicBrainzMetadataProvider = MusicBrainzProvider


def build_musicbrainz_query(query: MetadataQuery | Mapping[str, Any]) -> str:
    """Public compatibility helper for tests and callers."""

    return build_search_query(MetadataQuery.coerce(query))


def musicbrainz_url(base_url: str, path: str, params: Mapping[str, str]) -> str:
    """Build a request URL without leaking query secrets into logs."""

    return f"{base_url.rstrip('/')}/{path.lstrip('/')}?{urlencode(dict(params))}"


def __getattr__(name: str) -> Any:
    """Resolve observer aliases lazily to avoid an import cycle."""

    if name in {"MusicBrainzObserver", "MusicBrainzMetadataObserver"}:
        from trackforge.adapters.metadata.observers import MusicBrainzObserver

        return MusicBrainzObserver
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TTL",
    "MusicBrainzMetadataProvider",
    "MusicBrainzProvider",
    "build_musicbrainz_query",
    "build_search_query",
    "escape_lucene",
    "musicbrainz_url",
    "parse_musicbrainz_recording",
]

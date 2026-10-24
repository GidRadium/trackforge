"""Cover art providers: Cover Art Archive, iTunes, and Deezer.

Cover Art Archive is the preferred source.  The observer tries CAA, then
iTunes, then Deezer, and finally the source thumbnail.  Candidates are merely
observations: the resolver selects the final artwork.  When a content-addressed
store is supplied, downloaded bytes are stored there and only the hash is kept
in the JSON payload.
"""

from __future__ import annotations

import struct
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, cast

from trackforge.adapters.http.client import HttpClient
from trackforge.adapters.metadata.common import clean_text, mapping, sequence, valid_mbid
from trackforge.application.dto.providers import (
    CoverCandidate,
    CoverCandidatesPayload,
    MetadataQuery,
    ProviderObservation,
    ttl_expiry,
)
from trackforge.application.ports.storage import ContentAddressedStore
from trackforge.domain.errors import (
    OfflineModeError,
    ProviderUnavailable,
    RateLimited,
    UnsafeUrl,
)

DEFAULT_CAA_BASE_URL = "https://coverartarchive.org"
DEFAULT_ITUNES_BASE_URL = "https://itunes.apple.com"
DEFAULT_DEEZER_BASE_URL = "https://api.deezer.com"
DEFAULT_TTL = timedelta(days=7)
PROVIDER_VERSION = "1"
MAX_COVER_BYTES = 10 * 1024 * 1024
MIN_PREFERRED_SIZE = 1000

_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read PNG/JPEG/GIF/WebP dimensions without an image library."""

    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        width, height = struct.unpack(">II", data[16:24])
        return (int(width), int(height)) if width and height else None
    if len(data) >= 10 and data[:6] in {b"GIF87a", b"GIF89a"}:
        width, height = struct.unpack("<HH", data[6:10])
        return (int(width), int(height)) if width and height else None
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = data[12:16]
        if kind == b"VP8X" and len(data) >= 30:
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return width, height
        if kind == b"VP8 " and len(data) >= 30:
            start = data.find(b"\x9d\x01\x2a")
            if start >= 0 and len(data) >= start + 7:
                width = int.from_bytes(data[start + 3 : start + 5], "little") & 0x3FFF
                height = int.from_bytes(data[start + 5 : start + 7], "little") & 0x3FFF
                return width, height
        if kind == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return width, height
    if len(data) >= 4 and data[:2] == b"\xff\xd8":
        index = 2
        while index + 4 <= len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            index += 2
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if index + 2 > len(data):
                break
            length = int.from_bytes(data[index : index + 2], "big")
            if length < 2:
                break
            if marker in _JPEG_SOF_MARKERS and index + 7 <= len(data):
                height = int.from_bytes(data[index + 3 : index + 5], "big")
                width = int.from_bytes(data[index + 5 : index + 7], "big")
                return (int(width), int(height)) if width and height else None
            index += length
    return None


def _mime_for(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _mime_from_url(url: str | None) -> str | None:
    if not url:
        return None
    path = url.split("?", 1)[0].casefold()
    if path.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if path.endswith(".png"):
        return "image/png"
    if path.endswith(".webp"):
        return "image/webp"
    if path.endswith(".gif"):
        return "image/gif"
    return None


def _score(width: int | None, height: int | None, base: float) -> float:
    shortest = min(width or 0, height or 0)
    if shortest >= 2000:
        bonus = 0.10
    elif shortest >= MIN_PREFERRED_SIZE:
        bonus = 0.07
    elif shortest >= 600:
        bonus = 0.03
    elif shortest > 0:
        bonus = 0.0
    else:
        bonus = -0.05
    return max(0.0, min(1.0, base + bonus))


class _CoverProvider:
    id = "cover"
    version = PROVIDER_VERSION
    source = "cover"

    def __init__(
        self,
        client: HttpClient | None = None,
        *,
        cas: ContentAddressedStore | None = None,
        fetch_images: bool = False,
        clock: Callable[[], datetime] | None = None,
        ttl: timedelta = DEFAULT_TTL,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._cas = cas
        self._fetch_images = fetch_images
        self._clock = clock
        self._ttl = ttl

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _effective_client(self) -> HttpClient:
        if self._client is None:
            self._client = HttpClient()
        return self._client

    def _expires(self) -> datetime:
        from trackforge.domain.model._validation import utc_now

        return ttl_expiry(self._ttl, now=self._clock() if self._clock else utc_now())

    def _observation(
        self,
        candidates: Sequence[CoverCandidate],
        warnings: Sequence[str],
    ) -> ProviderObservation:
        payload = CoverCandidatesPayload(
            candidates=list(candidates),
            warnings=list(dict.fromkeys(warnings)),
        )
        return ProviderObservation(
            type="cover.candidates.v1",
            payload=payload.model_dump(mode="json"),
            source=self.source,
            provider_version=self.version,
            warnings=tuple(dict.fromkeys(warnings)),
            expires_at=self._expires(),
        )

    async def _measure(
        self,
        url: str | None,
        *,
        candidate: CoverCandidate,
    ) -> tuple[CoverCandidate, list[str]]:
        if not self._fetch_images or not url:
            return candidate, []
        warnings: list[str] = []
        try:
            data = await self._effective_client().get_bytes(url, max_bytes=MAX_COVER_BYTES)
        except (ProviderUnavailable, UnsafeUrl):
            warnings.append(f"cover_fetch_failed:{candidate.source}")
            return candidate, warnings
        dimensions = image_dimensions(data)
        mime = _mime_for(data) or candidate.mime
        artifact_ref = candidate.artifact_ref
        if self._cas is not None and data:
            try:
                artifact_ref = await self._cas.put_bytes(data)
            except OSError:
                warnings.append("cover_store_failed")
        width = dimensions[0] if dimensions else candidate.width
        height = dimensions[1] if dimensions else candidate.height
        return (
            CoverCandidate(
                source=candidate.source,
                url=candidate.url,
                release_mbid=candidate.release_mbid,
                mime=mime,
                width=width,
                height=height,
                size_bytes=len(data),
                score=_score(width, height, candidate.score),
                artifact_ref=str(artifact_ref) if artifact_ref else None,
            ),
            warnings,
        )


class CoverArtArchiveProvider(_CoverProvider):
    """Cover Art Archive release/release-group lookup."""

    id = "cover_art_archive"
    source = "cover_art_archive"

    def __init__(
        self,
        client: HttpClient | None = None,
        *,
        base_url: str = DEFAULT_CAA_BASE_URL,
        cas: ContentAddressedStore | None = None,
        fetch_images: bool = False,
        clock: Callable[[], datetime] | None = None,
        ttl: timedelta = DEFAULT_TTL,
    ) -> None:
        super().__init__(client, cas=cas, fetch_images=fetch_images, clock=clock, ttl=ttl)
        self._base_url = base_url.rstrip("/")

    async def search(
        self,
        query: MetadataQuery | Mapping[str, Any],
    ) -> Sequence[ProviderObservation]:
        normalized = MetadataQuery.coerce(query)
        if normalized.release_mbid:
            return [await self.lookup(normalized.release_mbid)]
        if normalized.release_group_mbid:
            return [await self.lookup(f"release-group:{normalized.release_group_mbid}")]
        return [self._observation([], ("no_release_mbid",))]

    async def lookup(
        self,
        identifier: str,
        entity: str | None = None,
    ) -> ProviderObservation:
        entity_name = entity or "release"
        value = str(identifier).strip()
        if ":" in value:
            prefix, _, remainder = value.partition(":")
            if prefix in {"release-group", "release_group"}:
                entity_name = "release-group"
            value = remainder
        mbid = valid_mbid(value)
        if mbid is None:
            raise ProviderUnavailable(
                "invalid Cover Art Archive release MBID",
                retryable=False,
                context={"identifier": identifier},
            )
        response = await self._effective_client().get(f"{self._base_url}/{entity_name}/{mbid}")
        if response.status_code == 404:
            return self._observation([], ("caa_not_found",))
        if not response.ok:
            response.raise_for_status()
        document = response.json()
        if not isinstance(document, Mapping):
            raise ProviderUnavailable(
                "Cover Art Archive returned a non-object response",
                retryable=False,
                context={"url": response.url},
            )
        warnings: list[str] = []
        candidates: list[CoverCandidate] = []
        images = sequence(document.get("images"))
        for raw in images:
            image = mapping(raw)
            if image.get("approved") is False:
                continue
            front = bool(image.get("front"))
            types = {str(item).casefold() for item in sequence(image.get("types"))}
            if not front and "front" not in types:
                if any(item.get("front") for item in map(mapping, images)):
                    continue
            url = clean_text(image.get("image"))
            if url is None:
                thumbnails = mapping(image.get("thumbnails"))
                url = clean_text(thumbnails.get("large")) or clean_text(thumbnails.get("small"))
            if url is None:
                continue
            candidate = CoverCandidate(
                source=self.source,
                url=url,
                release_mbid=mbid,
                mime=_mime_from_url(url),
                width=None,
                height=None,
                size_bytes=None,
                score=0.9 if front else 0.65,
            )
            measured, measure_warnings = await self._measure(url, candidate=candidate)
            warnings.extend(measure_warnings)
            candidates.append(measured)
        candidates.sort(key=lambda item: (item.score, item.resolution), reverse=True)
        if not candidates:
            warnings.append("caa_no_images")
        return self._observation(candidates, warnings)


class ItunesProvider(_CoverProvider):
    """iTunes Search API artwork fallback."""

    id = "itunes"
    source = "itunes"

    def __init__(
        self,
        client: HttpClient | None = None,
        *,
        base_url: str = DEFAULT_ITUNES_BASE_URL,
        cas: ContentAddressedStore | None = None,
        fetch_images: bool = False,
        clock: Callable[[], datetime] | None = None,
        ttl: timedelta = DEFAULT_TTL,
        country: str = "US",
    ) -> None:
        super().__init__(client, cas=cas, fetch_images=fetch_images, clock=clock, ttl=ttl)
        self._base_url = base_url.rstrip("/")
        self._country = country

    async def search(
        self,
        query: MetadataQuery | Mapping[str, Any],
    ) -> Sequence[ProviderObservation]:
        normalized = MetadataQuery.coerce(query)
        term = " ".join(part for part in (normalized.artist, normalized.title) if part)
        if not term:
            return [self._observation([], ("no_query",))]
        response = await self._effective_client().get(
            f"{self._base_url}/search",
            params={
                "term": term,
                "entity": "song",
                "media": "music",
                "limit": str(normalized.limit),
                "country": self._country,
            },
        )
        if not response.ok:
            response.raise_for_status()
        document = response.json()
        if not isinstance(document, Mapping):
            raise ProviderUnavailable(
                "iTunes returned a non-object response",
                retryable=False,
                context={"url": response.url},
            )
        warnings: list[str] = []
        candidates: list[CoverCandidate] = []
        for raw in sequence(document.get("results")):
            item = mapping(raw)
            artwork = clean_text(item.get("artworkUrl100")) or clean_text(item.get("artworkUrl60"))
            if artwork is None:
                continue
            url = _upscale_itunes_artwork(artwork)
            score = _score(3000, 3000, 0.65)
            candidate = CoverCandidate(
                source=self.source,
                url=url,
                release_mbid=None,
                mime=_mime_from_url(url),
                width=3000,
                height=3000,
                size_bytes=None,
                score=score,
            )
            measured, measure_warnings = await self._measure(url, candidate=candidate)
            warnings.extend(measure_warnings)
            candidates.append(measured)
        candidates.sort(key=lambda item: item.score, reverse=True)
        return [self._observation(candidates[: normalized.limit], warnings)]

    async def lookup(self, identifier: str) -> ProviderObservation:
        # iTunes has no stable public identifier lookup for artwork; a lookup
        # by term is treated as an artist/title query.
        return (await self.search(MetadataQuery(title=str(identifier))))[0]


class DeezerProvider(_CoverProvider):
    """Deezer public search API artwork fallback."""

    id = "deezer"
    source = "deezer"

    def __init__(
        self,
        client: HttpClient | None = None,
        *,
        base_url: str = DEFAULT_DEEZER_BASE_URL,
        cas: ContentAddressedStore | None = None,
        fetch_images: bool = False,
        clock: Callable[[], datetime] | None = None,
        ttl: timedelta = DEFAULT_TTL,
    ) -> None:
        super().__init__(client, cas=cas, fetch_images=fetch_images, clock=clock, ttl=ttl)
        self._base_url = base_url.rstrip("/")

    async def search(
        self,
        query: MetadataQuery | Mapping[str, Any],
    ) -> Sequence[ProviderObservation]:
        normalized = MetadataQuery.coerce(query)
        term = " ".join(part for part in (normalized.artist, normalized.title) if part)
        if not term:
            return [self._observation([], ("no_query",))]
        response = await self._effective_client().get(
            f"{self._base_url}/search",
            params={"q": term, "limit": str(normalized.limit)},
        )
        if not response.ok:
            response.raise_for_status()
        document = response.json()
        if not isinstance(document, Mapping):
            raise ProviderUnavailable(
                "Deezer returned a non-object response",
                retryable=False,
                context={"url": response.url},
            )
        warnings: list[str] = []
        candidates: list[CoverCandidate] = []
        for raw in sequence(document.get("data")):
            item = mapping(raw)
            album = mapping(item.get("album"))
            url = (
                clean_text(album.get("cover_xl"))
                or clean_text(album.get("cover_big"))
                or clean_text(album.get("cover"))
            )
            if url is None:
                continue
            size = 1000 if album.get("cover_xl") else (500 if album.get("cover_big") else 250)
            candidate = CoverCandidate(
                source=self.source,
                url=url,
                release_mbid=None,
                mime=_mime_from_url(url),
                width=size,
                height=size,
                size_bytes=None,
                score=_score(size, size, 0.55),
            )
            measured, measure_warnings = await self._measure(url, candidate=candidate)
            warnings.extend(measure_warnings)
            candidates.append(measured)
        candidates.sort(key=lambda item: item.score, reverse=True)
        return [self._observation(candidates[: normalized.limit], warnings)]

    async def lookup(self, identifier: str) -> ProviderObservation:
        return (await self.search(MetadataQuery(title=str(identifier))))[0]


class CoverArtProvider:
    """Try CAA, iTunes, and Deezer in that order."""

    id = "cover"
    version = PROVIDER_VERSION
    source = "cover_art_archive"

    def __init__(
        self,
        *providers: Any,
        caa: CoverArtArchiveProvider | None = None,
        itunes: ItunesProvider | None = None,
        deezer: DeezerProvider | None = None,
        client: HttpClient | None = None,
        cas: ContentAddressedStore | None = None,
        fetch_images: bool = False,
    ) -> None:
        if providers:
            self._providers = list(providers)
        else:
            self._providers = [
                caa or CoverArtArchiveProvider(client, cas=cas, fetch_images=fetch_images),
                itunes or ItunesProvider(client, cas=cas, fetch_images=fetch_images),
                deezer or DeezerProvider(client, cas=cas, fetch_images=fetch_images),
            ]

    @property
    def providers(self) -> tuple[Any, ...]:
        return tuple(self._providers)

    async def search(
        self,
        query: MetadataQuery | Mapping[str, Any],
    ) -> Sequence[ProviderObservation]:
        normalized = MetadataQuery.coerce(query)
        warnings: list[str] = []
        for provider in self._providers:
            try:
                observations = cast(
                    Sequence[ProviderObservation],
                    await provider.search(normalized),
                )
            except (ProviderUnavailable, RateLimited, OfflineModeError, UnsafeUrl, ValueError):
                warnings.append(
                    f"cover_provider_unavailable:{getattr(provider, 'source', 'unknown')}"
                )
                continue
            candidates = _candidates_from(observations)
            if candidates:
                return observations
        return [
            ProviderObservation(
                type="cover.candidates.v1",
                payload=CoverCandidatesPayload(warnings=list(dict.fromkeys(warnings))).model_dump(
                    mode="json"
                ),
                source=self.source,
                provider_version=self.version,
                warnings=tuple(dict.fromkeys(warnings)),
                expires_at=ttl_expiry(DEFAULT_TTL),
            )
        ]

    async def lookup(self, identifier: str) -> ProviderObservation:
        warnings: list[str] = []
        for provider in self._providers:
            try:
                observation = cast(
                    ProviderObservation,
                    await provider.lookup(identifier),
                )
            except (ProviderUnavailable, RateLimited, OfflineModeError, UnsafeUrl, ValueError):
                warnings.append(
                    f"cover_provider_unavailable:{getattr(provider, 'source', 'unknown')}"
                )
                continue
            if observation.payload.get("candidates"):
                return observation
        return ProviderObservation(
            type="cover.candidates.v1",
            payload=CoverCandidatesPayload(warnings=list(dict.fromkeys(warnings))).model_dump(
                mode="json"
            ),
            source=self.source,
            provider_version=self.version,
            warnings=tuple(dict.fromkeys(warnings)),
            expires_at=ttl_expiry(DEFAULT_TTL),
        )

    async def aclose(self) -> None:
        for provider in self._providers:
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()


def _upscale_itunes_artwork(url: str) -> str:
    import re

    return re.sub(r"/\d+x\d+(?:bb)?\.(jpg|jpeg|png)", r"/3000x3000bb.\1", url)


def _candidates_from(observations: Sequence[ProviderObservation]) -> list[CoverCandidate]:
    result: list[CoverCandidate] = []
    for observation in observations:
        raw = observation.payload.get("candidates")
        for item in sequence(raw):
            try:
                result.append(CoverCandidate.model_validate(item))
            except ValueError:
                continue
    return result


# Short aliases used by tests and plugin authors.
CAAProvider = CoverArtArchiveProvider
ITunesProvider = ItunesProvider


def __getattr__(name: str) -> Any:
    if name in {"CoverArtObserver", "CoverSearchObserver"}:
        from trackforge.adapters.metadata.observers import CoverArtObserver

        return CoverArtObserver
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CAAProvider",
    "CoverArtArchiveProvider",
    "CoverArtProvider",
    "DeezerProvider",
    "ITunesProvider",
    "ItunesProvider",
    "MAX_COVER_BYTES",
    "MIN_PREFERRED_SIZE",
    "image_dimensions",
]

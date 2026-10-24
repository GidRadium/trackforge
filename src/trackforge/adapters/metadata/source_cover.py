"""Fetch cover art advertised by the source itself.

`yt-dlp` exposes thumbnails for both YouTube and Yandex Music, and YouTube has
stable ``i.ytimg.com/vi/<id>/maxresdefault.jpg`` URLs.  This observer turns
those URLs into a normal ``cover.candidates.v1`` artifact with bytes stored in
CAS, so rendering can embed a cover without MusicBrainz/AcoustID.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import urlsplit

from trackforge.adapters.http.client import HttpClient
from trackforge.adapters.http.url_safety import UnsafeUrl
from trackforge.adapters.metadata.coverart import MAX_COVER_BYTES, image_dimensions
from trackforge.application.dto.observations import SourceMetadataPayload
from trackforge.application.dto.providers import CoverCandidate, CoverCandidatesPayload
from trackforge.application.ports.execution import (
    ArtifactDraft,
    StepContext,
    StepResult,
    StepWarning,
)
from trackforge.application.ports.observations import COVER_ARTIFACT, SOURCE_METADATA_ARTIFACT
from trackforge.application.ports.storage import ContentAddressedStore
from trackforge.domain.errors import OfflineModeError, ProviderUnavailable, RateLimited
from trackforge.domain.model.capabilities import (
    CapabilityKind,
    CapabilityManifest,
    Cost,
    Determinism,
    SideEffect,
)

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,}$")
_YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
)


class SourceCoverObserver:
    """Download the first usable source thumbnail and store it in CAS."""

    manifest = CapabilityManifest(
        id="source_cover",
        version="1",
        kind=CapabilityKind.OBSERVER,
        requires=(SOURCE_METADATA_ARTIFACT,),
        provides=(COVER_ARTIFACT,),
        cost=Cost(cpu_seconds=0.2, memory_mb=32.0, network_calls=1),
        determinism=Determinism.NON_DETERMINISTIC,
        cacheable=True,
        side_effects=SideEffect.NETWORK,
        rate_limit="coverart:2/s",
        description="Use the source thumbnail (including YouTube maxresdefault) as cover art.",
        config_schema={
            "type": "object",
            "properties": {"offline": {"type": "boolean"}},
        },
    )

    def __init__(
        self,
        *,
        client: HttpClient | None = None,
        cas: ContentAddressedStore | None = None,
        proxy_for: Callable[[str], str | None] | None = None,
        max_bytes: int = MAX_COVER_BYTES,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._client = client
        self._cas = cas
        self._proxy_for = proxy_for
        self._clients: dict[str | None, HttpClient] = {}
        self._max_bytes = max_bytes

    def _warning(self, code: str, message: str, *, user_action: str | None = None) -> StepWarning:
        return StepWarning(
            code=code,
            message=message,
            capability_id=self.manifest.id,
            provider="source_thumbnail",
            user_action=user_action,
        )

    def _skipped(self, warning: StepWarning) -> StepResult:
        return StepResult.skipped(warnings=(warning,))

    def _client_for(self, source: SourceMetadataPayload) -> HttpClient:
        if self._client is not None:
            return self._client
        proxy = self._proxy_for(source.webpage_url or "") if self._proxy_for is not None else None
        client = self._clients.get(proxy)
        if client is None:
            client = HttpClient(proxy=proxy)
            self._clients[proxy] = client
        return client

    async def run(self, ctx: StepContext) -> StepResult:
        if bool(ctx.config_subset.get("offline")):
            return self._skipped(
                self._warning(
                    "offline",
                    "source cover was skipped because offline mode is enabled",
                )
            )
        source_step = ctx.inputs.get(SOURCE_METADATA_ARTIFACT)
        if source_step is None:
            return self._skipped(
                self._warning("source_cover_missing_source", "source metadata is unavailable")
            )
        try:
            source = SourceMetadataPayload.model_validate_json(source_step.payload)
        except Exception:
            return self._skipped(
                self._warning("source_cover_invalid_source", "source metadata is invalid")
            )
        if self._cas is None:
            return self._skipped(
                self._warning(
                    "source_cover_store_unavailable",
                    "cover bytes cannot be stored without a content-addressed store",
                )
            )

        candidates = _thumbnail_candidates(source)
        if not candidates:
            return self._skipped(
                self._warning("source_cover_missing", "the source advertises no thumbnails")
            )

        warnings: list[StepWarning] = []
        client = self._client_for(source)
        for url, score in candidates:
            try:
                data = await client.get_bytes(url, max_bytes=self._max_bytes)
            except (ProviderUnavailable, RateLimited, UnsafeUrl, OfflineModeError):
                warnings.append(
                    self._warning(
                        "source_cover_fetch_failed",
                        f"source thumbnail could not be fetched: {_safe_source(url)}",
                    )
                )
                continue
            dimensions = image_dimensions(data)
            mime = _image_mime(data)
            if dimensions is None or mime is None:
                warnings.append(
                    self._warning(
                        "source_cover_not_image",
                        f"source thumbnail is not a supported image: {_safe_source(url)}",
                    )
                )
                continue
            try:
                content_hash = await self._cas.put_bytes(data)
            except OSError:
                warnings.append(
                    self._warning(
                        "source_cover_store_failed",
                        "source thumbnail could not be stored in CAS",
                    )
                )
                continue
            width, height = dimensions
            candidate = CoverCandidate(
                source="source_thumbnail",
                url=url,
                mime=mime,
                width=width,
                height=height,
                size_bytes=len(data),
                score=score,
                artifact_ref=str(content_hash),
            )
            payload = CoverCandidatesPayload(
                candidates=[candidate],
                warnings=[warning.code for warning in warnings],
            )
            draft = ArtifactDraft(
                type=COVER_ARTIFACT,
                payload=payload.model_dump(mode="json"),
                params={"source": "source_thumbnail", "url": url},
            )
            return StepResult(
                artifacts=(draft,),
                warnings=tuple(warnings),
                actual_cost=Cost(
                    cpu_seconds=0.1,
                    memory_mb=32.0,
                    network_calls=len(warnings) + 1,
                ),
            )
        return self._skipped(
            self._warning(
                "source_cover_unavailable",
                "none of the source thumbnails produced a usable image",
            )
        )


def _thumbnail_candidates(source: SourceMetadataPayload) -> list[tuple[str, float]]:
    seen: set[str] = set()
    result: list[tuple[str, float]] = []

    def add(url: str | None, score: float) -> None:
        if not url or url in seen:
            return
        seen.add(url)
        result.append((url, score))

    if _is_youtube(source):
        video_id = source.external_id.strip()
        if _VIDEO_ID_RE.fullmatch(video_id):
            base = f"https://i.ytimg.com/vi/{video_id}"
            add(f"{base}/maxresdefault.jpg", 0.95)
            add(f"{base}/sddefault.jpg", 0.85)
            add(f"{base}/hqdefault.jpg", 0.75)
            add(f"{base}/mqdefault.jpg", 0.65)

    thumbnails = sorted(
        source.thumbnails,
        key=lambda item: (item.width or 0) * (item.height or 0),
        reverse=True,
    )
    for index, thumbnail in enumerate(thumbnails[:8]):
        resolution = (thumbnail.width or 0) * (thumbnail.height or 0)
        score = min(0.9, 0.45 + resolution / 2_000_000) - index * 0.01
        add(thumbnail.url, max(0.1, score))
    return result


def _is_youtube(source: SourceMetadataPayload) -> bool:
    extractor = (source.extractor or "").casefold()
    if "youtube" in extractor:
        return True
    try:
        host = (urlsplit(source.webpage_url or "").hostname or "").casefold()
    except ValueError:
        return False
    return host in _YOUTUBE_HOSTS


def _image_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _safe_source(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    return f"{parts.scheme}://{parts.hostname or 'unknown'}{parts.path}"


__all__ = ["SourceCoverObserver"]

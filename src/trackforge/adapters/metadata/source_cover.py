"""Fetch cover art advertised by the source itself.

`yt-dlp` exposes thumbnails for both YouTube and Yandex Music, and YouTube has
stable ``img.youtube.com/vi/<id>/<variant>.jpg`` URLs.  This observer tries the
best variants first, rejects small 200-OK placeholders using the real decoded
dimensions, and keeps the highest-resolution usable image.  Bytes are stored in
CAS so rendering can embed a cover without MusicBrainz/AcoustID.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
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
_YOUTUBE_IMAGE_HOSTS = frozenset({"img.youtube.com", "i.ytimg.com", "ytimg.com", "www.ytimg.com"})
_IDEAL_COVER_PIXELS = 1280 * 720
_MIN_SOURCE_THUMBNAIL = (100, 75)


@dataclass(frozen=True, slots=True)
class _ImageCandidate:
    url: str
    score: float
    min_width: int
    min_height: int
    order: int


class SourceCoverObserver:
    """Download the first usable source thumbnail and store it in CAS."""

    manifest = CapabilityManifest(
        id="source_cover",
        version="2",
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
        webp_converter: Callable[[bytes], bytes | None] | None = None,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._client = client
        self._cas = cas
        self._proxy_for = proxy_for
        self._clients: dict[str | None, HttpClient] = {}
        self._max_bytes = max_bytes
        self._webp_converter = webp_converter or _convert_webp_to_png

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
        fetches = 0
        best_data: bytes | None = None
        best_mime: str | None = None
        best_dimensions: tuple[int, int] | None = None
        best_candidate: _ImageCandidate | None = None
        best_pixels = 0
        for candidate in candidates:
            fetches += 1
            try:
                data = await client.get_bytes(candidate.url, max_bytes=self._max_bytes)
            except (ProviderUnavailable, RateLimited, UnsafeUrl, OfflineModeError):
                warnings.append(
                    self._warning(
                        "source_cover_fetch_failed",
                        f"source thumbnail could not be fetched: {_safe_source(candidate.url)}",
                    )
                )
                continue
            dimensions = image_dimensions(data)
            mime = _image_mime(data)
            if dimensions is None or mime is None:
                warnings.append(
                    self._warning(
                        "source_cover_not_image",
                        f"source thumbnail is not a supported image: {_safe_source(candidate.url)}",
                    )
                )
                continue
            width, height = dimensions
            if width < candidate.min_width or height < candidate.min_height:
                warnings.append(
                    self._warning(
                        "source_cover_too_small",
                        (
                            f"source thumbnail is only {width}x{height} "
                            f"(minimum for this source is "
                            f"{candidate.min_width}x{candidate.min_height}): "
                            f"{_safe_source(candidate.url)}"
                        ),
                    )
                )
                continue
            if mime == "image/webp":
                converted = await asyncio.to_thread(self._webp_converter, data)
                converted_dimensions = image_dimensions(converted or b"")
                if converted is None or converted_dimensions is None:
                    warnings.append(
                        self._warning(
                            "source_cover_format_unsupported",
                            f"WebP cover could not be converted: {_safe_source(candidate.url)}",
                        )
                    )
                    continue
                data = converted
                dimensions = converted_dimensions
                mime = "image/png"
                width, height = dimensions
            if mime not in {"image/jpeg", "image/png"}:
                warnings.append(
                    self._warning(
                        "source_cover_format_unsupported",
                        f"source thumbnail format is not embeddable: {_safe_source(candidate.url)}",
                    )
                )
                continue
            pixels = width * height
            if pixels > best_pixels or (
                pixels == best_pixels
                and best_candidate is not None
                and candidate.order < best_candidate.order
            ):
                best_data = data
                best_mime = mime
                best_dimensions = (width, height)
                best_candidate = candidate
                best_pixels = pixels
            # 1280x720 or larger is the best YouTube can provide.  If a
            # lower-tier URL returned something smaller (for example a 120x90
            # placeholder), keep looking for a better fallback.
            if pixels >= _IDEAL_COVER_PIXELS:
                break

        if (
            best_data is None
            or best_mime is None
            or best_dimensions is None
            or best_candidate is None
        ):
            return self._skipped(
                self._warning(
                    "source_cover_unavailable",
                    "none of the source thumbnails produced a usable image",
                )
            )
        try:
            content_hash = await self._cas.put_bytes(best_data)
        except OSError:
            return self._skipped(
                self._warning(
                    "source_cover_store_failed",
                    "source thumbnail could not be stored in CAS",
                )
            )
        width, height = best_dimensions
        cover_candidate = CoverCandidate(
            source="source_thumbnail",
            url=best_candidate.url,
            mime=best_mime,
            width=width,
            height=height,
            size_bytes=len(best_data),
            score=_cover_score(best_candidate.score, width, height),
            artifact_ref=str(content_hash),
        )
        payload = CoverCandidatesPayload(
            candidates=[cover_candidate],
            warnings=[warning.code for warning in warnings],
        )
        draft = ArtifactDraft(
            type=COVER_ARTIFACT,
            payload=payload.model_dump(mode="json"),
            params={"source": "source_thumbnail", "url": best_candidate.url},
        )
        return StepResult(
            artifacts=(draft,),
            warnings=tuple(warnings),
            actual_cost=Cost(
                cpu_seconds=0.1,
                memory_mb=32.0,
                network_calls=fetches,
            ),
            summary=f"{width}×{height} {best_mime}, source_thumbnail",
            details={
                "width": width,
                "height": height,
                "mime": best_mime,
                "source": "source_thumbnail",
            },
        )


def _thumbnail_candidates(source: SourceMetadataPayload) -> list[_ImageCandidate]:
    seen: set[str] = set()
    result: list[_ImageCandidate] = []

    def add(
        url: str | None,
        score: float,
        min_width: int,
        min_height: int,
    ) -> None:
        if not url or url in seen:
            return
        seen.add(url)
        result.append(
            _ImageCandidate(
                url=url,
                score=score,
                min_width=min_width,
                min_height=min_height,
                order=len(result),
            )
        )

    if _is_youtube(source):
        video_id = source.external_id.strip()
        if _VIDEO_ID_RE.fullmatch(video_id):
            base = f"https://img.youtube.com/vi/{video_id}"
            # Best-to-worst.  YouTube may return a small placeholder with HTTP
            # 200 for a missing ``maxresdefault``; the minimum dimensions below
            # catch that case and the loop keeps looking for a better image.
            for name, score, minimum in (
                ("maxresdefault.jpg", 0.99, (640, 360)),
                ("hq720.jpg", 0.96, (640, 360)),
                ("maxresdefault.webp", 0.95, (640, 360)),
                ("sddefault.jpg", 0.88, (480, 360)),
                ("hqdefault.jpg", 0.82, (320, 180)),
                ("mqdefault.jpg", 0.74, (200, 120)),
                ("0.jpg", 0.70, (100, 75)),
                ("1.jpg", 0.68, (100, 75)),
                ("2.jpg", 0.66, (100, 75)),
                ("3.jpg", 0.64, (100, 75)),
                ("default.jpg", 0.55, (80, 60)),
            ):
                add(f"{base}/{name}", score, minimum[0], minimum[1])

    youtube_candidates_added = bool(result)
    thumbnails = sorted(
        source.thumbnails,
        key=lambda item: (item.width or 0) * (item.height or 0),
        reverse=True,
    )
    for index, thumbnail in enumerate(thumbnails[:8]):
        if youtube_candidates_added and _is_youtube_image_url(thumbnail.url):
            continue
        resolution = (thumbnail.width or 0) * (thumbnail.height or 0)
        score = min(0.9, 0.45 + resolution / 2_000_000) - index * 0.01
        add(
            thumbnail.url,
            max(0.1, score),
            _MIN_SOURCE_THUMBNAIL[0],
            _MIN_SOURCE_THUMBNAIL[1],
        )
    return result


def _is_youtube_image_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return False
    return host in _YOUTUBE_IMAGE_HOSTS


def _cover_score(base: float, width: int, height: int) -> float:
    pixels = max(1, width * height)
    bonus = min(0.04, pixels / _IDEAL_COVER_PIXELS * 0.04)
    return max(0.0, min(1.0, base + bonus))


def _convert_webp_to_png(data: bytes) -> bytes | None:
    """Convert a WebP cover to PNG so the canonical cover model can embed it."""

    executable = shutil.which("ffmpeg")
    if executable is None or not data:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed ffmpeg argv, no shell
            [
                executable,
                "-nostdin",
                "-v",
                "error",
                "-i",
                "pipe:0",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "pipe:1",
            ],
            input=data,
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    output = completed.stdout
    return output if output.startswith(b"\x89PNG\r\n\x1a\n") else None


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

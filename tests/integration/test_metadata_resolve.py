"""End-to-end metadata resolution with HTTP cassettes and a fake fpcalc.

The test uses a real local media fixture and the real observation pipeline.  All
network calls are intercepted by respx, while ``fpcalc`` is a tiny executable
script in ``tmp_path`` so the suite never depends on a system Chromaprint
installation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
import respx
from tests.adapters.conftest import FakeClock, fixture
from tests.fixtures.media import generate_media, generate_png, skip_without_media_tools

from trackforge.adapters.http.client import HttpClient
from trackforge.adapters.http.rate_limit import HostRateLimiter
from trackforge.adapters.http.url_safety import UrlSafetyGuard
from trackforge.application.dto.plan import AcquireOptions, PlanOptions
from trackforge.application.ports.observations import METADATA_ARTIFACTS
from trackforge.application.use_cases.observe_item import ObserveItem
from trackforge.bootstrap.container import (
    build_catalog_context,
    build_observer_capabilities,
)
from trackforge.config import TrackForgeConfig
from trackforge.domain.model.item import ItemStatus
from trackforge.domain.model.source import LocalPath

pytestmark = pytest.mark.integration

MBID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RELEASE_MBID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
MB_BASE = "https://musicbrainz.org/ws/2"
ACOUSTID_URL = "https://api.acoustid.org/v2/lookup"
CAA_URL = f"https://coverartarchive.org/release/{RELEASE_MBID}"
LRCLIB_URL = "https://lrclib.net/api/get"


def _write_fake_fpcalc(tmp_path: Path) -> Path:
    path = tmp_path / "fpcalc"
    path.write_text(
        f"""#!{sys.executable}
import json
import sys

if "-version" in sys.argv:
    print("fpcalc version 1.5.1")
else:
    print(json.dumps({{"duration": 0.75, "fingerprint": "AQADtEmiTestFingerprint"}}))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _metadata_config(config: TrackForgeConfig) -> TrackForgeConfig:
    return TrackForgeConfig(
        **{
            **config.model_dump(),
            "acoustid_api_key": "test-acoustid-key",
            "musicbrainz_user_agent": "trackforge-tests@example.com",
        }
    )


def _client(clock: FakeClock) -> HttpClient:
    return HttpClient(
        contact="trackforge-tests@example.com",
        guard=UrlSafetyGuard(resolver=lambda host, port: ("93.184.216.34",)),
        limiter=HostRateLimiter(clock=clock, sleep=clock.sleep),
        sleep=clock.sleep,
        rng=lambda: 0.0,
    )


async def _observe(
    context,
    config: TrackForgeConfig,
    item_id,
    *,
    clock: FakeClock,
    fpcalc: Path,
    offline: bool = False,
    refresh: bool = False,
):
    capabilities = build_observer_capabilities(
        config,
        cas=context.cas,
        http_client=_client(clock),
        fpcalc_executable=str(fpcalc),
    )
    observer = ObserveItem(
        context.unit_of_work,
        capabilities,
        context.artifact_store,
        config={"offline": offline, "profile": "balanced"},
        metadata_enabled=True,
    )
    return await observer.observe(item_id, "balanced", refresh=refresh)


@pytest.mark.asyncio
async def test_local_metadata_cassettes_resolve_title_artist_album_and_ids(
    tmp_path: Path,
    config: TrackForgeConfig,
) -> None:
    skip_without_media_tools()
    media = generate_media(tmp_path / "media", "mp3")
    cover = generate_png(tmp_path / "cover", width=1200, height=1200)
    fake_fpcalc = _write_fake_fpcalc(tmp_path)
    metadata_config = _metadata_config(config)
    clock = FakeClock()

    image_url = f"https://coverartarchive.org/release/{RELEASE_MBID}/front-1200.jpg"
    with respx.mock:
        mb_route = respx.get(f"{MB_BASE}/recording").mock(
            return_value=httpx.Response(200, json=fixture("musicbrainz_search"))
        )
        acoustid_route = respx.get(ACOUSTID_URL).mock(
            return_value=httpx.Response(200, json=fixture("acoustid_lookup"))
        )
        caa_route = respx.get(CAA_URL).mock(
            return_value=httpx.Response(200, json=fixture("caa_release"))
        )
        image_route = respx.get(image_url).mock(
            return_value=httpx.Response(
                200,
                content=cover.read_bytes(),
                headers={"Content-Type": "image/png"},
            )
        )
        lrclib_route = respx.get(LRCLIB_URL).mock(
            return_value=httpx.Response(200, json=fixture("lrclib_get"))
        )

        with build_catalog_context(metadata_config) as context:
            plan = await context.plan_item().plan(
                LocalPath(str(media.path)),
                PlanOptions(),
                context.source_resolvers(),
            )
            assert plan.blocked_reason is None
            acquired = await context.acquire_item().acquire(
                plan.candidates[0],
                AcquireOptions(),
                context.acquirers(),
            )
            observed = await _observe(
                context,
                metadata_config,
                acquired.item_id,
                clock=clock,
                fpcalc=fake_fpcalc,
            )
            assert observed.status is ItemStatus.OBSERVED
            artifact_types = {summary.type for summary in observed.artifacts}
            assert {str(artifact) for artifact in METADATA_ARTIFACTS} <= artifact_types
            assert "audio.fingerprint.v1" in artifact_types
            assert "cover.candidates.v1" in artifact_types
            assert "lyrics.candidates.v1" in artifact_types

            interpretation = await context.interpret_item().interpret(acquired.item_id)
            claim_fields = {(claim.field, claim.source) for claim in interpretation.claims}
            assert ("title", "musicbrainz") in claim_fields
            assert ("artist", "musicbrainz") in claim_fields
            assert ("album", "musicbrainz") in claim_fields
            assert ("mb_recording_id", "musicbrainz") in claim_fields
            assert ("isrc", "musicbrainz") in claim_fields
            assert ("artwork", "cover_art_archive") in claim_fields
            assert ("lyrics", "lrclib") in claim_fields
            assert ("synced_lyrics", "lrclib") in claim_fields
            assert interpretation.identity is not None
            assert interpretation.identity["status"] in {"proposed", "ambiguous"}
            assert interpretation.identity["hypotheses"]
            hypothesis = interpretation.identity["hypotheses"][0]
            assert hypothesis["score"] > 0.5
            assert hypothesis["evidence"]
            assert hypothesis["external_ids"]["musicbrainz_recording"] == MBID

            resolved = await context.resolve_item().resolve(acquired.item_id)
            assert resolved.fields["title"] == "Fixture Song"
            assert resolved.fields["artist"] == "MusicBrainz Artist"
            assert resolved.fields["album"] == "MusicBrainz Album"
            assert resolved.fields["date"] == "2024-05-03"
            assert resolved.fields["mb_recording_id"] == MBID
            assert resolved.fields["mb_release_id"] == RELEASE_MBID
            assert resolved.fields["isrc"] == "USRC17607839"
            assert "Rock" in resolved.fields["genre"]
            assert resolved.fields["label"] == "Fixture Label"
            assert resolved.fields["barcode"] == "1234567890123"
            assert resolved.fields["lyrics"] is not None
            assert resolved.fields["synced_lyrics"] is not None

            # A second observe serves metadata from cached artifacts.
            second = await _observe(
                context,
                metadata_config,
                acquired.item_id,
                clock=clock,
                fpcalc=fake_fpcalc,
            )
            assert second.cache_hit is True
            assert all(step.status in {"cached", "skipped"} for step in second.steps)

            # Expire the cached MusicBrainz artifact and observe again without
            # refresh: the executor must reuse it and surface a stale warning.
            mb_summary = next(
                summary for summary in second.artifacts if summary.type == "musicbrainz.search.v1"
            )
            mb_ref = await context.artifact_store.find(mb_summary.cache_key)
            assert mb_ref is not None
            manifest_document = json.loads(mb_ref.manifest_path.read_text(encoding="utf-8"))
            manifest_document["expires_at"] = "2000-01-01T00:00:00Z"
            mb_ref.manifest_path.write_text(
                json.dumps(manifest_document, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            stale = await _observe(
                context,
                metadata_config,
                acquired.item_id,
                clock=clock,
                fpcalc=fake_fpcalc,
            )
            assert stale.cache_hit is True
            assert "stale" in {warning.code for warning in stale.warnings}

        assert mb_route.call_count == 1
        assert acoustid_route.call_count == 1
        assert caa_route.call_count == 1
        assert image_route.call_count == 1
        assert lrclib_route.call_count == 1


@pytest.mark.asyncio
async def test_offline_metadata_skips_every_network_provider(
    tmp_path: Path,
    config: TrackForgeConfig,
) -> None:
    skip_without_media_tools()
    media = generate_media(tmp_path / "media", "mp3")
    fake_fpcalc = _write_fake_fpcalc(tmp_path)
    metadata_config = _metadata_config(config)
    clock = FakeClock()

    with respx.mock:
        network = respx.route().mock(return_value=httpx.Response(500))
        with build_catalog_context(metadata_config) as context:
            plan = await context.plan_item().plan(
                LocalPath(str(media.path)),
                PlanOptions(),
                context.source_resolvers(),
            )
            acquired = await context.acquire_item().acquire(
                plan.candidates[0],
                AcquireOptions(),
                context.acquirers(),
            )
            observed = await _observe(
                context,
                metadata_config,
                acquired.item_id,
                clock=clock,
                fpcalc=fake_fpcalc,
                offline=True,
                refresh=True,
            )
            status_by_capability = {step.capability_id: step.status for step in observed.steps}
            for capability in (
                "acoustid_lookup",
                "musicbrainz_search",
                "cover_search",
                "lrclib_search",
            ):
                assert status_by_capability[capability] == "skipped"
            warning_codes = {warning.code for warning in observed.warnings}
            assert "offline" in warning_codes
            assert "audio.fingerprint.v1" in {summary.type for summary in observed.artifacts}

        assert network.call_count == 0


@pytest.mark.asyncio
async def test_refresh_replaces_expired_external_artifacts(
    tmp_path: Path,
    config: TrackForgeConfig,
) -> None:
    skip_without_media_tools()
    media = generate_media(tmp_path / "media", "mp3")
    fake_fpcalc = _write_fake_fpcalc(tmp_path)
    metadata_config = _metadata_config(config)
    clock = FakeClock()

    with respx.mock:
        mb_route = respx.get(f"{MB_BASE}/recording").mock(
            return_value=httpx.Response(200, json=fixture("musicbrainz_search"))
        )
        respx.get(ACOUSTID_URL).mock(
            return_value=httpx.Response(200, json=fixture("acoustid_lookup"))
        )
        respx.get(CAA_URL).mock(return_value=httpx.Response(404))
        respx.get("https://itunes.apple.com/search").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        respx.get("https://api.deezer.com/search").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        respx.get(LRCLIB_URL).mock(return_value=httpx.Response(404))

        with build_catalog_context(metadata_config) as context:
            plan = await context.plan_item().plan(
                LocalPath(str(media.path)),
                PlanOptions(),
                context.source_resolvers(),
            )
            acquired = await context.acquire_item().acquire(
                plan.candidates[0],
                AcquireOptions(),
                context.acquirers(),
            )
            await _observe(
                context,
                metadata_config,
                acquired.item_id,
                clock=clock,
                fpcalc=fake_fpcalc,
            )
            assert mb_route.call_count == 1
            refreshed = await _observe(
                context,
                metadata_config,
                acquired.item_id,
                clock=clock,
                fpcalc=fake_fpcalc,
                refresh=True,
            )
            assert mb_route.call_count == 2
            assert "musicbrainz_search" in {
                step.capability_id for step in refreshed.steps if step.status == "succeeded"
            }

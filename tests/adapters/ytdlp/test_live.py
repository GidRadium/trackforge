"""Optional live network check.

CI excludes the ``network`` marker (see pyproject.toml).  Run manually with:

    TRACKFORGE_LIVE_YTDLP_URL="https://..." \
        uv run pytest tests/adapters/ytdlp/test_live.py -m network -q
"""

from __future__ import annotations

import os
import shutil

import pytest

from trackforge.adapters.sources.ytdlp.client import YtDlpClient
from trackforge.adapters.sources.ytdlp.source import YtDlpSource
from trackforge.domain.model.source import Url

pytestmark = pytest.mark.network


@pytest.mark.asyncio
async def test_live_single_track_metadata() -> None:
    url = os.environ.get("TRACKFORGE_LIVE_YTDLP_URL")
    if not url:
        pytest.skip("set TRACKFORGE_LIVE_YTDLP_URL to run the live yt-dlp check")
    if shutil.which("yt-dlp") is None:
        pytest.skip("yt-dlp is not installed")
    source = YtDlpSource(client=YtDlpClient())
    resolved = await source.resolve(Url(url))
    assert resolved.candidates
    assert resolved.candidates[0].formats
    assert not any(fmt.drm for fmt in resolved.candidates[0].formats)

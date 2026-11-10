"""Tiny image-header probe used to verify thumbnail dimensions.

Only a short prefix of the image is read.  Nothing is written to disk.
"""

# pyright: standard

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class ImageSize:
    width: int
    height: int
    ext: str
    mime: str


_JPEG_SOF = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def parse_image_size(data: bytes) -> ImageSize | None:
    """Parse PNG/JPEG/WebP/GIF dimensions from the first bytes of an image."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return ImageSize(width, height, "png", "image/png")

    if data.startswith(b"\xff\xd8"):
        index = 2
        while index + 4 <= len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            length = int.from_bytes(data[index + 2 : index + 4], "big")
            if marker in _JPEG_SOF and index + 9 <= len(data):
                height = int.from_bytes(data[index + 5 : index + 7], "big")
                width = int.from_bytes(data[index + 7 : index + 9], "big")
                return ImageSize(width, height, "jpg", "image/jpeg")
            if length < 2:
                break
            index += 2 + length

    if (
        len(data) >= 30
        and data[0:4] == b"RIFF"
        and data[8:12] == b"WEBP"
    ):
        chunk = data[12:16]
        if chunk == b"VP8X":
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return ImageSize(width, height, "webp", "image/webp")
        if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
            return ImageSize(width, height, "webp", "image/webp")
        if chunk == b"VP8L" and data[20] == 0x2F and len(data) >= 25:
            bits = int.from_bytes(data[21:25], "little")
            width = 1 + (bits & 0x3FFF)
            height = 1 + ((bits >> 14) & 0x3FFF)
            return ImageSize(width, height, "webp", "image/webp")

    if data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 10:
        width = int.from_bytes(data[6:8], "little")
        height = int.from_bytes(data[8:10], "little")
        return ImageSize(width, height, "gif", "image/gif")
    return None


def fetch_image_size(
    url: str,
    *,
    timeout: float = 20.0,
    proxy: str | None = None,
    headers: dict[str, str] | None = None,
) -> ImageSize | None:
    """Fetch a short prefix of ``url`` and parse its dimensions."""
    request_headers = {
        "User-Agent": "TrackForge/1.1",
        "Accept": "image/*",
        "Accept-Encoding": "identity",
        "Range": "bytes=0-65535",
    }
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers, method="GET")
    try:
        if proxy and urlsplit(proxy).scheme.lower() in {"http", "https"}:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
            response = opener.open(request, timeout=timeout)
        elif proxy:
            # urllib has no SOCKS support; leave dimensions unverified.
            return None
        else:
            response = urllib.request.urlopen(request, timeout=timeout)
        try:
            return parse_image_size(response.read(65536))
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    except (urllib.error.URLError, OSError, ValueError):
        return None


__all__ = ["ImageSize", "fetch_image_size", "parse_image_size"]

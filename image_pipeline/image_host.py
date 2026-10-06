"""freeimage.host uploader implementing the ``ImageHost`` protocol.

The API key comes from ``FREEIMAGE_API_KEY`` (set on the config object, which reads
the env var) - there is deliberately **no default key in code**.  HTTP 429 and 5xx
are retryable; invalid key / quota / forbidden raise ``PermanentAPIError`` which
aborts the whole run after the checkpoint has been flushed.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Mapping
from typing import Any, Final

import requests

from fetch import DomainRateLimiter, FetchError, PermanentAPIError, retry_with_backoff

LOG = logging.getLogger("pipeline.host")

ENDPOINT: Final[str] = "https://freeimage.host/api/1/upload"

_PERMANENT_HINTS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"invalid\s+(api\s*)?key", re.IGNORECASE),
    re.compile(r"api\s*key\s+(is\s+)?(missing|required|invalid)", re.IGNORECASE),
    re.compile(r"quota|limit\s+reached|exceeded", re.IGNORECASE),
    re.compile(r"forbidden|not\s+authori[sz]ed|banned", re.IGNORECASE),
    re.compile(r"invalid\s+(user|account)", re.IGNORECASE),
)


def classify_upload_response(payload: Mapping[str, Any]) -> tuple[str, str]:
    """Return ``(kind, message)`` with kind in ``{"ok", "permanent", "transient"}``."""
    status_code = payload.get("status_code")
    error = payload.get("error")
    message = ""
    if isinstance(error, Mapping):
        raw = error.get("message")
        message = raw if isinstance(raw, str) else str(raw or "")
    elif isinstance(error, str):
        message = error

    if str(status_code) == "200":
        image = payload.get("image")
        if isinstance(image, Mapping) and isinstance(image.get("url"), str):
            return "ok", str(image["url"])
        return "transient", "HTTP 200 without image.url"

    text = message or json.dumps(payload, default=str)[:300]
    if any(pattern.search(text) for pattern in _PERMANENT_HINTS):
        return "permanent", text
    if str(status_code) in {"401", "403", "400"}:
        return "permanent", text or f"HTTP {status_code}"
    return "transient", text or f"HTTP {status_code}"


class FreeImageHost:
    """Thread-safe image host client with its own upload rate limiter."""

    def __init__(
        self,
        *,
        api_key: str,
        min_interval_s: float = 0.6,
        timeout_s: float = 60.0,
        max_retries: int = 3,
        base_delay_s: float = 0.8,
        max_delay_s: float = 8.0,
        endpoint: str = ENDPOINT,
    ) -> None:
        if not api_key:
            raise PermanentAPIError("FREEIMAGE_API_KEY is not configured")
        self._api_key = api_key
        self._endpoint = endpoint
        self._timeout = timeout_s
        self._max_retries = max_retries
        self._base_delay = base_delay_s
        self._max_delay = max_delay_s
        self._limiter = DomainRateLimiter(min_interval_s)
        self._lock = threading.Lock()

    def upload(self, data: bytes, filename: str) -> str:
        """Upload JPEG bytes, return the public https URL."""

        def _do() -> str:
            with self._lock:
                self._limiter.wait(self._endpoint)
                time.sleep(0)
            response = requests.post(
                self._endpoint,
                data={"key": self._api_key, "format": "json", "action": "upload"},
                files={"source": (filename, data, "image/jpeg")},
                timeout=self._timeout,
            )
            if response.status_code == 429 or response.status_code >= 500:
                raise FetchError(f"upload HTTP {response.status_code}")
            try:
                payload: Any = response.json()
            except ValueError as exc:
                raise FetchError(f"upload returned non-JSON body: {exc}") from exc
            if not isinstance(payload, Mapping):
                raise FetchError("upload returned an unexpected JSON shape")
            kind, message = classify_upload_response(payload)
            if kind == "permanent":
                raise PermanentAPIError(f"image host refused the upload: {message}")
            if kind == "transient":
                raise FetchError(f"image host transient error: {message}")
            return message

        return retry_with_backoff(
            _do,
            max_retries=self._max_retries,
            base_delay_s=self._base_delay,
            max_delay_s=self._max_delay,
            description=f"upload {filename}",
        )

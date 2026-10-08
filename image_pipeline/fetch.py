"""HTTP layer: curl_cffi sessions, per-domain rate limiting, retry with jitter.

Only this module (plus ``image_host`` and ``csv_io``) touches the network or
the filesystem.  Everything else is pure logic.
"""

from __future__ import annotations

import contextlib
import logging
import random
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from typing import Any, Final, TypeVar
from urllib.parse import urlsplit

from curl_cffi import requests as cffi_requests

from config import PipelineConfig
from protocols import BinaryResponse, PageResponse

T = TypeVar("T")

LOG = logging.getLogger("pipeline.fetch")

#: HTTP statuses where retrying is pointless (page or request will never change).
PERMANENT_STATUS: Final[frozenset[int]] = frozenset(
    {
        int(HTTPStatus.BAD_REQUEST),          # 400
        int(HTTPStatus.UNAUTHORIZED),         # 401 (host side; page fetch: permanent)
        int(HTTPStatus.FORBIDDEN),            # 403
        int(HTTPStatus.NOT_FOUND),            # 404
        int(HTTPStatus.GONE),                 # 410
        int(HTTPStatus.UNPROCESSABLE_ENTITY), # 422
    }
)

#: HTTP statuses that are usually transient.
TRANSIENT_STATUS: Final[frozenset[int]] = frozenset(
    {
        408,
        425,
        int(HTTPStatus.TOO_MANY_REQUESTS),          # 429
        int(HTTPStatus.INTERNAL_SERVER_ERROR),      # 500
        int(HTTPStatus.BAD_GATEWAY),                # 502
        int(HTTPStatus.SERVICE_UNAVAILABLE),        # 503
        int(HTTPStatus.GATEWAY_TIMEOUT),            # 504
    }
)


class FetchError(RuntimeError):
    """Transient failure: retrying may help."""


class PermanentAPIError(FetchError):
    """Permanent failure: retrying cannot help (404 page, invalid API key...)."""


def classify_http_status(status: int) -> str:
    """Return 'ok' | 'permanent' | 'transient' for an HTTP status code."""
    if 200 <= status < 300:
        return "ok"
    if status in PERMANENT_STATUS:
        return "permanent"
    if status in TRANSIENT_STATUS:
        return "transient"
    if 400 <= status < 500:
        return "permanent"
    return "transient"


class DomainRateLimiter:
    """Minimum interval between requests to the same host, shared by all threads."""

    def __init__(self, min_interval_s: float) -> None:
        self._min_interval_s = max(0.0, min_interval_s)
        self._lock = threading.Lock()
        self._last_hit: dict[str, float] = {}

    def wait(self, url: str) -> None:
        """Block until this host may be contacted again."""
        if self._min_interval_s <= 0:
            return
        host = urlsplit(url).netloc.lower()
        if not host:
            return
        with self._lock:
            now = time.monotonic()
            last = self._last_hit.get(host)
            delay = 0.0
            if last is not None:
                delay = self._min_interval_s - (now - last)
            if delay > 0:
                time.sleep(delay)
            self._last_hit[host] = time.monotonic()


def retry_with_backoff(
    operation: Callable[[], T],
    *,
    max_retries: int,
    base_delay_s: float,
    max_delay_s: float,
    description: str,
    sleeper: Callable[[float], None] = time.sleep,
    random_fn: Callable[[], float] = random.random,
    attempts_holder: list[int] | None = None,
) -> T:
    """Run ``operation`` with exponential backoff + jitter.

    ``PermanentAPIError`` is re-raised immediately.  Other ``FetchError``s and
    network-level OSErrors are retried up to ``max_retries`` times.  This is pure
    logic: tests inject deterministic ``sleeper``/``random_fn`` callables.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return operation()
        except PermanentAPIError:
            raise
        except (FetchError, OSError) as exc:
            if attempt > max_retries:
                raise FetchError(
                    f"{description}: giving up after {attempt} attempts ({exc})"
                ) from exc
            delay = min(max_delay_s, base_delay_s * (2 ** (attempt - 1)))
            delay *= 0.5 + random_fn()          # full jitter in [0.5, 1.5)
            LOG.warning(
                "[retry] %s (attempt %d/%d) in %.2fs: %s",
                description,
                attempt,
                max_retries,
                delay,
                exc,
            )
            with contextlib.suppress(Exception):   # pragma: no cover - defensive
                sleeper(delay)
        finally:
            if attempts_holder is not None and len(attempts_holder) < attempt:
                attempts_holder.append(attempt)


class _SessionFactory:
    """One curl_cffi session **per host** per worker thread (sessions are not thread-safe).

    Reusing a session keeps the impersonated TLS fingerprint, the connection and the
    cookies for that host - which is what keeps a crawler polite and unblocked.  The
    impersonation profile is pinned in config, never random.
    """

    def __init__(self, config: PipelineConfig) -> None:
        self._config = config
        self._local = threading.local()

    def session(self, url: str = "") -> Any:
        host = urlsplit(url).netloc.lower() if url else ""
        cache: dict[str, Any] | None = getattr(self._local, "sessions", None)
        if cache is None:
            cache = {}
            self._local.sessions = cache
        session = cache.get(host)
        if session is None:
            session = cffi_requests.Session(
                impersonate=self._config.impersonate,
                timeout=(self._config.connect_timeout_s, self._config.request_timeout_s),
            )
            session.headers.update(
                {
                    "User-Agent": self._config.user_agent,
                    "Accept-Language": self._config.accept_language,
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "image/avif,image/webp,*/*;q=0.8"
                    ),
                }
            )
            cache[host] = session
        return session


class CurlCffiPageFetcher:
    """``PageFetcher`` implementation: HTML pages through curl_cffi."""

    def __init__(self, config: PipelineConfig, limiter: DomainRateLimiter) -> None:
        self._config = config
        self._limiter = limiter
        self._sessions = _SessionFactory(config)

    def fetch_page(self, url: str) -> PageResponse:
        def _do() -> PageResponse:
            self._limiter.wait(url)
            response = self._sessions.session(url).get(url, allow_redirects=True)
            kind = classify_http_status(response.status_code)
            if kind == "permanent":
                raise PermanentAPIError(
                    f"page {url} returned HTTP {response.status_code}"
                )
            if kind == "transient":
                raise FetchError(f"page {url} returned HTTP {response.status_code}")
            return PageResponse(
                url=str(response.url or url),
                status_code=response.status_code,
                text=response.text,
            )

        return retry_with_backoff(
            _do,
            max_retries=self._config.max_retries,
            base_delay_s=self._config.backoff_base_s,
            max_delay_s=self._config.backoff_max_s,
            description=f"GET page {url}",
        )


class CurlCffiImageFetcher:
    """``ImageFetcher`` implementation: bytes through curl_cffi, Referer = page."""

    def __init__(self, config: PipelineConfig, limiter: DomainRateLimiter) -> None:
        self._config = config
        self._limiter = limiter
        self._sessions = _SessionFactory(config)

    def fetch_image(self, url: str, referer: str) -> BinaryResponse:
        def _do() -> BinaryResponse:
            self._limiter.wait(url)
            headers = {"Referer": referer, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
            response = self._sessions.session(url).get(
                url, headers=headers, allow_redirects=True
            )
            kind = classify_http_status(response.status_code)
            if kind == "permanent":
                raise PermanentAPIError(
                    f"image {url} returned HTTP {response.status_code}"
                )
            if kind == "transient":
                raise FetchError(f"image {url} returned HTTP {response.status_code}")
            content_type = str(response.headers.get("content-type", ""))
            return BinaryResponse(
                url=str(response.url or url),
                status_code=response.status_code,
                content=bytes(response.content),
                content_type=content_type,
            )

        return retry_with_backoff(
            _do,
            max_retries=self._config.max_retries,
            base_delay_s=self._config.backoff_base_s,
            max_delay_s=self._config.backoff_max_s,
            description=f"GET image {url}",
        )

    def probe_image(self, url: str, referer: str, max_bytes: int) -> BinaryResponse | None:
        """Range-GET only the first ``max_bytes``: dimensions without the download.

        Returns ``None`` when the server refuses, answers with HTML, or the bytes do
        not carry dimensions.  Never raises - a probe is an optimisation, not a gate.
        """
        try:
            self._limiter.wait(url)
            headers = {
                "Referer": referer,
                "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                "Range": f"bytes=0-{max(4096, max_bytes) - 1}",
            }
            response = self._sessions.session(url).get(
                url, headers=headers, allow_redirects=True
            )
            if response.status_code not in (200, 206):
                return None
            content_type = str(response.headers.get("content-type", ""))
            if "html" in content_type.lower():
                return None
            return BinaryResponse(
                url=str(response.url or url),
                status_code=response.status_code,
                content=bytes(response.content),
                content_type=content_type,
            )
        except Exception:                                   # pragma: no cover - network
            return None


class CurlCffiJsonHttp:
    """``sources.JsonHttp``: one polite GET for search/sitemap/robots endpoints.

    Uses the *search* timeout, one attempt, and swallows every failure as ``None``:
    a fallback source being down must never break a row.
    """

    def __init__(self, config: PipelineConfig, limiter: DomainRateLimiter) -> None:
        self._config = config
        self._limiter = limiter
        self._sessions = _SessionFactory(config)

    def get_text(self, url: str) -> str | None:
        try:
            self._limiter.wait(url)
            response = self._sessions.session(url).get(
                url,
                allow_redirects=True,
                timeout=self._config.source_search_timeout_s,
                headers={"Accept": "application/json,application/xml,text/xml,text/html;q=0.9,*/*;q=0.8"},
            )
        except Exception:
            return None
        if response.status_code != 200:
            return None
        text = response.text
        return text if isinstance(text, str) and text.strip() else None

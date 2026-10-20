"""Network boundaries as Protocols, so the pipeline is testable with fakes.

Nothing in ``pipeline.py`` imports ``requests``/``curl_cffi``: it only knows
these three interfaces.  Production implementations live in ``fetch.py`` and
``image_host.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from models import ModelKeys, PageCandidate, SourceSearchReport


@dataclass(frozen=True)
class PageResponse:
    """A fetched HTML page."""

    url: str
    status_code: int
    text: str


@dataclass(frozen=True)
class BinaryResponse:
    """A fetched binary body plus its declared content type."""

    url: str
    status_code: int
    content: bytes
    content_type: str


@runtime_checkable
class PageFetcher(Protocol):
    """Fetches HTML pages.  Raises FetchError/PermanentAPIError on failure."""

    def fetch_page(self, url: str) -> PageResponse:
        ...


@runtime_checkable
class ImageFetcher(Protocol):
    """Fetches image bytes with the page URL as Referer."""

    def fetch_image(self, url: str, referer: str) -> BinaryResponse:
        ...

    def probe_image(self, url: str, referer: str, max_bytes: int) -> BinaryResponse | None:
        """Fetch only the first ``max_bytes`` of an image (dimension probe).

        Optional by design: an implementation may return ``None`` and the pipeline
        simply downloads the whole file instead.
        """
        ...


@runtime_checkable
class SourceSearcher(Protocol):
    """Finds candidate product pages when the sheet's own URLs did not solve a row.

    Returns the candidates plus a human-readable line for ``run_report.csv``.  The
    implementation must only *find pages* - verification is the pipeline's job (S15).
    """

    def find_candidates(
        self, *, title: str, brand: str | None, keys: ModelKeys, wide: bool = False
    ) -> tuple[list[PageCandidate], SourceSearchReport]:
        ...


@runtime_checkable
class ImageHost(Protocol):
    """Uploads JPEG bytes and returns a public https URL.

    Implementations raise ``PermanentAPIError`` when retrying cannot help
    (invalid key, quota, forbidden) and ``FetchError`` for transient failures.
    """

    def upload(self, data: bytes, filename: str) -> str:
        ...

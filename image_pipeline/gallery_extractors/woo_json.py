"""WooCommerce Store API: a product's own image list as JSON.

Same reason as the Shopify module: on a WooCommerce shop the product picture list is
authoritative (and complete) in the Store API, while the HTML gallery can be lazy-loaded
or missing entirely.  Pure functions only; the pipeline decides when to fetch.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from gallery_extractors import ExtractionContext
from model_matching import find_matching_keys
from models import ImageCandidate
from url_filters import canonicalize_url, dedup_key

#: Markers that a page is WordPress/WooCommerce, used when the host is not registered.
WOO_MARKERS: tuple[str, ...] = (
    "woocommerce",
    "wp-content",
    "wp-json",
    "wp-includes",
)

#: Both spellings of the public Store API products route.
STORE_API_PATHS: tuple[str, ...] = (
    "/wp-json/wc/store/v1/products",
    "/wp-json/wc/store/products",
)


def woo_product_slug(page_url: str) -> str:
    """Last path segment of a product page (the Store API looks products up by slug)."""
    path = urlsplit(page_url).path.rstrip("/")
    if not path:
        return ""
    return path.rsplit("/", 1)[-1]


def woo_store_api_urls(page_url: str) -> tuple[str, ...]:
    """Candidate Store API URLs for this product page (in the order to try them)."""
    parts = urlsplit(page_url)
    slug = woo_product_slug(page_url)
    if not slug:
        return ()
    return tuple(
        urlunsplit((parts.scheme or "https", parts.netloc, path, f"slug={slug}", ""))
        for path in STORE_API_PATHS
    )


def looks_like_woo(_page_url: str, html: str) -> bool:
    """True when the page carries WordPress/WooCommerce markers."""
    lowered = html.lower()
    return any(marker in lowered for marker in WOO_MARKERS)


def _entries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    if isinstance(payload, dict):
        product = payload.get("product") if isinstance(payload.get("product"), dict) else payload
        return [product] if isinstance(product, dict) else []
    return []


def parse_store_products(text: str, context: ExtractionContext) -> list[ImageCandidate]:
    """Image candidates from a Store API products response (``[]`` when unusable)."""
    try:
        payload: Any = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    candidates: list[ImageCandidate] = []
    order = 200                                   # after the page's own gallery order
    seen: set[str] = set()
    for product in _entries(payload):
        images = product.get("images")
        if not isinstance(images, list):
            continue
        for image in images:
            url = ""
            if isinstance(image, dict):
                for key in ("src", "url", "full", "large"):
                    if isinstance(image.get(key), str) and image[key].strip():
                        url = image[key].strip()
                        break
            elif isinstance(image, str):
                url = image.strip()
            if not url:
                continue
            url = canonicalize_url(url, context.page_url)
            key = dedup_key(url)
            if not url or not key or key in seen:
                continue
            seen.add(key)
            candidates.append(
                ImageCandidate(
                    url=url,
                    page_url=context.page_url,
                    source="woo_json",
                    order=order,
                    matches_model=bool(
                        find_matching_keys(url, context.keys, include_title_key=False)
                    ),
                )
            )
            order += 1
    return candidates

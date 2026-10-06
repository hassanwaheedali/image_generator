"""Shopify product JSON: the product's *whole* image list, not just one picture.

Why this exists (live case, 2026-10-06): a Shopify shop's search endpoint
(``/search/suggest.json``) returns exactly **one** image per product (``featured_image``),
so a retail row that is rescued by the fallback chain could come back with a single
picture.  The product's own JSON - ``/products/<handle>.json`` - carries every image the
shop has for it, which is what "at least 3 images" needs.

Pure functions only (``text -> objects``): the pipeline decides *when* to fetch, so this
module stays testable without a network.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from gallery_extractors import ExtractionContext
from model_matching import find_matching_keys
from models import ImageCandidate
from url_filters import canonicalize_url, dedup_key

#: ``/products/<handle>`` - a Shopify product page (optionally with a locale prefix).
_PRODUCT_PATH_RE = re.compile(r"/products/([^/?#]+)", re.IGNORECASE)

#: Markers that a page is Shopify, used when the host is not in ``source_platforms``.
SHOPIFY_MARKERS: tuple[str, ...] = (
    "cdn.shopify.com",
    "shopify.com",
    "shopify.theme",
    "myshopify.com",
    "shopifycdn.net",
)


def shopify_product_json_url(page_url: str) -> str:
    """``/products/<handle>?variant=…`` -> ``/products/<handle>.json`` (``""`` if not one)."""
    parts = urlsplit(page_url)
    match = _PRODUCT_PATH_RE.search(parts.path)
    if match is None:
        return ""
    handle = match.group(1)
    path = f"{parts.path[: match.start(1)]}{handle}.json"
    return urlunsplit((parts.scheme or "https", parts.netloc, path, "", ""))


def looks_like_shopify(page_url: str, html: str) -> bool:
    """True for a Shopify product page (URL shape, or the platform's own markers)."""
    if not _PRODUCT_PATH_RE.search(urlsplit(page_url).path):
        return False
    return any(marker in html for marker in SHOPIFY_MARKERS)


def _image_urls(payload: Any) -> list[str]:
    """Every image URL in a Shopify product payload, in the shop's own order.

    Handles both shapes seen in the wild: ``/products/x.json`` (``images`` is a list of
    objects with ``src``, plus ``media[].preview_image.src``) and ``/products/x.js``
    (``images`` is a list of ``//cdn.shopify.com/...`` strings).
    """
    product = payload.get("product") if isinstance(payload, dict) else None
    if not isinstance(product, dict):
        product = payload if isinstance(payload, dict) else {}
    urls: list[str] = []

    def add(raw: Any) -> None:
        if isinstance(raw, str) and raw.strip():
            candidate = raw.strip()
            if candidate.startswith("//"):
                candidate = f"https:{candidate}"
            if candidate not in urls:
                urls.append(candidate)
        elif isinstance(raw, dict):
            for key in ("src", "url", "original_src"):
                if isinstance(raw.get(key), str) and raw[key].strip():
                    add(raw[key])
                    return

    for key in ("images", "media"):
        entries = product.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and "preview_image" in entry:
                add(entry["preview_image"])
            else:
                add(entry)
    featured = product.get("featured_image")
    if isinstance(featured, str) and featured.strip():
        add(featured)
    return urls


def parse_product_json(text: str, context: ExtractionContext) -> list[ImageCandidate]:
    """Image candidates from a Shopify product JSON body (``[]`` on anything unexpected)."""
    try:
        payload: Any = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    candidates: list[ImageCandidate] = []
    order = 200                                   # after the page's own gallery order
    seen: set[str] = set()
    for raw_url in _image_urls(payload):
        # Canonicalise exactly like the HTML gallery does, so the same asset (the page's
        # ``?v=`` form and the JSON's) collapses onto one dedup key instead of two.
        url = canonicalize_url(raw_url, context.page_url)
        key = dedup_key(url)
        if not url or not key or key in seen:
            continue
        seen.add(key)
        candidates.append(
            ImageCandidate(
                url=url,
                page_url=context.page_url,
                source="shopify_json",
                order=order,
                matches_model=bool(
                    find_matching_keys(url, context.keys, include_title_key=False)
                ),
            )
        )
        order += 1
    return candidates

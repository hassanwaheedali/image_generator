"""Source 3: generic embedded JSON ``images`` arrays (Shopify-style).

Only used on strength-2 pages, and only when the JSON object's **own**
``title``/``handle``/``sku`` matches this product's keys - a bare ``"images"`` list
could belong to any other product on the page.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from gallery_extractors import ExtractionContext
from gallery_extractors.jsonutil import (
    iter_json_objects,
    iter_script_bodies,
    string_values,
)
from model_matching import find_matching_keys
from models import ImageCandidate
from url_filters import canonicalize_url

_IDENTITY_FIELDS = ("title", "handle", "sku", "name", "product_title", "productTitle")


def _identity_text(node: dict[str, Any]) -> str:
    parts: list[str] = []
    for field in _IDENTITY_FIELDS:
        value = node.get(field)
        if isinstance(value, str):
            parts.append(value)
    return " | ".join(parts)


def _image_urls(node: dict[str, Any]) -> list[str]:
    collected: list[str] = []
    for key in ("images", "media", "featured_image", "featuredImage"):
        value = node.get(key)
        if value is None:
            continue
        collected.extend(string_values(value))
    return collected


def extract(context: ExtractionContext) -> list[ImageCandidate]:
    """Images of embedded JSON objects that name this exact product."""
    if context.strength < 2:
        return []
    candidates: list[ImageCandidate] = []
    order = 0
    sources = list(iter_script_bodies(context.raw_html))
    if context.raw_html:
        sources.append(context.raw_html)
    for block in sources:
        for node in iter_json_objects(block):
            identity = _identity_text(node)
            if not identity or not find_matching_keys(identity, context.keys):
                continue
            for url in _image_urls(node):
                if urlsplit(url).path.lower().endswith((".js", ".css", ".json")):
                    continue
                canonical = canonicalize_url(url, context.page_url)
                if not canonical:
                    continue
                candidates.append(
                    ImageCandidate(
                        url=canonical,
                        page_url=context.page_url,
                        source="embedded_json",
                        order=order,
                        matches_model=bool(
                            find_matching_keys(canonical, context.keys, include_title_key=False)
                        ),
                        dom_flags=(),
                    )
                )
                order += 1
    return candidates

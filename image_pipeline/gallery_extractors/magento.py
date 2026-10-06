"""Magento gallery extractor (``mage/gallery/gallery`` JSON).

Magento stores the real gallery in ``x-magento-init`` JSON::

    {"[data-gallery-role=gallery-placeholder]": {"mage/gallery/gallery": {
        "data": [{"img": "...", "full": "...", "thumb": "..."}]}}}

Registered for Magento-ish hosts (eshop.pel.com.pk, many appliance shops).
"""

from __future__ import annotations

from typing import Any

from gallery_extractors import ExtractionContext, register_extractor
from gallery_extractors.jsonutil import (
    iter_json_objects,
    iter_script_bodies,
    walk_json,
)
from model_matching import find_matching_keys
from models import ImageCandidate
from url_filters import canonicalize_url

_GALLERY_KEY = "mage/gallery/gallery"
_IMAGE_KEYS = ("full", "img", "large", "medium", "thumb")


def _gallery_items(node: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    payload = node.get(_GALLERY_KEY)
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            items.extend(item for item in data if isinstance(item, dict))
    return items


def extract(context: ExtractionContext) -> list[ImageCandidate]:
    """Magento gallery images, largest asset first (``full`` before ``img``)."""
    candidates: list[ImageCandidate] = []
    order = 0
    blocks = list(iter_script_bodies(context.raw_html)) or [context.raw_html]
    seen: set[str] = set()
    for block in blocks:
        for node in iter_json_objects(block):
            for payload in walk_json(node):
                if not isinstance(payload, dict) or _GALLERY_KEY not in payload:
                    continue
                for item in _gallery_items(payload):
                    for key in _IMAGE_KEYS:
                        value = item.get(key)
                        if not isinstance(value, str):
                            continue
                        canonical = canonicalize_url(value, context.page_url)
                        if not canonical or canonical in seen:
                            continue
                        seen.add(canonical)
                        candidates.append(
                            ImageCandidate(
                                url=canonical,
                                page_url=context.page_url,
                                source="registry:magento",
                                order=order,
                                matches_model=bool(
                                    find_matching_keys(
                                        canonical, context.keys, include_title_key=False
                                    )
                                ),
                                dom_flags=(),
                            )
                        )
                        order += 1
                        break                      # one asset per gallery item
    return candidates


register_extractor("eshop.pel.com.pk", "magento")(extract)

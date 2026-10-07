"""JSON-LD product nodes - source 2 of the allowed gallery sources.

Only a node whose **own** name/sku/mpn/model/productID/url matches the product keys
is used, and only that node's ``image`` is read.  ``ItemList`` / related nodes are
never descended into.
"""

from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup

from gallery_extractors import ExtractionContext
from gallery_extractors.jsonutil import iter_ld_json_blocks, safe_json_loads, walk_json
from model_matching import find_matching_keys
from models import ImageCandidate
from url_filters import canonicalize_url, has_image_extension

_PRODUCT_TYPES = ("product", "individualproduct", "productgroup")
_IDENTITY_FIELDS = ("name", "sku", "mpn", "model", "productID", "productId", "url")


def _type_is_product(node: dict[str, Any]) -> bool:
    node_type = node.get("@type")
    values: list[str] = []
    if isinstance(node_type, str):
        values = [node_type]
    elif isinstance(node_type, list):
        values = [str(item) for item in node_type if isinstance(item, str)]
    return any(value.strip().lower() in _PRODUCT_TYPES for value in values)


def _identity_text(node: dict[str, Any]) -> str:
    parts: list[str] = []
    for field in _IDENTITY_FIELDS:
        value = node.get(field)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            name = value.get("name")
            if isinstance(name, str):
                parts.append(name)
        elif isinstance(value, list):
            parts.extend(str(item) for item in value if isinstance(item, str))
    return " | ".join(parts)


def _image_urls(node: dict[str, Any]) -> list[str]:
    value = node.get("image")
    urls: list[str] = []
    if isinstance(value, str):
        urls.append(value)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict):
                for key in ("url", "contentUrl", "@id"):
                    candidate = item.get(key)
                    if isinstance(candidate, str):
                        urls.append(candidate)
    elif isinstance(value, dict):
        for key in ("url", "contentUrl", "@id"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                urls.append(candidate)
    return urls


def product_node_texts(soup: BeautifulSoup) -> list[str]:
    """Identity strings of every JSON-LD product node (used by page verification)."""
    texts: list[str] = []
    for block in iter_ld_json_blocks(soup):
        payload = safe_json_loads(block)
        if payload is None:
            continue
        for node in walk_json(payload):
            if isinstance(node, dict) and _type_is_product(node):
                identity = _identity_text(node)
                if identity:
                    texts.append(identity)
    return texts


def extract(context: ExtractionContext) -> list[ImageCandidate]:
    """Images of JSON-LD product nodes whose own identity matches this product."""
    candidates: list[ImageCandidate] = []
    order = 0
    for block in iter_ld_json_blocks(context.soup):
        payload = safe_json_loads(block)
        if payload is None:
            continue
        for node in walk_json(payload):
            if not isinstance(node, dict) or not _type_is_product(node):
                continue
            identity = _identity_text(node)
            if not identity or not find_matching_keys(identity, context.keys):
                continue
            for url in _image_urls(node):
                canonical = canonicalize_url(url, context.page_url)
                if not canonical or not has_image_extension(canonical):
                    continue
                candidates.append(
                    ImageCandidate(
                        url=canonical,
                        page_url=context.page_url,
                        source="json_ld",
                        order=order,
                        matches_model=bool(
                            find_matching_keys(canonical, context.keys, include_title_key=False)
                        ),
                        dom_flags=(),
                    )
                )
                order += 1
    return candidates

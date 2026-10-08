"""Source 3b: universal embedded *state* JSON (TCL ``pageData``, AEM, Next.js, ...).

``embedded_json`` only reads ``<script>`` bodies for a few hardcoded keys, so sites
that keep their gallery in a hidden ``<input value="...">`` (TCL) or a hydration blob
never match.  This source harvests JSON from every place a brand site stores state -
``<script>`` bodies, hidden inputs, and ``data-*``/``property`` attributes - then walks
the parsed tree and emits, for **this product only**, any image list found by file
extension rather than field name (``pictureUrl``/``url``/``src``/``original`` all work).

Additive and bounded: it only runs on strength-2 pages, caps the number of parsed
containers and images per node, and every emitted URL still passes the shared
safeguards - so at worst it contributes nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from bs4 import BeautifulSoup

from gallery_extractors import ExtractionContext
from gallery_extractors.jsonutil import safe_json_loads, string_values, walk_json
from model_matching import find_matching_keys
from models import ImageCandidate, ModelKeys
from url_filters import canonicalize_url, has_image_extension

#: Fields that name the product a JSON node describes (superset of embedded_json's).
_IDENTITY_FIELDS: tuple[str, ...] = (
    "title",
    "handle",
    "sku",
    "name",
    "product_title",
    "productTitle",
    "customerModel",
    "model",
    "productName",
    "product_name",
)

_MAX_CONTAINERS: int = 24        # parsed JSON roots per page (bounded work)
_MAX_IMAGES_PER_NODE: int = 12   # gallery cap per matched product node


def _identity_text(node: dict[str, Any]) -> str:
    """Concatenate known product identity fields found in a JSON object."""
    parts: list[str] = []
    for field in _IDENTITY_FIELDS:
        value = node.get(field)
        if isinstance(value, str):
            parts.append(value)
    return " | ".join(parts)


def _node_matches_product(node: dict[str, Any], keys: ModelKeys) -> bool:
    """True when the dictionary node describes the requested product model."""
    identity = _identity_text(node)
    return bool(identity and find_matching_keys(identity, keys))


def _extract_node_image_urls(
    node: dict[str, Any], page_url: str, limit: int = _MAX_IMAGES_PER_NODE
) -> list[str]:
    """Extract unique valid image URLs from string values inside a product node."""
    urls: list[str] = []
    for raw in string_values(node):
        canonical = canonicalize_url(raw, page_url)
        if not canonical or not has_image_extension(canonical) or canonical in urls:
            continue
        urls.append(canonical)
        if len(urls) >= limit:
            break
    return urls


def _build_candidate(
    url: str, page_url: str, keys: ModelKeys, order: int
) -> ImageCandidate:
    """Construct an ImageCandidate from an extracted URL."""
    return ImageCandidate(
        url=url,
        page_url=page_url,
        source="state_json",
        order=order,
        matches_model=bool(find_matching_keys(url, keys, include_title_key=False)),
        dom_flags=(),
    )


def _parse_json_if_object(text: str) -> Any | None:
    """Parse a string only if it starts with a JSON object or array character."""
    stripped = text.strip()
    if not stripped or stripped[0] not in ("{", "["):
        return None
    return safe_json_loads(stripped)


def _iter_raw_state_strings(soup: BeautifulSoup) -> Iterator[str]:
    """Yield text from script bodies and attribute values that may contain JSON."""
    for script in soup.find_all("script"):
        text = (script.string or script.get_text() or "").strip()
        if text:
            yield text

    for tag in soup.find_all(["input", "div", "section"]):
        for value in tag.attrs.values():
            if isinstance(value, str):
                yield value


def _iter_state_payloads(context: ExtractionContext) -> Iterator[Any]:
    """Every JSON blob the page stores in scripts, hidden inputs or data attributes."""
    seen = 0
    for text in _iter_raw_state_strings(context.soup):
        if seen >= _MAX_CONTAINERS:
            return
        parsed = _parse_json_if_object(text)
        if parsed is not None:
            seen += 1
            yield parsed


def extract(context: ExtractionContext) -> list[ImageCandidate]:
    """Images of embedded state objects that name this exact product."""
    if context.strength < 2:
        return []

    candidates: list[ImageCandidate] = []
    for payload in _iter_state_payloads(context):
        for node in walk_json(payload):
            if not isinstance(node, dict) or not _node_matches_product(node, context.keys):
                continue
            for url in _extract_node_image_urls(node, context.page_url):
                candidates.append(
                    _build_candidate(url, context.page_url, context.keys, len(candidates))
                )

    return candidates

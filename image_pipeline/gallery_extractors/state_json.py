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

from gallery_extractors import ExtractionContext
from gallery_extractors.jsonutil import safe_json_loads, string_values, walk_json
from model_matching import find_matching_keys
from models import ImageCandidate
from url_filters import canonicalize_url, has_image_extension

#: Fields that name the product a JSON node describes (superset of embedded_json's).
_IDENTITY_FIELDS = (
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

_MAX_CONTAINERS = 24        # parsed JSON roots per page (bounded work)
_MAX_IMAGES_PER_NODE = 12   # gallery cap per matched product node


def _identity_text(node: dict[str, Any]) -> str:
    parts: list[str] = []
    for field in _IDENTITY_FIELDS:
        value = node.get(field)
        if isinstance(value, str):
            parts.append(value)
    return " | ".join(parts)


def _iter_state_payloads(context: ExtractionContext) -> Iterator[Any]:
    """Every JSON blob the page stores in scripts, hidden inputs or data attributes."""
    seen = 0
    for script in context.soup.find_all("script"):
        if seen >= _MAX_CONTAINERS:
            return
        text = (script.string or script.get_text() or "").strip()
        if text[:1] in ("{", "["):
            parsed = safe_json_loads(text)
            if parsed is not None:
                seen += 1
                yield parsed
    for tag in context.soup.find_all(["input", "div", "section"]):
        if seen >= _MAX_CONTAINERS:
            return
        for value in tag.attrs.values():
            if not isinstance(value, str):
                continue
            text = value.strip()
            if text[:1] in ("{", "["):
                parsed = safe_json_loads(text)
                if parsed is not None:
                    seen += 1
                    yield parsed


def extract(context: ExtractionContext) -> list[ImageCandidate]:
    """Images of embedded state objects that name this exact product."""
    if context.strength < 2:
        return []
    candidates: list[ImageCandidate] = []
    order = 0
    for payload in _iter_state_payloads(context):
        for node in walk_json(payload):
            if not isinstance(node, dict):
                continue
            identity = _identity_text(node)
            if not identity or not find_matching_keys(identity, context.keys):
                continue
            urls: list[str] = []
            for raw in string_values(node):
                canonical = canonicalize_url(raw, context.page_url)
                if canonical and has_image_extension(canonical) and canonical not in urls:
                    urls.append(canonical)
                if len(urls) >= _MAX_IMAGES_PER_NODE:
                    break
            for url in urls:
                candidates.append(
                    ImageCandidate(
                        url=url,
                        page_url=context.page_url,
                        source="state_json",
                        order=order,
                        matches_model=bool(
                            find_matching_keys(url, context.keys, include_title_key=False)
                        ),
                        dom_flags=(),
                    )
                )
                order += 1
    return candidates

"""Source 4 (dedicated gallery containers) and source 5 (scoped whole-page fallback).

The fallback is deliberately the strictest path in the program: it runs only when
sources 1-4 produced nothing, the page has **no** gallery container, the page is
strength 2, and every image URL must itself carry a model key.  That is what keeps
S12 ("zero fallbacks") true while still allowing a strictly-scoped safety net.
"""

from __future__ import annotations

from typing import Any

from gallery_extractors import (
    ExtractionContext,
    candidates_from_nodes,
    has_gallery_container,
    nodes_with_gallery_ancestor,
)
from models import ImageCandidate

#: Upper bound on DOM-audit candidates per page (keeps the decision log readable).
MAX_DOM_AUDIT_NODES = 60


def dom_audit_nodes(context: ExtractionContext) -> list[Any]:
    """Images that S2/S3 must explicitly reject, so the verdict is in the log.

    These nodes are *not* gallery candidates: the pipeline rejects every one of them
    with the rule id recorded in ``dom_flags`` before any download happens.
    """
    from gallery_extractors import dom_flags_for

    audited: list[Any] = []
    for node in context.soup.find_all(["img", "source", "a"]):
        if dom_flags_for(node, context.page_url):
            audited.append(node)
        if len(audited) >= MAX_DOM_AUDIT_NODES:
            break
    return audited


def extract_container_images(context: ExtractionContext) -> list[ImageCandidate]:
    """Images inside dedicated gallery containers (source 4) + the S2/S3 audit set."""
    nodes = nodes_with_gallery_ancestor(context.soup)
    candidates = (
        candidates_from_nodes(nodes, context=context, source="gallery_container")
        if nodes
        else []
    )
    audited = dom_audit_nodes(context)
    if audited:
        candidates.extend(
            candidates_from_nodes(
                audited, context=context, source="dom_audit", start_order=1000
            )
        )
    return candidates


def model_bearing_nodes(context: ExtractionContext) -> list[Any]:
    """Every img/source/a whose own URL is anchored to this product (S12 anchor)."""
    from gallery_extractors import collect_img_urls
    from model_matching import url_matches_this_product

    nodes: list[Any] = []
    for node in context.soup.find_all(["img", "source", "a"]):
        for url in collect_img_urls(node, context.page_url):
            if url_matches_this_product(
                url,
                context.keys,
                brand=context.brand,
                min_length=context.brand_match_min_length,
                min_digits=context.brand_match_min_digits,
            ):
                nodes.append(node)
                break
    return nodes


def extract_whole_page_fallback(context: ExtractionContext) -> list[ImageCandidate]:
    """Strictly-scoped whole-page fallback (source 5).

    Requires all of: no earlier source produced anything (the caller enforces this),
    a verified page, no gallery container, and a model key inside each image URL.
    A bare ``src`` with no identifying token is never accepted here - that is
    safeguard S12 ("no fallback image sources").
    """
    if context.strength < 1:
        return []
    if has_gallery_container(context.soup):
        return []
    nodes = model_bearing_nodes(context)
    if not nodes:
        return []
    return candidates_from_nodes(nodes, context=context, source="whole_page")

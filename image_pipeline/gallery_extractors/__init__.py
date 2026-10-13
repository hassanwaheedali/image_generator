"""Gallery extractor registry.

Adding a brand = **one new file + one ``@register_extractor`` line**.  There is no
if/elif ladder anywhere: the registry maps a domain substring to one function.

Extractors return :class:`ImageCandidate` objects (provenance + order + the DOM
safeguard flags that can only be evaluated while the tag is attached to the tree).
All *decisions* are still made by the single shared filter pipeline, so no extractor
can bypass a safeguard.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from image_quality import to_rgb  # noqa: F401  (re-exported convenience)
from model_matching import extract_colour_families, find_matching_keys
from models import ImageCandidate, ModelKeys
from page_verification import (
    GALLERY_CONTAINER_SELECTORS,
    is_gallery_container,
    is_in_description_region,
    is_linked_to_other_page,
)
from url_filters import canonicalize_url, dedup_key


# --------------------------------------------------------------------------- #
# context
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ExtractionContext:
    """Everything an extractor may look at (already stripped of unwanted regions)."""

    page_url: str
    soup: BeautifulSoup
    keys: ModelKeys
    strength: int
    brand: str | None
    raw_html: str
    match_rule: str = "model_key"
    brand_match_min_length: int = 5
    brand_match_min_digits: int = 3


ExtractorFn = Callable[[ExtractionContext], list[ImageCandidate]]

#: domain substring -> extractor.  First matching entry wins.
REGISTRY: dict[str, ExtractorFn] = {}


def register_extractor(*domain_substrings: str) -> Callable[[ExtractorFn], ExtractorFn]:
    """Decorator: ``@register_extractor("dawlance")`` registers the extractor."""

    def decorator(function: ExtractorFn) -> ExtractorFn:
        for fragment in domain_substrings:
            REGISTRY[fragment.lower()] = function
        return function

    return decorator


def registry_extractor_for(page_url: str) -> tuple[str, ExtractorFn] | None:
    """The brand/platform extractor responsible for this host, if any."""
    host = urlsplit(page_url).netloc.lower()
    for fragment, extractor in REGISTRY.items():
        if fragment in host:
            return fragment, extractor
    return None


# --------------------------------------------------------------------------- #
# URL collection from DOM nodes
# --------------------------------------------------------------------------- #
_ATTR_PRIORITY: tuple[str, ...] = (
    "data-large_image",
    "data-zoom-image",
    "data-highres",
    "data-full",
    "data-original",
    "data-image",
    # Haier's product template puts the crisp gallery asset here (owner-supplied markup,
    # 2026-10-10): <img src="…_60.png" data-imgurl="…_1200.png">. Missing this is why the
    # run took the 60px thumbnail and then *guessed* a size, landing on a blurry unnamed
    # file while the page named the sharp one.
    "data-imgurl",
    "data-lazy-src",
    "data-src",
    "src",
)

_SRCSET_ENTRY_RE = re.compile(r"(\S+)\s+(\d+)(w|x)?", re.IGNORECASE)


def _best_from_srcset(value: str) -> str | None:
    """Largest declared width wins (spec 6.2)."""
    best: tuple[int, str] | None = None
    for match in _SRCSET_ENTRY_RE.finditer(value):
        url, width = match.group(1), int(match.group(2))
        if best is None or width > best[0]:
            best = (width, url)
    return best[1] if best else None


def collect_img_urls(node: Any, page_url: str) -> list[str]:
    """Candidate URLs of one DOM node, best (largest) first, canonicalised."""
    urls: list[str] = []

    if getattr(node, "name", None) == "source":
        srcset = node.get("srcset") or node.get("data-srcset")
        if isinstance(srcset, str):
            best = _best_from_srcset(srcset)
            if best:
                urls.append(best)
        return _canonical_unique(urls, page_url)

    for attribute in _ATTR_PRIORITY:
        value = node.get(attribute)
        if isinstance(value, str) and value.strip():
            urls.append(value)

    for attribute in ("srcset", "data-srcset"):
        value = node.get(attribute)
        if isinstance(value, str):
            best = _best_from_srcset(value)
            if best:
                urls.append(best)

    for source in node.find_all("source"):
        value = source.get("srcset") or source.get("data-srcset")
        if isinstance(value, str):
            best = _best_from_srcset(value)
            if best:
                urls.append(best)

    if getattr(node, "name", None) in ("a", "div"):
        href = node.get("href")
        if isinstance(href, str) and _looks_like_image(href):
            urls.insert(0, href)

    parent = getattr(node, "parent", None)
    if parent is not None:
        for attr in ("data-image", "href"):
            pval = parent.get(attr)
            if isinstance(pval, str) and _looks_like_image(pval):
                urls.insert(0, pval)

    return _canonical_unique(urls, page_url)


def _looks_like_image(url: str) -> bool:
    clean = urlsplit(url).path.lower()
    return clean.endswith((".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".bmp"))


def _canonical_unique(urls: Iterable[str], page_url: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for url in urls:
        canonical = canonicalize_url(url, page_url)
        if not canonical:
            continue
        key = dedup_key(canonical)
        if key in seen:
            continue
        seen.add(key)
        out.append(canonical)
    return out


# --------------------------------------------------------------------------- #
# candidate construction
# --------------------------------------------------------------------------- #
def dom_flags_for(node: Any, page_url: str) -> tuple[str, ...]:
    """S2/S3 flags for a node - the only checks that need DOM context."""
    flags: list[str] = []
    if is_in_description_region(node):
        flags.append("S2")
    if is_linked_to_other_page(node, page_url):
        flags.append("S3")
    return tuple(flags)


def candidates_from_nodes(
    nodes: Iterable[Any],
    *,
    context: ExtractionContext,
    source: str,
    start_order: int = 0,
) -> list[ImageCandidate]:
    """Turn DOM nodes into candidates, preserving gallery order."""
    candidates: list[ImageCandidate] = []
    order = start_order
    for node in nodes:
        urls = collect_img_urls(node, context.page_url)
        if not urls:
            continue
        best = urls[0]
        candidates.append(
            ImageCandidate(
                url=best,
                page_url=context.page_url,
                source=source,
                order=order,
                matches_model=bool(
                    find_matching_keys(best, context.keys, include_title_key=False)
                ),
                dom_flags=dom_flags_for(node, context.page_url),
            )
        )
        order += 1
    return candidates


def nodes_with_gallery_ancestor(soup: BeautifulSoup) -> list[Any]:
    """Every img/source/a that sits inside a dedicated gallery container."""
    nodes: list[Any] = []
    for node in soup.find_all(["img", "source", "a"]):
        ancestor = node.parent
        while ancestor is not None and getattr(ancestor, "name", None) not in (None, "[document]"):
            if is_gallery_container(ancestor):
                nodes.append(node)
                break
            ancestor = ancestor.parent
    return nodes


def has_gallery_container(soup: BeautifulSoup) -> bool:
    """True when the page has any dedicated gallery container."""
    return any(soup.select(selector) for selector in GALLERY_CONTAINER_SELECTORS)


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def _sort_key(candidate: ImageCandidate, context: ExtractionContext) -> tuple[int, int, int]:
    """Colour-preferred, then model-first, then gallery order (all stable)."""
    colour_rank = 1
    if context.keys.colours:
        url_colours = extract_colour_families(candidate.url)
        if url_colours & set(context.keys.colours):
            colour_rank = 0
    model_rank = 0 if candidate.matches_model else 1
    return (colour_rank, model_rank, candidate.order)


def extract_candidates(context: ExtractionContext) -> list[ImageCandidate]:
    """Run the allowed sources in spec order and return the merged candidate list.

    Order of sources (spec 6.3): 1 registry extractor, 2 JSON-LD product node,
    3 embedded JSON, 4 dedicated gallery containers, 5 strictly-scoped whole-page
    fallback (only when 1-4 produced nothing).  Candidates are de-duplicated by
    ``dedup_key`` and sorted colour-first, model-first, gallery order.
    """
    from gallery_extractors import embedded_json, generic_gallery, json_ld, state_json

    collected: list[ImageCandidate] = []

    registry = registry_extractor_for(context.page_url)
    if registry is not None:
        fragment, extractor = registry
        collected.extend(extractor(context))
        _ = fragment

    collected.extend(json_ld.extract(context))
    collected.extend(embedded_json.extract(context))
    collected.extend(state_json.extract(context))
    collected.extend(generic_gallery.extract_container_images(context))

    if not collected:
        collected.extend(generic_gallery.extract_whole_page_fallback(context))

    unique: dict[str, ImageCandidate] = {}
    for candidate in collected:
        key = dedup_key(candidate.url)
        if key and key not in unique:
            unique[key] = candidate
    return sorted(unique.values(), key=lambda candidate: _sort_key(candidate, context))


# Register the built-in brand/platform extractors (one file + one line each).
from gallery_extractors import dawlance as _dawlance  # noqa: E402,F401
from gallery_extractors import haier as _haier  # noqa: E402,F401
from gallery_extractors import magento as _magento  # noqa: E402,F401

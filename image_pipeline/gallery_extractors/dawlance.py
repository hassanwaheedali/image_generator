"""Dawlance (dawlance.com.pk) gallery extractor.

Dawlance media URLs carry the page's own MDM/model code.  Rule: accept a media URL
only when it contains **this page's** MDM code (taken from the page URL); when the
page URL has no usable code, fall back to requiring a model key inside the media URL.
"""

from __future__ import annotations

import re
from typing import Any

from gallery_extractors import (
    ExtractionContext,
    collect_img_urls,
    register_extractor,
)
from model_matching import find_matching_keys
from models import ImageCandidate

DAWLANCE_GALLERY_SELECTORS: tuple[str, ...] = (
    ".sd_imgs",
    ".js_prodetail_imglist",
    ".smallList",
    ".product-gallery",
    ".product-image",
    ".item-img",
)

#: MDM codes look like ``DW-7500``, ``DWT-270``, ``DWK-151`` inside the URL slug.
_MDM_RE = re.compile(r"(?<![a-z0-9])([a-z]{1,4}[-_]?\d{3,5}[a-z]{0,4})(?![a-z0-9])")


def page_mdm_code(page_url: str) -> str:
    """The model code carried by the page URL itself (``""`` when there is none)."""
    cleaned = re.sub(r"[^a-z0-9]", "", page_url.lower())
    hits = _MDM_RE.findall(page_url.lower())
    if not hits:
        return ""
    # prefer the longest hit (DW-7500 beats 7500), then the last one in the URL
    best = max(hits, key=lambda hit: len(re.sub(r"[^a-z0-9]", "", hit)))
    return re.sub(r"[^a-z0-9]", "", best.lower()) or cleaned[:0]


def extract(context: ExtractionContext) -> list[ImageCandidate]:
    """Dawlance gallery images belonging to *this* page's code."""
    mdm = page_mdm_code(context.page_url)
    nodes: list[Any] = []
    for selector in DAWLANCE_GALLERY_SELECTORS:
        for container in context.soup.select(selector):
            nodes.extend(container.find_all(["img", "source", "a"]))

    candidates: list[ImageCandidate] = []
    order = 0
    for node in nodes:
        for url in collect_img_urls(node, context.page_url):
            flat = re.sub(r"[^a-z0-9]", "", url.lower())
            belongs = mdm and mdm in flat
            if not belongs:
                # no MDM on the page URL -> require a model key in the media URL
                belongs = bool(find_matching_keys(url, context.keys, include_title_key=False))
            if not belongs:
                continue
            candidates.append(
                ImageCandidate(
                    url=url,
                    page_url=context.page_url,
                    source="registry:dawlance",
                    order=order,
                    matches_model=True,
                    dom_flags=(),
                )
            )
            order += 1
            break                                   # one asset per node
    return candidates


register_extractor("dawlance")(extract)

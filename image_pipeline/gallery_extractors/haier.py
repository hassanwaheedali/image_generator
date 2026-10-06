"""Haier (haier.com) gallery extractor.

Haier product pages keep the gallery in container classes ``sd_imgs``,
``js_prodetail_imglist`` and ``smallList``; the thumbnails carry the high-resolution
asset in ``data-large_image`` / ``data-zoom-image`` (handled by ``collect_img_urls``).
"""

from __future__ import annotations

from gallery_extractors import (
    ExtractionContext,
    candidates_from_nodes,
    register_extractor,
)
from models import ImageCandidate

HAIER_GALLERY_SELECTORS: tuple[str, ...] = (
    ".sd_imgs",
    ".js_prodetail_imglist",
    ".smallList",
    "#sd_imgs",
    ".prodetailimg",
    ".product-detail-img",
)


def extract(context: ExtractionContext) -> list[ImageCandidate]:
    """Images inside Haier's gallery containers."""
    nodes: list[object] = []
    for selector in HAIER_GALLERY_SELECTORS:
        for container in context.soup.select(selector):
            nodes.extend(container.find_all(["img", "source", "a"]))
    if not nodes:
        return []
    return candidates_from_nodes(nodes, context=context, source="registry:haier")


register_extractor("haier.com")(extract)

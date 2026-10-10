"""Page verification (S1) and the DOM-level safeguards (S2, S3).

* ``verify_page``      - is this page really about *this* product?  Strength 0 means
                         "extract nothing from this page" (S1).
* ``is_in_description_region`` - S2: images inside description/article content are
                         never a gallery source.
* ``is_linked_to_other_page``  - S3: an image wrapped in a link to a different page
                         belongs to that other page (related products, upsells).
"""

from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from soupsieve import match as css_match

from config import PipelineConfig
from model_matching import (
    extract_variant_facts,
    fuzzy_identity_match,
    match_product,
    near_miss_codes,
    variant_conflicts,
)
from models import ModelKeys, PageMatchResult
from url_filters import (
    IMAGE_EXTENSIONS,
    host_is_official,
    host_is_other_manufacturer,
)

LOG = logging.getLogger("pipeline.page")

# --------------------------------------------------------------------------- #
# S2 - description / article regions (never a gallery source)
# --------------------------------------------------------------------------- #
DESCRIPTION_REGION_SELECTORS: tuple[str, ...] = (
    "#description",
    ".description",
    ".product-description",
    ".product__description",
    "#tab-description",
    ".woocommerce-Tabs-panel",
    ".tab-content",
    ".tab-pane",
    ".product-tabs",
    ".long-description",
    ".rte",
    ".entry-content",
    "article",
    '[itemprop="description"]',
    '[id*="description" i]',
    '[class*="description" i]',
    '[class*="specification" i]',
    '[class*="overview" i]',
)

#: A gallery container always wins over a description wrapper above it: templates
#: routinely put the gallery inside a tab/article wrapper.
GALLERY_CONTAINER_SELECTORS: tuple[str, ...] = (
    ".product-gallery",
    ".woocommerce-product-gallery",
    ".product-images",
    ".product__photos",
    ".gallery-thumbs",
    ".thumbnails",
    ".product-thumbnails",
    # Product-hero slots.  A shop that ships one picture per model puts it in the hero,
    # not in a gallery widget, and the strictly-scoped page fallback never runs once any
    # other source produced a candidate - so the one real photo on the page was invisible
    # (live Orient page 2026-10-10: <div class="pdp-hero__image"><picture><img
    # src="../assets/products-images-2026/air-conditioners/15-ton-frost-classic-white-
    # dc-inverter.png" alt="Frost 18W Classic White">, zero candidates pointed at it while
    # eight cross-sell tiles were extracted and rejected).
    '[class*="pdp-hero" i]',
    '[class*="product-hero" i]',
    '[class*="hero__image" i]',
    '[class*="product-image" i]',
    '[class*="product-media" i]',
    ".slick-slider",
    ".flexslider",
    ".product-media-gallery",
    ".gallery-placeholder",
    ".fotorama",
    ".swiper",
    "[data-gallery-role]",
    ".product-single__photos",
    ".product__media",
    ".pdp-gallery",
    ".sd_imgs",
    ".js_prodetail_imglist",
    ".smallList",
)

#: Regions that are removed from the DOM before extraction and before the body-text
#: check (header/footer/nav/related/share).  Description regions are *kept* - S2
#: rejects their images individually.
UNWANTED_REGION_SELECTORS: tuple[str, ...] = (
    "header",
    "footer",
    "nav",
    ".site-header",
    ".site-footer",
    ".masthead",
    ".topbar",
    ".navbar",
    ".menu",
    ".breadcrumb",
    ".breadcrumbs",
    ".sidebar",
    ".related",
    ".related-products",
    ".related_products",
    ".recommendations",
    ".recommended",
    ".recommended-products",
    ".upsell",
    ".upsells",
    ".cross-sell",
    ".crosssell",
    ".you-may-also-like",
    ".also-viewed",
    ".recently-viewed",
    ".recently_viewed",
    '[class*="related-product" i]',
    '[class*="recommend" i]',
    '[id*="recommend" i]',
    ".social-share",
    ".share-buttons",
    ".sharebar",
    ".cookie",
    ".cookie-banner",
    ".newsletter",
    ".modal",
    '[class*="advert" i]',
    '[id*="advert" i]',
    '[class*="promo-banner" i]',
)

STRIPPED_ATTR = "data-pipeline-stripped"

_DESCRIPTION_SELECTOR = ", ".join(DESCRIPTION_REGION_SELECTORS)
_GALLERY_SELECTOR = ", ".join(GALLERY_CONTAINER_SELECTORS)
_UNWANTED_SELECTOR = ", ".join(UNWANTED_REGION_SELECTORS)


def _matches(tag: Any, selector: str) -> bool:
    if getattr(tag, "name", None) in (None, "[document]"):
        return False
    try:
        return bool(css_match(selector, tag))
    except Exception:                              # pragma: no cover - soupsieve edge
        return False


def is_description_region(tag: Any) -> bool:
    """True when the element itself is a description/specification region."""
    return _matches(tag, _DESCRIPTION_SELECTOR)


def is_gallery_container(tag: Any) -> bool:
    """True when the element is a dedicated gallery container."""
    return _matches(tag, _GALLERY_SELECTOR)


def is_in_description_region(tag: Any, *, boundary: Any | None = None) -> bool:
    """Safeguard S2: is this image inside description/article content?

    Walks the ancestors only.  A dedicated gallery container **inside** a tab or
    article wrapper protects its images (it is the nearest meaningful ancestor),
    while an image inside a real ``.description`` block is rejected.  Elements that
    the stripper already removed are treated as "not part of the page".
    """
    current = getattr(tag, "parent", None)
    while current is not None and current is not boundary:
        if getattr(current, "name", None) in (None, "[document]"):
            break
        if current.has_attr(STRIPPED_ATTR):
            return False
        if is_gallery_container(current):
            return False
        if is_description_region(current):
            return True
        current = current.parent
    return False


def strip_unwanted_regions(soup: BeautifulSoup, page_url: str = "") -> BeautifulSoup:
    """Remove header/footer/nav/related/share/cookie regions before extraction.

    Removed subtrees are also marked with ``data-pipeline-stripped`` on their parent
    context when possible, so a later ancestor walk cannot resurrect them.
    """
    for element in soup.select(_UNWANTED_SELECTOR):
        parent = element.parent
        if parent is not None and getattr(parent, "name", None) not in (None, "[document]"):
            with contextlib.suppress(Exception):   # pragma: no cover - defensive
                parent[STRIPPED_ATTR] = "1"
        element.decompose()
    for comment in soup.find_all(string=lambda text: isinstance(text, type(soup.Comment))):
        comment.extract()
    return soup


# --------------------------------------------------------------------------- #
# S3 - links pointing at another page
# --------------------------------------------------------------------------- #
def _normalized_page_key(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    host = parts.netloc.lower().split(":")[0]
    host = host.removeprefix("www.")
    path = parts.path.rstrip("/").lower()
    return host, path


def _is_image_file_url(url: str) -> bool:
    path = urlsplit(url).path
    return "." in path and path.rsplit(".", 1)[1].lower() in IMAGE_EXTENSIONS


def is_linked_to_other_page(tag: Any, page_url: str) -> bool:
    """Safeguard S3: image inside a link that points to a *different* page.

    A link to the image file itself (lightbox / zoom) is allowed.
    """
    anchor = tag if getattr(tag, "name", None) == "a" else tag.find_parent("a")
    if anchor is None:
        return False
    href = anchor.get("href")
    if not href or not isinstance(href, str):
        return False
    href = href.strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return False
    absolute = urljoin(page_url, href)
    if _is_image_file_url(absolute):
        return False                               # lightbox: same product, bigger file
    return _normalized_page_key(absolute) != _normalized_page_key(page_url)


# --------------------------------------------------------------------------- #
# text helpers
# --------------------------------------------------------------------------- #
def page_body_text(soup: BeautifulSoup) -> str:
    """Visible-ish text of the cleaned page (scripts/styles removed)."""
    for element in soup.find_all(["script", "style", "noscript", "template"]):
        element.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


_COLOUR_LABEL_RE = re.compile(r"^colou?r[:\s]*$", re.IGNORECASE)
_COLOURWAY_VALUE_RE = re.compile(r"^[A-Za-z][A-Za-z -]{0,29}$")


def _looks_like_colourway(value: str) -> bool:
    """A short alphabetic phrase - a colourway, not a sentence or a swatch list.

    At most three words, so the flattened swatch menu (``Red Blaze Marine Impression
    ...``) can never pass for a single selected value.
    """
    value = value.strip()
    if not _COLOURWAY_VALUE_RE.match(value):
        return False
    return 1 <= len(value.split()) <= 3


def selected_colourway(soup: BeautifulSoup) -> str:
    """The colourway the page itself declares as *selected*, or ``""`` (no opinion).

    Variant pages (live: PEL SGD 425) print the chosen colour in exactly two labelled
    places while the swatch menu lists *every* option, so only the labelled value may be
    trusted.  We read, in order:

    * a spec table row whose first cell is ``Colour``/``Color`` - the second cell is the
      selected value (``<tr><td>Colour</td><td>Red Blaze</td></tr>``);
    * an inline label element holding exactly ``Color:``/``Colour`` whose next sibling
      carries the value.

    No brand, domain or colour table is involved; a page without such a label simply
    yields ``""`` and the row stays fail-open, exactly as today.
    """
    for row in soup.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
        if len(cells) >= 2 and _COLOUR_LABEL_RE.match(cells[0]) and _looks_like_colourway(cells[1]):
            return str(cells[1])
    for label in soup.find_all(string=_COLOUR_LABEL_RE):
        parent = label.parent
        sibling = parent.find_next_sibling() if parent is not None else None
        if sibling is not None:
            value = str(sibling.get_text(" ", strip=True))
            if _looks_like_colourway(value):
                return value
    return ""


def page_product_name(soup: BeautifulSoup) -> str:
    """The page's **own product name**: title, og:title, twitter:title, h1s, JSON-LD.

    Deliberately narrow - colour and variant checks must not read swatch lists in the
    body text (they would fire on every page).
    """
    parts: list[str] = []
    if soup.title and soup.title.string:
        parts.append(soup.title.string)
    for selector, attribute in (
        ("meta[property='og:title']", "content"),
        ("meta[name='twitter:title']", "content"),
        ("meta[property='twitter:title']", "content"),
        ("meta[name='title']", "content"),
    ):
        for meta in soup.select(selector):
            value = meta.get(attribute)
            if isinstance(value, str) and value.strip():
                parts.append(value)
    for heading in soup.find_all(["h1", "h2"]):
        text = heading.get_text(" ", strip=True)
        if text:
            parts.append(text)
    from gallery_extractors.json_ld import product_node_texts

    parts.extend(product_node_texts(soup))
    return " | ".join(dict.fromkeys(part.strip() for part in parts if part.strip()))


# --------------------------------------------------------------------------- #
# matching settings + S1
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MatchSettings:
    """The subset of config that page verification needs (keeps tests simple)."""

    allow_key_truncation_match: bool = True
    brand_match_enabled: bool = True
    brand_match_min_length: int = 5
    brand_match_min_digits: int = 3
    brand_match_reject_other_brand_domain: bool = True
    variant_separation_enabled: bool = True
    flag_variant_mismatch: bool = True
    allow_title_only_verification: bool = False

    @staticmethod
    def from_config(config: PipelineConfig) -> MatchSettings:
        return MatchSettings(
            allow_key_truncation_match=config.allow_key_truncation_match,
            brand_match_enabled=config.brand_match_enabled,
            brand_match_min_length=config.brand_match_min_length,
            brand_match_min_digits=config.brand_match_min_digits,
            brand_match_reject_other_brand_domain=config.brand_match_reject_other_brand_domain,
            variant_separation_enabled=config.variant_separation_enabled,
            flag_variant_mismatch=config.flag_variant_mismatch,
            allow_title_only_verification=config.allow_title_only_verification,
        )


def _strong_fields(soup: BeautifulSoup, page_url: str) -> dict[str, str]:
    """URL, title, og:title and every h1 - the fields that can certify a model."""
    fields: dict[str, str] = {"url": page_url}
    name = page_product_name(soup)
    if name:
        fields["page_name"] = name
    if soup.title and soup.title.string:
        fields["title"] = soup.title.string
    for index, heading in enumerate(soup.find_all("h1")):
        text = heading.get_text(" ", strip=True)
        if text:
            fields[f"h1#{index}"] = text
    return fields


def verify_page(
    page_url: str,
    html: str,
    keys: ModelKeys,
    *,
    brand: str | None = None,
    settings: MatchSettings | None = None,
) -> PageMatchResult:
    """Safeguard S1: does this page show *this* product?  (strength 2 / 1 / 0)

    Two recognition rules only (model key, brand + code - see
    :func:`model_matching.match_product`).  A page that shows another colourway is
    strength 0 as well: the colour is part of the product's identity, and publishing
    the wrong colour costs the company a penalty (customer decision 2026-10-07,
    superseding the 2026-10-05 "flag it and keep it" rule).  A size/type difference
    stays a note; a page that contradicts another brand's domain, or shows no code at
    all, is strength 0 and yields nothing.
    """
    options = settings or MatchSettings()
    if not keys.all_keys and not keys.keywords:
        return PageMatchResult(page_url, 0, (), "title carries no model key", "none")

    soup = strip_unwanted_regions(BeautifulSoup(html, "lxml"), page_url)
    strong = _strong_fields(soup, page_url)
    page_name = strong.get("page_name", "") or strong.get("title", "")

    host = urlsplit(page_url).netloc.lower().split(":")[0]
    if (
        options.brand_match_reject_other_brand_domain
        and brand
        and host_is_other_manufacturer(host, brand)
    ):
        return PageMatchResult(
            page_url,
            0,
            (),
            f"page is on another manufacturer's domain ({host})",
            "other_brand_domain",
            (),
            page_name,
        )

    decision = match_product(
        keys=keys,
        brand=brand,
        strong_text=" \n ".join(strong.values()),
        body_text=page_body_text(soup),
        selected_colourway=selected_colourway(soup),
        brand_match_enabled=options.brand_match_enabled,
        brand_match_min_length=options.brand_match_min_length,
        brand_match_min_digits=options.brand_match_min_digits,
        allow_key_truncation=options.allow_key_truncation_match,
        variant_separation_enabled=options.variant_separation_enabled,
    )
    strength = decision.strength
    rule = decision.rule
    found = decision.found
    notes: list[str] = []

    if strength == 0 and brand and host_is_official(page_url, brand):
        # The brand's own page spells its own series letter: the sheet says ``12X``, Orient's
        # page and file name say ``titanium-12s-olive-black-t3-plus``.  Exact codes still
        # decide every product-vs-product question - this only lifts *the brand's own host*
        # from "refuse this page" to the tolerated level, so the official gallery is looked
        # at before a marketplace's copy of the product.  A retailer page never gets this.
        identity = fuzzy_identity_match(f"{page_name} {page_url}", keys)
        if identity:
            strength, rule, found = 1, "fuzzy_identity", (identity,)

    # A size/type difference only annotates the run report; a colour difference is not
    # flagged here any more - match_product rejects that page outright.
    if strength > 0 and options.flag_variant_mismatch and keys.variant_facts:
        title_facts = keys.variant_dict()
        page_facts = extract_variant_facts(page_name)
        notes.extend(
            f"variant mismatch (flagged): {axis} {title_facts.get(axis, '?')} (title) vs "
            f"{page_facts.get(axis, '?')} (page)"
            for axis in variant_conflicts(title_facts, page_facts)
        )

    if rule == "fuzzy_identity":
        detail = (
            f"brand page names this product line; only its capacity letter differs "
            f"({'/'.join(found)} matched with the letter tolerated) - tolerated, official host"
        )
    elif decision.rule == "colour_conflict":
        # The page carries the model's own code but a different colourway.
        detail = (
            f"page shows another colourway (title: {keys.colour_word}) - "
            "a different colour is a different product"
        )
    elif strength == 0:
        hints = near_miss_codes(page_name, keys)
        detail = "no model key found on the page"
        if hints:
            detail += f" (page shows {'/'.join(hints)})"
    elif rule == "brand_code":
        detail = f"brand+code match: {brand} + {'/'.join(found)} in the page name/URL"
    elif rule == "body_brand_code":
        detail = f"brand+code match: {brand} + {'/'.join(found)} in the body text"
    elif rule == "body_model_key":
        detail = "model key found in body text only"
    elif rule in ("keyword", "body_keyword"):
        detail = "keyword match - this product is sold by name, not by a model code"
    else:
        detail = f"model key found in the page name/URL ({'/'.join(found)})"

    return PageMatchResult(
        page_url,
        strength,
        found,
        detail,
        rule,
        tuple(notes),
        page_name,
        tolerated=decision.tolerated,
    )

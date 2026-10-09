"""JSON-LD product nodes - source 2 of the allowed gallery sources.

Only a node whose **own** name/sku/mpn/model/productID/url matches the product keys
is used, and only that node's ``image`` is read.  ``ItemList`` / related nodes are
never descended into.

One exception, deliberately narrow: a row whose title carries *no* model code at all
("Frost 18W Classic White | Orient Electronics Pakistan") owns nothing to match, so the
gate above refuses the page's own node and the image the page declared is never seen.
Such a row accepts a node that names **this very page** as its product page - a claim a
related-product tile cannot make.  Rows that do own a model key are matched exactly as
before.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

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


#: Fields a node may use to name its own product page.
_PAGE_URL_FIELDS = ("url", "@id", "mainEntityOfPage")


def _page_slug(value: str) -> str:
    """The last path segment of a URL - or a bare sku - without its file extension."""
    path = urlsplit(value).path
    text = unquote(path or value).strip("/")
    tail = text.rsplit("/", 1)[-1]
    return re.sub(r"\.(?:html?|php|aspx?|shtml)$", "", tail, flags=re.IGNORECASE).casefold()


#: Fields whose words name the *product a human would recognise*.  ``url`` and ``sku``
#: are deliberately absent: they are what the page-identity check reads, and a field used
#: to prove the node belongs to this page cannot also vouch for its identity - a tile
#: lying its way in simply copies ``sku: "frost-18w-classic-white"`` from the page slug.
_NAME_FIELDS = ("name", "brand")


def _node_words(node: dict[str, Any]) -> set[str]:
    """Lowercase words the node uses for *itself*, from its name fields only."""
    parts: list[str] = []
    for field in _NAME_FIELDS:
        value = node.get(field)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            name = value.get("name")
            if isinstance(name, str):
                parts.append(name)
    return set(re.findall(r"[a-z]{3,}", " ".join(parts).casefold()))


def _shares_a_word(node: dict[str, Any], context: ExtractionContext) -> bool:
    """True when the node's own name repeats at least one word of the row's title.

    A shop template that copies the current page's URL into every product block - which
    real catalogs do, for canonical/SEO reasons - would otherwise let a *related*
    product's photo in under the page-identity exception.  One shared word keeps the
    honest case (node "Frost 18W Classic White" vs the same title) and blocks the lie
    (node "Megatron Titanium 12X Olive Black" on the Frost page) - measured on both
    shapes in tests/test_gallery_extractors.py, and the comparison must not read the
    node's own ``url``: that string *is* the page slug.
    """
    words = _node_words(node)
    title_words = set(context.keys.name_words) or set(
        re.findall(r"[a-z]{3,}", context.keys.cleaned_title.casefold())
    )
    return bool(words & title_words)


def _declares_page(node: dict[str, Any], page_url: str) -> bool:
    """True when the product node names *this* page as its own product page.

    Stronger than a name match, which is why it is trusted for model-free rows: the
    node has to carry this URL (or a sku equal to this page's slug), and a shop's
    related-product / upsell nodes always name their own address instead.
    """
    wanted = _page_slug(page_url)
    if not wanted:
        return False
    for field in _PAGE_URL_FIELDS:
        value = node.get(field)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, dict):
                item = item.get("@id") or item.get("url")
            if isinstance(item, str) and item.strip() and (
                _page_slug(urljoin(page_url, item.strip())) == wanted
            ):
                return True
    sku = node.get("sku")
    return bool(isinstance(sku, str) and _page_slug(sku.strip()) == wanted)


def extract(context: ExtractionContext) -> list[ImageCandidate]:
    """Images of JSON-LD product nodes whose own identity matches this product."""
    candidates: list[ImageCandidate] = []
    order = 0
    # A title without a model code has nothing to match, and then the page's own node
    # is the only honest source left (owner finding 2026-10-10, uploads/live/orient_frost.html:
    # the node declared og-images/frost-18w-classic-white.jpg and extract() returned 0).
    model_free = not context.keys.tokens and not context.keys.identities
    for block in iter_ld_json_blocks(context.soup):
        payload = safe_json_loads(block)
        if payload is None:
            continue
        for node in walk_json(payload):
            if not isinstance(node, dict) or not _type_is_product(node):
                continue
            identity = _identity_text(node)
            if not identity:
                continue
            if not find_matching_keys(identity, context.keys) and not (
                model_free and _declares_page(node, context.page_url) and _shares_a_word(node, context)
            ):
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

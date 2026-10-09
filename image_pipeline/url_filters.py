"""URL-level filters: keywords, canonicalisation, dedup keys, fetchability.

Cheap checks that run *before* any network call (S5, S11) plus the URL
canonicalisation rules of section 6.2.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from urllib.parse import SplitResult, parse_qs, unquote, urljoin, urlsplit, urlunsplit

# --------------------------------------------------------------------------- #
# S5 - bad keyword blocklist (token based; substring matching is forbidden)
# --------------------------------------------------------------------------- #
BAD_URL_KEYWORDS: frozenset[str] = frozenset(
    {
        "logo", "icon", "badge", "payment", "cart", "review", "banner", "flag",
        "footer", "whatsapp", "facebook", "instagram", "twitter", "linkedin",
        "pinterest", "share", "sharer", "avatar", "placeholder", "pixel",
        "spacer", "qr", "qrcode", "barcode", "warranty", "guarantee", "seal",
        "certified", "energy-guide", "star-rating", "special-price", "topimg",
        "newpro", "remote", "accessory", "accessories",
    }
)

#: Multi-word entries match as consecutive tokens.
MULTI_WORD_BAD_KEYWORDS: tuple[str, ...] = (
    "energy-guide", "star-rating", "special-price", "water-saving", "cold-plasma",
)

#: Resize suffixes stripped by canonicalisation (section 6.2).
_SIZE_SUFFIX_RE = re.compile(
    r"[-_]\d{2,4}x\d{2,4}|[-_](?:medium|large|grande|thumb|thumbnail|compact|small|scaled)"
    r"(?=\.|$)",
    re.IGNORECASE,
)

IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {"jpg", "jpeg", "png", "webp", "gif", "avif", "bmp", "tiff", "tif"}
)

_TOKEN_SPLIT_RE = re.compile(r"[^0-9a-z]+")

#: CDN transform segments such as ``1000Wx1000H`` - they describe a resize, not the
#: asset, so they are removed from the path (otherwise every photo of a product
#: collapses onto the same "parent folder/filename" dedup key).
_TRANSFORM_SEGMENT_RE = re.compile(r"^\d{2,5}[wWhH]?x\d{2,5}[hH]?$")

#: Raster formats a shop may serve the same asset in.
_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {"png", "jpg", "jpeg", "webp", "gif", "bmp", "avif", "tif", "tiff"}
)

#: Generic file names that carry no identity of their own.
_GENERIC_FILENAMES: frozenset[str] = frozenset(
    {
        "image", "image.webp", "image.jpg", "image.jpeg", "image.png", "original",
        "original.jpg", "photo", "photo.jpg", "product.jpg", "main.jpg", "index.jpg",
    }
)

_GENERIC_DIR_NAMES: frozenset[str] = frozenset(
    {"resize", "media", "images", "image", "cache", "cdn", "files", "assets", "product"}
)

# --------------------------------------------------------------------------- #
# domain / path blocklists (data only - the logic lives in the functions below)
# --------------------------------------------------------------------------- #
#: Social, search-engine and app-store hosts: never a product page (S11).
EXCLUDED_DOMAINS: tuple[str, ...] = (
    "facebook.com",
    "fb.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "pinterest.com",
    "linkedin.com",
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "whatsapp.com",
    "wa.me",
    "telegram.org",
    "t.me",
    "google.com",
    "googleusercontent.com",
    "gstatic.com",
    "bing.com",
    "yahoo.com",
    "doubleclick.net",
    "apple.com",
    "itunes.apple.com",
    "play.google.com",
    "amazon.com",
)

#: Path fragments that mean "not a single product page" (S11).
NON_PRODUCT_PATH_PATTERNS: tuple[str, ...] = (
    "/category/",
    "/categories/",
    "/blog/",
    "/news/",
    "/article/",
    "/about",
    "/contact",
    "/support/",
    "/service/",
    "/services/",
    "/warranty",
    "/press/",
    "/media-center/",
    "/campaign/",
    "/promotions/",
    "/promotion/",
    "/deals/",
    "/search",
    "/tag/",
    "/tags/",
    "/author/",
    "/cart",
    "/checkout",
    "/account",
    "/login",
    "/register",
    "/compare",
    "/faq",
    "/stores/",
    "/showroom",
    "/track-order",
    "/terms",
    "/privacy",
    "/sitemap",
)

#: Query keys that turn any URL into a search result (S11).
SEARCH_QUERY_KEYS: frozenset[str] = frozenset(
    {"q", "s", "search", "query", "keyword", "keywords", "kw", "k", "p", "page"}
)

# --------------------------------------------------------------------------- #
# brands
# --------------------------------------------------------------------------- #
#: Manufacturers only (a shop is not a brand) -> the domains they own.
KNOWN_BRANDS: dict[str, tuple[str, ...]] = {
    "haier": ("haier.com", "haier.com.pk", "haierpk.com"),
    "dawlance": ("dawlance.com.pk", "dawlance.com"),
    "pel": ("pel.com.pk", "pel.com"),
    "tcl": ("tcl.com", "tclpakistan.com"),
    "homage": ("homage.com.pk", "homage.pk"),
    "orient": ("orient.com.pk", "orient.com"),
    "gree": ("gree.com", "gree.com.pk"),
    "samsung": ("samsung.com",),
    "lg": ("lg.com",),
    "kenwood": ("kenwood.com", "kenwoodpakistan.com", "kenwoodpakistan.pk"),
    "boss": ("bossappliances.com.pk",),
    "changhong": ("changhong.com",),
    "ecostar": ("ecostar.com.pk",),
    "dsm": ("dsm.com.pk",),
    # Added 2026-10-08 after verifying each domain against the brand's own site, so a
    # reseller is never mistaken for the manufacturer (an "official" host relaxes the S7
    # gallery test, so a wrong entry would let worse images through).
    # Super Asia Muhammad Din Sons Ltd: corporate site + its own online store.
    "superasia": ("superasiagroup.com", "superasiastore.com"),
    # Hisense Pakistan (Green Leaves / Hisense): both Pakistani brand sites + global.
    "hisense": ("hisensepakistan.com", "hisense.com.pk", "hisense.com"),
    # Midea Pakistan (Green Leaves Pvt Ltd): official online store + corporate + global.
    "midea": ("mideapakistan.store", "mideapakistan.com", "midea.com"),
}

#: Model-code prefixes that name the maker when the title prints a line name
#: instead of the brand ("E-Comfort Pro KEC-1267S" -> kenwood).
MODEL_PREFIX_BRAND: tuple[tuple[str, str], ...] = (("KEC", "kenwood"),)

#: Manufacturer domain -> brand (reverse map, built once).
DOMAIN_TO_BRAND: dict[str, str] = {
    domain: brand for brand, domains in KNOWN_BRANDS.items() for domain in domains
}

#: Retail marketplaces / classifieds: they are never "another manufacturer".
RETAIL_MARKETPLACE_DOMAINS: frozenset[str] = frozenset(
    {
        "daraz.pk",
        "priceoye.pk",
        "alfatah.com.pk",
        "alfamall.pk",
        "digimall.pk",
        "symbios.pk",
        "homeshopping.pk",
        "mega.pk",
        "telemart.pk",
        "ishopping.pk",
        "olx.com.pk",
        "priceblaze.pk",
        "mycart.pk",
        "shopmart.pk",
        "galaxy.pk",
    }
)


def url_path_tokens(url: str) -> tuple[str, ...]:
    """Path segments split into alphanumeric tokens (lowercased)."""
    path = urlsplit(url).path
    return tuple(token for token in _TOKEN_SPLIT_RE.split(path.lower()) if token)


def url_filename(url: str) -> str:
    """Last path segment of a URL (no query string)."""
    return urlsplit(url).path.rsplit("/", 1)[-1]


def has_bad_keyword(url: str, extra_keywords: Iterable[str] = ()) -> bool:
    """Safeguard S5: ads/banners/badges/social graphics by URL tokens.

    Whole-token equality only, so ``cartridge`` is not rejected by ``cart`` and
    ``silicon`` is not rejected by ``icon``.  ``banner2`` is rejected by
    ``banner`` because a keyword also matches itself plus digits.
    """
    tokens = url_path_tokens(url)
    if not tokens:
        return False
    token_set = set(tokens)
    keywords = set(BAD_URL_KEYWORDS)
    keywords.update(k.lower() for k in extra_keywords)
    for keyword in keywords:
        if keyword in token_set:
            return True
        # keyword plus digits ("banner2") is the same blocker
        pattern = re.compile(rf"^{re.escape(keyword)}\d+$")
        if any(pattern.match(token) for token in tokens):
            return True
    for phrase in (*MULTI_WORD_BAD_KEYWORDS, *(extra_keywords or ())):
        phrase_tokens = tuple(t for t in _TOKEN_SPLIT_RE.split(phrase.lower()) if t)
        if len(phrase_tokens) < 2:
            continue
        window = len(phrase_tokens)
        for start in range(max(0, len(tokens) - window + 1)):
            if tokens[start : start + window] == phrase_tokens:
                return True
    return False


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _host_in_excluded(host: str) -> bool:
    return any(_host_matches(host, domain) for domain in EXCLUDED_DOMAINS)


def detect_brand(url: str, title: str = "") -> str | None:
    """Brand for a source URL: manufacturer-domain reverse lookup, then title.

    Retail domains are deliberately absent from ``KNOWN_BRANDS``; for those the
    leading words of the title decide (which is what the sheet provides).  Two words
    are tried because a brand may be spelled with a space while the table key is one
    word - the catalogue writes ``Super Asia``, the table holds ``superasia``.
    """
    host = urlsplit(url).netloc.lower().split(":")[0]
    for domain, domain_brand in DOMAIN_TO_BRAND.items():
        if _host_matches(host, domain):
            return str(domain_brand)
    words = [match.group(0).lower() for match in re.finditer(r"[A-Za-z]{2,}", title)][:2]
    if not words:
        return None
    if words[0] in KNOWN_BRANDS:
        return words[0]
    joined = "".join(words)
    if len(words) == 2 and joined in KNOWN_BRANDS:
        return joined
    # The SKU prefix names the maker when the title carries a line name instead
    # (owner 2026-10-09: "E-Comfort Pro KEC-1267S" is a Kenwood - "you can get
    # kenwood through sku KEC").  Without it the official brand site is never
    # searched for the row.
    for prefix, brand in MODEL_PREFIX_BRAND:
        if re.search(rf"\b{prefix}[-\s]?\d", title, re.IGNORECASE):
            return brand
    return None


def host_is_other_manufacturer(host: str, brand: str | None) -> bool:
    """True when ``host`` is a *different* manufacturer's domain.

    Used to stop a brand+tail match on another brand's site (a Dawlance row must
    never be satisfied by ``samsung.com``).  Marketplaces and shops never count.
    """
    if not brand or not host:
        return False
    host = host.lower().split(":")[0]
    if not brand or brand not in KNOWN_BRANDS:
        return False
    if host in RETAIL_MARKETPLACE_DOMAINS:
        return False
    for owned in KNOWN_BRANDS[brand]:
        if _host_matches(host, owned):
            return False
    for domain in RETAIL_MARKETPLACE_DOMAINS:
        if _host_matches(host, domain):
            return False
    for other_brand, domains in KNOWN_BRANDS.items():
        if other_brand == brand:
            continue
        for domain in domains:
            if _host_matches(host, domain):
                return True
    return False


def host_is_official(url: str, brand: str | None) -> bool:
    """True when ``url`` sits on a domain the brand itself owns.

    Used to decide whether a page may get the curated-gallery S7 acceptance: only the
    manufacturer's own product page qualifies, never a marketplace or a retail listing.
    """
    domains = official_domains(brand)
    if not domains:
        return False
    host = urlsplit(url).netloc.lower().split(":")[0]
    if not host:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def official_domains(brand: str | None) -> tuple[str, ...]:
    """Domains that count as 'official' for a brand (used to prioritise URLs)."""
    if not brand:
        return ()
    return tuple(KNOWN_BRANDS.get(brand, ()))


def prioritize_source_urls(urls: Sequence[str], brand: str | None) -> list[str]:
    """Stable sort: official brand domains first, everything else keeps order."""
    domains = official_domains(brand)
    if not domains:
        return list(urls)

    def rank(url: str) -> int:
        host = urlsplit(url).netloc.lower().split(":")[0]
        return 0 if any(_host_matches(host, domain) for domain in domains) else 1

    return sorted(urls, key=rank)              # Python sort is stable


def is_fetchable_product_url(
    url: str,
    *,
    block_collections_without_products: bool = True,
) -> bool:
    """Safeguard S11: may this URL be fetched as a product page at all?"""
    if not url or not url.strip():
        return False
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https"):
        return False                            # data:, javascript:, mailto:, ...
    host = parts.netloc.lower().split(":")[0]
    if not host or "." not in host:
        return False
    if _host_in_excluded(host):
        return False
    path = parts.path.lower()
    if block_collections_without_products and "/collections/" in path:
        after = path.split("/collections/", 1)[1]
        if "/products/" not in after:
            return False
    for pattern in NON_PRODUCT_PATH_PATTERNS:
        if pattern in path:
            return False
    for key in SEARCH_QUERY_KEYS:
        if key in parse_qs(parts.query, keep_blank_values=True):
            return False
        if re.search(rf"(?:^|[?&;]){re.escape(key)}=", parts.query):
            return False
    # an image is not a product page
    return not path.endswith((".png", ".jpg", ".jpeg"))


def _path_has_image_extension(path: str) -> bool:
    if "." not in path:
        return False
    return path.rsplit(".", 1)[1].lower() in IMAGE_EXTENSIONS


def canonicalize_url(url: str, page_url: str) -> str:
    """Resolve, strip resize suffixes, drop the query only for real image files.

    Returns ``""`` for URLs that must be ignored (``data:`` URIs, fragments-only,
    tracking pixels).
    """
    if not url or not url.strip():
        return ""
    candidate = url.strip()
    if candidate.startswith("#"):
        return ""
    if (candidate.startswith("//") and page_url) or (page_url and not urlsplit(candidate).scheme):
        candidate = urljoin(page_url, candidate)
    parts = urlsplit(candidate)
    if parts.scheme.lower() not in ("http", "https"):
        return ""
    if not parts.netloc:
        return ""
    if parts.path.lower().endswith("/1x1.gif") or parts.path.lower().endswith("/1x1.png"):
        return ""
    decoded_path = unquote(parts.path)
    directory, _, filename = decoded_path.rpartition("/")
    if not filename:
        return ""
    filename = _SIZE_SUFFIX_RE.sub("", filename)
    if not filename:
        return ""
    path = f"{directory}/{filename}" if directory else f"/{filename}"
    query = parts.query
    if query and _path_has_image_extension(path):
        query = ""
    query = _strip_tracking_params(query)
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, query, "")
    )


_TRACKING_PARAM_RE = re.compile(
    r"^(?:utm_[a-z]+|fbclid|gclid|msclkid|mc_[a-z]+|_ga|ref|ref_?src|_=)$",
    re.IGNORECASE,
)


#: Human-readable brand names for the output CSV (falls back to Capitalised).
BRAND_DISPLAY: dict[str, str] = {
    "haier": "Haier",
    "dawlance": "Dawlance",
    "pel": "PEL",
    "tcl": "TCL",
    "homage": "Homage",
    "orient": "Orient",
    "gree": "Gree",
    "samsung": "Samsung",
    "lg": "LG",
    "kenwood": "Kenwood",
    "changhong": "Changhong",
    "ecostar": "EcoStar",
    "dsm": "DSM",
    "boss": "Boss",
    "superasia": "Super Asia",
    "hisense": "Hisense",
    "midea": "Midea",
}


def display_brand(brand: str | None, title: str = "") -> str:
    """Brand for the output sheet: known brands keep their official spelling."""
    if brand:
        known = BRAND_DISPLAY.get(brand.lower())
        if known:
            return known
        return brand[:1].upper() + brand[1:]
    for match in re.finditer(r"[A-Za-z]{3,}", title):
        word = match.group(0)
        if word.lower() not in {"semi", "automatic", "inch", "fully", "smart", "front", "top"}:
            return word[:1].upper() + word[1:]
    return ""


def is_excluded_host(host: str, excluded: Iterable[str]) -> bool:
    """True when ``host`` is on the excluded list (substring match, e.g. symbios.pk)."""
    host = host.lower().split(":")[0]
    return any(entry and entry.lower() in host for entry in excluded)


# --------------------------------------------------------------------------- #
# largest-size rewrite (plan.md 11.5 step 2) - data, not code branches
# --------------------------------------------------------------------------- #
#: (name, pattern, replacement) applied in order.  A rewrite is only *tried*, never
#: forced: the URL the page itself gave is always kept as the last candidate.
UPSCALE_REWRITE_RULES: tuple[tuple[str, str, str], ...] = (
    # Haier image.haier.com ships 60px thumbs (_60.png) and a soft _480.png on product
    # pages; the crisp asset on the same ladder is _1680 (live page 2026-10-08:
    # h55k85guxfux.shtml hero ..._1680.jpg, gallery _60/_480).  Extensions arrive
    # upper-cased too (.PNG), hence the inline ignore-case flag.
    ("haier-thumb", r"(?i)_\d{2,3}(\.(?:jpg|jpeg|png|webp))$", r"_1000\1"),
    # Shopify / Shopline sized variants.
    ("shopify-size", r"_(\d{2,4})x(\d{2,4})(\.(?:jpg|jpeg|png|webp))$", r"_1024x1024\3"),
    # Shopify single-dimension thumbs (``_288x.png``): the same asset exists much larger, and
    # probing the 288px variant rejects it at S8 before the large sibling is ever requested.
    ("shopify-single", r"_\d{2,4}x(\.(?:jpg|jpeg|png|webp))$", r"_1024x1024\1"),
    # Magento resized cache directories.
    ("magento-cache", r"/cache/[0-9a-f]{16,}/", "/"),
    # WordPress / WooCommerce resize suffixes.
    ("woo-size", r"-(\d{2,4})x(\d{2,4})(\.(?:jpg|jpeg|png|webp))$", r"\3"),
    # Dawlance-style transform segments (/media/resize/<ASSET>.png/1000Wx1000H/image.webp):
    # the asset path itself is the largest form.
    ("cdn-transform", r"/\d{2,5}[wWhH]?x\d{2,5}[hH]?/image\.(?:webp|jpg|jpeg|png)$", ""),
)


def upscale_candidates(url: str, official: bool = False) -> tuple[str, ...]:
    """URL variants to try, largest first, ending with the URL exactly as given.

    ``official`` (the page sits on the brand's own domain) unlocks the Haier size ladder:
    Haier's CDN often lacks ``_1000`` but ships ``_800``/``_480``, so an official page tries
    all three instead of betting on a single size that 404s.  Retailers keep the single
    ``_1000`` rewrite - their S8 protection is unchanged.
    """
    out: list[str] = []
    parts = urlsplit(url)
    path, query = parts.path, parts.query
    for name, pattern, replacement in UPSCALE_REWRITE_RULES:
        if name == "cdn-transform" and "dawlance" in parts.netloc.lower():
            # Dawlance CDN endpoints require the transform segment (/1000Wx1000H/image.webp);
            # stripping it causes an immediate Akamai HTTP/2 stream reset (error 0x1 PROTOCOL_ERROR).
            continue
        if name == "haier-thumb" and official:
            # Live probe of image.haier.com (2026-10-08, fetch-calibrated 404 vs 200):
            # the suffix-less original (W020...819.png) is the full-res asset and
            # EXISTS; _1680/_1000/_800 are 404 for gallery assets (only some heroes
            # ship _1680).  So the ladder starts at the stripped original.
            stripped = re.sub(pattern, r"\1", path)
            if stripped != path:
                _add_candidate(out, parts, stripped, query, url)
            for size in (1680, 1000, 800, 480):
                rewritten = re.sub(pattern, rf"_{size}\1", path)
                if rewritten != path:
                    _add_candidate(out, parts, rewritten, query, url)
            continue
        rewritten = re.sub(pattern, replacement, path)
        if rewritten != path:
            _add_candidate(out, parts, rewritten, query, url)
    out.append(url)
    return tuple(out)


def _add_candidate(
    out: list[str], parts: SplitResult, rewritten: str, query: str, original: str
) -> None:
    candidate = urlunsplit((parts.scheme, parts.netloc, rewritten, query, parts.fragment))
    if candidate != original and candidate not in out:
        out.append(candidate)


def has_image_extension(url: str) -> bool:
    """True when the URL path ends in a known raster extension.

    JSON-LD / embedded-JSON ``image`` fields are not always images - a shop may put a *page*
    URL (or a timestamped route) in there, which then burns a download and 404s (live case:
    telex.pk ``/products/2026-06-28T...``).  The DOM path already guards ``<a href>`` with
    ``_looks_like_image``; the JSON sources use this predicate instead.
    """
    path = urlsplit(url).path.lower()
    return any(path.endswith("." + ext) for ext in IMAGE_EXTENSIONS)


def _strip_tracking_params(query: str) -> str:
    if not query:
        return ""
    kept: list[str] = []
    for pair in query.split("&"):
        if not pair:
            continue
        name = pair.split("=", 1)[0]
        if _TRACKING_PARAM_RE.match(unquote(name)):
            continue
        kept.append(pair)
    return "&".join(kept)


def dedup_key(canonical_url: str) -> str:
    """``parent-folder/filename`` (lowercased) - the per-product dedup identity.

    Two different folders each holding ``front.png`` are different images.  When the
    tail of the path is generic (``.../image.webp``, a resize folder, ...) more path
    segments are pulled in until something identifying is found, so a CDN that names
    every asset ``image.webp`` still yields one key per photo.
    """
    if not canonical_url:
        return ""
    parts = urlsplit(canonical_url)
    path = unquote(parts.path)
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return ""
    # resize directives (``1000Wx1000H``) describe a transform, not the asset, so the
    # same photo at two sizes collapses onto one key - the URL itself stays untouched.
    without_transforms = [
        segment for segment in segments if not _TRANSFORM_SEGMENT_RE.match(segment)
    ]
    if without_transforms:
        segments = without_transforms
    # A *served render* is ``<asset>.<ext>/<render-name>.<ext>`` (SFCC/Shopify style, e.g.
    # ``…/F1103.png/1000Wx1000H/image.webp``).  That last raw segment names the transform
    # output, not the asset, so it must stay out of the key - otherwise the gallery's own
    # zoom link to the same picture (``…/F1103.png``) counted as a second asset and bought a
    # second download slot (live: Dawlance DW 7500 gallery, 10 photos, one slot wasted).
    last = segments[-1].lower()
    if (
        len(segments) >= 2
        and _path_has_image_extension(segments[-2])
        and (last in _GENERIC_FILENAMES or last.rsplit(".", 1)[0] in _GENERIC_FILENAMES)
    ):
        segments = segments[:-1]
    take = 2 if len(segments) > 1 else 1
    while take < len(segments):
        head = segments[-take].lower()
        parent = segments[-(take + 1)].lower()
        stem = head.rsplit(".", 1)[0]
        generic = (
            head in _GENERIC_FILENAMES
            or stem in _GENERIC_FILENAMES
            or _TRANSFORM_SEGMENT_RE.match(head) is not None
            or parent in _GENERIC_DIR_NAMES
            or _TRANSFORM_SEGMENT_RE.match(parent) is not None
        )
        if not generic:
            break
        take += 1
    key = "/".join(segment.lower() for segment in segments[-take:])
    # The same photo is routinely served as image.webp / image.png / photo.jpg.  Those are
    # one asset, so the format suffix must not create a second identity: otherwise the
    # same picture is downloaded twice, which burned the per-page download allowance and
    # starved the real remaining gallery images (live case: Dawlance DW-7500C, 4 official
    # gallery photos, the 4th never reached).
    stem, dot, extension = key.rpartition(".")
    if dot and extension.lower() in _IMAGE_EXTENSIONS:
        key = stem
    if parts.query and not _path_has_image_extension(path):
        key = f"{key}?{parts.query.lower()}"
    return key

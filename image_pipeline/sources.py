"""The fallback source chain (plan.md section 11).

When the sheet's own URLs do not solve a row (no page matched, or a matched page
yielded no usable image), this module finds *candidate product pages* elsewhere:

    1. the brand's own sources      (HaierMall, haier.com.pk, dawlance, TCL, PEL, Homage)
    2. retail Shopify shops         (JSON search: /search/suggest.json)
    3. retail WooCommerce shops     (JSON search: /wp-json/wc/store/products)

Three rules keep this safe and cheap:

* **JSON method first.** A source's platform is declared in
  ``config.source_platforms`` (measured, not guessed), so a search costs exactly one
  request.  HTML crawling is not used at all here.
* **A candidate is never trusted.** :mod:`pipeline` runs the same ``verify_page``
  (safeguard S15) on the candidate with the *sheet's* model before a single image URL
  is looked at.  The search term is always the sheet's model too - never a code found
  on a page, so a stale URL can never "fix itself" into a successor model.
* **Cheap by construction.** Official brand sites first answer from a cached sitemap
  index (one crawl per brand, not per product); deadlines are bounded by config
  (``fallback_max_pages_per_row``, ``fallback_max_search_per_source``), every request
  goes through the shared ``DomainRateLimiter`` and ``robots.txt`` is respected.

Everything here is data-driven: to add a shop, add one line to
``config.source_platforms`` and one to ``config.brand_sources`` /
``config.retail_shopify_sources`` / ``config.retail_woo_sources``.  No code change.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote_plus, urlsplit, urlunsplit

from config import PipelineConfig
from csv_io import atomic_write_text
from model_matching import (
    capacity_codes,
    fuzzy_identity_match,
    fuzzy_series_anchor,
    has_series_name,
    identity_is_capacity_glued,
    truncation_variants,
)
from models import ModelKeys, PageCandidate, SourceSearchReport
from textnorm import normalize_key
from url_filters import is_excluded_host

LOG = logging.getLogger("pipeline.sources")

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_SITEMAPINDEX_RE = re.compile(r"<sitemapindex", re.IGNORECASE)
_SITEMAP_CHILD_HINTS: tuple[str, ...] = (
    "product",
    "tv",
    "appliance",
    "refrigerator",
    "washing",
    "led",
    "ac",
    "kitchen",
)


# --------------------------------------------------------------------------- #
# the tiny HTTP boundary (so the chain is testable without a network)
# --------------------------------------------------------------------------- #
class JsonHttp(Protocol):
    """One GET, text back.  Returns ``None`` for any failure (never raises)."""

    def get_text(self, url: str) -> str | None:
        ...


class SitemapCache:
    """``state/sitemaps/<host>.jsonl`` - one URL index per brand, refreshed on TTL.

    The cache is what makes the official-brand step cheap: the first row of a brand
    pays one crawl, every later row is a local lookup.
    """

    def __init__(self, folder: Path, ttl_days: int) -> None:
        self._folder = folder
        self._ttl_s = max(1, ttl_days) * 24 * 3600

    def _path(self, host: str) -> Path:
        safe = re.sub(r"[^a-z0-9._-]", "_", host.lower())
        return self._folder / f"{safe}.jsonl"

    def load(self, host: str, *, refresh: bool) -> list[str] | None:
        """Cached URLs for ``host``, or ``None`` when there is no fresh cache."""
        path = self._path(host)
        if refresh or not path.exists():
            return None
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return None
        if age > self._ttl_s:
            return None
        urls: list[str] = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    url = payload.get("url")
                    if isinstance(url, str) and url:
                        urls.append(url)
        except OSError:
            return None
        return urls or None

    def save(self, host: str, urls: list[str]) -> None:
        path = self._path(host)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = "".join(
            json.dumps({"url": url}, ensure_ascii=False) + "\n" for url in urls
        )
        try:
            atomic_write_text(path, lines)
        except OSError as exc:                              # pragma: no cover - disk
            LOG.warning("sitemap cache not written for %s: %s", host, exc)


class RobotsCache:
    """Minimal ``robots.txt`` courtesy check, cached per host for the whole run."""

    def __init__(self, http: JsonHttp) -> None:
        self._http = http
        self._rules: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}

    def allows(self, url: str) -> bool:
        parts = urlsplit(url)
        host = parts.netloc.lower()
        base = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
        if host not in self._rules:
            text = self._http.get_text(base) or ""
            disallow: list[str] = []
            allow: list[str] = []
            applies = False
            for raw_line in text.splitlines():
                line = raw_line.split("#", 1)[0].strip()
                if not line or ":" not in line:
                    continue
                key, _, value = line.partition(":")
                key = key.strip().lower()
                value = value.strip()
                if key == "user-agent":
                    applies = value == "*" or value.lower().startswith("image_pipeline")
                elif applies and key == "disallow" and value:
                    disallow.append(value)
                elif applies and key == "allow" and value:
                    allow.append(value)
            self._rules[host] = (tuple(disallow), tuple(allow))
        blocked, permitted = self._rules[host]
        path = parts.path or "/"
        if any(path.startswith(prefix) for prefix in permitted):
            return True
        return not any(prefix == "/" or path.startswith(prefix) for prefix in blocked)


# --------------------------------------------------------------------------- #
# parsing (pure functions - each one unit-tested)
# --------------------------------------------------------------------------- #
_SEPARATOR_RUN_RE = re.compile(r"[\s\-_/.]+")


def shop_spellings(token: str) -> list[str]:
    """The ways a shop may spell one model key, mechanically: as printed, hyphen, glued, spaced.

    Pure separator normalisation - no per-model table, so a key the sheet adds tomorrow
    works the same day.  Live reason (2026-10-06): Surmawala's Shopify suggest endpoint
    answered ``DW-7500`` for the sheet's ``DW 7500``, so the sheet's own spacing must not be
    the only spelling tried.
    """
    hyphen = _SEPARATOR_RUN_RE.sub("-", token)
    glued = _SEPARATOR_RUN_RE.sub("", token)
    spaced = _SEPARATOR_RUN_RE.sub(" ", token)
    spellings: list[str] = []
    for spelling in (token, hyphen, glued, spaced):
        if spelling and spelling not in spellings:
            spellings.append(spelling)
    return spellings


def search_terms(keys: ModelKeys, *, brand: str | None = None, limit: int = 3, title: str = "") -> tuple[str, ...]:
    """The terms a shop is searched for: always the **sheet's own** model keys.

    All spellings of the first key are tried before the next key - the *term* is still the
    sheet's own model, never a code read off some page.  When the row's brand is known it
    leads the query ("Orient 18X" before "18X"): a bare capacity code is shared by every
    maker, and an unbranded query is how a Panasonic ``18XKF9`` page surfaced for the
    Orient Megatron row (owner run 2026-10-12).  The bare spelling stays as the fallback,
    so a shop that indexes the code alone is still found - and the page's own brand gate
    remains the hard stop.

    A row that carries **no model code at all** is searched by its own keywords instead
    (see :func:`_keyword_terms`); before this it was never searchable - live 2026-10-14:
    the ``AVANTE+ ... (9173)`` row silently queried zero sources.
    """
    terms: list[str] = []

    def add(term: str, *, cap: int = limit) -> bool:
        if term not in terms:
            terms.append(term)
        return len(terms) >= cap

    for token in keys.tokens:
        for spelling in shop_spellings(token):
            if brand and add(f"{brand.title()} {spelling}"):
                return tuple(terms)
            if add(spelling):
                return tuple(terms)
    if not terms:
        # A code-less row gets one more query than a coded one: the bare number
        # stays as the last spelling for shops that index the code alone.
        for term in _keyword_terms(keys, brand=brand, title=title):
            if add(term, cap=limit + 1):
                break
    return tuple(terms)


#: How a shop spells a tier: ``AVANTE+`` - the symbol, never the word "Plus" (owner 2026-10-14).
_SERIES_PLUS_RE = re.compile(r"([A-Za-z]{2,})\+")


def _series_spelling(keywords: tuple[str, ...], brand: str | None, title: str) -> str:
    """The series word for a query, in the spelling a shop's own search answers to."""
    series = next((w.title() for w in keywords if w.isalpha() and w != (brand or "").lower()), "")
    glued = _SERIES_PLUS_RE.search(title)
    if glued and (not series or series.lower() == glued.group(1).lower()):
        return glued.group(1).title() + "+"
    return series


def _keyword_terms(keys: ModelKeys, *, brand: str | None, title: str) -> list[str]:
    """Queries for a code-less row, built from its own keywords (max 4).

    The words are the sheet's own (``('dawlance','avante','12','gem','9173')``), so a term
    is never something read off a page.  Most specific first: ``Brand Series+ Number`` >
    ``Brand Series+`` > ``Brand Number`` > bare spellings.
    """
    numbers = [word for word in keys.keywords if any(char.isdigit() for char in word)]
    number = max(numbers, key=len) if numbers else ""
    series = _series_spelling(keys.keywords, brand, title)
    prefix = brand.title() if brand else ""

    def joined(*parts: str) -> str:
        return " ".join(part for part in parts if part)

    out: list[str] = []
    for term in (joined(prefix, series, number), joined(prefix, series),
                 joined(prefix, number), number, series):
        if term and term not in out:
            out.append(term)
    return out[:4]


def _as_report(found: list[PageCandidate], tried: list[str]) -> SourceSearchReport:
    """One small constructor keeps the report shape in a single place."""
    used = ", ".join(dict.fromkeys(candidate.origin for candidate in found))
    return SourceSearchReport(
        tried=tuple(tried),
        used=used,
        note=(
            f"fallback search: {len(found)} candidate page(s) from {used}"
            if found
            else ""
        ),
    )


def parse_shopify_suggest(text: str, base: str) -> list[PageCandidate]:
    """Shopify ``/search/suggest.json`` payload -> candidates (page URLs only)."""
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError:
        return []
    products = (
        ((payload.get("resources") or {}).get("results") or {}).get("products") or []
    )
    return _candidates_from(products, base, "json-search", url_key="url", title_key="title")


def parse_woo_products(text: str, base: str) -> list[PageCandidate]:
    """WooCommerce ``/wp-json/wc/store/products`` payload -> candidates."""
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return _candidates_from(payload, base, "json-search", url_key="permalink", title_key="name")


def _candidates_from(
    rows: list[Any],
    base: str,
    method: str,
    *,
    url_key: str,
    title_key: str,
) -> list[PageCandidate]:
    host = urlsplit(base).netloc.lower()
    out: list[PageCandidate] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = row.get(url_key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        url = raw.strip()
        if url.startswith("/"):
            url = f"{urlsplit(base).scheme or 'https'}://{host}{url}"
        if urlsplit(url).netloc.lower() != host:
            continue                            # only pages on the searched shop
        title = row.get(title_key)
        out.append(
            PageCandidate(
                url=url,
                origin=f"{method}:{host}",
                method=method,
                title_hint=title if isinstance(title, str) else "",
            )
        )
    return out


def parse_sitemap(text: str) -> tuple[list[str], list[str]]:
    """Sitemap XML -> (page urls, child sitemap urls)."""
    urls = _LOC_RE.findall(text)
    if _SITEMAPINDEX_RE.search(text):
        return [], urls
    return urls, []


def model_tokens_in_url(url: str, keys: ModelKeys) -> bool:
    """Does the URL slug carry this product's model (separators ignored)?

    Codes first, exactly as before.  Then :func:`fuzzy_identity_match`, because a brand's
    file names are built from the model *name* plus the brand's own capacity letter, which
    is not the letter the reseller's title used (Orient: ``12X`` in the sheet, ``12s`` in
    ``titanium-12s-olive-black-t3-plus.html``).  Without that, no URL on the brand's site is
    ever offered for such a row and the fallback ends up on Daraz and Lazada - which is what
    the owner saw on 2026-10-10 ("why did it not go to brand official").

    Last, the bounded capacity code itself (owner run 2026-10-11, "why not going to brand
    official strictly first"): ``Orient 18X White`` has the official page
    ``platinium-18x-matt-white-t3-plus.html``, but ``18x`` is only three letters, so the
    code loop above never carried it.  Two-or-three digits plus one letter (``18x``,
    ``19c``) are shape-bounded enough to match on their own - a bare number never does,
    and another letter (``18g``) stays out.  This route opens **only for a row that has
    nothing but the code** (no series name): a named row like ``Megatron 12X`` keeps the
    fuzzy route, which demands the name, so the code alone never drags in another series
    that happens to share the capacity letter.

    The glued-identity routes carry one more condition for a named row (owner run
    2026-10-11, the Innovia download): the identity ``19cultra`` is the capacity code
    ``19c`` glued to the tail ``ultra``, and Orient's whole Ultra line spells it -
    the real sitemap matched Blaze's, Glide's and Innovia's pages for the Pulse row.
    A capacity-glued identity (and its fuzzy letter tolerance) therefore only counts
    when the URL also names the row's own line (``pulse``); a real part number stays
    trusted wherever it appears.
    """
    path = urlsplit(url).path or url
    slug = normalize_key(path)
    anchor = fuzzy_series_anchor(keys)

    def names_the_line() -> bool:
        # The slug is glued (``pulse-19c-ultra`` -> ``pulse19cult...``), so a boundary
        # regex would refuse the row's own page; the line name is distinctive enough to
        # match as a substring.
        return any(word in slug for word in anchor)

    for token in keys.tokens:
        if not token:
            continue
        for variant in (token, *truncation_variants(token)):
            if len(variant) >= 4 and variant in slug:
                return True
    for identity in keys.identities:
        if not identity:
            continue
        guarded = identity_is_capacity_glued(identity) and bool(anchor)
        for variant in (identity, *truncation_variants(identity)):
            if len(variant) >= 4 and variant in slug and (not guarded or names_the_line()):
                return True
    if not has_series_name(keys):
        ours = capacity_codes(" ".join(keys.tokens)) or capacity_codes(keys.cleaned_title)
        if ours and ours & capacity_codes(urlsplit(url).path):
            return True
    if not fuzzy_identity_match(slug, keys):
        return False
    return not anchor or names_the_line()


def rank_candidates(
    candidates: list[PageCandidate],
    keys: ModelKeys,
) -> list[PageCandidate]:
    """Preference order for candidate pages of **one** source.  Never a rejection.

    A sitemap lists a whole model family in arbitrary order (``DW-7500`` white,
    ``DW-7500C``, ``DW-7500-G``, ``DW-7500-CB`` ...), and a Shopify ``suggest.json``
    payload can put a wrong-slug handle first (live: Surmawala's white DW-7500 sits at
    ``/products/…dw-6550g-white…``).  Two free signals from the sheet decide the order:

    1. the sheet's own colour word (``WHITE``) appears in the page URL or the shop's own
       title for it - that is the variant this row is about;
    2. the model token/identity appears in the URL slug.

    Ties keep the source's own order (it is relevance-ranked already).  Colour and
    variant differences stay flags everywhere else - this only decides what is tried
    first, exactly like the photo ordering in the gallery extractor.
    """
    colours = {colour.lower() for colour in keys.colours}

    def score(index_and_candidate: tuple[int, PageCandidate]) -> tuple[int, int, int]:
        index, candidate = index_and_candidate
        haystack = f"{candidate.url} {candidate.title_hint}".lower()
        colour_hit = 0 if colours and any(colour in haystack for colour in colours) else 1
        title_hint_lower = candidate.title_hint.lower()
        has_model = model_tokens_in_url(candidate.url, keys) or (
            bool(keys.tokens) and any(t.lower() in title_hint_lower for t in keys.tokens if t)
        )
        model_hit = 0 if has_model else 1
        return (colour_hit, model_hit, index)

    return [candidate for _, candidate in sorted(enumerate(candidates), key=score)]


# --------------------------------------------------------------------------- #
# the chain
# --------------------------------------------------------------------------- #
class SourceChain:
    """Finds candidate product pages when the sheet's own URLs did not solve a row."""

    def __init__(
        self,
        config: PipelineConfig,
        http: JsonHttp,
        *,
        sitemap_cache: SitemapCache | None = None,
        robots: RobotsCache | None = None,
    ) -> None:
        self._config = config
        self._http = http
        self._sitemaps = sitemap_cache or SitemapCache(
            config.state_dir / "sitemaps", config.sitemap_ttl_days
        )
        self._robots = robots or RobotsCache(http)

    # -- public API ---------------------------------------------------------
    def find_candidates(
        self,
        *,
        title: str,
        brand: str | None,
        keys: ModelKeys,
    ) -> tuple[list[PageCandidate], SourceSearchReport]:
        """Candidate pages for this row, official brand sources first."""
        terms = search_terms(keys, brand=brand, title=title)
        if not terms:
            # Never silent: the row had neither a model code nor keywords to search with.
            return [], SourceSearchReport((), note="no search terms built - fallback search skipped")
        tried: list[str] = []
        found: list[PageCandidate] = []
        seen: set[str] = set()
        per_source = self._config.fallback_max_pages_per_source

        for base in self._config.sources_for(brand):
            if len(found) >= self._config.fallback_max_pages_per_row:
                break
            host = urlsplit(base).netloc.lower() or base.lower()
            if is_excluded_host(host, self._config.excluded_hosts):
                tried.append(f"{host} (skipped)")
                continue
            # brand sites are registered with their path ("www.haier.com/pk")
            platform = self._config.platform_of(base)
            page_url = f"{base.rstrip('/')}/"
            if self._config.respect_robots and not self._robots.allows(page_url):
                tried.append(f"{host} (robots)")
                LOG.info("source skipped (robots): %s", host)
                continue
            candidates = self._search(base, host, platform, terms, keys)
            # The sheet's variant first, then this source's own relevance order.  Only
            # this source's share of the budget is taken, so one chatty source (a brand
            # sitemap listing the whole family) cannot hide the priority-2 retailers.
            candidates = rank_candidates(candidates, keys)[:per_source]
            tried.append(f"{host}:{platform}:{len(candidates)}")
            for candidate in candidates:
                if candidate.url in seen:
                    continue
                seen.add(candidate.url)
                found.append(candidate)
                if len(found) >= self._config.fallback_max_pages_per_row:
                    break

        return found, _as_report(found, tried)

    # -- per-platform search ------------------------------------------------
    def _search(
        self,
        base: str,
        host: str,
        platform: str,
        terms: tuple[str, ...],
        keys: ModelKeys,
    ) -> list[PageCandidate]:
        if platform == "shopify":
            return self._shopify(base, host, terms)
        if platform == "woo":
            return self._woo(base, host, terms)
        if platform == "sitemap":
            return self._from_sitemap(base, host, keys)
        return []

    def _shopify(self, base: str, host: str, terms: tuple[str, ...]) -> list[PageCandidate]:
        limit = self._config.fallback_max_pages_per_row
        for term in terms:
            url = (
                f"{base.rstrip('/')}/search/suggest.json?q={quote_plus(term)}"
                f"&resources[type]=product&resources[limit]={limit}"
            )
            text = self._get(url)
            if text is None:
                continue
            hits = parse_shopify_suggest(text, base)
            if hits:
                return hits
        return []

    def _woo(self, base: str, host: str, terms: tuple[str, ...]) -> list[PageCandidate]:
        limit = self._config.fallback_max_pages_per_row
        for term in terms:
            url = (
                f"{base.rstrip('/')}/wp-json/wc/store/products"
                f"?search={quote_plus(term)}&per_page={limit}"
            )
            text = self._get(url)
            if text is None:
                continue
            hits = parse_woo_products(text, base)
            if hits:
                return hits
        return []

    def _from_sitemap(self, base: str, host: str, keys: ModelKeys) -> list[PageCandidate]:
        urls = self._sitemap_urls(base, host)
        if not urls:
            return []
        matched = [url for url in urls if model_tokens_in_url(url, keys)]
        LOG.info(
            "[search] %s: sitemap %d url(s), %d match the model", host, len(urls), len(matched)
        )
        candidates = [
            PageCandidate(
                url=url,
                origin=f"sitemap:{host}",
                method="sitemap",
                title_hint=url.rsplit("/", 1)[-1],
            )
            for url in matched
        ]
        # Rank before truncating: a sitemap lists the whole model family in arbitrary
        # order, so the sheet's own variant must not be cut off by the budget.
        return rank_candidates(candidates, keys)[: self._config.fallback_max_pages_per_row]

    def _sitemap_urls(self, base: str, host: str) -> list[str]:
        cached = self._sitemaps.load(host, refresh=self._config.refresh_sitemaps)
        if cached is not None:
            return cached
        root = f"{base.rstrip('/')}/sitemap.xml"
        text = self._get(root)
        if text is None:
            # Visible, not silent: the owner kept seeing "www.tcl.com:sitemap:0" with no
            # way to tell a blocked/missing sitemap from a model-less one.
            LOG.warning("[search] %s: sitemap.xml fetch failed - official search yields 0", host)
            return []
        pages, children = parse_sitemap(text)
        if children:
            wanted = [
                url
                for url in children
                if any(hint in url.lower() for hint in _SITEMAP_CHILD_HINTS)
            ][: self._config.sitemap_max_child_maps]
            for child in wanted:
                if len(pages) >= self._config.sitemap_max_urls:
                    break
                child_text = self._get(child)
                if child_text is None:
                    continue
                child_pages, _ = parse_sitemap(child_text)
                pages.extend(child_pages)
        pages = pages[: self._config.sitemap_max_urls]
        if pages:
            self._sitemaps.save(host, pages)
        return pages

    def _get(self, url: str) -> str | None:
        """One polite GET through the injected HTTP boundary."""
        if self._config.fallback_max_search_per_source <= 0:
            return None
        return self._http.get_text(url)

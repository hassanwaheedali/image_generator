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
from model_matching import normalize_key, truncation_variants
from models import ModelKeys, PageCandidate, SourceSearchReport
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


def search_terms(keys: ModelKeys, *, limit: int = 3) -> tuple[str, ...]:
    """The terms a shop is searched for: always the **sheet's own** model keys.

    All spellings of the first key are tried before the next key - the *term* is still the
    sheet's own model, never a code read off some page.
    """
    terms: list[str] = []
    for token in keys.tokens:
        for spelling in shop_spellings(token):
            if spelling not in terms:
                terms.append(spelling)
            if len(terms) >= limit:
                return tuple(terms)
    return tuple(terms)


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
    """Does the URL slug carry this product's model (separators ignored)?"""
    slug = normalize_key(url)
    for token in (*keys.tokens, *keys.identities):
        if not token:
            continue
        for variant in (token, *truncation_variants(token)):
            if len(variant) >= 4 and variant in slug:
                return True
    return False


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
        model_hit = 0 if model_tokens_in_url(candidate.url, keys) else 1
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
        terms = search_terms(keys)
        if not terms:
            return [], SourceSearchReport(())
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

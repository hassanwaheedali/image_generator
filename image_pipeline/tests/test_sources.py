"""The fallback source chain (plan.md section 11): parsing, caches, ordering, robots."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from config import PipelineConfig
from model_matching import build_model_keys
from models import PageCandidate
from sources import (
    RobotsCache,
    SitemapCache,
    SourceChain,
    model_tokens_in_url,
    parse_shopify_suggest,
    parse_sitemap,
    parse_woo_products,
    search_terms,
)


class FakeHttp:
    """Maps URL -> text; anything else is a miss (like a dead source)."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def get_text(self, url: str) -> str | None:
        self.requested.append(url)
        return self.pages.get(url)


def _config(tmp_path: Path, **overrides: object) -> PipelineConfig:
    defaults: dict[str, object] = {
        "state_dir": tmp_path / "state",
        "output_dir": tmp_path / "out",
        "assets_dir": tmp_path / "assets",
        "domain_min_interval_s": 0.0,
        "fallback_max_pages_per_row": 3,
        "dry_run": True,          # the chain itself never uploads; keeps the key check out
    }
    defaults.update(overrides)
    return PipelineConfig(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# terms and parsers
# --------------------------------------------------------------------------- #
def test_search_terms_always_come_from_the_sheet() -> None:
    keys = build_model_keys("Haier 50 Inch QLED Google LED TV (H50S80EUX)")
    assert search_terms(keys) == ("H50S80EUX",)


def test_shopify_suggest_parsing() -> None:
    payload = json.dumps(
        {
            "resources": {
                "results": {
                    "products": [
                        {"url": "/products/haier-50-tv", "title": "Haier 50 TV"},
                        {"url": "https://evil.example/p/x", "title": "other host"},
                    ]
                }
            }
        }
    )
    hits = parse_shopify_suggest(payload, "https://www.surmawala.pk")
    assert len(hits) == 1                                   # foreign host dropped
    assert hits[0].url == "https://www.surmawala.pk/products/haier-50-tv"
    assert hits[0].method == "json-search"
    assert hits[0].origin == "json-search:www.surmawala.pk"


def test_woo_products_parsing() -> None:
    payload = json.dumps(
        [{"permalink": "https://www.alfatah.com.pk/product/haier-h50s80eux/", "name": "Haier"}]
    )
    hits = parse_woo_products(payload, "https://www.alfatah.com.pk")
    assert hits[0].url.endswith("/product/haier-h50s80eux/")
    assert hits[0].title_hint == "Haier"


def test_parsers_never_raise_on_garbage() -> None:
    assert parse_shopify_suggest("<html>blocked</html>", "https://x.pk") == []
    assert parse_woo_products('{"detail":"no route"}', "https://x.pk") == []


def test_sitemap_parsing_and_child_detection() -> None:
    index = (
        "<?xml version='1.0'?><sitemapindex>"
        "<sitemap><loc>https://x.pk/sitemap-products.xml</loc></sitemap>"
        "</sitemapindex>"
    )
    pages, children = parse_sitemap(index)
    assert pages == [] and children == ["https://x.pk/sitemap-products.xml"]

    urlset = "<urlset><url><loc>https://x.pk/tvs/h50s80eux.shtml</loc></url></urlset>"
    pages, children = parse_sitemap(urlset)
    assert pages == ["https://x.pk/tvs/h50s80eux.shtml"] and children == []


def test_model_tokens_in_url_ignores_separators() -> None:
    keys = build_model_keys("Haier 50 Inch QLED Google LED TV (H50S80EUX)")
    assert model_tokens_in_url("https://www.haier.com/pk/tvs/h50s80eux.shtml", keys)
    assert model_tokens_in_url("https://x.pk/p/haier-h50s80eux-tv", keys)
    assert not model_tokens_in_url("https://x.pk/p/h55s80eux-tv", keys)


# --------------------------------------------------------------------------- #
# caches and robots
# --------------------------------------------------------------------------- #
def test_sitemap_cache_roundtrip(tmp_path: Path) -> None:
    cache = SitemapCache(tmp_path / "sitemaps", ttl_days=7)
    assert cache.load("example.pk", refresh=False) is None
    cache.save("example.pk", ["https://example.pk/p/a", "https://example.pk/p/b"])
    assert cache.load("example.pk", refresh=False) == [
        "https://example.pk/p/a",
        "https://example.pk/p/b",
    ]
    assert cache.load("example.pk", refresh=True) is None          # --refresh-sitemaps


def test_robots_disallow_blocks_a_source() -> None:
    http = FakeHttp(
        {"https://shop.pk/robots.txt": "User-agent: *\nDisallow: /search\n"}
    )
    robots = RobotsCache(http)
    assert robots.allows("https://shop.pk/products/x")
    assert not robots.allows("https://shop.pk/search?q=1")
    # the file is fetched once per host for the whole run
    assert http.requested.count("https://shop.pk/robots.txt") == 1


# --------------------------------------------------------------------------- #
# chain ordering, limits, exclusions
# --------------------------------------------------------------------------- #
def test_chain_prefers_official_brand_sources_then_retail(tmp_path: Path) -> None:
    config = _config(tmp_path)
    chain = SourceChain(config, FakeHttp({}))
    order = chain._config.sources_for("haier")
    assert order[0] == "https://haiermall.pk"
    assert "https://www.haier.com/pk" in order[:2]
    assert "https://www.surmawala.pk" in order
    assert order.index("https://www.surmawala.pk") > order.index("https://www.haier.com/pk")


def test_chain_returns_candidates_and_a_report(tmp_path: Path) -> None:
    config = _config(tmp_path)
    payload = json.dumps(
        {"resources": {"results": {"products": [{"url": "/products/a", "title": "A"}]}}}
    )
    http = FakeHttp(
        {
            "https://www.surmawala.pk/search/suggest.json?q=H50S80EUX"
            "&resources[type]=product&resources[limit]=3": payload,
            "https://www.surmawala.pk/robots.txt": "",
        }
    )
    chain = SourceChain(config, http)
    keys = build_model_keys("Haier 50 Inch QLED Google LED TV (H50S80EUX)")
    found, report = chain.find_candidates(title=keys.cleaned_title, brand="haier", keys=keys)
    assert [candidate.url for candidate in found] == [
        "https://www.surmawala.pk/products/a"
    ]
    assert "surmawala" in report.used and "candidate page" in report.note


def test_chain_respects_excluded_hosts(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), excluded_hosts=("www.surmawala.pk",))
    chain = SourceChain(config, FakeHttp({}))
    keys = build_model_keys("Haier HSU-12 1.5 Ton")
    found, report = chain.find_candidates(title="x", brand="haier", keys=keys)
    assert found == []
    assert "www.surmawala.pk (skipped)" in report.tried


def test_respect_robots_switch_disables_the_check(tmp_path: Path) -> None:
    """``respect_robots=False`` is the documented off-switch for the courtesy check."""
    payload = json.dumps(
        {"resources": {"results": {"products": [{"url": "/products/a", "title": "A"}]}}}
    )
    blocked = "User-agent: *\nDisallow: /\n"
    config = _config(tmp_path)
    http = FakeHttp(
        {
            "https://www.surmawala.pk/search/suggest.json?q=HSU-12HFPAA"
            "&resources[type]=product&resources[limit]=3": payload,
            "https://www.surmawala.pk/robots.txt": blocked,
        }
    )
    keys = build_model_keys("Haier HSU-12HFPAA 1.5 Ton")
    assert SourceChain(config, http).find_candidates(
        title="x", brand="haier", keys=keys
    )[0] == []
    assert (
        "www.surmawala.pk (robots)" in SourceChain(config, http).find_candidates(
            title="x", brand="haier", keys=keys
        )[1].tried
    )
    relaxed = replace(config, respect_robots=False)
    found, _ = SourceChain(relaxed, http).find_candidates(title="x", brand="haier", keys=keys)
    assert [candidate.url for candidate in found] == ["https://www.surmawala.pk/products/a"]


def test_chain_uses_the_sitemap_for_brand_sites(tmp_path: Path) -> None:
    config = _config(tmp_path)
    urlset = (
        "<urlset>"
        "<url><loc>https://www.haier.com/pk/tvs/h50s80eux.shtml</loc></url>"
        "<url><loc>https://www.haier.com/pk/about/company.shtml</loc></url>"
        "</urlset>"
    )
    http = FakeHttp(
        {
            "https://www.haier.com/pk/robots.txt": "",
            "https://www.haier.com/pk/sitemap.xml": urlset,
        }
    )
    chain = SourceChain(config, http)
    keys = build_model_keys("Haier 50 Inch QLED Google LED TV (H50S80EUX)")
    found, report = chain.find_candidates(title="x", brand="haier", keys=keys)
    assert [candidate.url for candidate in found] == [
        "https://www.haier.com/pk/tvs/h50s80eux.shtml"
    ]
    # second call answers from the cache: no second sitemap request
    before = http.requested.count("https://www.haier.com/pk/sitemap.xml")
    chain.find_candidates(title="x", brand="haier", keys=keys)
    assert http.requested.count("https://www.haier.com/pk/sitemap.xml") == before


def test_candidate_is_only_a_page_never_an_image() -> None:
    """S15: the chain hands over URLs of pages - image lists are never taken from it."""
    payload = json.dumps(
        {
            "resources": {
                "results": {
                    "products": [
                        {
                            "url": "/products/a",
                            "title": "A",
                            "image": "https://cdn.shop/a.jpg",
                            "images": ["https://cdn.shop/a.jpg"],
                        }
                    ]
                }
            }
        }
    )
    hits = parse_shopify_suggest(payload, "https://shop.pk")
    assert [hit.url for hit in hits] == ["https://shop.pk/products/a"]
    assert isinstance(hits[0], PageCandidate)
    assert not hasattr(hits[0], "images")

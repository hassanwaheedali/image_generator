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
    rank_candidates,
    search_terms,
    shop_spellings,
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


def test_search_terms_spell_each_key_the_three_ways_shops_know() -> None:
    """The sheet's own spelling, the hyphenated one and the glued one.

    A live check of Surmawala's Shopify suggest endpoint (2026-10-06) answered
    ``DW-7500`` while the sheet prints ``DW 7500``; the *term* is still our model, never a
    code read off some page.  This is also what stops a spaced sheet title from dying
    before it reaches any shop.
    """
    spaced = build_model_keys("DW 7500 TWIN TUB CLEAR LID (WHITE)")
    assert search_terms(spaced) == ("DW 7500", "DW-7500", "DW7500")
    assert search_terms(spaced, limit=2) == ("DW 7500", "DW-7500")
    # a key the sheet already hyphenates gets glued and spaced forms too - live DW-7500C row
    hyphenated = build_model_keys("Dawlance Semi Automatic Washing Machine 10kg (DW-7500C)")
    assert search_terms(hyphenated) == ("DW-7500C", "DW7500C", "DW 7500C")
    # a code with no separators is not mangled into dashed letter runs
    assert shop_spellings("H50S80EUX") == ["H50S80EUX"]
    assert search_terms(build_model_keys("Haier 50 Inch QLED (H50S80EUX)")) == ("H50S80EUX",)


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


# --------------------------------------------------------------------------- #
# budget per source, and variant ranking (live Dawlance / Surmawala case)
# --------------------------------------------------------------------------- #
#: The real Shopify answer for q=DW-7500 on surmawala.pk (fetched 2026-10-06).  Note the
#: white machine's handle: ``…dw-6550g-white…`` - the slug does not carry the model, the
#: title does.
SURMAWALA_REAL_PAYLOAD = json.dumps(
    {
        "resources": {
            "results": {
                "products": [
                    {
                        "url": "/products/dawlance-washing-machine-dw-6550g-white-price-in-pakistan"
                        "?_pos=1&_psq=DW-7500&_psid=ecc428a06&_ss=e",
                        "title": "Dawlance Semi Automatic Washing Machine DW-7500 - 10Kg",
                    },
                    {
                        "url": "/products/dawlance-semi-automatic-washing-machine-dw-7500c-10kg"
                        "?_pos=2&_psq=DW-7500&_psid=ecc428a06&_ss=e",
                        "title": "Dawlance Semi Automatic Washing Machine DW-7500C - 10Kg",
                    },
                ]
            }
        }
    }
)

DW_SITEMAP = (
    "<urlset>"
    + "".join(
        f"<url><loc>{url}</loc></url>"
        for url in (
            (
                "https://www.dawlance.com.pk/twin-tub-washing-machine/dw-7500-twin-tub-glass-lid"
                "-black-grey-semi-automatic-washing-machines"
            ),
            "https://www.dawlance.com.pk/product/dw-7500-c/",
            "https://www.dawlance.com.pk/product/dw-7500-g/",
            "https://www.dawlance.com.pk/product/dw-7500-g-fl/",
            (
                "https://www.dawlance.com.pk/twin-tub-washing-machine/dw-7500-twin-tub-clear-lid"
                "-white-semi-automatic-washing-machines"
            ),
        )
    )
    + "</urlset>"
)


def _chain_fixture(tmp_path: Path, **overrides: object) -> tuple[SourceChain, FakeHttp]:
    defaults: dict[str, object] = {"fallback_max_pages_per_row": 6, "fallback_max_pages_per_source": 3}
    defaults.update(overrides)
    config = _config(tmp_path, **defaults)
    http = FakeHttp(
        {
            "https://www.dawlance.com.pk/robots.txt": "User-agent: *\nDisallow:\n",
            "https://www.dawlance.com.pk/sitemap.xml": DW_SITEMAP,
            "https://www.surmawala.pk/robots.txt": "User-agent: *\nDisallow:\n",
            "https://www.surmawala.pk/search/suggest.json?q=DW-7500"
            "&resources[type]=product&resources[limit]=6": SURMAWALA_REAL_PAYLOAD,
        }
    )
    return SourceChain(config, http, sitemap_cache=SitemapCache(tmp_path / "sm", 7)), http


def test_one_talkative_source_cannot_hide_the_next_priority_source(tmp_path: Path) -> None:
    """Live bug (2026-10-06): the Dawlance sitemap answered with the whole DW-7500 family,
    filled the candidate budget, and surmawala.pk - the priority-2 retailer that really has
    the product - was never queried, so the row failed although the shop had it."""
    chain, http = _chain_fixture(tmp_path)
    keys = build_model_keys("Dawlance DW-7500 WHITE Twin Tub Washing Machine")
    found, report = chain.find_candidates(title=keys.cleaned_title, brand="dawlance", keys=keys)

    assert any("surmawala" in url for url in http.requested), "the retail tier must be searched"
    origins = [candidate.origin for candidate in found]
    assert sum("dawlance" in origin for origin in origins) == 3          # its share, not all 6
    assert sum("surmawala" in origin for origin in origins) == 2         # the shop that has it
    assert any("dw-6550g-white" in candidate.url for candidate in found)
    assert "www.surmawala.pk:shopify:2" in report.tried
    assert report.used.startswith("sitemap:")


def test_each_source_contributes_at_most_its_share_of_the_budget(tmp_path: Path) -> None:
    """``fallback_max_pages_per_source`` is what keeps the chain priority-wise."""
    chain, _ = _chain_fixture(tmp_path, fallback_max_pages_per_row=6, fallback_max_pages_per_source=2)
    keys = build_model_keys("Dawlance DW-7500 WHITE Twin Tub Washing Machine")
    found, _ = chain.find_candidates(title="x", brand="dawlance", keys=keys)
    assert sum("dawlance" in candidate.origin for candidate in found) == 2
    assert len(found) == 4                                              # 2 brand + 2 retail


def test_candidate_ranking_prefers_the_sheets_own_variant(tmp_path: Path) -> None:
    """Never a rejection - colour only decides the order (as in the photo gallery)."""
    keys = build_model_keys("Dawlance DW-7500 WHITE Twin Tub Washing Machine")
    black_grey = PageCandidate(
        url="https://www.dawlance.com.pk/x/dw-7500-twin-tub-glass-lid-black-grey-washing-machines",
        origin="sitemap:www.dawlance.com.pk",
        method="sitemap",
    )
    white = PageCandidate(
        url="https://www.dawlance.com.pk/x/dw-7500-twin-tub-clear-lid-white-washing-machines",
        origin="sitemap:www.dawlance.com.pk",
        method="sitemap",
    )
    unrelated = PageCandidate(
        url="https://www.dawlance.com.pk/product/dw-7500-g/",
        origin="sitemap:www.dawlance.com.pk",
        method="sitemap",
    )
    ranked = rank_candidates([black_grey, unrelated, white], keys)
    assert [candidate.url for candidate in ranked] == [white.url, black_grey.url, unrelated.url]

    # no colour in the sheet -> the source's own order is kept untouched
    plain = build_model_keys("Dawlance DW-7500 Twin Tub Washing Machine")
    assert [candidate.url for candidate in rank_candidates([black_grey, white], plain)] == [
        black_grey.url,
        white.url,
    ]

    # a shop's own title for the page counts too (Surmawala's white handle is mis-slugged)
    mis_slugged = PageCandidate(
        url="https://www.surmawala.pk/products/dawlance-washing-machine-dw-6550g-white-price-in-pakistan",
        origin="json-search:www.surmawala.pk",
        method="json-search",
        title_hint="Dawlance Semi Automatic Washing Machine DW-7500 - 10Kg",
    )
    other = PageCandidate(
        url="https://www.surmawala.pk/products/dawlance-semi-automatic-washing-machine-dw-7500c-10kg",
        origin="json-search:www.surmawala.pk",
        method="json-search",
        title_hint="Dawlance Semi Automatic Washing Machine DW-7500C - 10Kg",
    )
    assert [candidate.url for candidate in rank_candidates([other, mis_slugged], keys)] == [
        mis_slugged.url,
        other.url,
    ]


def test_shandaarbuy_is_a_default_retail_shopify_source(tmp_path: Path) -> None:
    """Owner 2026-10-08: the TCL 98C8L row's only live page anywhere was
    shandaarbuy.pk/products/tcl-98-c8l-... - the chain must query that shop."""
    config = _config(tmp_path, fallback_max_pages_per_row=6)
    assert "https://shandaarbuy.pk" in config.retail_shopify_sources
    payload = json.dumps(
        {
            "resources": {
                "results": {
                    "products": [
                        {
                            "url": "/products/tcl-98-c8l-qd-mini-led-google-tv",
                            "title": "TCL 98 C8L QD-Mini LED Google TV",
                        }
                    ]
                }
            }
        }
    )
    http = FakeHttp(
        {
            "https://shandaarbuy.pk/search/suggest.json?q=98C8L"
            "&resources[type]=product&resources[limit]=6": payload
        }
    )
    chain = SourceChain(config, http, sitemap_cache=SitemapCache(tmp_path / "sm", 7))
    keys = build_model_keys("TCL 98 Inch SQD Mini LED TV (98C8L)")
    found, report = chain.find_candidates(title=keys.cleaned_title, brand="tcl", keys=keys)
    assert any("shandaarbuy" in candidate.url for candidate in found)
    assert "shandaarbuy.pk:shopify:1" in report.tried

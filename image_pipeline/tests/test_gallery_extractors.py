"""Extraction sources 1-5, provenance and the strictly-scoped fallback."""

from __future__ import annotations

from bs4 import BeautifulSoup
from conftest import load_fixture

from gallery_extractors import ExtractionContext, extract_candidates, registry_extractor_for
from gallery_extractors.json_ld import extract as json_ld_extract
from gallery_extractors.shopify_json import (
    looks_like_shopify,
    parse_product_json,
    shopify_product_json_url,
)
from gallery_extractors.woo_json import (
    looks_like_woo,
    parse_store_products,
    woo_store_api_urls,
)
from model_matching import build_model_keys
from models import ImageCandidate
from page_verification import MatchSettings, strip_unwanted_regions, verify_page

HAIER = "https://www.haier.com/pk/hsu-12hfpaa.shtml"
PEL = "https://eshop.pel.com.pk/pel-kitchen-pro-23-ltr-microwave-oven"


def _context(page_url: str, html: str, title: str, brand: str | None, strength: int = 2) -> ExtractionContext:
    keys = build_model_keys(title)
    soup = strip_unwanted_regions(BeautifulSoup(html, "lxml"), page_url)
    return ExtractionContext(
        page_url=page_url,
        soup=soup,
        keys=keys,
        strength=strength,
        brand=brand,
        raw_html=html,
    )


# --------------------------------------------------------------------------- #
# sources
# --------------------------------------------------------------------------- #
def test_gallery_container_images_and_provenance() -> None:
    context = _context(HAIER, load_fixture("product_ok"), "Haier HSU-12HFPAA", "haier")
    candidates = extract_candidates(context)
    urls = [candidate.url for candidate in candidates]
    assert "https://cdn.haier.com/p/hsu-12hfpaa-1.jpg" in urls        # data-large_image wins
    assert "https://cdn.haier.com/p/hsu-12hfpaa-2.jpg" in urls        # largest srcset width
    assert all(candidate.source in {"json_ld", "gallery_container"} for candidate in candidates)
    assert urls[0].endswith("hsu-12hfpaa-1.jpg")                       # model URLs first


def test_jsonld_image_field_that_is_a_page_url_is_dropped() -> None:
    """A shop may put a *page* route (no image extension) in the JSON-LD ``image`` field.

    Those burn a download and 404 (live: telex.pk ``/products/<timestamp>``).  The extractor
    must only emit URLs that actually look like images.
    """
    html = """<html><head><title>Haier HSU-12HFPAA</title>
    <script type="application/ld+json">
    {"@type": "Product", "name": "Haier HSU-12HFPAA",
     "image": ["https://www.telex.pk/products/2026-06-28T22:25:28+05:00",
               "https://cdn.telex.pk/p/hsu-12hfpaa-a.jpg"]}
    </script></head><body></body></html>"""
    context = _context("https://www.telex.pk/hsu-12hfpaa", html, "Haier HSU-12HFPAA", "haier")
    urls = [candidate.url for candidate in extract_candidates(context)]
    assert "https://cdn.telex.pk/p/hsu-12hfpaa-a.jpg" in urls
    assert not any("2026-06-28" in url for url in urls)


def test_jsonld_two_products_only_matching_node() -> None:
    context = _context(
        HAIER, load_fixture("jsonld_two_products"), "Haier HSU-12HFPAA", "haier"
    )
    urls = [candidate.url for candidate in extract_candidates(context)]
    assert any("hsu-12hfpaa-a.jpg" in url for url in urls)
    assert not any("hsu-18hfpaa-a.jpg" in url for url in urls)


def test_magento_extractor_prefers_full_asset() -> None:
    assert registry_extractor_for(PEL) is not None
    context = _context(PEL, load_fixture("magento_gallery"), "PEL PMO-23 Microwave Oven", "pel")
    urls = [candidate.url for candidate in extract_candidates(context)]
    assert "https://eshop.pel.com.pk/media/catalog/product/p/m/pmo-23-1-full.jpg" in urls
    assert all(candidate.source == "registry:magento" for candidate in candidates_of(context))


def candidates_of(context: ExtractionContext) -> list[ImageCandidate]:
    return list(extract_candidates(context))


def test_no_gallery_and_no_model_urls_gives_nothing() -> None:
    """S12: a valid match with no gallery and no model-bearing URL yields nothing."""
    context = _context(HAIER, load_fixture("no_gallery"), "Haier HSU-12HFPAA", "haier")
    assert extract_candidates(context) == []


def test_whole_page_fallback_needs_a_verified_page_and_a_model_url() -> None:
    """Customer decision: any verified page is treated the same way (strength 1 or 2)."""
    html = """<html><head><title>Haier HSU-12HFPAA AC</title></head><body>
    <img src="https://cdn.haier.com/p/hsu-12hfpaa-lo.jpg">
    <img src="https://cdn.haier.com/p/hero-shot.jpg"></body></html>"""
    for strength in (2, 1):
        context = _context(HAIER, html, "Haier HSU-12HFPAA", "haier", strength=strength)
        urls = [candidate.url for candidate in extract_candidates(context)]
        # only the model-bearing URL - the unlabelled hero shot is never rescued (S12)
        assert urls == ["https://cdn.haier.com/p/hsu-12hfpaa-lo.jpg"]

    unverified = _context(HAIER, html, "Haier HSU-12HFPAA", "haier", strength=0)
    assert extract_candidates(unverified) == []


def test_whole_page_fallback_works_for_a_brand_code_page() -> None:
    """A brand+code page (538 IFGA for Haier HRF-538 IFGA) behaves like any other page."""
    html = """<html><head><title>Haier Refrigerator 538 IFGA</title></head><body>
    <img src="https://cdn.haier.com/p/538-ifga-lo.jpg">
    <img src="https://cdn.haier.com/p/hero-shot.jpg"></body></html>"""
    keys = build_model_keys("Haier HRF-538 IFGA")
    soup = strip_unwanted_regions(BeautifulSoup(html, "lxml"), HAIER)
    context = ExtractionContext(
        page_url="https://www.haier.com/pk/538-ifga",
        soup=soup,
        keys=keys,
        strength=2,
        brand="haier",
        raw_html=html,
        match_rule="brand_code",
    )
    urls = [candidate.url for candidate in extract_candidates(context)]
    assert urls == ["https://cdn.haier.com/p/538-ifga-lo.jpg"]


def test_candidates_carry_dom_flags() -> None:
    context = _context(HAIER, load_fixture("description_images"), "Haier HSU-12HFPAA", "haier")
    flagged = {candidate.url: candidate.dom_flags for candidate in extract_candidates(context)}
    assert flagged["https://cdn.haier.com/p/hsu-12hfpaa-description-chart.jpg"] == ("S2",)
    assert flagged["https://cdn.haier.com/p/hsu-12hfpaa-gallery.jpg"] == ()


def test_hidden_tracking_pixel_and_data_uri_ignored() -> None:
    html = """<html><head><title>Haier HSU-12HFPAA</title></head>
    <body><div class="product-gallery">
      <img src="data:image/gif;base64,R0lGOD">
      <img src="/media/1x1.gif">
      <img src="/media/hsu-12hfpaa-9.jpg"></div></body></html>"""
    context = _context(HAIER, html, "Haier HSU-12HFPAA", "haier")
    urls = [candidate.url for candidate in extract_candidates(context)]
    assert urls == ["https://www.haier.com/media/hsu-12hfpaa-9.jpg"]


def test_verified_strength_is_used_by_context() -> None:
    keys = build_model_keys("Haier HSU-12HFPAA")
    result = verify_page(HAIER, load_fixture("product_ok"), keys, brand="haier", settings=MatchSettings())
    assert result.strength == 2


# --------------------------------------------------------------------------- #
# structured product JSON: the whole image list (Shopify / WooCommerce)
# --------------------------------------------------------------------------- #
SHOPIFY_PAGE = "https://www.surmawala.pk/products/dawlance-semi-automatic-dw-7500c-10kg"

SHOPIFY_PRODUCT_JSON = """{"product": {
  "title": "Dawlance Semi Automatic Washing Machine DW-7500C - 10Kg",
  "images": [
    {"src": "https://cdn.shopify.com/s/files/1/0829/6490/6267/files/7500-c.png?v=1", "width": 1200},
    {"src": "https://cdn.shopify.com/s/files/1/0829/6490/6267/files/7500-c-side.png?v=2"},
    {"src": "https://cdn.shopify.com/s/files/1/0829/6490/6267/files/7500-c-top.png?v=3"}
  ],
  "media": [{"preview_image": {"src": "https://cdn.shopify.com/s/files/1/0829/6490/6267/files/7500-c-lid.png"}}],
  "featured_image": "https://cdn.shopify.com/s/files/1/0829/6490/6267/files/7500-c.png?v=1"
}}"""


def test_shopify_product_json_url() -> None:
    assert shopify_product_json_url(SHOPIFY_PAGE) == f"{SHOPIFY_PAGE}.json"
    assert shopify_product_json_url(f"{SHOPIFY_PAGE}?variant=123") == f"{SHOPIFY_PAGE}.json"
    assert shopify_product_json_url("https://shop.pk/collections/washing") == ""
    assert looks_like_shopify(SHOPIFY_PAGE, "<html>cdn.shopify.com</html>")
    assert not looks_like_shopify("https://shop.pk/collections/washing", "cdn.shopify.com")


def test_shopify_product_json_yields_every_image() -> None:
    """A Shopify `suggest.json` result carries one `featured_image`; the product's own JSON
    carries them all - which is how a rescued retail row gets 3+ pictures."""
    context = _context(SHOPIFY_PAGE, "<html></html>", "Dawlance DW-7500C Twin Tub", "dawlance")
    candidates = parse_product_json(SHOPIFY_PRODUCT_JSON, context)
    urls = [candidate.url for candidate in candidates]
    assert len(urls) == 4                                    # 3 images + 1 media preview
    assert all(candidate.source == "shopify_json" for candidate in candidates)
    assert all("?v=" not in url for url in urls)             # the cache-buster is canonicalised
    assert len(set(urls)) == 4
    assert parse_product_json("<html>blocked</html>", context) == []


def test_woo_store_api_urls_and_parsing() -> None:
    page = "https://www.alfatah.com.pk/product/pel-11kg-top-load-pawm-1100i/"
    urls = woo_store_api_urls(page)
    assert urls[0].endswith("/wp-json/wc/store/v1/products?slug=pel-11kg-top-load-pawm-1100i")
    assert urls[1].endswith("/wp-json/wc/store/products?slug=pel-11kg-top-load-pawm-1100i")
    assert looks_like_woo(page, "<html>wp-content woocommerce</html>")
    payload = """[{"id": 1, "images": [
        {"src": "https://www.alfatah.com.pk/wp-content/uploads/pawm-1100-1.jpg"},
        {"src": "https://www.alfatah.com.pk/wp-content/uploads/pawm-1100-2.jpg"}]}]"""
    context = _context(page, "<html></html>", "PEL PAWM-1100 Top Load", "pel")
    candidates = parse_store_products(payload, context)
    assert [candidate.url for candidate in candidates] == [
        "https://www.alfatah.com.pk/wp-content/uploads/pawm-1100-1.jpg",
        "https://www.alfatah.com.pk/wp-content/uploads/pawm-1100-2.jpg",
    ]
    assert all(candidate.source == "woo_json" for candidate in candidates)
    assert parse_store_products('{"detail":"no route"}', context) == []


# --------------------------------------------------------------------------- #
# S-json_ld: a row with no model code may still be identified by its own page
# --------------------------------------------------------------------------- #
_LDPAGE = "https://www.orient.com.pk/products/frost-18w-classic-white.html"


def _two_product_page() -> str:
    """A shop page whose JSON-LD names the product *and* a related one.

    Shape taken from the live capture ``uploads/live/orient_frost.html``
    (owner, 2026-10-10): the Frost node declares its image as
    ``assets/og-images/frost-18w-classic-white.jpg`` and the page's ``<img>`` tags
    are all cross-sell tiles.
    """
    return (
        '<html><head><script type="application/ld+json">'
        '{"@context":"https://schema.org","@graph":['
        '{"@type":"Product","name":"Megatron Titanium 12X","sku":"megatron-12x",'
        '"url":"https://www.orient.com.pk/products/megatron-titanium-12x.html",'
        '"image":"https://www.orient.com.pk/assets/products-images/ac/megatron/01.png"},'
        '{"@type":"Product","name":"Frost 18W Classic White","sku":"frost-18w-classic-white",'
        f'"url":"{_LDPAGE}",'
        '"image":"https://www.orient.com.pk/assets/og-images/frost-18w-classic-white.jpg?v=20260618c"}'
        "]}</script></head><body></body></html>"
    )


def test_model_free_title_still_reads_the_page_owning_node() -> None:
    """Frost failed the row because a title with no model code owns nothing to match."""
    context = _context(_LDPAGE, _two_product_page(), "Frost 18W Classic White | Orient Electronics Pakistan", "orient")
    urls = [candidate.url for candidate in json_ld_extract(context)]
    assert urls == ["https://www.orient.com.pk/assets/og-images/frost-18w-classic-white.jpg"]
    assert context.keys.tokens == () and context.keys.identities == ()


def test_page_owning_node_never_leaks_a_related_product() -> None:
    """The exception is only for the node that names *this* page; the tile stays out."""
    context = _context(_LDPAGE, _two_product_page(), "Frost 18W Classic White | Orient Electronics Pakistan", "orient")
    assert all("megatron" not in candidate.url for candidate in json_ld_extract(context))


def test_model_bearing_rows_keep_the_strict_gate() -> None:
    """Rows that own a model code are matched exactly as before - nothing is loosened."""
    context = _context(_LDPAGE, _two_product_page(), "Orient Megatron FW-9999X Titanium", "orient")
    assert context.keys.tokens  # the row owns a model code, so the exception must not apply
    assert [c.url for c in json_ld_extract(context)] == []


def test_page_identity_lie_is_blocked_even_with_the_page_slug_as_sku() -> None:
    """The word guard must not read the fields the identity check already trusts.

    A tile that copies ``url`` (or ``sku``) from the page it is sitting on is the shape
    real SEO templates produce; the name is what says which product the node is.
    """
    def run(name: str, sku: str, url: str, image: str) -> list[str]:
        html = (
            '<html><head><script type="application/ld+json">'
            f'{{"@context":"https://schema.org","@type":"Product","name":"{name}",'
            f'"sku":"{sku}","url":"{url}","image":"{image}"}}'
            "</script></head><body></body></html>"
        )
        context = _context(_LDPAGE, html, "Frost 18W Classic White | Orient Electronics Pakistan", "orient")
        return [candidate.url for candidate in json_ld_extract(context)]

    slug = "frost-18w-classic-white"
    assert run("Outdoor Unit Only", slug, "https://other.example/p", "https://cdn/ac/other.png") == []
    assert run("Megatron Titanium 12X", "zzz", _LDPAGE, "https://cdn/ac/megatron.png") == []
    assert run("Frost 18W Classic White", "zzz", _LDPAGE, "https://cdn/og/frost.jpg") == [
        "https://cdn/og/frost.jpg"
    ]
    # a node whose only shared word with the title is the brand is still this product
    assert run("Split AC 18000 BTU by Orient", "zzz", _LDPAGE, "https://cdn/og/ok.jpg") == [
        "https://cdn/og/ok.jpg"
    ]


def test_product_hero_picture_is_a_gallery_source() -> None:
    """A one-picture-per-model shop puts the photo in the hero, not in a gallery widget.

    Live Orient page, 2026-10-10: the row's only real photo sat in
    ``<div class="pdp-hero__image"><picture><img src="...15-ton-frost-classic-white-
    dc-inverter.png" alt="Frost 18W Classic White">``, while eight cross-sell tiles were
    extracted and rejected - because the page-wide fallback never runs once another
    source produced a candidate.  8 candidates -> 9, and the hero is among them.
    """
    page = "https://www.orient.com.pk/products/frost-18w-classic-white.html"
    hero = "../assets/products-images-2026/air-conditioners/15-ton-frost-classic-white-dc-inverter.png"
    html = (
        '<html><body><section class="pdp-hero"><div class="pdp-hero__image"><picture>'
        f'<source type="image/webp" srcset="{hero}.webp">'
        f'<img src="{hero}" alt="Frost 18W Classic White">'
        "</picture></div>"
        '<div class="related-products"><a href="../megatron-x.html">'
        '<img src="../assets/products-images/ac/ultron/05.png"></a></div>'
        "</section></body></html>"
    )
    context = _context(page, html, "Frost 18W Classic White | Orient Electronics Pakistan", "orient")
    candidates = extract_candidates(context)
    hero_candidate = next(c for c in candidates if "products-images-2026" in c.url)
    tile = next((c for c in candidates if "ultron" in c.url), None)
    # the hero is an absolute URL on the product page, carrying no S3 flag: nothing links it away
    assert hero_candidate.url == (
        "https://www.orient.com.pk/assets/products-images-2026/air-conditioners/"
        "15-ton-frost-classic-white-dc-inverter.png"
    )
    assert hero_candidate.page_url == page
    assert "S3" not in hero_candidate.dom_flags
    # the cross-sell tile keeps being flagged, so the new selectors cannot smuggle in
    # another model's photo
    if tile is not None:
        assert "S3" in tile.dom_flags


# --------------------------------------------------------------------------- #
# S19 - Haier names the crisp file in data-imgurl (owner's run, 2026-10-10)
# --------------------------------------------------------------------------- #
_HAIER_GALLERY_HTML = """
<html><body>
  <div class="sd_imgs">
    <ul>
      <li data-id="W020260404458450118371">
        <a href="javascript:;" class="js_proImg_cur">
          <img src="https://www.haier.com/pk/media/image/product/31365/s/W020260404458450118371_60.png"
               data-imgurl="https://www.haier.com/pk/media/image/product/31365/lg/W020260404458450118371_1200.png"
               alt="Haier HSU-13HFPC">
        </a>
      </li>
      <li data-id="W020260406514479331714">
        <a href="javascript:;">
          <img src="https://www.haier.com/pk/media/image/product/31365/s/W020260406514479331714_60.png"
               data-imgurl="https://www.haier.com/pk/media/image/product/31365/lg/W020260406514479331714_1200.png"
               alt="Haier HSU-13HFPC side view">
        </a>
      </li>
      <li data-id="W020260404458942582487">
        <a href="javascript:;">
          <img src="https://www.haier.com/pk/media/image/product/31365/s/W020260404458942582487_60.png"
               data-imgurl="https://www.haier.com/pk/media/image/product/31365/lg/W020260404458942582487.png"
               alt="Haier logo">
        </a>
      </li>
    </ul>
  </div>
</body></html>
"""


def test_haier_gallery_uses_the_declared_crisp_file() -> None:
    """The owner's live complaint (2026-10-10): "the page gallery has 6 crystal clear
    images, why did you download a blurry one".  Haier's markup puts the 60px thumbnail
    in ``src`` and the 1200px asset in ``data-imgurl`` - an attribute this pipeline did
    not read, so it took the thumb and then *guessed* a size onto an unnamed file.
    """
    context = _context(HAIER, _HAIER_GALLERY_HTML, "Haier HSU-13HFPC 0.75 WD", "haier")
    urls = [candidate.url for candidate in extract_candidates(context)]
    assert (
        "https://www.haier.com/pk/media/image/product/31365/lg/W020260404458450118371_1200.png"
        in urls
    )
    assert all("_60.png" not in url for url in urls), urls


def test_haier_emblem_is_still_extracted_but_fails_on_pixels() -> None:
    """Extraction is not where a brand's own gallery gets judged: the emblem file is
    named by the page, so it is collected (and S20 removes it once its pixels are read).
    """
    context = _context(HAIER, _HAIER_GALLERY_HTML, "Haier HSU-13HFPC 0.75 WD", "haier")
    urls = [candidate.url for candidate in extract_candidates(context)]
    assert any("W020260404458942582487" in url for url in urls)


def test_shopify_productview_gallery_extracted_from_html() -> None:
    """Shopify themes (like Surmawala's Halo/Ella theme) use .productView-thumbnail-wrapper
    and .productView-images. All product gallery images must be extracted as gallery candidates.
    """
    html = """
    <html><body>
      <div class="productView-images-wrapper">
        <div class="productView-thumbnail-wrapper">
          <div class="productView-thumbnail"><img src="https://cdn.shopify.com/s/files/1/0829/6490/6267/files/7991.jpg"></div>
          <div class="productView-thumbnail"><img src="https://cdn.shopify.com/s/files/1/0829/6490/6267/files/imgi_58.jpg"></div>
          <div class="productView-thumbnail"><img src="https://cdn.shopify.com/s/files/1/0829/6490/6267/files/imgi_61.jpg"></div>
        </div>
      </div>
    </body></html>
    """
    context = _context(SHOPIFY_PAGE, html, "Jackpot JP-7991 Single Washer", "jackpot")
    candidates = extract_candidates(context)
    gallery_cands = [c for c in candidates if c.source == "gallery_container"]
    assert len(gallery_cands) == 3
    assert all("files/imgi_" in c.url or "files/7991" in c.url for c in gallery_cands)


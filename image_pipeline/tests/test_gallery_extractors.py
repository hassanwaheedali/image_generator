"""Extraction sources 1-5, provenance and the strictly-scoped fallback."""

from __future__ import annotations

from bs4 import BeautifulSoup
from conftest import load_fixture

from gallery_extractors import ExtractionContext, extract_candidates, registry_extractor_for
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

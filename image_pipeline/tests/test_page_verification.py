"""S1/S2/S3, the two recognition rules, and the colour/variant flags."""

from __future__ import annotations

from bs4 import BeautifulSoup
from conftest import load_fixture

from model_matching import build_model_keys
from page_verification import (
    MatchSettings,
    is_in_description_region,
    is_linked_to_other_page,
    selected_colourway,
    strip_unwanted_regions,
    verify_page,
)

HAIER = "https://www.haier.com/pk/hsu-12hfpaa.shtml"


# --------------------------------------------------------------------------- #
# S1
# --------------------------------------------------------------------------- #
def test_strength0_page_yields_no_images() -> None:
    keys = build_model_keys("Haier HSU-12HFPAA")
    result = verify_page(
        "https://shop.example/category/air-conditioners",
        load_fixture("listing_page"),
        keys,
        brand="haier",
    )
    assert result.strength == 0
    assert result.accepted is False
    assert result.match_rule == "none"
    assert result.matched_keys == ()


def test_strong_page_matches_in_title_and_url() -> None:
    keys = build_model_keys("Haier HSU-12HFPAA")
    result = verify_page(HAIER, load_fixture("product_ok"), keys, brand="haier")
    assert result.strength == 2
    assert result.match_rule == "model_key"
    assert result.tolerated is False        # the fixture prints the model itself
    assert "HSU-12HFPAA" in result.matched_keys


def test_weak_page_extracts_gallery() -> None:
    """Customer decision: a body-text-only match is treated as a normal page."""
    keys = build_model_keys("Haier HSU-12HFPAA")
    result = verify_page(
        "https://shop.example/p/inverter-ac", load_fixture("weak_page"), keys, brand="haier"
    )
    assert result.strength == 1
    assert result.match_rule == "body_model_key"


def test_a_page_showing_another_colour_is_rejected() -> None:
    """Customer decision (2026-10-07): another colourway is another product.

    This supersedes the 2026-10-05 rule that flagged the difference and kept the
    page - publishing the wrong colour costs the company a penalty, so the page is
    strength 0 and the row keeps looking for the colour it was asked for.
    """
    html = """<html><head><title>Haier HRF-538 IFGA Refrigerator Black</title></head>
    <body><h1>Haier HRF-538 IFGA Black</h1></body></html>"""
    keys = build_model_keys("Haier Refrigerator HRF-538 IFGA Golden")
    result = verify_page("https://www.haier.com/pk/hrf-538", html, keys, brand="haier")
    assert result.strength == 0
    assert result.match_rule == "colour_conflict"
    assert "another colourway" in result.detail
    assert result.variant_notes == ()          # the rejection says it, no note needed


def test_the_same_colour_in_longer_words_still_matches() -> None:
    """"Pearl Golden" on the page is the row's own colour, not another variant."""
    html = """<html><head><title>Haier HRF-538 IFGA Refrigerator Pearl Golden</title></head>
    <body><h1>Haier HRF-538 IFGA</h1></body></html>"""
    keys = build_model_keys("Haier Refrigerator HRF-538 IFGA Golden")
    result = verify_page("https://www.haier.com/pk/hrf-538", html, keys, brand="haier")
    assert result.strength == 2
    assert result.match_rule == "model_key"


def test_variant_separation_can_be_switched_off() -> None:
    """One config field rolls the whole rule back (colour-blind matching returns)."""
    html = """<html><head><title>Haier HRF-538 IFGA Refrigerator Black</title></head>
    <body><h1>Haier HRF-538 IFGA Black</h1></body></html>"""
    keys = build_model_keys("Haier Refrigerator HRF-538 IFGA Golden")
    result = verify_page(
        "https://www.haier.com/pk/hrf-538",
        html,
        keys,
        brand="haier",
        settings=MatchSettings(variant_separation_enabled=False),
    )
    assert result.strength == 2
    assert result.match_rule == "model_key"


def test_colour_silence_ok() -> None:
    html = """<html><head><title>Haier HRF-538 IFGA Refrigerator</title></head>
    <body><h1>Haier HRF-538 IFGA</h1></body></html>"""
    keys = build_model_keys("Haier Refrigerator HRF-538 IFGA Golden")
    result = verify_page("https://www.haier.com/pk/hrf-538", html, keys, brand="haier")
    assert result.strength == 2


def test_variant_mismatch_flags_not_fails() -> None:
    html = """<html><head><title>Dawlance DW-7500C 12 kg Washing Machine</title></head>
    <body><h1>Dawlance DW-7500C 12 kg Twin Tub</h1></body></html>"""
    keys = build_model_keys("Dawlance Semi Automatic Washing Machine 10kg (DW-7500C)")
    result = verify_page("https://www.dawlance.com.pk/p/dw-7500c", html, keys, brand="dawlance")
    assert result.strength == 2
    assert result.variant_notes and "capacity_kg" in result.variant_notes[0]


# --------------------------------------------------------------------------- #
# brand + code (the one relaxed rule - see plan section 3.6)
# --------------------------------------------------------------------------- #
def test_brand_code_match_haier_538_ifga() -> None:
    html = """<html><head><title>Haier Refrigerator 538 IFGA INV</title></head>
    <body><h1>Haier Refrigerator 538 IFGA INV</h1></body></html>"""
    keys = build_model_keys("Haier HRF-538 IFGA")
    result = verify_page("https://www.haier.com/pk/538-ifga", html, keys, brand="haier")
    assert result.strength == 2
    assert result.match_rule == "brand_code"


def test_brand_code_without_brand_rejected() -> None:
    html = """<html><head><title>Refrigerator 538 IFGA INV</title></head>
    <body><h1>Refrigerator 538 IFGA INV</h1></body></html>"""
    keys = build_model_keys("Haier HRF-538 IFGA")
    result = verify_page("https://unknown-shop.example/p/538", html, keys, brand=None)
    assert result.strength == 0


def test_conflicting_prefix_rejected() -> None:
    html = """<html><head><title>Haier HSF-538 IFGA Refrigerator</title></head>
    <body><h1>Haier HSF-538 IFGA</h1></body></html>"""
    keys = build_model_keys("Haier HRF-538 IFGA")
    result = verify_page("https://www.haier.com/pk/hsf-538", html, keys, brand="haier")
    assert result.strength == 0


def test_brand_initial_prefix_allowed() -> None:
    html = """<html><head><title>Haier 55 Inch QLED TV H55S80EUX</title></head>
    <body><h1>Haier 55S80EUX</h1></body></html>"""
    keys = build_model_keys("Haier 55 Inch 4K Smart QLED TV (55S80EUX)")
    result = verify_page(
        "https://www.daraz.pk/products/haier-55-h55s80eux-i123.html", html, keys, brand="haier"
    )
    assert result.strength == 2


def test_short_tail_not_relaxed() -> None:
    html = """<html><head><title>Haier Inverter AC 12 series</title></head>
    <body><h1>Haier 12</h1></body></html>"""
    keys = build_model_keys("Haier HSU-12 1.5 Ton")
    result = verify_page("https://www.haier.com/pk/cool", html, keys, brand="haier")
    assert result.strength == 0


def test_other_manufacturer_domain_rejected() -> None:
    html = """<html><head><title>Dawlance 538 IFGA Refrigerator</title></head>
    <body><h1>Dawlance 538 IFGA</h1></body></html>"""
    keys = build_model_keys("Dawlance DW-538 IFGA")
    result = verify_page("https://www.samsung.com/pk/538-ifga", html, keys, brand="dawlance")
    assert result.strength == 0


def test_retail_domain_brand_code_allowed() -> None:
    html = """<html><head><title>Haier Refrigerator 538 IFGA INV</title></head>
    <body><h1>Haier 538 IFGA</h1></body></html>"""
    keys = build_model_keys("Haier HRF-538 IFGA")
    result = verify_page("https://www.daraz.pk/products/haier-538-ifga-i9.html", html, keys, brand="haier")
    assert result.strength == 2


def test_brand_code_off_switch() -> None:
    html = """<html><head><title>Haier Refrigerator 538 IFGA INV</title></head></html>"""
    keys = build_model_keys("Haier HRF-538 IFGA")
    settings = MatchSettings(brand_match_enabled=False)
    result = verify_page("https://www.haier.com/pk/538-ifga", html, keys, brand="haier", settings=settings)
    assert result.strength == 0


def test_dropped_trailing_letter_page_matches() -> None:
    """Live finding: the sheet says DW-7500C and the Dawlance page prints DW-7500."""
    html = """<html><head><title>DW-7500 TWIN TUB GLASS LID BLACK/GREY</title></head>
    <body><h1>DW-7500 Twin Tub Washing Machine</h1></body></html>"""
    keys = build_model_keys("Dawlance Semi Automatic Washing Machine 10kg (DW-7500C)")
    result = verify_page(
        "https://www.dawlance.com.pk/twin-tub-washing-machine/dw-7500-twin-tub",
        html,
        keys,
        brand="dawlance",
    )
    assert result.strength == 2
    assert result.match_rule == "model_key"
    # and the flag travels with it: the page does not *print* the sheet's model
    assert result.tolerated is True


def test_a_page_that_prints_the_model_is_not_tolerated() -> None:
    html = """<html><head><title>DW-7500C TWIN TUB GLASS LID BLACK/GREY</title></head>
    <body><h1>DW-7500C Twin Tub Washing Machine</h1></body></html>"""
    keys = build_model_keys("Dawlance Semi Automatic Washing Machine 10kg (DW-7500C)")
    result = verify_page(
        "https://www.surmawala.pk/products/dawlance-semi-automatic-washing-machine-dw-7500c-10kg",
        html,
        keys,
        brand="dawlance",
    )
    assert result.strength == 2
    assert result.tolerated is False


def test_different_haier_model_still_rejected() -> None:
    html = """<html><head><title>Haier HSU-18HFPAA Inverter AC</title></head>
    <body><h1>Haier HSU-18HFPAA</h1></body></html>"""
    keys = build_model_keys("Haier HSU-12HFPAA 1.5 Ton")
    result = verify_page("https://www.daraz.pk/products/haier-hsu-18-i7.html", html, keys, brand="haier")
    assert result.strength == 0
    assert result.match_rule == "none"


# --------------------------------------------------------------------------- #
# S2 / S3
# --------------------------------------------------------------------------- #
def test_description_image_nested_deep_rejected() -> None:
    soup = strip_unwanted_regions(BeautifulSoup(load_fixture("description_images"), "lxml"), HAIER)
    image = soup.select_one('img[src*="description-chart"]')
    assert image is not None
    assert is_in_description_region(image) is True


def test_gallery_inside_tab_not_treated_as_description() -> None:
    soup = strip_unwanted_regions(BeautifulSoup(load_fixture("description_images"), "lxml"), HAIER)
    image = soup.select_one('img[src*="inside-tab"]')
    assert image is not None
    assert is_in_description_region(image) is False


def test_article_content_rejected() -> None:
    soup = strip_unwanted_regions(BeautifulSoup(load_fixture("description_images"), "lxml"), HAIER)
    image = soup.select_one('img[src*="blog"]')
    assert image is not None
    assert is_in_description_region(image) is True


def test_related_carousel_excluded() -> None:
    soup = strip_unwanted_regions(BeautifulSoup(load_fixture("related_carousel"), "lxml"), HAIER)
    assert soup.select_one('img[src*="hsu-18hfpaa"]') is None      # container removed
    gallery_image = soup.select_one('img[src*="hsu-12hfpaa-front"]')
    assert gallery_image is not None
    assert is_in_description_region(gallery_image) is False


def test_lightbox_link_to_own_file_kept() -> None:
    soup = BeautifulSoup('<a href="/media/hsu-12hfpaa-1.jpg"><img src="/media/thumb.jpg"></a>', "lxml")
    anchor = soup.find("a")
    assert is_linked_to_other_page(anchor, HAIER) is False


def test_link_to_other_page_detected() -> None:
    soup = BeautifulSoup('<a href="/product/other-model"><img src="/media/x.jpg"></a>', "lxml")
    anchor = soup.find("a")
    assert is_linked_to_other_page(anchor, HAIER) is True


def test_strip_unwanted_regions_marks_parent() -> None:
    soup = strip_unwanted_regions(
        BeautifulSoup("<body><div><div class='related'><img src='/x.jpg'></div></div></body>", "lxml")
    )
    assert soup.select_one("img") is None
    assert soup.select_one("div[data-pipeline-stripped]") is not None

def test_a_page_naming_another_series_is_a_last_resort_not_proof() -> None:
    """Plan task 4: same code + colour but a different series line (Daimond vs Cuby)
    is a different product - demoted to a flagged last resort, never exact proof."""
    html = """<html><head><title>Dawlance (9191-WB) Cuby RED</title></head>
    <body><h1>Dawlance (9191-WB) Cuby RED</h1></body></html>"""
    keys = build_model_keys("Dawlance (9191-WB) Daimond RED")
    result = verify_page("https://www.dawlance.com.pk/9191", html, keys, brand="dawlance")
    assert result.strength > 0
    assert result.tolerated          # flagged last resort, not a silent exact match

    same = verify_page(
        "https://www.dawlance.com.pk/9191",
        """<html><head><title>Dawlance (9191-WB) Daimond RED</title></head>
        <body><h1>Dawlance (9191-WB) Daimond RED</h1></body></html>""",
        keys, brand="dawlance",
    )
    assert same.strength > 0 and not same.tolerated


def test_selected_colourway_reads_only_the_labelled_value() -> None:
    """Owner 2026-10-08: the swatch menu lists every option, so only the labelled value counts."""
    table = BeautifulSoup(
        "<table><tr><td>Type</td><td>Straight Glass Door</td></tr>"
        "<tr><td>Colour</td><td>Red Blaze</td></tr></table>", "lxml")
    assert selected_colourway(table) == "Red Blaze"
    inline = BeautifulSoup(
        "<div><span>Color:</span><span>Marine Impression</span>"
        "<label>Red Blaze</label><label>Maroon Blaze</label></div>", "lxml")
    assert selected_colourway(inline) == "Marine Impression"
    # a page with only the swatch menu gives no opinion at all
    swatches = BeautifulSoup("<label>Marine Impression</label><label>Red Blaze</label>", "lxml")
    assert selected_colourway(swatches) == ""
    # and a flattened whole menu is too long to be a single value
    toolong = BeautifulSoup("<tr><td>Colour</td><td>Red Blaze Marine Impression Maroon Blaze</td></tr>", "lxml")
    assert selected_colourway(toolong) == ""


def test_labelled_body_colour_separates_variant_pages() -> None:
    """The PEL case: title names no colour, the body label carries the selected one."""
    from model_matching import build_model_keys, match_product
    from models import ModelKeys

    pel = "PEL Straight 3 Taps Glass Door Water Dispenser (PWD-425-GD)"
    rows = {
        "Marine": build_model_keys(pel + "-Marine Impression"),
        "Red": build_model_keys(pel + "-Red Blaze"),
        "Maroon": build_model_keys(pel + "-Maroon Blaze"),
    }

    def clean(row: ModelKeys, selected: str) -> bool:
        m = match_product(keys=row, brand="pel", strong_text=pel, selected_colourway=selected)
        return m.matched and not m.tolerated

    # no label -> fail open, exactly as today
    assert clean(rows["Marine"], "")
    # the page's own label decides
    assert not clean(rows["Marine"], "Red Blaze")
    assert clean(rows["Marine"], "Marine Impression")
    assert clean(rows["Red"], "Red Blaze")
    assert not clean(rows["Red"], "Maroon Blaze")
    assert match_product(
        keys=rows["Red"], brand="pel", strong_text=pel, selected_colourway="Maroon Blaze"
    ).rule == "colour_conflict"


def test_a_code_less_row_is_verified_by_its_keywords() -> None:
    """Live 2026-10-09 rows like "Mega T3 Pro 1 Ton | Inverter Split AC | Air
    Conditioners - Dawlance" were rejected at 4.1.  They now verify by keywords."""
    keys = build_model_keys("Mega T3 Pro 1 Ton | Inverter Split AC | Air Conditioners - Dawlance")
    assert not keys.all_keys and keys.keywords
    page = (
        "<html><head><title>Dawlance Mega T3 Pro 1 Ton Inverter Split AC</title></head>"
        "<body>Dawlance Mega T3 Pro 1 Ton Inverter Split AC price</body></html>"
    )
    result = verify_page("https://dawlance.com.pk/mega-t3-pro-1-ton", page, keys, brand="dawlance")
    assert result.accepted and result.match_rule == "keyword"


MEGATRON_TITLE = (
    "Orient Megatron Titanium 12X Olive Black 1 Ton Inverter AC\u2013T3 Tropical Wind "
    "Puff Heat &amp; Cool 4D Air Throw"
)


def _orient_page(model: str) -> str:
    return (
        "<html><head>"
        f"<title>Orient {model} | Orient Electronics Pakistan</title>"
        f"<meta name='description' content='Orient {model} inverter split air conditioner'>"
        "</head><body>"
        f"<h1>{model}</h1>"
        "<p>Tropical Wind Puff, 4D Air Throw, DC Inverter, heat and cool.</p>"
        "</body></html>"
    )


def test_the_official_page_is_believed_when_it_only_differs_in_the_capacity_letter() -> None:
    """Orient writes ``Titanium 12S`` where the sheet writes ``Titanium 12X``, and S1 was
    discarding the brand's own page for that one letter while a retailer's page carrying the
    sheet's spelling was accepted at strength 2 (owner: "why here not go to brand official").
    A page on the brand's own domain that names the line is now enough to look at its pictures."""
    keys = build_model_keys(MEGATRON_TITLE)
    result = verify_page(
        "https://www.orient.com.pk/products/titanium-12s-olive-black-t3-plus.html",
        _orient_page("Titanium 12S Olive Black T3+"),
        keys,
        brand="orient",
    )
    assert result.strength == 1
    assert result.match_rule == "fuzzy_identity"
    assert "capacity letter" in result.detail


def test_the_tolerance_is_for_the_brand_own_domain_only() -> None:
    """Same page text on a shop's domain stays refused: the shop that writes ``12S`` too
    might be selling the 12U, and a retailer has no such obligation to be exact."""
    keys = build_model_keys(MEGATRON_TITLE)
    result = verify_page(
        "https://www.aysonline.pk/products/titanium-12s-olive-black-t3-plus/",
        _orient_page("Titanium 12S Olive Black T3+"),
        keys,
        brand="orient",
    )
    assert result.strength == 0
    assert result.match_rule == "none"


def test_the_tolerance_does_not_accept_the_sibling_capacity() -> None:
    """The other end of the same guard: the brand's 18U page is still not a match for a 12X
    row, so ``fuzzy`` means one wrong letter, not "any page in the shop"."""
    keys = build_model_keys(MEGATRON_TITLE)
    result = verify_page(
        "https://www.orient.com.pk/products/titanium-18u-olive-black-t3-plus.html",
        _orient_page("Titanium 18U Olive Black T3+"),
        keys,
        brand="orient",
    )
    assert result.strength == 0


# --------------------------------------------------------------------------- #
# S1 must never accept another brand's product, and never trust the query string
# --------------------------------------------------------------------------- #
def test_a_page_showing_another_brand_is_rejected_even_when_the_code_matches() -> None:
    """Live 2026-10-11, owner's run: the ``Orient Split AC Hyper 18X Super White`` row
    deferred Panasonic and Gree pages as tolerated spellings because their codes contain
    ``18X``.  The page's own product brand outranks any code overlap: an Orient row is
    never satisfied by a Panasonic, whatever the page prints around the number."""
    keys = build_model_keys("Orient Split AC Hyper 18X Super White")
    for page_url, page_title in (
        (
            (
                "https://jalalelectronics.com/products/panasonic-split-ac-inverter-1-5ton-"
                "ue-18xkf9-heat-cool-t3-compressor"
            ),
            "Panasonic Split AC Inverter 1.5Ton UE-18XKF9 Heat Cool T3 Compressor",
        ),
        (
            (
                "https://electrociti.pk/products/gree-gs-18pith-16-turbo-18-pro-t3-white-"
                "1-5-ton-air-conditioner"
            ),
            "Gree GS-18PITH-16 Turbo 18 Pro T3 White 1.5 Ton Air Conditioner",
        ),
    ):
        html = f"<html><head><title>{page_title}</title></head><body><h1>{page_title}</h1></body></html>"
        result = verify_page(page_url, html, keys, brand="orient")
        assert result.strength == 0, page_title
        assert result.accepted is False
        assert result.tolerated is False, "another brand is a reject, never a last resort"
        assert result.match_rule == "other_brand_product"


def test_the_same_code_on_the_row_s_own_brand_still_matches() -> None:
    """The brand check must not bite the hand that feeds it: an Orient page on a
    retailer carrying the same ``18X`` stays proof."""
    keys = build_model_keys("Orient Split AC Hyper 18X Super White")
    title = "Orient Split AC Inverter 1.5Ton 18X Mega T3 Ampere Lock Heat Cool"
    html = f"<html><head><title>{title}</title></head><body><h1>{title}</h1></body></html>"
    result = verify_page("https://jalalelectronics.com/products/orient-18x-mega", html, keys, brand="orient")
    assert result.strength == 2
    assert result.accepted is True


def test_a_model_key_only_in_the_query_string_is_no_evidence() -> None:
    """Shopify search-result URLs echo the searched term (``?_psq=18X``).  The echo is
    what made Panasonic and Gree pages 'match' the Orient row - the URL evidence is the
    host and path only, exactly as the relaxed matcher already does."""
    keys = build_model_keys("Orient Split AC Hyper 18X Super White")
    title = "Air Conditioners - Jalal Electronics"
    page_url = "https://jalalelectronics.com/search?page=1&_psq=18X&_ss=e"
    html = f"<html><head><title>{title}</title></head><body><h1>{title}</h1></body></html>"
    result = verify_page(page_url, html, keys, brand="orient")
    assert result.strength == 0
    assert result.accepted is False


# --------------------------------------------------------------------------- #

# The official-host fuzzy route must never speak for another product line
# --------------------------------------------------------------------------- #
PULSE_ROW = "Pulse 19C Ultra White T3 | Orient Electronics Pakistan"


def _orient_line_page(model: str) -> str:
    return (
        "<html><head>"
        f"<title>Orient {model} | Orient Electronics Pakistan</title>"
        "</head><body>"
        f"<h1>{model}</h1>"
        "<p>DC Inverter split air conditioner, heat and cool.</p>"
        "</body></html>"
    )


def test_the_official_fuzzy_route_never_promotes_a_sibling_line_page() -> None:
    """Owner run 2026-10-11: for the ``Pulse 19C Ultra`` row the brand's own sitemap
    listed three *other* products of the same Ultra line, and the official-host fuzzy
    promotion accepted every one of them.  ``19C Ultra`` is a capacity code plus a tail
    the whole line shares, so ``19[a-z]?ultra`` matches Innovia's ``19S Ultra`` and the
    glued ``19cultra`` sits inside Blaze's and Glide's names.  A fuzzy promotion is a
    lookup of the brand's own page for *this* product: the page must name the row's own
    line (``pulse``), and a page match_product already refused (another colourway) can
    never be rescued by it."""
    keys = build_model_keys(PULSE_ROW)
    cases = (
        (
            "https://www.orient.com.pk/products/innova-19s-ultra-white-t3.html",
            "Innova 19S Ultra White T3",
        ),
        (
            "https://www.orient.com.pk/products/blaze-19c-ultra-golden-t3.html",
            "Blaze 19C Ultra Golden T3",
        ),
        (
            "https://www.orient.com.pk/products/glide-19c-ultra-silver-t3.html",
            "Glide 19C Ultra Silver T3",
        ),
    )
    for page_url, page_title in cases:
        result = verify_page(page_url, _orient_line_page(page_title), keys, brand="orient")
        assert result.strength == 0, page_url
        assert result.accepted is False, page_url

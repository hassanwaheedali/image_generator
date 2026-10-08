"""S5 keyword filtering, S11 fetchability, canonicalisation and dedup keys."""

from __future__ import annotations

import pytest

from url_filters import (
    canonicalize_url,
    dedup_key,
    detect_brand,
    display_brand,
    has_bad_keyword,
    has_image_extension,
    host_is_official,
    host_is_other_manufacturer,
    is_fetchable_product_url,
    prioritize_source_urls,
    upscale_candidates,
)


# --------------------------------------------------------------------------- #
# S5 - token based keyword blocklist
# --------------------------------------------------------------------------- #
def test_cartridge_not_rejected_cart_rejected() -> None:
    assert not has_bad_keyword("https://shop.example/media/cartridge-filter.png")
    assert has_bad_keyword("https://shop.example/media/cart.png")


def test_silicon_not_rejected_icon_rejected() -> None:
    assert not has_bad_keyword("https://shop.example/media/silicon-pad.png")
    assert has_bad_keyword("https://shop.example/media/icon-menu.png")


def test_banner_with_digits_rejected() -> None:
    assert has_bad_keyword("https://shop.example/media/banner2.jpg")
    assert has_bad_keyword("https://shop.example/media/logo-header.jpg")
    assert has_bad_keyword("https://shop.example/media/qr-code.png")


def test_multi_word_keywords() -> None:
    assert has_bad_keyword("https://shop.example/media/special-price-badge.png")
    assert has_bad_keyword("https://shop.example/media/energy-guide.png")
    assert not has_bad_keyword("https://shop.example/media/special-cover.png")


@pytest.mark.parametrize(
    "url",
    [
        "https://shop.example/media/watermark-sample.jpg",
        "https://shop.example/media/victoria-cartridge.jpg",
    ],
)
def test_keyword_needs_full_token(url: str) -> None:
    assert not has_bad_keyword(url)


# --------------------------------------------------------------------------- #
# S11 - fetchability
# --------------------------------------------------------------------------- #
def test_listing_and_blog_urls_not_fetched() -> None:
    assert not is_fetchable_product_url("https://shop.example/category/home-appliances")
    assert not is_fetchable_product_url("https://shop.example/blog/best-acs-2026")
    assert not is_fetchable_product_url("https://shop.example/search?q=hsu-12")
    assert not is_fetchable_product_url("https://facebook.com/shop")
    assert not is_fetchable_product_url("https://shop.example/about")
    assert not is_fetchable_product_url("https://shop.example/collections/inverter-acs")
    assert is_fetchable_product_url("https://shop.example/collections/ac/products/hsu-12")
    assert is_fetchable_product_url("https://www.haier.com/pk/hsu-12hfpaa.shtml")


def test_non_http_and_blank_urls() -> None:
    assert not is_fetchable_product_url("")
    assert not is_fetchable_product_url("data:image/png;base64,AAA")
    assert not is_fetchable_product_url("javascript:void(0)")


# --------------------------------------------------------------------------- #
# canonicalisation
# --------------------------------------------------------------------------- #
def test_canonicalize_strips_resize_suffixes() -> None:
    assert canonicalize_url("https://cdn.shop/p/a/front_800x800.png", "") == (
        "https://cdn.shop/p/a/front.png"
    )
    assert canonicalize_url("https://cdn.shop/p/a/front-500x500.jpg", "") == (
        "https://cdn.shop/p/a/front.jpg"
    )
    assert canonicalize_url("https://cdn.shop/p/a/front_medium.png", "") == (
        "https://cdn.shop/p/a/front.png"
    )


def test_query_kept_when_path_is_not_an_image() -> None:
    first = canonicalize_url("https://shop.example/image.php?id=123", "https://shop.example/p")
    second = canonicalize_url("https://shop.example/image.php?id=456", "https://shop.example/p")
    assert "id=123" in first and "id=456" in second
    assert dedup_key(first) != dedup_key(second)


def test_query_dropped_for_real_image_files() -> None:
    canonical = canonicalize_url("https://cdn.shop/p/product_800x800.jpg?v=3", "")
    assert canonical == "https://cdn.shop/p/product.jpg"


def test_protocol_relative_and_relative_resolution() -> None:
    assert canonicalize_url("//cdn.shop/p/a.jpg", "https://shop.example/p") == (
        "https://cdn.shop/p/a.jpg"
    )
    assert canonicalize_url("/media/a.jpg", "https://shop.example/product/x") == (
        "https://shop.example/media/a.jpg"
    )
    assert canonicalize_url("data:image/png;base64,AAA", "https://shop.example") == ""


def test_dedup_key_includes_parent_folder() -> None:
    """The folder is part of the identity - two ``front.png`` files are two photos."""
    left = dedup_key("https://cdn.shop/a/front.png")
    right = dedup_key("https://cdn.shop/b/front.png")
    assert left == "a/front"
    assert right == "b/front"
    assert left != right


def test_the_same_photo_in_another_format_is_one_asset() -> None:
    """Live bug (Dawlance DW-7500C): the same gallery photo was served as image.png and
    image.webp, both were downloaded, and the wasted download starved the next real photo
    out of the per-page allowance.  The format suffix is not an identity."""
    base = "https://www.dawlance.com.pk/media/resize/F01000000030_LO1_20220415_113939.png"
    keys = {
        dedup_key(f"{base}/1000Wx1000H/image.webp"),
        dedup_key(f"{base}/1000Wx1000H/image.png"),
        dedup_key(f"{base}/2000Wx2000H/image.webp"),
        dedup_key(f"{base}/1000Wx1000H/image.jpg"),
    }
    assert len(keys) == 1, keys
    # ...but a different photo in the same folder keeps its own key
    other = dedup_key(f"{base.replace('LO1', 'LO2')}/1000Wx1000H/image.webp")
    assert other not in keys
    # ...and a plain CDN filename collapses the same way, still keeping the folder
    assert dedup_key("https://cdn.shop/p/hsu-12hfpaa-1.webp") == "p/hsu-12hfpaa-1"
    assert dedup_key("https://cdn.shop/p/hsu-12hfpaa-1.jpg") == "p/hsu-12hfpaa-1"


def test_cdn_transform_segments_are_kept_for_fetching() -> None:
    """The URL must stay downloadable (Dawlance serves only the transformed path)."""
    url = (
        "https://www.dawlance.com.pk/media/resize/F01000000030_LO1_20220415_113939.png"
        "/1000Wx1000H/image.webp"
    )
    assert canonicalize_url(url, "") == url


def test_zoom_link_and_served_render_are_one_asset() -> None:
    """Live case (Dawlance SFCC gallery): the lightbox links the asset itself
    (``…/FILE.png``) while the visible picture is ``…/FILE.png/1000Wx1000H/image.webp``.
    Both are the same photograph, so they must not take two download slots - the per-page
    allowance is what decides how many of the product's own photos are ever examined."""
    asset = "https://www.dawlance.com.pk/media/resize/F11031006930_LO1_20231120_115524.png"
    assert dedup_key(f"{asset}/1000Wx1000H/image.webp") == dedup_key(asset)
    assert dedup_key(f"{asset}/2000Wx2000H/image.jpg") == dedup_key(asset)
    # a different asset in the same folder stays a different picture
    assert dedup_key(asset.replace("LO1", "LO2")) != dedup_key(asset)


def test_dedup_key_sees_through_cdn_transforms() -> None:
    """Two photos whose file name is identical must still get different keys."""
    first = (
        "https://www.dawlance.com.pk/media/resize/F01000000030_LO1_20220415_113939.png"
        "/1000Wx1000H/image.webp"
    )
    second = (
        "https://www.dawlance.com.pk/media/resize/F01000000030_LO2_20220415_113951.png"
        "/1000Wx1000H/image.webp"
    )
    smaller = (
        "https://www.dawlance.com.pk/media/resize/F01000000030_LO1_20220415_113939.png"
        "/500Wx500H/image.webp"
    )
    assert dedup_key(first) != dedup_key(second)
    assert dedup_key(smaller) == dedup_key(first)          # same asset, other size


def test_generic_filenames_still_get_distinct_keys() -> None:
    left = dedup_key("https://cdn.shop/files/aaa-111/image.webp")
    right = dedup_key("https://cdn.shop/files/bbb-222/image.webp")
    assert left != right


# --------------------------------------------------------------------------- #
# brand detection
# --------------------------------------------------------------------------- #
def test_brand_from_domain_then_title() -> None:
    assert detect_brand("https://www.haier.com/pk/x.shtml") == "haier"
    assert detect_brand("https://www.daraz.pk/products/x.html", "Haier HSU-12HFPAA") == "haier"
    assert detect_brand("https://www.daraz.pk/products/x.html", "Some Brand AC") is None


def test_other_manufacturer_domain_guard() -> None:
    assert host_is_other_manufacturer("www.samsung.com", "dawlance")
    assert not host_is_other_manufacturer("www.dawlance.com.pk", "dawlance")
    assert not host_is_other_manufacturer("www.daraz.pk", "dawlance")
    assert not host_is_other_manufacturer("eshop.pel.com.pk", "pel")


def test_shopify_single_dimension_thumb_is_upscaled() -> None:
    variants = upscale_candidates("https://cdn.shop.example/p/product_288x.png")
    assert variants[0] == "https://cdn.shop.example/p/product_1024x1024.png"
    assert variants[-1] == "https://cdn.shop.example/p/product_288x.png"


def test_has_image_extension_separates_assets_from_page_routes() -> None:
    assert has_image_extension("https://cdn.telex.pk/p/hsu-12hfpaa-a.jpg")
    assert has_image_extension("https://www.dawlance.com.pk/media/resize/F.png/1000Wx1000H/image.webp")
    assert not has_image_extension("https://www.telex.pk/products/2026-06-28T22:25:28+05:00")
    assert not has_image_extension("https://www.telex.pk/products/image")


def test_prioritize_source_urls_prefers_official_domain() -> None:
    urls = [
        "https://www.daraz.pk/products/x.html",
        "https://www.haier.com/pk/hsu-12hfpaa.shtml",
        "https://priceoye.pk/x",
    ]
    assert prioritize_source_urls(urls, "haier")[0] == urls[1]
    assert prioritize_source_urls(urls, None) == urls


def test_every_catalogue_brand_has_its_official_domain() -> None:
    """Owner 2026-10-08: the three catalogue/input brands that were missing now rank first.

    ``Superasia``, ``Hisense`` and ``Midea`` appear in the shipped catalogue and in the
    owner's input sheet but were absent from ``KNOWN_BRANDS``, so their official pages
    got no priority and no curated-gallery acceptance.
    """
    sheet_order = [
        "https://mideapakistan.store/products/midea-go-easy",
        "https://alizainabelectronics.com/products/midea-go-easy-18hrfnqc6-1-5-ton-dc-inverter-ac",
        "https://www.mabdullah.pk/products/midea-18hrfn1-qc6-1-5-ton-go-easy-t3-inverter-ac",
    ]
    assert prioritize_source_urls(list(reversed(sheet_order)), "midea")[0] == sheet_order[0]
    assert host_is_official(sheet_order[0], "midea")
    assert not host_is_official(sheet_order[1], "midea")
    assert host_is_official("https://superasiastore.com/products/sa-241", "superasia")
    assert host_is_official("https://hisensepakistan.com/product/a6k-55/", "hisense")
    # and the brand is found from either the URL or the title
    assert detect_brand(sheet_order[0], "") == "midea"
    assert detect_brand("", "Midea Go Easy 1.5 Ton DC Inverter AC") == "midea"
    # two-word brand: the catalogue writes "Super Asia", the table key is one word
    assert detect_brand("", "Super Asia Smart Wash Twin Tub 7.5KG Washing Machine") == "superasia"
    assert display_brand("superasia") == "Super Asia"
    assert display_brand("midea") == "Midea"

"""S6/S7/S8/S9/S13 gates and the isolation guards."""

from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from conftest import (
    busy_centre_image,
    clean_product_image,
    jpeg_bytes,
    png_bytes,
    qr_image,
    tiny_image,
    white_on_white_image,
    wide_strip_image,
)
from PIL import Image, ImageDraw

from compositing import (
    apply_template,
    header_safe_margin,
    isolate_main_product,
    template_content_box,
    template_has_transparency,
    to_square_canvas,
)
from config import PipelineConfig
from image_quality import (
    ImageValidationError,
    body_layout,
    border_is_clean,
    diagram_sheet_reason,
    has_extreme_aspect,
    is_large_enough,
    is_marketing_layout,
    is_monochrome_emblem,
    is_qr_or_barcode,
    layout_metrics,
    marketing_layout_reason,
    marketing_panel_split,
    neutral_corner_count,
    run_quality_gates,
    strip_promo_band,
    trim_edge_seams,
    validate_image_bytes,
)


# --------------------------------------------------------------------------- #
# S8 / S6
# --------------------------------------------------------------------------- #
def test_clean_white_product_accepted(config: PipelineConfig) -> None:
    image = clean_product_image()
    assert is_large_enough(image, min_short_side=180, min_long_side=350)
    assert not has_extreme_aspect(image, max_aspect_ratio=3.5)
    assert border_is_clean(
        image,
        ring_ratio=config.border_ring_ratio,
        ring_min_px=config.border_ring_min_px,
        tolerance=config.border_bg_tolerance,
        std_max=config.border_std_max,
        min_fraction=config.border_clean_min_fraction,
    )
    assert run_quality_gates(image, config)[0] == "ok"


def test_tiny_thumbnail_rejected() -> None:
    assert not is_large_enough(tiny_image(100), min_short_side=180, min_long_side=350)


def test_wide_strip_rejected() -> None:
    assert has_extreme_aspect(wide_strip_image(), max_aspect_ratio=3.5)


def test_white_corners_busy_middle_rejected(config: PipelineConfig) -> None:
    image = busy_centre_image()
    assert not border_is_clean(
        image,
        ring_ratio=config.border_ring_ratio,
        ring_min_px=config.border_ring_min_px,
        tolerance=config.border_bg_tolerance,
        std_max=config.border_std_max,
        min_fraction=config.border_clean_min_fraction,
    )
    rule, detail = run_quality_gates(busy_centre_image(width=600, height=600), config)
    assert rule == "S7", detail


def test_neutral_corner_count() -> None:
    assert neutral_corner_count(clean_product_image(), 0.12, 0.86, 0.14) >= 2
    assert neutral_corner_count(busy_centre_image(width=600, height=600), 0.12, 0.86, 0.14) < 4


# --------------------------------------------------------------------------- #
# S13
# --------------------------------------------------------------------------- #
def test_non_image_and_oversize_rejected() -> None:
    with pytest.raises(ImageValidationError) as text_error:
        validate_image_bytes(b"not an image at all", "text/html", max_bytes=1000, max_pixels=10**6)
    assert text_error.value.rule_id == "S13"

    with pytest.raises(ImageValidationError):
        validate_image_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "image/svg+xml",
                             max_bytes=1000, max_pixels=10**6)

    with pytest.raises(ImageValidationError):
        validate_image_bytes(png_bytes(clean_product_image(50, 50)), "image/png",
                             max_bytes=10, max_pixels=10**6)

    with pytest.raises(ImageValidationError):
        validate_image_bytes(png_bytes(clean_product_image(50, 50)), "", max_bytes=10**7,
                             max_pixels=100)

    image = validate_image_bytes(jpeg_bytes(clean_product_image()), "image/jpeg; charset=binary",
                                 max_bytes=10**7, max_pixels=10**7)
    assert image.size == (900, 900)


def test_lossy_payload_below_minimum_rejected() -> None:
    # 470x556 image saved at very low quality so payload < 12,000 bytes
    img = clean_product_image(470, 556)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=20)
    low_payload = buf.getvalue()

    with pytest.raises(ImageValidationError) as err:
        validate_image_bytes(
            low_payload,
            "image/jpeg",
            max_bytes=10**7,
            max_pixels=10**7,
            min_lossy_bytes=12_000,
            min_lossy_bytes_per_pixel=0.025,
        )
    assert err.value.rule_id == "S13"
    assert "below minimum 12000" in err.value.detail or "over-compressed" in err.value.detail


def test_lossy_overcompressed_bpp_rejected() -> None:
    # A larger canvas 800x800 with low byte density (e.g. flat white background + low quality JPEG)
    img = clean_product_image(800, 800)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=30)
    low_bpp = buf.getvalue()

    # Pass min_lossy_bytes that passes (1000), but high min_lossy_bytes_per_pixel that fails
    with pytest.raises(ImageValidationError) as err:
        validate_image_bytes(
            low_bpp,
            "image/jpeg",
            max_bytes=10**7,
            max_pixels=10**7,
            min_lossy_bytes=1_000,
            min_lossy_bytes_per_pixel=0.5,
        )
    assert err.value.rule_id == "S13"
    assert "over-compressed" in err.value.detail


def test_lossless_png_not_subject_to_lossy_thresholds() -> None:
    # Lossless PNG should never be rejected by lossy thresholds
    png_data = png_bytes(clean_product_image(200, 200))
    image = validate_image_bytes(
        png_data,
        "image/png",
        max_bytes=10**7,
        max_pixels=10**7,
        min_lossy_bytes=50_000,
        min_lossy_bytes_per_pixel=1.0,
    )
    assert image.size == (200, 200)


def test_exif_rotation_applied() -> None:
    base = clean_product_image(400, 300)
    from PIL import Image as PILImage

    exif = base.getexif()
    exif[274] = 6                                        # orientation: rotate 90
    buffer_bytes = __import__("io").BytesIO()
    base.save(buffer_bytes, format="JPEG", exif=exif)
    image = validate_image_bytes(buffer_bytes.getvalue(), "image/jpeg", max_bytes=10**7, max_pixels=10**7)
    assert image.size == (300, 400)
    assert isinstance(image, PILImage.Image)


# --------------------------------------------------------------------------- #
# S9
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    __import__("importlib").util.find_spec("cv2") is None,
    reason="OpenCV is not installed",
)
def test_qr_rejected() -> None:
    try:
        image = qr_image()
    except Exception as exc:                             # pragma: no cover - encoder missing
        pytest.skip(f"OpenCV cannot encode a QR code: {exc}")
    assert is_qr_or_barcode(image, "https://cdn.shop/p/hsu-12hfpaa-qr.png") is True


def test_qr_filename_token_rejected() -> None:
    assert is_qr_or_barcode(clean_product_image(), "https://cdn.shop/p/qr-code.png") is True
    assert is_qr_or_barcode(clean_product_image(), "https://cdn.shop/p/hsu-12hfpaa-1.png") is False


# --------------------------------------------------------------------------- #
# isolation guards + templates
# --------------------------------------------------------------------------- #
def test_isolation_keeps_product_and_never_returns_empty(config: PipelineConfig) -> None:
    isolated, degraded = isolate_main_product(clean_product_image(), config)
    assert isolated.width > 100 and isolated.height > 100
    assert isolated.width < 900                            # the background was trimmed
    assert degraded is False


def test_white_on_white_isolation_does_not_crash(config: PipelineConfig) -> None:
    isolated, degraded = isolate_main_product(white_on_white_image(), config)
    assert isolated.width > 0 and isolated.height > 0
    assert degraded is True


def _split_shell_image(width: int = 1200, height: int = 700) -> Image.Image:
    """A white product whose lower body is fainter than ``isolation_bg_distance``.

    This is the owner-run shape of 2026-10-10 (Candy/Orient/Panasonic): the top casing
    is dark enough for the foreground mask, the soft shading below it is not, so the
    mask - and the crop that follows it - stops at the product's waist.
    """
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    left, right = int(width * 0.1), int(width * 0.9)
    top = int(height * 0.25)
    draw.rectangle((left, top, right, top + 90), fill=(190, 190, 190))      # mask sees this
    draw.rectangle((left, top + 90, right, int(height * 0.75)), fill=(243, 243, 243))
    return image                                                            # mask misses this


def test_crop_closure_recovers_a_faint_lower_body(config: PipelineConfig) -> None:
    """S18: a crop may not cut the product, even where the foreground mask goes blind."""
    image = _split_shell_image()
    without, _ = isolate_main_product(image, replace(config, isolation_crop_closure=False))
    with_closure, degraded = isolate_main_product(image, config)
    assert with_closure.height > without.height * 1.5      # the faint half came back
    assert without.height < 200                            # the shape of the old failure
    ink = np.asarray(with_closure.convert("RGB"), dtype=int)
    assert (ink == 243).any()                              # the recovered rows are inside
    assert degraded is False


def test_crop_closure_ignores_a_detached_mark(config: PipelineConfig) -> None:
    """S18 only follows ink connected to the crop border; a lone corner logo stays out."""
    marked = _split_shell_image()
    ImageDraw.Draw(marked).rectangle((20, 20, 60, 40), fill=(10, 10, 10))
    plain, _ = isolate_main_product(_split_shell_image(), config)
    with_mark, _ = isolate_main_product(marked, config)
    assert with_mark.size == plain.size


def test_crop_closure_stays_out_of_non_white_backgrounds(config: PipelineConfig) -> None:
    """S18 measures ink against the studio background, and only runs on a near-white one.

    Measured on a dark frame before the restriction: the whole background is ink, it is
    one connected body touching the crop border, and the crop grew from 413x213 to the
    full 900x600 - which would have published a coloured background into a white
    template.  The closure exists for white-on-white blindness only.
    """
    for background in ((8, 8, 8), (70, 90, 140)):
        image = Image.new("RGB", (900, 600), background)
        ImageDraw.Draw(image).rounded_rectangle((250, 200, 650, 400), radius=18, fill=(250, 250, 250))
        without, _ = isolate_main_product(image, replace(config, isolation_crop_closure=False))
        with_closure, _ = isolate_main_product(image, config)
        assert without.size == with_closure.size


def test_crop_closure_leaves_a_complete_crop_untouched(config: PipelineConfig) -> None:
    """Nothing a clean crop already contains is rearranged by the closure."""
    image = clean_product_image(900, 400)
    without, _ = isolate_main_product(image, replace(config, isolation_crop_closure=False))
    with_closure, _ = isolate_main_product(image, config)
    assert without.size == with_closure.size


def test_square_canvas_and_sharpen(config: PipelineConfig) -> None:
    square = to_square_canvas(clean_product_image(900, 400), config.canonical_side,
                              fill_ratio=config.product_fill_ratio)
    assert square.size == (config.canonical_side, config.canonical_side)


def _branded_template(size: tuple[int, int] = (800, 800)) -> Image.Image:
    """A template shaped like real brand art: a *semi-transparent* logo band with soft
    edges, a footer band, and a decoration inside the content area.

    The band is deliberately semi-transparent: that is the case where a product pasted
    underneath it would bleed through and visibly damage the logo.
    """
    template = Image.new("RGBA", size, (255, 255, 255, 255))
    draw = ImageDraw.Draw(template)
    draw.rectangle((0, 0, size[0], 90), fill=(12, 78, 160, 200))     # logo band (alpha!)
    draw.rectangle((0, size[1] - 60, size[0], size[1]), fill=(12, 78, 160, 200))
    draw.rectangle((size[0] - 150, size[1] // 2, size[0] - 90, size[1] // 2 + 60),
                   fill=(200, 30, 60, 255))                           # content-area decoration
    return template


def test_header_safe_margin_measures_the_template_band(config: PipelineConfig) -> None:
    template = Image.new("RGBA", (800, 800), (255, 255, 255, 255))
    draw = ImageDraw.Draw(template)
    draw.rectangle((0, 0, 800, 90), fill=(10, 70, 150, 255))
    margin = header_safe_margin(template, config)
    assert 90 <= margin <= 120


# --------------------------------------------------------------------------- #
# Task 11b: the template's logo band must survive compositing untouched
# --------------------------------------------------------------------------- #
def test_logo_band_is_byte_identical_after_compositing(config: PipelineConfig) -> None:
    """The customer rule: the product must never touch the template's logo band."""
    template = _branded_template()
    margin = header_safe_margin(template, config)
    assert margin >= 90                                    # the band really is detected

    square = to_square_canvas(
        # deliberately wide and tall products, to push the paste box around
        isolate_main_product(clean_product_image(900, 400), config)[0],
        config.canonical_side,
        fill_ratio=config.product_fill_ratio,
    )
    result = apply_template(square, template, config)

    before = np.asarray(template.convert("RGBA"), dtype=np.uint8)[:margin]
    after = np.asarray(result.convert("RGBA"), dtype=np.uint8)[:margin]
    assert np.array_equal(before, after)                   # not one pixel changed

    # ...and the product was really pasted (a vacuous test would prove nothing)
    below = np.asarray(result.convert("RGB"), dtype=np.uint8)[margin:]
    assert (below < 240).any()


def test_template_decoration_inside_the_content_area_survives(config: PipelineConfig) -> None:
    """The product canvas must never erase the template's own artwork underneath it."""
    template = _branded_template()
    decoration = (650, 400, 710, 460)                      # the red square in _branded_template
    result = apply_template(
        to_square_canvas(
            isolate_main_product(clean_product_image(900, 900), config)[0],
            config.canonical_side,
            fill_ratio=config.product_fill_ratio,
        ),
        template,
        config,
    )
    before = np.asarray(template.convert("RGBA"), dtype=np.uint8)[
        decoration[1] : decoration[3], decoration[0] : decoration[2]
    ]
    after = np.asarray(result.convert("RGBA"), dtype=np.uint8)[
        decoration[1] : decoration[3], decoration[0] : decoration[2]
    ]
    assert np.array_equal(before, after)


def test_product_is_never_pasted_above_the_safe_margin(config: PipelineConfig) -> None:
    template = _branded_template()
    left, top, right, bottom = template_content_box(template, config)
    assert top >= header_safe_margin(template, config)     # content box starts below the band
    assert left > 0 and bottom < template.size[1]          # side and bottom margins too


def _shipped_assets_config(tmp_path: Path) -> PipelineConfig:
    """A config pointing at the **real** `assets/` folder of the project."""
    return PipelineConfig(
        assets_dir=Path(__file__).resolve().parent.parent / "assets",
        output_dir=tmp_path / "out",
        state_dir=tmp_path / "state",
        dry_run=True,
    )


def _rendered(image: Image.Image) -> np.ndarray:
    """What a human sees: the RGBA image flattened onto white (how the final JPEG looks)."""
    flat = Image.alpha_composite(Image.new("RGBA", image.size, (255, 255, 255, 255)), image.convert("RGBA"))
    return np.asarray(flat.convert("RGB"), dtype=np.uint8)


def test_real_asset_templates_protect_their_band(tmp_path: Path) -> None:
    """Same guarantee against the actual DigiMall/AlfaMall files shipped in assets/.

    Byte-identical, not merely "looks the same": the white knockout is restricted to the
    product area, so nothing - not even a near-white anti-aliased pixel - may change up
    there.  The product itself must still be visible in the content area.
    """
    config = _shipped_assets_config(tmp_path)
    for key in ("digimall_template", "alfamall_template"):
        template = Image.open(config.asset_paths[key]).convert("RGBA")
        margin = header_safe_margin(template, config)
        assert margin > 0, f"{key}: no logo band found"
        square = to_square_canvas(
            isolate_main_product(clean_product_image(900, 900), config)[0],
            config.canonical_side,
            fill_ratio=config.product_fill_ratio,
        )
        result = apply_template(square, template, config)
        before = np.asarray(template, dtype=np.uint8)[:margin]
        after = np.asarray(result.convert("RGBA"), dtype=np.uint8)[:margin]
        assert np.array_equal(before, after), f"{key}: logo band was modified"

        content = _rendered(result)[margin + 20 : margin + 400, 500:900]
        assert content.mean() < 230, f"{key}: the product is not visible below the band"


# --------------------------------------------------------------------------- #
# Live bug (2026-10-06): the shipped templates carry an all-opaque alpha channel
# --------------------------------------------------------------------------- #
def _opaque_alpha_template(size: tuple[int, int] = (400, 400)) -> Image.Image:
    """A PNG that carries an alpha channel which is 255 everywhere (a real export style)."""
    template = Image.new("RGBA", size, (255, 255, 255, 255))
    ImageDraw.Draw(template).rectangle((0, 0, size[0], 40), fill=(12, 78, 160, 255))
    return template


def test_opaque_templates_are_never_treated_as_transparent(tmp_path: Path) -> None:
    """The white knockout must run for every opaque template - with or without an alpha
    channel.  Getting this wrong painted the template over the product."""
    config = _shipped_assets_config(tmp_path)
    shipped = {
        key: Image.open(config.asset_paths[key]) for key in ("digimall_template", "alfamall_template")
    }
    for key, template in shipped.items():
        assert not template_has_transparency(       # the shipped art must knockout its white
            template, min_alpha=config.template_transparency_min_alpha
        ), f"{key}: an opaque template was mistaken for transparency"

    carries_alpha = _opaque_alpha_template()        # the other shape this bug appears in
    assert "A" in carries_alpha.getbands()
    assert not template_has_transparency(
        carries_alpha, min_alpha=config.template_transparency_min_alpha
    )


def test_an_opaque_alpha_channel_does_not_hide_the_product(config: PipelineConfig) -> None:
    """The live failure: the white knockout was skipped, the opaque template was painted
    over the product, and *every* final image came out as the bare template.

    The template here is exactly the shape of the shipped ones: RGBA, alpha 255 everywhere,
    white middle, coloured logo band.
    """
    template = Image.new("RGBA", (800, 800), (255, 255, 255, 255))
    ImageDraw.Draw(template).rectangle((0, 0, 800, 90), fill=(12, 78, 160, 255))
    product = Image.new("RGB", (800, 800), (220, 30, 40))

    result = apply_template(product, template, config).convert("RGB")
    array = np.asarray(result, dtype=np.uint8)

    red = ((array[:, :, 0] > 180) & (array[:, :, 1] < 80) & (array[:, :, 2] < 80)).sum()
    assert red > 300_000, f"only {red} product pixels survived - the template hid the product"
    assert not np.array_equal(array, _rendered(template)), "output equals the bare template"


def test_shipped_templates_really_show_two_different_products(tmp_path: Path) -> None:
    """Two different products must never produce the same final image."""
    config = _shipped_assets_config(tmp_path)
    template = Image.open(config.asset_paths["digimall_template"]).convert("RGBA")
    left = apply_template(Image.new("RGB", (900, 900), (220, 30, 40)), template, config)
    right = apply_template(Image.new("RGB", (900, 900), (20, 90, 190)), template, config)

    left_array = np.asarray(left.convert("RGB"), dtype=np.uint8)
    right_array = np.asarray(right.convert("RGB"), dtype=np.uint8)
    assert not np.array_equal(left_array, right_array)
    for name, array, dominant in (
        ("red", left_array, 0),
        ("blue", right_array, 2),
    ):
        band = array[200:1000, 200:1000]
        assert band[:, :, dominant].mean() > 120, f"{name}: product not visible in the middle"
        assert not np.array_equal(array, _rendered(template)), f"{name}: output is the template"


def test_apply_template_keeps_template_size(config: PipelineConfig) -> None:
    template = Image.open(config.asset_paths["digimall_template"]).convert("RGBA")
    result = apply_template(clean_product_image(), template, config)
    assert result.size == template.size


# --------------------------------------------------------------------------- #
# Live finding (2026-10-06): PWM-1250 fell through *both* pages because a promo
# strip and a layout seam crossed the S7 ring on otherwise perfect photos.
# --------------------------------------------------------------------------- #
def _product_photo_with_bottom_band(size: tuple[int, int] = (900, 900)) -> Image.Image:
    """White studio shot + a dark promo strip glued to the bottom edge (shop style)."""
    image = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(image)
    height = size[1]
    draw.rectangle((size[0] // 2 - 150, 200, size[0] // 2 + 150, height - 300), fill=(200, 205, 210))
    draw.rectangle((0, height - 160, size[0], height), fill=(58, 58, 62))          # the strip
    draw.rectangle((size[0] - 260, height - 140, size[0] - 60, height - 30), fill=(30, 70, 150))
    draw.text((40, height - 100), "PWM Single Tub 1250 SA", fill=(255, 255, 255))
    return image


def test_promo_band_is_cut_so_the_photo_passes_S7(config: PipelineConfig) -> None:
    """The strip is not product content: cut it, then the S7 ring test passes."""
    image = _product_photo_with_bottom_band()
    assert run_quality_gates(image, config)[0] == "S7"  # what happened live

    cropped, removed = strip_promo_band(image, config)
    assert removed is not None and removed[0] == "bottom"
    assert 150 <= removed[1] <= 175, removed                # strip + its white margin
    assert run_quality_gates(cropped, config)[0] == "ok"
    assert cropped.size[0] == image.size[0]


def test_promo_band_is_never_cut_without_a_clean_gap(config: PipelineConfig) -> None:
    """A dark object running off the bottom edge is product content, not a strip."""
    image = Image.new("RGB", (900, 900), (255, 255, 255))
    ImageDraw.Draw(image).rectangle((300, 300, 600, 900), fill=(40, 40, 45))
    cropped, removed = strip_promo_band(image, config)
    assert removed is None
    assert cropped is image


def test_promo_band_detection_can_be_switched_off(config: PipelineConfig) -> None:
    image = _product_photo_with_bottom_band()
    cropped, removed = strip_promo_band(image, replace(config, promo_band_strip_enabled=False))
    assert removed is None and cropped is image


def _photo_with_edge_seam(size: tuple[int, int] = (900, 900)) -> Image.Image:
    """Product on white plus a 2 px divider line across the top edge (as shipped live).

    That is the real artefact: the S7 top band mixes white with the dark line, its standard
    deviation exceeds ``border_std_max`` and the flawless photo is rejected.
    """
    image = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((320, 180, 560, size[1] - 150), fill=(205, 208, 212))       # product
    draw.rectangle((0, 0, size[0] - 1, 1), fill=(60, 60, 60))                  # edge seam
    return image


def test_edge_seam_is_trimmed_so_the_photo_passes_S7(config: PipelineConfig) -> None:
    # Strict (rescue off) so the test exercises the seam trim itself, not the studio rescue:
    # the seam photo has 4 neutral corners and would otherwise be rescued before trimming.
    strict = replace(config, studio_background_rescue=False)
    image = _photo_with_edge_seam()
    assert run_quality_gates(image, strict)[0] == "S7"          # what happened live

    trimmed, removed = trim_edge_seams(image, config)
    assert removed and removed[0][0] == "horizontal-start", removed
    assert run_quality_gates(trimmed, strict)[0] == "ok"
    assert trimmed.size[1] < image.size[1]
    assert trimmed.size[0] == image.size[0]


def test_a_seam_beside_real_content_is_left_alone(config: PipelineConfig) -> None:
    """The sliver beyond a seam must be empty: a strip of product must never be cut off."""
    image = Image.new("RGB", (900, 900), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((700, 0, 704, 899), fill=(120, 120, 120))                  # seam, not at the edge
    draw.rectangle((720, 300, 860, 600), fill=(120, 30, 30))                  # real content beyond it
    trimmed, removed = trim_edge_seams(image, config)
    assert removed == []
    assert trimmed is image


# --------------------------------------------------------------------------- #
# User order (2026-10-06): the new brand templates + "big, centred, never on the
# logo, never glued to the bottom".
# --------------------------------------------------------------------------- #
def _paste_bbox(template: Image.Image, composite: Image.Image) -> tuple[int, int, int, int]:
    """Where the product actually landed = pixels that differ from the bare template."""
    def flat(im: Image.Image) -> np.ndarray:
        base = Image.new("RGBA", im.size, (255, 255, 255, 255))
        merged = Image.alpha_composite(base, im.convert("RGBA"))
        return np.asarray(merged.convert("RGB"), dtype=np.int16)

    diff = np.abs(flat(composite) - flat(template)).sum(axis=2) > 8
    rows = np.where(diff.sum(axis=1) > 3)[0]
    cols = np.where(diff.sum(axis=0) > 3)[0]
    return int(rows[0]), int(rows[-1]), int(cols[0]), int(cols[-1])


def _shaped_product(width: int, height: int) -> Image.Image:
    image = Image.new("RGB", (900, 900), (255, 255, 255))
    ImageDraw.Draw(image).rectangle(
        (450 - width // 2, 450 - height // 2, 450 + width // 2, 450 + height // 2),
        fill=(170, 60, 60),
    )
    return image


@pytest.mark.parametrize("key", ["digimall_template", "alfamall_template"])
def test_product_fills_the_real_template(tmp_path: Path, key: str) -> None:
    """The user's complaint ("very damn small"): a tall product must fill the canvas.

    Measured on the pixels that actually changed, so the template's own logo cannot
    inflate the number.
    """
    config = _shipped_assets_config(tmp_path)
    template = Image.open(config.asset_paths[key]).convert("RGBA")
    canvas_h = template.size[1]

    square = to_square_canvas(
        isolate_main_product(_shaped_product(300, 780), config)[0],
        config.canonical_side,
        fill_ratio=config.product_fill_ratio,
    )
    result = apply_template(square, template, config)
    top, bottom, left, right = _paste_bbox(template, result)
    height = bottom - top + 1
    width = right - left + 1

    assert height >= 0.70 * canvas_h, f"{key}: tall product only {100*height/canvas_h:.0f}% of the canvas"
    assert width <= config.canonical_side, "product must not be wider than the canvas"


@pytest.mark.parametrize("key", ["digimall_template", "alfamall_template"])
def test_wide_product_fills_the_real_template(tmp_path: Path, key: str) -> None:
    config = _shipped_assets_config(tmp_path)
    template = Image.open(config.asset_paths[key]).convert("RGBA")
    width, height = template.size

    square = to_square_canvas(
        isolate_main_product(_shaped_product(780, 640), config)[0],
        config.canonical_side,
        fill_ratio=config.product_fill_ratio,
    )
    result = apply_template(square, template, config)
    top, bottom, left, right = _paste_bbox(template, result)

    filled = 100 * (right - left + 1) / width
    assert right - left + 1 >= 0.75 * width, f"{key}: wide product only filled {filled:.0f}% of the width"
    assert top >= header_safe_margin(template, config), f"{key}: product reached into the logo area"


@pytest.mark.parametrize("key", ["digimall_template", "alfamall_template"])
def test_product_is_centred_and_never_glued_to_the_bottom(tmp_path: Path, key: str) -> None:
    config = _shipped_assets_config(tmp_path)
    template = Image.open(config.asset_paths[key]).convert("RGBA")
    width, height = template.size
    margin = header_safe_margin(template, config)

    square = to_square_canvas(
        isolate_main_product(_shaped_product(300, 780), config)[0],
        config.canonical_side,
        fill_ratio=config.product_fill_ratio,
    )
    top, bottom, left, right = _paste_bbox(template, apply_template(square, template, config))

    assert abs((left + right) / 2 - width / 2) <= 3, f"{key}: not horizontally centred"
    assert bottom >= height - 0.06 * height, f"{key}: product is glued to the bottom edge"
    assert top >= margin, f"{key}: product touched the logo band (row {top} < {margin})"


# --------------------------------------------------------------------------- #
# Root cause found live (2026-10-06): Dawlance's official gallery had FOUR images
# and only one survived, so the row shipped a single picture.
# --------------------------------------------------------------------------- #
def _studio_shot_that_fills_the_frame(size: tuple[int, int] = (1000, 1000)) -> Image.Image:
    """White studio background, but the product touches the frame edges.

    That is what a manufacturer's own "top view" / "open lid" gallery photo looks like: a
    genuine white-background product shot that still fails the strict S7 border-ring test.
    """
    image = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(image)
    # touches the top and bottom edges (a "top view" / "open lid" shot) but the four corners
    # stay clean white - exactly the shape of the real Dawlance gallery photos
    draw.rectangle((200, 0, size[0] - 200, size[1]), fill=(52, 54, 58))
    draw.ellipse((420, 300, 580, 460), fill=(230, 232, 235))
    return image


def test_studio_shot_that_fills_the_frame_is_accepted_everywhere(config: PipelineConfig) -> None:
    """A white studio shot whose product touches the edges is accepted on any host.

    The corners (4/4 neutral) prove a studio backdrop, so the universal rescue accepts it
    even on a retail page.  This is the 2026-10-07 policy change: the same geometry that the
    official-gallery acceptance used is no longer gated by host (live case: Surmawala
    DW-7500 white shot).  Switching the rescue off restores the old strict retail behaviour.
    """
    image = _studio_shot_that_fills_the_frame()
    assert neutral_corner_count(image, config.corner_patch_ratio, config.neutral_bright_min_value,
                                config.neutral_bright_max_saturation) == 4
    assert run_quality_gates(image, config)[0] == "ok"                  # retail / universal rescue
    assert run_quality_gates(image, config, official_gallery=True)[0] == "ok"


def test_studio_rescue_can_be_switched_off(config: PipelineConfig) -> None:
    image = _studio_shot_that_fills_the_frame()
    strict = replace(config, studio_background_rescue=False)
    assert run_quality_gates(image, strict)[0] == "S7"


def _edge_touching_white_shot() -> Image.Image:
    """White backdrop, but the product spans top-to-bottom with wide side margins.

    The 12%-of-side corner patches stay pure white while the product crosses the top and
    bottom edges - the exact geometry of the owner's Surmawala washer photo, which the strict
    border-ring test rejected even though the corners were 4/4 neutral.
    """
    size = 1200
    array = np.full((size, size, 3), 255, np.uint8)
    array[20:120, 200:1000] = (120, 120, 120)        # grey top deck touching the top edge
    array[120:870, 200:1000] = (245, 245, 245)       # near-white body
    array[870:1180, 200:1000] = (30, 30, 30)         # dark base touching the bottom edge
    return Image.fromarray(array)


def test_edge_touching_product_on_white_is_accepted(config: PipelineConfig) -> None:
    image = _edge_touching_white_shot()
    assert not border_is_clean(
        image, ring_ratio=config.border_ring_ratio, ring_min_px=config.border_ring_min_px,
        tolerance=config.border_bg_tolerance, std_max=config.border_std_max,
        min_fraction=config.border_clean_min_fraction,
    )
    assert run_quality_gates(image, config)[0] == "ok"


def test_owner_washer_photo_is_accepted(config: PipelineConfig) -> None:
    """The exact artefact from the 2026-10-07 bug report must now pass S7."""
    path = Path(__file__).resolve().parents[2] / "uploads" / "image-3.png"
    if not path.exists():                                   # pragma: no cover - asset optional
        pytest.skip("uploads/image-3.png not present")
    assert run_quality_gates(Image.open(path), config)[0] == "ok"


def test_official_gallery_acceptance_still_rejects_a_coloured_scene(config: PipelineConfig) -> None:
    """A lifestyle shot is not rescued by the official-gallery acceptance: its corners are
    not neutral studio background."""
    image = Image.new("RGB", (900, 900), (30, 90, 200))                 # blue everywhere
    ImageDraw.Draw(image).rectangle((250, 250, 650, 700), fill=(180, 40, 40))
    assert run_quality_gates(image, config, official_gallery=True)[0] == "S7"


def test_official_gallery_acceptance_can_be_switched_off(config: PipelineConfig) -> None:
    # Both rescues off: the image then has no path past the strict border-ring test.
    image = _studio_shot_that_fills_the_frame()
    strict = replace(config, official_gallery_relaxed_s7=False, studio_background_rescue=False)
    assert run_quality_gates(image, strict, official_gallery=True)[0] == "S7"


# --------------------------------------------------------------------------- #
# S16 - Visual Color Gate
# --------------------------------------------------------------------------- #
def _create_synthetic_appliance(body_color: tuple[int, int, int]) -> Image.Image:
    """Create a studio shot: 1000x1000 white background with a central appliance body."""
    img = Image.new("RGB", (1000, 1000), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    # Refrigerator body in center
    draw.rectangle((250, 150, 750, 850), fill=body_color)
    # Neutral black handle and base
    draw.rectangle((270, 450, 300, 550), fill=(20, 20, 20))
    draw.rectangle((250, 850, 750, 880), fill=(40, 40, 40))
    return img


def test_visual_color_gate_rejects_purple_and_red_when_golden_expected() -> None:
    from image_quality import verify_image_color

    purple_shot = _create_synthetic_appliance((120, 65, 125))  # Purple body
    red_shot = _create_synthetic_appliance((180, 30, 40))      # Red body
    gold_shot = _create_synthetic_appliance((210, 195, 160))   # Golden/Champagne body

    ok, reason = verify_image_color(purple_shot, "golden")
    assert not ok, "Purple refrigerator must be rejected when target is golden"
    assert "purple" in reason.lower()

    ok, reason = verify_image_color(red_shot, "golden")
    assert not ok, "Red refrigerator must be rejected when target is golden"
    assert "red" in reason.lower()

    ok, _ = verify_image_color(gold_shot, "golden")
    assert ok, "Golden refrigerator must be accepted when target is golden"


def test_visual_color_gate_fails_open_when_no_target_color() -> None:
    from image_quality import verify_image_color

    purple_shot = _create_synthetic_appliance((120, 65, 125))
    ok, _ = verify_image_color(purple_shot, "")
    assert ok, "When title specifies no color, S16 must fail open"


def test_visual_color_gate_live_homage_images() -> None:
    from image_quality import verify_image_color

    slug = "homage-freezer-on-top-refrigerator-18-cu-ft-golden-hrf-47662-gd"
    base = Path(__file__).resolve().parents[1] / "output" / "images" / slug / "final_alfamall"
    p01 = base / "01-alfamall.jpg"
    p02 = base / "02-alfamall.jpg"
    p03 = base / "03-alfamall.jpg"

    if not (p01.exists() and p02.exists() and p03.exists()):
        pytest.skip("Homage images not present in output directory")

    # Image 01 is Purple
    ok, reason = verify_image_color(Image.open(p01), "golden")
    assert not ok, f"Image 01 (purple) must be rejected, got {ok} ({reason})"

    # Image 02 is Maroon/Red
    ok, reason = verify_image_color(Image.open(p02), "golden")
    assert not ok, f"Image 02 (red) must be rejected, got {ok} ({reason})"

    # Image 03 is Golden
    ok, reason = verify_image_color(Image.open(p03), "golden")
    assert ok, f"Image 03 (golden) must be accepted, got {ok} ({reason})"


def test_visual_color_gate_expanded_conflicts_matrix() -> None:
    from image_quality import verify_image_color

    purple_shot = _create_synthetic_appliance((120, 65, 125))  # Purple body
    red_shot = _create_synthetic_appliance((180, 30, 40))      # Red body
    blue_shot = _create_synthetic_appliance((30, 60, 180))     # Blue body
    green_shot = _create_synthetic_appliance((40, 150, 50))    # Green body
    gold_shot = _create_synthetic_appliance((210, 195, 160))   # Golden body
    white_shot = _create_synthetic_appliance((245, 245, 245))  # White body
    black_shot = _create_synthetic_appliance((25, 25, 25))     # Black body
    silver_shot = _create_synthetic_appliance((180, 180, 180)) # Silver body

    # 1. White target must reject chromatic variants (purple, red, blue, green, gold)
    for bad_shot in (purple_shot, red_shot, blue_shot, green_shot, gold_shot):
        ok, reason = verify_image_color(bad_shot, "white")
        assert not ok, f"Expected rejection for white target with chromatic body, got reason: {reason}"
    assert verify_image_color(white_shot, "white")[0] is True

    # 2. Black target must reject chromatic variants
    for bad_shot in (purple_shot, red_shot, blue_shot, green_shot, gold_shot):
        ok, reason = verify_image_color(bad_shot, "black")
        assert not ok, f"Expected rejection for black target with chromatic body, got reason: {reason}"
    assert verify_image_color(black_shot, "black")[0] is True

    # 3. Grey / Silver target must reject chromatic variants
    for bad_shot in (purple_shot, red_shot, blue_shot, green_shot, gold_shot):
        ok, reason = verify_image_color(bad_shot, "silver")
        assert not ok, f"Expected rejection for silver target with chromatic body, got reason: {reason}"
    assert verify_image_color(silver_shot, "silver")[0] is True

    # 4. Blue target must reject red, purple, gold, green
    for bad_shot in (purple_shot, red_shot, gold_shot, green_shot):
        ok, reason = verify_image_color(bad_shot, "blue")
        assert not ok, f"Expected rejection for blue target, got reason: {reason}"
    assert verify_image_color(blue_shot, "blue")[0] is True

    # 5. Green target must reject red, purple, gold, blue
    for bad_shot in (purple_shot, red_shot, gold_shot, blue_shot):
        ok, reason = verify_image_color(bad_shot, "green")
        assert not ok, f"Expected rejection for green target, got reason: {reason}"
    assert verify_image_color(green_shot, "green")[0] is True

    # 6. Gold target must reject purple, red, blue, green
    for bad_shot in (purple_shot, red_shot, blue_shot, green_shot):
        ok, reason = verify_image_color(bad_shot, "golden")
        assert not ok, f"Expected rejection for golden target, got reason: {reason}"
    assert verify_image_color(gold_shot, "golden")[0] is True

    # 7. Red target must reject purple, gold, blue, green
    for bad_shot in (purple_shot, gold_shot, blue_shot, green_shot):
        ok, reason = verify_image_color(bad_shot, "red")
        assert not ok, f"Expected rejection for red target, got reason: {reason}"
    assert verify_image_color(red_shot, "red")[0] is True



def test_only_the_quality_floors_are_salvageable() -> None:
    """S13's lossy floors mark their reject salvageable; broken payloads never are."""
    buf = io.BytesIO()
    Image.new("RGB", (300, 300), (200, 170, 60)).save(buf, "JPEG", quality=20)
    payload = buf.getvalue()
    with pytest.raises(ImageValidationError) as floor_hit:
        validate_image_bytes(
            payload,
            "image/jpeg",
            max_bytes=10_000_000,
            max_pixels=50_000_000,
            min_lossy_bytes=len(payload) + 1000,
        )
    assert floor_hit.value.salvageable is True
    with pytest.raises(ImageValidationError) as broken:
        validate_image_bytes(
            b"this is not an image at all",
            "image/png",
            max_bytes=10_000_000,
            max_pixels=50_000_000,
        )
    assert broken.value.salvageable is False


def test_sharpness_score_separates_crisp_from_blurred() -> None:
    """Contrast-normalised: flat sharp shots ~0.029+, textured photos higher,
    blurred <= 0.01; the salvage floor is 0.02."""
    from image_quality import sharpness_score

    crisp = Image.new("RGB", (800, 800), (240, 240, 240))
    draw = ImageDraw.Draw(crisp)
    draw.rounded_rectangle((180, 140, 620, 660), radius=24, fill=(200, 170, 60))
    pale = Image.new("RGB", (800, 800), (240, 240, 240))
    ImageDraw.Draw(pale).rounded_rectangle(
        (180, 140, 620, 660), radius=24, fill=(207, 195, 165)
    )
    blurred = crisp.resize((90, 90)).resize((800, 800))
    assert sharpness_score(crisp) > 0.02
    assert sharpness_score(pale) > 0.02                 # low contrast but sharp
    assert sharpness_score(blurred) < 0.01


# --------------------------------------------------------------------------- #
# white-on-white isolation guards (owner 2026-10-08: sliver crop, remote zoom)
# --------------------------------------------------------------------------- #
def test_white_on_white_sliver_publishes_full_frame(config: PipelineConfig) -> None:
    """A white unit on white bg leaves only dark vent slivers - crop nothing."""
    from compositing import isolate_main_product

    canvas = Image.new("RGB", (400, 400), (255, 255, 255))
    ImageDraw.Draw(canvas).rectangle((50, 196, 350, 204), fill=(60, 60, 60))
    out, degraded = isolate_main_product(canvas, config)
    assert out.size == (400, 400)          # full frame, not a vent sliver
    assert degraded is True


def test_white_on_white_body_is_cropped_with_generous_padding(
    config: PipelineConfig,
) -> None:
    """A real near-white body still isolates, padded so its edges survive."""
    from compositing import isolate_main_product

    canvas = Image.new("RGB", (400, 400), (255, 255, 255))
    ImageDraw.Draw(canvas).rectangle((80, 100, 320, 300), fill=(238, 238, 238))
    out, degraded = isolate_main_product(canvas, config)
    assert out.size != (400, 400)
    assert out.width >= 240 + 40           # body plus generous padding each side
    assert degraded is True


def test_upscale_cap_keeps_small_sources_small() -> None:
    """A 60px remote must not be blown to 10x mush across the canvas."""
    from compositing import to_square_canvas

    tiny = Image.new("RGB", (60, 40), (30, 30, 30))
    canvas = to_square_canvas(tiny, 1000, max_upscale=2.0)
    assert canvas.getpixel((5, 5)) == (255, 255, 255)     # canvas padding survives
    assert canvas.getpixel((500, 500)) != (255, 255, 255)  # product still centred


# --------------------------------------------------------------------------- #
# S16 achromatic colourway rules (owner 2026-10-08: white unit on a black row)
# --------------------------------------------------------------------------- #
def test_black_target_rejects_a_bright_unit() -> None:
    from image_quality import verify_image_color

    shot = Image.new("RGB", (600, 600), (255, 255, 255))
    ImageDraw.Draw(shot).rectangle((110, 150, 490, 450), fill=(245, 235, 205))
    ok, reason = verify_image_color(shot, "black")
    assert ok is False
    assert "bright" in reason


def test_white_target_rejects_a_dark_unit() -> None:
    from image_quality import verify_image_color

    shot = Image.new("RGB", (600, 600), (255, 255, 255))
    ImageDraw.Draw(shot).rectangle((50, 50, 550, 550), fill=(15, 15, 18))
    ok, reason = verify_image_color(shot, "white")
    assert ok is False
    assert "dark" in reason


def test_white_target_tolerates_a_small_blue_accent() -> None:
    """The WD-1051 dispenser: white body, blue control panel - not a conflict."""
    from image_quality import verify_image_color

    shot = Image.new("RGB", (600, 600), (255, 255, 255))
    draw = ImageDraw.Draw(shot)
    draw.rectangle((150, 120, 450, 520), fill=(250, 250, 250))
    draw.rectangle((280, 160, 360, 200), fill=(40, 80, 200))
    ok, _ = verify_image_color(shot, "white")
    assert ok is True


# --------------------------------------------------------------------------- #
# S13 magic-byte sniffing (owner 2026-10-08: tossdown serves "webp" content-type)
# --------------------------------------------------------------------------- #
def test_mislabeled_webp_content_type_still_accepted() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (300, 300), (200, 30, 30)).save(buffer, format="WEBP", quality=90)
    image = validate_image_bytes(
        buffer.getvalue(), "webp", max_bytes=5_000_000, max_pixels=30_000_000
    )
    assert image.size == (300, 300)


def test_non_raster_content_type_without_magic_bytes_rejected() -> None:
    with pytest.raises(ImageValidationError) as info:
        validate_image_bytes(
            b"<html>not a picture</html>",
            "text/html",
            max_bytes=5_000_000,
            max_pixels=30_000_000,
        )
    assert info.value.rule_id == "S13"


def test_black_row_keeps_a_small_correct_black_unit() -> None:
    """Audit 2026-10-09: a small black product on a big white canvas must survive."""
    from image_quality import verify_image_color

    shot = Image.new("RGB", (1000, 1000), (255, 255, 255))
    ImageDraw.Draw(shot).rectangle((350, 320, 650, 680), fill=(20, 20, 22))
    ok, reason = verify_image_color(shot, "black")
    assert ok is True, reason


def test_white_row_rejects_a_wide_dark_unit() -> None:
    """Audit 2026-10-09: a slim wide black AC on a white row - mean alone misses it."""
    from image_quality import verify_image_color

    shot = Image.new("RGB", (1000, 1000), (255, 255, 255))
    d = ImageDraw.Draw(shot)
    d.rectangle((75, 360, 925, 640), fill=(22, 22, 24))
    d.rectangle((95, 585, 905, 610), fill=(40, 40, 42))
    ok, reason = verify_image_color(shot, "white")
    assert ok is False
    assert "dark" in reason


def _studio_gradient_jpeg() -> bytes:
    """A crisp studio shot that compresses below the bpp floor (smooth gradient)."""
    arr = np.full((900, 900, 3), 250, np.uint8)
    ys, xs = np.mgrid[0:900, 0:900]
    body = (ys > 250) & (ys < 650) & (xs > 100) & (xs < 800)
    grad = (180 + 60 * np.sin(xs / 90.0)).astype(np.uint8)
    arr[body, 0] = grad[body]
    arr[body, 1] = (grad * 0.9).astype(np.uint8)[body]
    arr[body, 2] = (grad * 0.7).astype(np.uint8)[body]
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG", quality=55)
    return buf.getvalue()


def test_a_sharp_below_floor_payload_passes_when_sharpness_is_checked() -> None:
    """Owner 2026-10-09: the golden Haier hero (bpp 0.018) is crisp - compression
    floors must only bite where the picture is actually blurred."""
    data = _studio_gradient_jpeg()
    with pytest.raises(ImageValidationError):
        validate_image_bytes(
            data,
            "image/jpeg",
            max_bytes=20_000_000,
            max_pixels=50_000_000,
            min_lossy_bytes=12000,
            min_lossy_bytes_per_pixel=0.025,
        )                                                               # old strictness
    image = validate_image_bytes(
        data,
        "image/jpeg",
        max_bytes=20_000_000,
        max_pixels=50_000_000,
        min_lossy_bytes=12000,
        min_lossy_bytes_per_pixel=0.025,
        blur_sharpness_floor=0.02,
    )
    assert image.size == (900, 900)


def test_a_blurred_below_floor_payload_stays_rejected_with_sharpness_checked() -> None:
    data = _studio_gradient_jpeg()
    blurred = Image.open(io.BytesIO(data)).resize((90, 90)).resize((900, 900))
    buf = io.BytesIO()
    blurred.save(buf, "JPEG", quality=55)
    with pytest.raises(ImageValidationError) as exc:
        validate_image_bytes(
            buf.getvalue(),
            "image/jpeg",
            max_bytes=20_000_000,
            max_pixels=50_000_000,
            min_lossy_bytes=12000,
            min_lossy_bytes_per_pixel=0.025,
            blur_sharpness_floor=0.02,
        )
    assert "blurry" in exc.value.detail


def _grey_emblem() -> Image.Image:
    """The Haier 'AI + leaf' badge shape: monochrome ink on a grey disc."""
    from PIL import ImageDraw

    emblem = Image.new("RGB", (900, 900), (217, 217, 217))
    draw = ImageDraw.Draw(emblem)
    draw.ellipse((20, 20, 880, 880), outline=(85, 85, 85), width=22)
    draw.regular_polygon((430, 380, 190), 6, outline=(85, 85, 85), width=26)
    draw.ellipse((430, 430, 800, 800), outline=(85, 85, 85), width=26)
    draw.line((470, 760, 760, 470), fill=(85, 85, 85), width=26)
    return emblem


def test_a_monochrome_emblem_is_rejected_by_s14() -> None:
    """Live 2026-10-09: the registry gallery's AI emblem sailed through S7 (its
    ring *is* calm, its corners neutral).  S14 names the logo: achromatic, grey
    ground, dark ink."""
    config = PipelineConfig(output_dir=Path("/tmp/s14"), dry_run=True)
    assert run_quality_gates(_grey_emblem(), config)[0] == "S14"


def test_product_photos_still_pass_s14() -> None:
    config = PipelineConfig(output_dir=Path("/tmp/s14"), dry_run=True)
    for colour in ("#c9a227", "#e8e8e8", "#101010", "#2f5fa8"):
        rule, _ = run_quality_gates(clean_product_image(900, 900, colour), config)
        assert rule == "ok", colour


def test_a_white_product_on_a_grey_ground_is_not_an_emblem() -> None:
    """Live false positive 2026-10-09: Kenwood's perfect official shot is a white
    unit on a light-grey studio ground - achromatic, grey corners - and the first
    S14 draft ate it.  The body brighter than the ground is what proves a product:
    an emblem carries nothing brighter than its disc."""
    from PIL import ImageDraw

    shot = Image.new("RGB", (500, 280), (217, 217, 217))
    draw = ImageDraw.Draw(shot)
    draw.rounded_rectangle((30, 40, 470, 200), radius=12, fill=(250, 250, 250))
    draw.rectangle((60, 210, 440, 235), fill=(90, 90, 90))
    draw.rectangle((215, 150, 285, 162), fill=(60, 60, 60))
    config = PipelineConfig(output_dir=Path("/tmp/s14"), dry_run=True)
    assert not is_monochrome_emblem(shot, config)
    assert run_quality_gates(shot, config)[0] != "S14"


def test_an_official_grey_ground_studio_shot_passes_the_gates() -> None:
    """Owner 2026-10-09: brand sites are official - the model match (S1) proves the
    page, so their light-grey studio grounds get the calmer corner bar and the
    perfect Kenwood shot passes instead of dying on S7/S14."""
    from PIL import ImageDraw

    shot = Image.new("RGB", (500, 280), (217, 217, 217))
    draw = ImageDraw.Draw(shot)
    draw.rounded_rectangle((30, 40, 470, 200), radius=12, fill=(250, 250, 250))
    draw.rectangle((60, 210, 440, 235), fill=(90, 90, 90))
    draw.rectangle((215, 150, 285, 162), fill=(60, 60, 60))
    config = PipelineConfig(output_dir=Path("/tmp/s14"), dry_run=True)
    assert run_quality_gates(shot, config, official_gallery=True) == ("ok", "")


def test_the_emblem_still_fails_on_official_galleries() -> None:
    """S14 is content-based, so the brand's own badge stays rejected even where the
    S7 bar is relaxed."""
    config = PipelineConfig(output_dir=Path("/tmp/s14"), dry_run=True)
    assert run_quality_gates(_grey_emblem(), config, official_gallery=True)[0] == "S14"


# --------------------------------------------------------------------------- #
# S20 - full-bleed marketing artwork on a white canvas
# --------------------------------------------------------------------------- #
def _white_ground_emblem(size: int = 1182, cell: int = 25) -> Image.Image:
    """A brand emblem sheet on studio white - the shape of Haier's ``logo_white_bg``
    files the owner reported on 2026-10-10 (1182x1182, mean saturation ~0.3, artwork
    reaching all four edges).  The mark is dense blocky stroke-work, so a 64x64
    resample of it keeps jumping between dark and light; the four corner boxes stay
    clean studio white, which is exactly why S7 and S14 both stayed quiet about it.
    """
    image = Image.new("RGB", (size, size), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    rng = np.random.default_rng(7)                       # fixed seed: a deterministic test
    margin = size // 8
    shades = (35, 120, 255, 60, 255)
    for y in range(0, size, cell):
        for x in range(0, size, cell):
            in_mark = (margin <= x <= size - margin - 1) or (margin <= y <= size - margin - 1)
            inside = not (x < margin or y < margin or x >= size - margin or y >= size - margin)
            if in_mark and inside:
                shade = shades[int(rng.integers(0, len(shades)))]
                if shade != 255:
                    draw.rectangle((x, y, x + cell - 1, y + cell - 1), fill=(shade,) * 3)
    for edge in (0, size - 1):                           # and the ink touches every side
        draw.point((edge, size // 2), fill=(35, 35, 35))
        draw.point((size // 2, edge), fill=(35, 35, 35))
    return image


def _full_bleed_poster(size: int = 800) -> Image.Image:
    """Orient's marketing renders: a light-blue wash painted past all four edges."""
    image = Image.new("RGB", (size, size), (238, 244, 251))
    draw = ImageDraw.Draw(image)
    draw.rectangle((size // 8, size // 8, 7 * size // 8, size // 4), fill=(30, 60, 160))
    draw.rectangle((size // 8, size // 2, 3 * size // 4, 3 * size // 4), fill=(250, 250, 250))
    return image


def test_s20_rejects_an_emblem_on_a_white_ground(config: PipelineConfig) -> None:
    """The hole the owner kept hitting: S14 only fires on a grey ground, so a white
    ``logo_white_bg`` emblem walked past every gate.  S20 judges the content, and it
    fires end to end - the calm white ring keeps S7 quiet, exactly like the live files.
    """
    image = _white_ground_emblem()
    assert is_marketing_layout(image, config)
    rule, detail = run_quality_gates(image, config)
    assert rule == "S20", detail
    assert "line art" in detail


def test_s20_rejects_a_full_bleed_marketing_render(config: PipelineConfig) -> None:
    image = _full_bleed_poster()
    ink_fraction, span, _texture = layout_metrics(image)
    assert span >= 0.98 and ink_fraction >= config.marketing_max_ink_fraction
    assert is_marketing_layout(image, config)
    assert "painted artwork" in marketing_layout_reason(image, config)


def test_s20_leaves_a_product_that_fills_the_frame_alone(config: PipelineConfig) -> None:
    """A white unit photographed edge to edge on a white studio ground is not artwork:
    ink 0.36 and texture 10 in the owner's Midea gallery, both far below the cut-offs."""
    image = Image.new("RGB", (600, 600), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 190, 599, 410), fill=(205, 207, 211))     # long unit, spans wide
    draw.rectangle((0, 411, 599, 430), fill=(150, 152, 156))
    assert not is_marketing_layout(image, config)


def test_s20_does_not_speak_about_a_lifestyle_scene(config: PipelineConfig) -> None:
    """A photo of a room also fills its frame, but its corners are not a white canvas -
    that case belongs to S7, and S20 must not answer for it."""
    image = Image.new("RGB", (600, 600), (30, 90, 200))
    draw = ImageDraw.Draw(image)
    for step in range(0, 600, 7):
        draw.line((step, 0, step, 600), fill=(180, 40, 40), width=2)
    assert not is_marketing_layout(image, config)


def test_s20_can_be_switched_off(config: PipelineConfig) -> None:
    relaxed = replace(config, marketing_layout_enabled=False)
    assert run_quality_gates(_white_ground_emblem(), relaxed)[0] != "S20"


# --------------------------------------------------------------------------- #
# S21 - in-box / manual caption sheets inside a product gallery
# --------------------------------------------------------------------------- #
def _caption_sheet(size: int = 1000) -> Image.Image:
    """Dawlance's "In-box items" slide: six line icons, a caption under each, flat on white.

    The owner's live complaint (2026-10-10): "It downloads the inbox items pictures and
    remote manual please fix".  Structurally this is not a photograph of anything: it is a
    grid of comparable parts with nothing leading.
    """
    image = Image.new("RGB", (size, size), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    cell = size // 3
    for row in range(2):
        for col in range(3):
            x0 = col * cell + cell // 6
            y0 = row * cell + cell // 5
            draw.rectangle((x0, y0, x0 + cell // 2, y0 + cell // 3), outline=(20, 20, 20), width=6)
            caption_y = y0 + cell // 3 + cell // 12
            draw.rectangle((x0 - 10, caption_y, x0 + cell // 2 + 10, caption_y + 22), fill=(30, 30, 30))
    return image


def test_s21_rejects_a_caption_sheet_of_comparable_parts(config: PipelineConfig) -> None:
    image = _caption_sheet()
    parts, dominant = body_layout(image)
    assert parts >= config.diagram_min_parts and dominant <= config.diagram_max_dominant_share
    assert run_quality_gates(image, config)[0] == "S21"


def test_s21_keeps_a_product_with_its_accessories(config: PipelineConfig) -> None:
    """A unit photographed next to its remote is two parts with one clearly leading - a
    real product shot, and the live corpus backs the cut-off (real photos: at most 3 parts,
    the leader covering 94 % of the ink)."""
    image = Image.new("RGB", (900, 900), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((60, 300, 700, 520), fill=(206, 208, 212))      # the unit
    draw.rectangle((760, 340, 840, 520), fill=(120, 122, 126))     # the remote
    parts, dominant = body_layout(image)
    assert dominant > config.diagram_max_dominant_share
    assert not diagram_sheet_reason(image, config)


def test_s21_can_be_switched_off(config: PipelineConfig) -> None:
    from dataclasses import replace

    assert diagram_sheet_reason(_caption_sheet(), replace(config, diagram_sheet_enabled=False)) == ""


# --------------------------------------------------------------------------- #
# S7.panel - a pasted marketing panel is cut, a dark product is not
# --------------------------------------------------------------------------- #
def _unit_with_badge_panel(size: int = 600) -> Image.Image:
    """The owner's Dawlance file of 2026-10-10, drawn as a fixture.

    A studio shot of the indoor unit on a light grey plate, with a **flat black panel**
    glued underneath it carrying feature badges, a capacity pill and a warranty block.
    The panel is pasted artwork: dead flat, full width, and its top edge is one hard
    straight line - which is what the split keys on.
    """
    image = Image.new("RGB", (size, size * 3 // 5), (246, 247, 249))
    draw = ImageDraw.Draw(image)
    w, h = image.size
    band = int(h * 0.55)
    draw.rounded_rectangle(
        (w // 16, h // 20, 15 * w // 16, int(h * 0.50)), radius=12, fill=(232, 233, 236)
    )
    draw.rectangle((w // 16, int(h * 0.45), 15 * w // 16, int(h * 0.50)), fill=(58, 60, 64))
    draw.rectangle((0, band, w, h), fill=(8, 9, 12))
    for i in range(4):
        cx = w // 8 + i * w // 6
        draw.ellipse((cx - w // 20, band + h // 8, cx + w // 20, band + h // 3), fill=(28, 60, 150))
        draw.rectangle((cx - w // 20, band + h // 3, cx + w // 20, band + h // 3 + 8), fill=(238, 240, 244))
    draw.rounded_rectangle((w // 2 - w // 10, band + h // 20, w // 2 + w // 10, band + h // 8),
                           radius=8, fill=(40, 70, 170))
    return image


def _dark_product_on_dark_ground(size: int = 600) -> Image.Image:
    """A black appliance on a grey studio ground - the shape a naive "cut the dark bottom"
    rule destroys, and the reason the split demands a flat panel and a hard edge."""
    image = Image.new("RGB", (size, size), (198, 200, 204))
    draw = ImageDraw.Draw(image)
    for y in range(size):                       # a real gradient, not a pasted block
        shade = 198 - int(120 * (y / size))
        draw.line((0, y, size, y), fill=(shade, shade, shade + 4))
    draw.rectangle((size // 6, size // 4, 5 * size // 6, 13 * size // 16), fill=(24, 25, 28))
    draw.rectangle((size // 6, size // 3, 5 * size // 6, size // 3 + 10), fill=(150, 152, 158))
    return image


def test_marketing_panel_split_cuts_the_pasted_panel(config: PipelineConfig) -> None:
    image = _unit_with_badge_panel()
    crop, panel = marketing_panel_split(image, config)
    assert panel is not None
    side, pixels = panel
    assert side == "bottom" and pixels > 0
    assert crop.size == (image.size[0], image.size[1] - pixels)
    assert crop.size[1] > config.min_short_side          # the product area is still a photo


def test_the_cut_product_passes_the_gates(config: PipelineConfig) -> None:
    """"only products gets at end": the panel is gone and the unit is accepted."""
    image = _unit_with_badge_panel()
    assert run_quality_gates(image, config)[0] == "S20"  # as published before tonight
    crop, panel = marketing_panel_split(image, config)
    assert panel is not None
    assert run_quality_gates(crop, config, panel_cut=panel) == ("ok", "")
    # without the detector's verdict the same crop is still read as artwork
    assert run_quality_gates(crop, config)[0] == "S20"


def test_marketing_panel_split_leaves_a_dark_product_alone(config: PipelineConfig) -> None:
    assert marketing_panel_split(_dark_product_on_dark_ground(), config)[1] is None


def test_the_wash_exemption_is_only_reachable_through_a_real_cut(config: PipelineConfig) -> None:
    """The exemption must not become a way back for an ad render: a full-bleed light wash is
    still rejected whenever nothing was cut, and the poster shape is never cut."""
    image = _full_bleed_poster()
    assert marketing_panel_split(image, config)[1] is None
    assert run_quality_gates(image, config)[0] == "S20"
    assert run_quality_gates(image, config, panel_cut=marketing_panel_split(image, config)[1])[0] == "S20"


def test_marketing_panel_split_can_be_switched_off(config: PipelineConfig) -> None:
    off = replace(config, marketing_panel_split_enabled=False)
    assert marketing_panel_split(_unit_with_badge_panel(), off)[1] is None

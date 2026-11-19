"""Unit tests for the perceptual CIELAB Two-Sided Evidence Color Gate (color_gate.py)."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from color_gate import (
    evaluate_image_color,
    strip_non_colourway_phrases,
)


def _studio_box(
    body_rgb: tuple[int, int, int],
    *,
    bg_rgb: tuple[int, int, int] = (255, 255, 255),
    box: tuple[int, int, int, int] = (250, 150, 750, 850),
    size: int = 1000,
) -> Image.Image:
    img = Image.new("RGB", (size, size), bg_rgb)
    ImageDraw.Draw(img).rectangle(box, fill=body_rgb)
    return img


# --------------------------------------------------------------------------- #
# Layer 1: Non-colourway technical idiom stripping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("title", "expected_absent"),
    [
        ("Haier 1.5 Ton White Inverter AC Blue Fin", "blue"),
        ("Dawlance Washing Machine Silver Ion Tech", "silver"),
        ("A4Tech Mechanical Keyboard Red Switch", "red"),
        ("HDMI 2.1 Cable Gold Plated 3m", "gold"),
        ("LaserJet Black & White Printer", "black"),
    ],
)
def test_strip_non_colourway_phrases_removes_tech_specs(
    title: str, expected_absent: str
) -> None:
    phrases = (
        "blue fin",
        "silver ion",
        "red switch",
        "gold plated",
        "black & white",
    )
    cleaned = strip_non_colourway_phrases(title, phrases)
    assert expected_absent not in cleaned.lower()


# --------------------------------------------------------------------------- #
# Layer 2 & 3: Foreground-only CIELAB two-sided evidence
# --------------------------------------------------------------------------- #
def test_slender_black_product_passes_on_white_and_light_grey_backdrops() -> None:
    # Thin vertical cable / blender occupying only ~8% of the canvas
    for bg in ((255, 255, 255), (242, 242, 242)):
        img = Image.new("RGB", (800, 800), bg)
        draw = ImageDraw.Draw(img)
        draw.rectangle((365, 100, 435, 420), fill=(24, 24, 26))
        draw.rectangle((375, 420, 425, 700), fill=(185, 185, 190))

        verdict = evaluate_image_color(img, "black")
        assert verdict.outcome == "pass", verdict
        assert verdict.target_share >= 0.35


def test_white_cable_on_black_row_rejected() -> None:
    img = Image.new("RGB", (800, 800), (255, 255, 255))
    ImageDraw.Draw(img).rectangle((350, 120, 450, 700), fill=(235, 235, 238))

    verdict = evaluate_image_color(img, "black")
    assert verdict.outcome == "reject"
    assert "bright" in verdict.reason


def test_multi_colour_accented_red_printer_passes_via_positive_presence() -> None:
    """HP Red Printer pattern: white printer body + red top bar (~22% fg) + warm
    sample photo (~30% fg).  Old single-median HSV rejected as 'gold'; two-sided
    CIELAB confirms red target presence and accepts."""
    img = Image.new("RGB", (800, 800), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle((140, 260, 660, 340), fill=(195, 35, 45))   # red top strip
    draw.rectangle((140, 340, 660, 480), fill=(230, 230, 232)) # light grey/white body
    draw.rectangle((260, 480, 540, 620), fill=(210, 175, 125)) # warm photo output

    verdict = evaluate_image_color(img, "red")
    assert verdict.outcome == "pass", verdict
    assert verdict.target_share >= 0.12


def test_black_mouse_inside_bright_retail_box_passes_via_positive_presence() -> None:
    """Hoco/VIZO pattern: black product (~20% fg) inside a bright retail box."""
    img = Image.new("RGB", (800, 800), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle((200, 120, 600, 680), fill=(205, 230, 240)) # pale cyan retail box
    draw.ellipse((280, 300, 520, 520), fill=(25, 25, 28))      # black mouse inside box

    verdict = evaluate_image_color(img, "black")
    assert verdict.outcome == "pass", verdict
    assert verdict.target_share >= 0.12


def test_glossy_black_glass_sheen_vs_dyed_navy_separated_in_cielab() -> None:
    sheen = _studio_box((30, 40, 50))
    navy = _studio_box((27, 42, 82))

    assert evaluate_image_color(sheen, "black").outcome == "pass"
    assert evaluate_image_color(sheen, "grey").outcome == "pass"

    navy_verdict = evaluate_image_color(navy, "black")
    assert navy_verdict.outcome == "reject"
    assert "saturated colour" in navy_verdict.reason


def test_rose_gold_and_burgundy_shade_volumes() -> None:
    blush = _studio_box((218, 178, 161))    # rose gold blush (L* 76, C* 18, h 49)
    rose = _studio_box((178, 134, 146))     # rose gold pink  (L* 60, C* 19, h 0)
    burgundy = _studio_box((125, 86, 165))  # pearl burgundy  (L* 44, C* 49, h 311)
    green = _studio_box((40, 150, 50))
    blue = _studio_box((30, 60, 180))

    assert evaluate_image_color(blush, "rose gold").outcome == "pass"
    assert evaluate_image_color(rose, "rose gold").outcome == "pass"
    assert evaluate_image_color(burgundy, "burgundy").outcome == "pass"

    assert evaluate_image_color(green, "rose gold").outcome == "reject"
    assert evaluate_image_color(blue, "rose gold").outcome == "reject"
    assert evaluate_image_color(green, "burgundy").outcome == "reject"


def test_wrong_colour_refrigerators_still_rejected_with_rival_evidence() -> None:
    purple = _studio_box((120, 65, 125))
    red = _studio_box((180, 30, 40))
    gold = _studio_box((210, 195, 160))

    v_purple = evaluate_image_color(purple, "golden")
    assert v_purple.outcome == "reject"
    assert v_purple.dominant_rival == "purple"
    assert v_purple.rival_share >= 0.45

    v_red = evaluate_image_color(red, "golden")
    assert v_red.outcome == "reject"
    assert v_red.dominant_rival == "red"

    v_gold = evaluate_image_color(gold, "golden")
    assert v_gold.outcome == "pass"
    assert v_gold.target_share >= 0.45


def test_grey_neutral_studio_shot_fails_open_on_chromatic_target() -> None:
    grey = _studio_box((180, 180, 180))
    verdict = evaluate_image_color(grey, "red")
    assert verdict.outcome == "pass"

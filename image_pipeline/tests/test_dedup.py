"""S10: near-duplicate removal and, above all, per-product isolation of the state."""

from __future__ import annotations

import threading

from conftest import clean_product_image, second_angle_image
from PIL import Image

from dedup import ProductDedup, compute_fingerprint, hamming_distance, is_visual_duplicate


def new_dedup(*, colour_min_agreement: int = 2) -> ProductDedup:
    return ProductDedup(
        hard_threshold=6,
        soft_threshold=14,
        corr_threshold=0.995,
        colour_min_agreement=colour_min_agreement,
    )


#: Five clearly different silhouettes: a test's several pictures of one colour are angles
#: of one product, never duplicates of each other - only their colour is meant to agree.
_SHAPES = (
    (120, 80, 420, 780),
    (80, 200, 780, 560),
    (200, 120, 700, 700),
    (100, 300, 500, 620),
    (300, 60, 860, 500),
)


def _product(rgb: tuple[int, int, int], index: int) -> Image.Image:
    from PIL import ImageDraw

    image = Image.new("RGB", (900, 900), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = _SHAPES[index % len(_SHAPES)]
    draw.rounded_rectangle((left, top, right, bottom), radius=18, fill=rgb)
    draw.ellipse((left + 20, top + 20, left + 70, top + 70), fill=(70, 70, 70))
    return image


_BLUE = (47, 95, 168)
_RED = (180, 30, 40)
_PURPLE = (120, 65, 125)
_GOLD = (212, 175, 55)


def test_same_image_resized_is_duplicate() -> None:
    original = clean_product_image(900, 900)
    resized = original.resize((600, 600), Image.Resampling.LANCZOS)
    dedup = new_dedup()
    assert dedup.add(original, "https://cdn.shop/p/a-1.png") is True
    assert dedup.add(resized, "https://cdn.shop/p/a-1-600x600.png") is False


def test_different_angle_not_duplicate() -> None:
    dedup = new_dedup()
    dedup.add(clean_product_image(), "https://cdn.shop/p/a-1.png")
    assert dedup.add(second_angle_image(), "https://cdn.shop/p/a-2.png") is True


def test_dedup_state_not_shared_across_products() -> None:
    """Two products processed back to back must both keep the same image (S10)."""
    image = clean_product_image()
    first = new_dedup()
    second = new_dedup()
    assert first.add(image, "https://cdn.shop/p/shared.png") is True
    assert second.add(image, "https://cdn.shop/p/shared.png") is True
    assert len(first) == 1 and len(second) == 1


def test_dedup_state_not_shared_between_threads() -> None:
    """Same guarantee while two products are processed concurrently."""
    image = clean_product_image()
    results: dict[str, bool] = {}
    lock = threading.Lock()

    def worker(name: str) -> None:
        dedup = new_dedup()
        kept = dedup.add(image, f"https://cdn.shop/{name}.png")
        with lock:
            results[name] = kept

    threads = [threading.Thread(target=worker, args=(f"p{index}",)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == {f"p{index}": True for index in range(4)}


def test_hamming_and_soft_thresholds() -> None:
    left = compute_fingerprint(clean_product_image())
    right = compute_fingerprint(clean_product_image(880, 880))
    assert hamming_distance(left.dhash, right.dhash) <= 6
    assert is_visual_duplicate(left, [right], hard_threshold=6, soft_threshold=14, corr_threshold=0.995)


def test_the_gallery_colour_locks_only_after_two_pictures_agree() -> None:
    """S17's rule, owner 2026-10-16: one odd picture never defines the album. The family
    locks once ``colour_min_agreement`` accepted shots agree on it, and only then can a
    different family be refused - a wrong colour picture costs a shop penalty, a wrong
    *rejection* silently starves the row."""
    dedup = new_dedup()
    assert dedup.add(_product(_RED, 0), "https://cdn.shop/red-1.png") is True
    ok, _ = dedup.check_color_consistency(_product(_PURPLE, 1))
    assert ok, "one red shot is not yet the album's colour"
    assert dedup.add(_product(_PURPLE, 1), "https://cdn.shop/purple-1.png") is True
    assert dedup.add(_product(_RED, 2), "https://cdn.shop/red-2.png") is True
    assert dedup.add(_product((170, 25, 45), 3), "https://cdn.shop/red-3.png") is True
    ok, reason = dedup.check_color_consistency(_product(_GOLD, 4))
    assert not ok
    assert "gold" in reason.lower() and "red" in reason.lower()


def test_an_album_holding_two_families_is_never_called_settled() -> None:
    """The lock is a veto mechanism, not a verdict on the album: a gallery that already
    accepted red *and* purple holds two products, and must be reported to the owner even
    once purple or red has outvoted the other.  Reading "locked" as "settled" is how a
    mixed album published silently (fresh-context review, 2026-10-16)."""
    dedup = new_dedup()
    assert dedup.add(_product(_RED, 0), "https://cdn.shop/red-1.png") is True
    assert dedup.add(_product(_PURPLE, 1), "https://cdn.shop/purple-1.png") is True
    assert dedup.add(_product(_RED, 2), "https://cdn.shop/red-2.png") is True
    assert dedup.add(_product(_RED, 3), "https://cdn.shop/red-3.png") is True
    assert dedup.colour_families == ("red", "purple")
    assert dedup.colour_settled is False


def test_one_family_and_neutrals_alone_is_a_settled_album() -> None:
    """What the rule is happy about: every coloured shot agreeing, and any number of
    neutral studio shots saying nothing."""
    dedup = new_dedup()
    assert dedup.add(_product(_BLUE, 0), "https://cdn.shop/b1.png") is True
    assert dedup.add(_product((245, 245, 245), 1), "https://cdn.shop/w1.png") is True
    assert dedup.add(_product((30, 60, 180), 2), "https://cdn.shop/b2.png") is True
    assert dedup.colour_families == ("blue",)
    assert dedup.colour_settled is True


def test_neutral_pictures_are_no_opinion_about_the_album_colour() -> None:
    """A white or grey shot says nothing about the product's colour, so it neither counts
    toward the lock nor breaks it - which is why an AC or refrigerator studio gallery is
    untouched by this rule."""
    dedup = new_dedup()
    assert dedup.add(_product((245, 245, 245), 0), "https://cdn.shop/w1.png") is True
    assert dedup.add(_product((150, 150, 152), 1), "https://cdn.shop/g1.png") is True
    assert dedup.colour_families == ()
    ok, _ = dedup.check_color_consistency(_product(_BLUE, 2))
    assert ok


def test_an_unresolved_mix_is_asked_about_not_guessed() -> None:
    """Two families and no two accepted pictures agree on either: nothing locks, so the
    run must not pick a colour for the owner - it publishes what it has and reports the
    question with what it saw (owner 2026-10-16: a wrong colour picture is a penalty)."""
    dedup = new_dedup()
    assert dedup.add(_product(_BLUE, 0), "https://cdn.shop/b1.png") is True
    ok, _ = dedup.check_color_consistency(_product(_RED, 1))
    assert ok, "one picture of each colour decides nothing yet"
    assert dedup.add(_product(_RED, 1), "https://cdn.shop/r1.png") is True
    assert dedup.colour_settled is False
    assert dedup.colour_sightings == (("blue", 1), ("red", 1))


def test_a_settled_gallery_is_never_asked_about() -> None:
    """Once two accepted pictures agree, the album has a colour: a refused sibling is
    settled business, not a question for the owner - which is what keeps the notice rare."""
    dedup = new_dedup()
    assert dedup.add(_product(_BLUE, 0), "https://cdn.shop/b1.png") is True
    assert dedup.add(_product((30, 60, 180), 1), "https://cdn.shop/b2.png") is True
    ok, _ = dedup.check_color_consistency(_product(_RED, 2))
    assert not ok
    assert dedup.colour_settled is True


def _ac_angle(display_x: int = 690, flap: bool = False) -> Image.Image:
    """A champagne split-AC studio render; the knobs mimic two catalogue angles."""
    from PIL import ImageDraw

    img = Image.new("RGB", (900, 900), (250, 250, 250))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((60, 300, 840, 560), radius=40, fill=(203, 180, 140))
    draw.rounded_rectangle((60, 500, 840, 600), radius=30, fill=(150, 110, 90))
    draw.rectangle((display_x, 380, display_x + 90, 420), fill=(240, 235, 225))
    if flap:
        for x in range(100, 800, 60):
            draw.line((x, 320, x + 30, 340), fill=(170, 150, 115), width=4)
    return img


def test_two_close_catalog_angles_are_both_kept() -> None:
    """Live 2026-10-09: the row's correct second angle was rejected as a
    "near-duplicate" because the full-frame hash is dominated by the white
    backdrop.  The product-region fingerprint must keep both angles."""
    dedup = new_dedup()
    assert dedup.add(_ac_angle(), "https://cdn.haier.com/a/front.jpg") is True
    assert dedup.add(_ac_angle(display_x=640, flap=True), "https://cdn.haier.com/a/vent.jpg") is True


def test_a_re_served_copy_of_the_same_angle_still_duplicates() -> None:
    import io

    original = _ac_angle()
    buf = io.BytesIO()
    original.resize((630, 630)).save(buf, "JPEG", quality=80)
    served = Image.open(io.BytesIO(buf.getvalue())).resize((900, 900))
    dedup = new_dedup()
    assert dedup.add(original, "https://cdn.haier.com/a/front.jpg") is True
    assert dedup.add(served, "https://cdn.haier.com/a/front-630.jpg") is False

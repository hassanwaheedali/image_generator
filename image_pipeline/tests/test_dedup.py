"""S10: near-duplicate removal and, above all, per-product isolation of the state."""

from __future__ import annotations

import threading

from conftest import clean_product_image, second_angle_image
from PIL import Image

from dedup import ProductDedup, compute_fingerprint, hamming_distance, is_visual_duplicate


def new_dedup() -> ProductDedup:
    return ProductDedup(hard_threshold=6, soft_threshold=14, corr_threshold=0.995)


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


def test_intra_gallery_color_consistency() -> None:
    from PIL import ImageDraw

    dedup = new_dedup()
    img_red = Image.new("RGB", (800, 800), (255, 255, 255))
    ImageDraw.Draw(img_red).rectangle((200, 100, 600, 700), fill=(180, 30, 40))

    img_purple = Image.new("RGB", (800, 800), (255, 255, 255))
    ImageDraw.Draw(img_purple).rectangle((200, 100, 600, 700), fill=(120, 65, 125))

    assert dedup.add(img_red, "https://cdn.shop/red.png") is True
    ok, reason = dedup.check_color_consistency(img_purple)
    assert not ok
    assert "purple" in reason.lower() and "red" in reason.lower()


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

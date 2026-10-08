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

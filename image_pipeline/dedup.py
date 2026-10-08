"""Safeguard S10: near-duplicate removal, strictly per product.

The dedup state lives in a ``ProductDedup`` instance that is created inside
``pipeline.process_product`` and thrown away with the product.  There is **no**
module-level state, so an image can never be carried from one product to another.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from PIL import Image

from image_quality import to_rgb


@dataclass(frozen=True)
class ImageFingerprint:
    """dHash (64 bits) plus a small normalised grey thumbnail for correlation."""

    dhash: int
    gray: np.ndarray


def compute_fingerprint(image: Image.Image) -> ImageFingerprint:
    """dHash of the 9x8 grey thumbnail + a 32x32 normalised grey patch."""
    rgb = to_rgb(image)
    small = np.asarray(rgb.convert("L").resize((9, 8), Image.Resampling.LANCZOS), dtype=int)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    patch = np.asarray(rgb.convert("L").resize((32, 32), Image.Resampling.BILINEAR), dtype=float)
    patch -= patch.mean()
    norm = float(np.linalg.norm(patch))
    if norm > 0:
        patch /= norm
    return ImageFingerprint(dhash=value, gray=patch)


def hamming_distance(left: int, right: int) -> int:
    """Number of differing bits between two 64-bit dHashes."""
    return int((left ^ right).bit_count())


def _correlation(left: np.ndarray, right: np.ndarray) -> float:
    if left.shape != right.shape:
        return 0.0
    return float(np.clip(np.sum(left * right), -1.0, 1.0))


def is_visual_duplicate(
    fingerprint: ImageFingerprint,
    existing: Sequence[ImageFingerprint],
    *,
    hard_threshold: int,
    soft_threshold: int,
    corr_threshold: float,
) -> bool:
    """Same angle twice (resize/re-encode) => duplicate; a different angle is not."""
    for candidate in existing:
        distance = hamming_distance(fingerprint.dhash, candidate.dhash)
        if distance <= hard_threshold:
            return True
        if distance <= soft_threshold and _correlation(fingerprint.gray, candidate.gray) >= corr_threshold:
            return True
    return False


class ProductDedup:
    """Per-product fingerprint list.  Never global, never shared (S10)."""

    def __init__(self, *, hard_threshold: int, soft_threshold: int, corr_threshold: float) -> None:
        self._hard = hard_threshold
        self._soft = soft_threshold
        self._corr = corr_threshold
        self._entries: list[tuple[str, ImageFingerprint]] = []
        self._accepted_color_families: list[str] = []

    def check_color_consistency(self, image: Image.Image) -> tuple[bool, str]:
        """Safeguard S17: checks if image foreground color contradicts already-accepted images."""
        if not self._accepted_color_families:
            return True, ""
        from image_quality import extract_foreground_color_profile

        profile = extract_foreground_color_profile(image)
        if not profile.is_chromatic:
            return True, ""
        first_fam = self._accepted_color_families[0]
        _CHROMATIC_SET = (
            "purple", "red", "blue", "green", "gold", "orange", "yellow", "pink", "brown",
        )
        if profile.dominant_family != first_fam and profile.dominant_family in _CHROMATIC_SET:
            return False, (
                f"intra-gallery colour conflict: candidate is {profile.dominant_family} "
                f"(hue {profile.median_hue:.0f}°) while accepted gallery is {first_fam}"
            )
        return True, ""

    def is_duplicate(self, image: Image.Image) -> bool:
        """True when this image repeats an already accepted angle of this product."""
        fingerprint = compute_fingerprint(image)
        return is_visual_duplicate(
            fingerprint,
            [entry for _, entry in self._entries],
            hard_threshold=self._hard,
            soft_threshold=self._soft,
            corr_threshold=self._corr,
        )

    def add(self, image: Image.Image, url: str) -> bool:
        """Remember an accepted image.  Returns False when it was a duplicate."""
        fingerprint = compute_fingerprint(image)
        if is_visual_duplicate(
            fingerprint,
            [entry for _, entry in self._entries],
            hard_threshold=self._hard,
            soft_threshold=self._soft,
            corr_threshold=self._corr,
        ):
            return False
        self._entries.append((url, fingerprint))
        from image_quality import extract_foreground_color_profile

        profile = extract_foreground_color_profile(image)
        _CHROMATIC_SET = (
            "purple", "red", "blue", "green", "gold", "orange", "yellow", "pink", "brown",
        )
        if profile.is_chromatic and profile.dominant_family in _CHROMATIC_SET:
            self._accepted_color_families.append(profile.dominant_family)
        return True

    @property
    def urls(self) -> tuple[str, ...]:
        return tuple(url for url, _ in self._entries)

    def __len__(self) -> int:
        return len(self._entries)

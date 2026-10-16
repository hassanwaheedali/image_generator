"""Safeguard S10: near-duplicate removal, strictly per product.

The dedup state lives in a ``ProductDedup`` instance that is created inside
``pipeline.process_product`` and thrown away with the product.  There is **no**
module-level state, so an image can never be carried from one product to another.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from PIL import Image

from image_quality import body_shape, extract_foreground_color_profile, shape_overlap, to_rgb

#: The labels ``image_quality.extract_foreground_color_profile`` uses when a picture does
#: **not** name a colour: the neutral verdict and the hue-ambiguous catch-all.  Everything
#: else the profiler can emit (red, gold, blue, green, purple, and any family added later)
#: is a colour someone could stand behind, so this list of *exclusions* is the whole
#: vocabulary S17 needs - no family list to keep in step with the profiler (owner rule:
#: derive, don't hand-list; fresh-context review 2026-10-16 flagged the nine-name table I
#: first wrote as four dead entries plus a silent no-op on any rename).
_NO_COLOUR_FAMILIES = frozenset({"neutral", "chromatic", ""})


def _chromatic_family(image: Image.Image) -> tuple[str, float]:
    """The image's colour family and hue, or ``("", 0.0)`` when it names no colour."""
    profile = extract_foreground_color_profile(image)
    if not profile.is_chromatic or profile.dominant_family in _NO_COLOUR_FAMILIES:
        return "", 0.0
    return profile.dominant_family, profile.median_hue


class GalleryColour:
    """Safeguard S17's memory: which family the album is, and what the pages showed.

    One product has one colour, so a gallery never mixes two colour **families** - a red
    refrigerator beside a blue one is two products on one shop page, and a wrong picture
    costs a penalty. A single picture never defines the album either: the family locks
    only when ``min_agreement`` accepted pictures agree on it, because a detail shot of a
    white AC with a black louvre reads as a different family and must not delete the row's
    real photos (owner 2026-10-16).  Every family *seen* is counted, refused pictures
    included, so a row that states no colour can be reported back to the owner instead of
    guessed for him.
    """

    def __init__(self, *, min_agreement: int) -> None:
        self._min_agreement = min_agreement
        self._accepted: Counter[str] = Counter()
        self._lock = ""

    def verdict(self, image: Image.Image) -> tuple[bool, str]:
        """True when this candidate may join the gallery; the reason when it may not."""
        family, hue = _chromatic_family(image)
        if not self._locked(family):
            return True, ""
        return False, (
            f"intra-gallery colour conflict: candidate is {family} (hue {hue:.0f}°) "
            f"while this gallery is {self._lock}"
        )

    def record(self, image: Image.Image) -> None:
        """Remember an accepted picture's family; two agreeing ones fix the album."""
        family, _ = _chromatic_family(image)
        if not family:
            return
        self._accepted[family] += 1
        if self._accepted[family] >= self._min_agreement:
            self._lock = family

    def _locked(self, family: str) -> bool:
        """True when this picture's family contradicts the album's settled colour."""
        return bool(family) and bool(self._lock) and family != self._lock

    @property
    def settled(self) -> bool:
        """Whether this album is one colour.

        The lock is a veto, not a verdict: an album that accepted red *and* purple holds two
        products even once one of them has outvoted the other, so the answer comes from how
        many families were accepted, never from whether a lock exists (fresh-context review
        2026-10-16: reading "locked" as "settled" let a mixed gallery publish silently).
        """
        return len(self._accepted) < 2

    @property
    def families(self) -> tuple[str, ...]:
        """The colour families this gallery accepted, most-published first."""
        return tuple(family for family, _ in self.sightings)

    @property
    def sightings(self) -> tuple[tuple[str, int], ...]:
        return tuple(sorted(self._accepted.items(), key=lambda item: (-item[1], item[0])))


@dataclass(frozen=True)
class ImageFingerprint:
    """dHash (64 bits), a normalised grey thumbnail, and the product's body silhouette."""

    dhash: int
    gray: np.ndarray
    #: Largest solid body normalised to its own box (see ``image_quality.body_shape``),
    #: or ``None`` when there is no measurable body / no scipy.
    shape: np.ndarray | None = None


def _product_patch(rgb: Image.Image, size: int = 32) -> np.ndarray:
    """Normalised grey patch of the *product region* only.

    A full-frame patch is dominated by the white studio backdrop, so two different
    angles of the same appliance correlate ~0.998 and read as re-served copies.
    Cropping to the non-background bbox puts every pixel of the budget on the
    product itself: re-served copies still correlate ~1.0, a moved display / open
    flap / other angle drops to ~0.98 (measured 2026-10-09).
    """
    array = np.asarray(rgb, dtype=int)
    mask = np.abs(array - array[0, 0]).sum(axis=-1) > 48
    ys, xs = np.nonzero(mask)
    if len(xs) >= 10:
        box = (
            max(int(xs.min()) - 4, 0),
            max(int(ys.min()) - 4, 0),
            min(int(xs.max()) + 5, array.shape[1]),
            min(int(ys.max()) + 5, array.shape[0]),
        )
        rgb = rgb.crop(box)
    patch = np.asarray(rgb.convert("L").resize((size, size), Image.Resampling.BILINEAR), dtype=float)
    patch -= patch.mean()
    norm = float(np.linalg.norm(patch))
    return patch / norm if norm > 0 else patch


def compute_fingerprint(image: Image.Image) -> ImageFingerprint:
    """dHash of the 9x8 grey thumbnail + a 32x32 product-region grey patch."""
    rgb = to_rgb(image)
    small = np.asarray(rgb.convert("L").resize((9, 8), Image.Resampling.LANCZOS), dtype=int)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return ImageFingerprint(dhash=value, gray=_product_patch(rgb), shape=body_shape(rgb))


def hamming_distance(left: int, right: int) -> int:
    """Number of differing bits between two 64-bit dHashes."""
    return int((left ^ right).bit_count())


def _correlation(left: np.ndarray, right: np.ndarray) -> float:
    if left.shape != right.shape:
        return 0.0
    return float(np.clip(np.sum(left * right), -1.0, 1.0))


def duplicate_reason(
    fingerprint: ImageFingerprint,
    existing: Sequence[ImageFingerprint],
    *,
    hard_threshold: int,
    soft_threshold: int,
    corr_threshold: float,
    shape_iou_min: float = 0.90,
    hard_corr_threshold: float = 0.985,
) -> str:
    """Why this image repeats an already accepted angle, or ``""`` when it does not.

    The decision is unchanged from the shipped rule - what this function adds is the
    *sentence*, because "near-duplicate" in ``decisions.jsonl`` told a reviewer nothing
    about which signal fired or how close the pair was.

    Why the rule cannot simply be tightened, measured on the owner's 33-product run
    (2026-10-09): the pairs he calls duplicates and the pair his own test guards are not
    separable by any of these signals.

    | pair                                  | dHash | silhouette IoU | body correlation |
    |---------------------------------------|-------|----------------|------------------|
    | Ecostar 01~02 (he says: duplicate)    |     3 |          0.985 |            0.802 |
    | Gree Pular 01~02 (he says: duplicate) |     4 |          0.985 |            0.555 |
    | open-louver flap vs flat (must keep)  |     2 |          1.000 |            0.965 |

    The protected pair is *closer* on every measure than both real duplicates, so any
    threshold loose enough to catch them also deletes a legitimate second angle - which
    is the bug the correlation demand was added to stop.  Tightening dedup is therefore
    NOT the fix for a repeated download: see ``review_shape_match``, which records the
    ambiguity for a human instead of guessing, and ``dedup_key`` in
    :mod:`url_filters`, which prevents the same asset being fetched twice by identity.
    """
    for position, candidate in enumerate(existing, start=1):
        distance = hamming_distance(fingerprint.dhash, candidate.dhash)
        correlation = _correlation(fingerprint.gray, candidate.gray)
        if distance <= hard_threshold and correlation >= hard_corr_threshold:
            return (
                f"re-served copy of accepted image #{position} "
                f"(dHash {distance} bits apart, correlation {correlation:.4f})"
            )
        if distance <= soft_threshold and correlation >= corr_threshold:
            return (
                f"near-identical to accepted image #{position} "
                f"(dHash {distance} bits apart, correlation {correlation:.4f})"
            )
    return ""


def is_visual_duplicate(
    fingerprint: ImageFingerprint,
    existing: Sequence[ImageFingerprint],
    *,
    hard_threshold: int,
    soft_threshold: int,
    corr_threshold: float,
    hard_corr_threshold: float = 0.985,
) -> bool:
    """True when this image repeats an already accepted angle of the same product."""
    return bool(
        duplicate_reason(
            fingerprint,
            existing,
            hard_threshold=hard_threshold,
            soft_threshold=soft_threshold,
            corr_threshold=corr_threshold,
            hard_corr_threshold=hard_corr_threshold,
        )
    )


class ProductDedup:
    """Per-product fingerprint list.  Never global, never shared (S10)."""

    def __init__(
        self,
        *,
        hard_threshold: int,
        soft_threshold: int,
        corr_threshold: float,
        hard_corr_threshold: float = 0.985,
        shape_iou_min: float = 0.90,
        colour_min_agreement: int = 2,
    ) -> None:
        self._hard = hard_threshold
        self._soft = soft_threshold
        self._corr = corr_threshold
        self._hard_corr = hard_corr_threshold
        self._shape_iou = shape_iou_min
        self._entries: list[tuple[str, ImageFingerprint]] = []
        self._colour = GalleryColour(min_agreement=colour_min_agreement)

    def check_color_consistency(self, image: Image.Image) -> tuple[bool, str]:
        """Safeguard S17: may this picture join the gallery already accepted?"""
        return self._colour.verdict(image)

    @property
    def colour_families(self) -> tuple[str, ...]:
        """The colour families this product's gallery accepted."""
        return self._colour.families

    @property
    def colour_settled(self) -> bool:
        """False when this product's gallery holds more than one colour family - a choice
        only the owner can settle, so the run reports it instead of guessing (S17)."""
        return self._colour.settled

    @property
    def colour_sightings(self) -> tuple[tuple[str, int], ...]:
        """Each accepted family with how often it appeared - the owner's counts."""
        return self._colour.sightings

    def duplicate_reason(
        self, image: Image.Image, *, fingerprint: ImageFingerprint | None = None
    ) -> str:
        """The sentence explaining a repeat, or ``""`` when this is a new angle.

        ``fingerprint`` lets the caller compute the hash once per candidate: the gate and
        the bookkeeping below need the same value, and re-resizing a 1200px image twice
        per accepted picture was measurable waste (2.0x, owner run 2026-10-09).
        """
        current = compute_fingerprint(image) if fingerprint is None else fingerprint
        return duplicate_reason(
            current,
            [entry for _, entry in self._entries],
            hard_threshold=self._hard,
            soft_threshold=self._soft,
            corr_threshold=self._corr,
            hard_corr_threshold=self._hard_corr,
            shape_iou_min=self._shape_iou,
        )

    def is_duplicate(
        self, image: Image.Image, *, fingerprint: ImageFingerprint | None = None
    ) -> bool:
        """True when this image repeats an already accepted angle of this product."""
        return bool(self.duplicate_reason(image, fingerprint=fingerprint))

    def review_shape_match(
        self, image: Image.Image, *, fingerprint: ImageFingerprint | None = None
    ) -> str:
        """An angle that looks like a repeat but is not proven one - flag it, keep it.

        Returned for the review CSV: inside the hard hamming radius but short of the
        correlation bar, the pair is *probably* the same picture from another shop, yet
        the measurements cannot say so (see :func:`duplicate_reason`).  Publishing a
        second angle of the same product is cheap; deleting a product's only side view is
        not, so this is a note for a human rather than a rejection.
        """
        current = compute_fingerprint(image) if fingerprint is None else fingerprint
        best = ""
        for position, candidate in enumerate([entry for _, entry in self._entries], start=1):
            distance = hamming_distance(current.dhash, candidate.dhash)
            if distance > self._hard:
                continue
            correlation = _correlation(current.gray, candidate.gray)
            if correlation >= self._hard_corr:
                continue                                        # already a real duplicate
            overlap = shape_overlap(current.shape, candidate.shape)
            best = (
                f"possible repeat of accepted image #{position}: dHash {distance} bits apart, "
                f"correlation {correlation:.4f}, silhouette IoU {overlap:.3f} - kept, review"
            )
        return best

    def add(
        self, image: Image.Image, url: str, *, fingerprint: ImageFingerprint | None = None
    ) -> bool:
        """Remember an accepted image.  Returns False when it was a duplicate."""
        current = compute_fingerprint(image) if fingerprint is None else fingerprint
        if self.duplicate_reason(image, fingerprint=current):
            return False
        self._entries.append((url, current))
        self._colour.record(image)
        return True

    @property
    def urls(self) -> tuple[str, ...]:
        return tuple(url for url, _ in self._entries)

    def __len__(self) -> int:
        return len(self._entries)

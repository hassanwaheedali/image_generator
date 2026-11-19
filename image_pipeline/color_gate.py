"""Perceptual CIELAB Two-Sided Evidence Color Gate (Safeguard S16 v2).

Replaces brittle single-median HSV hue checks with perceptual CIELAB (L*, C*, h°)
3D colour regions and a two-sided evidence rule over the true foreground mask:

1. **Foreground-only masking**: edge-connected flood-fill strips studio backdrops
   (pure white, off-white, or light grey) so slender products (cables, hair curlers,
   hand blenders, tower speakers) are never diluted by canvas padding.
2. **Positive presence check**: if at least ``min_target_presence`` (default 12%) of
   foreground pixels fall inside the target colour's 3D CIELAB volume, the target
   colour is confirmed present and the image passes (handles accented printers,
   retail-boxed black accessories, and multi-tone products).
3. **Rival contradiction check**: only when target evidence is absent does the gate
   check whether a mutually exclusive rival colour dominates the foreground
   (>= ``min_rival_dominance``, default 45%) at genuine perceptual chroma
   (``C* >= min_chroma``, default 18.0 — naturally separating glossy black glass
   sheen C*~8 and pale studio plates C*~14 from dyed navy C*~27 and gold C*~20).
4. **Structured 3-way verdict** (``ColorVerdict``): carries exact numeric shares so
   callers can enforce hard rejection in scrape / multi-URL runs or emit review
   diagnostics on sole direct-mode / curated inputs.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

import cv2
import numpy as np
from PIL import Image
from scipy import ndimage

from variant_terms import COLOUR_FAMILIES

ColorOutcome = Literal["pass", "review", "reject"]

#: Downsampled analysis canvas side (96x96 takes < 2ms including LAB + flood-fill).
_SAMPLE_SIDE: Final[int] = 96

#: Perceptual lightness upper bound for chromatic pixels (pure studio white L* > 92).
_MAX_CHROMATIC_L: Final[float] = 92.0

#: Default non-colourway technical idioms ignored when extracting product colour from titles.
DEFAULT_NON_COLOURWAY_PHRASES: Final[tuple[str, ...]] = (
    "blue fin",
    "silver ion",
    "gold plated",
    "red switch",
    "blue switch",
    "brown switch",
    "white led",
    "blue led",
    "red led",
    "green led",
    "black and white",
    "black & white",
)


@dataclass(frozen=True)
class ColorRegion:
    """3D perceptual volume in CIELAB (L* in 0..100, C* >= 0, h° in 0..360)."""

    l_min: float
    l_max: float
    c_min: float
    c_max: float
    hue_arc: tuple[float, float] | None = None


@dataclass(frozen=True)
class ColorVerdict:
    """Structured result of perceptual colour verification."""

    outcome: ColorOutcome
    target: str
    target_family: str
    target_share: float = 0.0
    dominant_rival: str = ""
    rival_share: float = 0.0
    reason: str = ""

    @property
    def accepted(self) -> bool:
        """True when the image is allowed to proceed (``pass`` or ``review``)."""
        return self.outcome != "reject"


#: Calibrated 3D CIELAB regions for colour families and specific shade targets.
#: Specific shades take precedence over their coarse parent family when named.
LAB_REGIONS: Final[dict[str, ColorRegion]] = {
    # Achromatic finishes
    "black": ColorRegion(l_min=0.0, l_max=42.0, c_min=0.0, c_max=16.0),
    "white": ColorRegion(l_min=76.0, l_max=100.0, c_min=0.0, c_max=14.0),
    "grey": ColorRegion(l_min=12.0, l_max=86.0, c_min=0.0, c_max=16.0),
    # Chromatic families
    "gold": ColorRegion(l_min=45.0, l_max=92.0, c_min=12.0, c_max=75.0, hue_arc=(55.0, 112.0)),
    "red": ColorRegion(l_min=15.0, l_max=75.0, c_min=18.0, c_max=130.0, hue_arc=(345.0, 48.0)),
    "purple": ColorRegion(l_min=12.0, l_max=78.0, c_min=16.0, c_max=120.0, hue_arc=(296.0, 16.0)),
    "blue": ColorRegion(l_min=10.0, l_max=82.0, c_min=16.0, c_max=130.0, hue_arc=(205.0, 305.0)),
    "green": ColorRegion(l_min=15.0, l_max=85.0, c_min=15.0, c_max=130.0, hue_arc=(108.0, 215.0)),
    "brown": ColorRegion(l_min=10.0, l_max=66.0, c_min=10.0, c_max=72.0, hue_arc=(22.0, 88.0)),
    "beige": ColorRegion(l_min=58.0, l_max=95.0, c_min=6.0, c_max=36.0, hue_arc=(55.0, 110.0)),
    "yellow": ColorRegion(l_min=55.0, l_max=98.0, c_min=18.0, c_max=130.0, hue_arc=(75.0, 115.0)),
    "orange": ColorRegion(l_min=35.0, l_max=85.0, c_min=20.0, c_max=130.0, hue_arc=(40.0, 78.0)),
    "pink": ColorRegion(l_min=40.0, l_max=92.0, c_min=12.0, c_max=95.0, hue_arc=(325.0, 35.0)),
    # Specific shade volumes (take priority over coarse parent family)
    "rose gold": ColorRegion(l_min=42.0, l_max=92.0, c_min=10.0, c_max=48.0, hue_arc=(340.0, 88.0)),
    "copper": ColorRegion(l_min=25.0, l_max=78.0, c_min=14.0, c_max=78.0, hue_arc=(20.0, 75.0)),
    "bronze": ColorRegion(l_min=20.0, l_max=72.0, c_min=12.0, c_max=68.0, hue_arc=(35.0, 95.0)),
    "champagne": ColorRegion(l_min=60.0, l_max=94.0, c_min=10.0, c_max=45.0, hue_arc=(55.0, 110.0)),
    "burgundy": ColorRegion(l_min=12.0, l_max=62.0, c_min=15.0, c_max=100.0, hue_arc=(300.0, 42.0)),
    "wine": ColorRegion(l_min=12.0, l_max=62.0, c_min=15.0, c_max=100.0, hue_arc=(300.0, 42.0)),
    "maroon": ColorRegion(l_min=10.0, l_max=58.0, c_min=15.0, c_max=100.0, hue_arc=(330.0, 45.0)),
    "teal": ColorRegion(l_min=15.0, l_max=80.0, c_min=14.0, c_max=90.0, hue_arc=(170.0, 235.0)),
    "olive": ColorRegion(l_min=18.0, l_max=72.0, c_min=12.0, c_max=75.0, hue_arc=(95.0, 145.0)),
}

#: Mutually exclusive chromatic rival families per target family.
CONFLICTS: Final[dict[str, frozenset[str]]] = {
    "gold": frozenset({"purple", "red", "blue", "green", "pink"}),
    "red": frozenset({"purple", "gold", "blue", "green", "yellow", "orange"}),
    "purple": frozenset({"red", "gold", "blue", "green", "yellow", "orange"}),
    "blue": frozenset({"red", "purple", "gold", "green", "yellow", "orange"}),
    "green": frozenset({"red", "purple", "gold", "blue", "yellow", "orange"}),
    "beige": frozenset({"purple", "red", "blue", "green", "pink"}),
    "brown": frozenset({"purple", "blue", "green"}),
    "yellow": frozenset({"purple", "red", "blue", "green"}),
    "orange": frozenset({"purple", "blue", "green"}),
    "pink": frozenset({"blue", "green", "gold"}),
}

#: Partition of the 360° CIELAB hue wheel into rival chromatic families.
_RIVAL_HUE_BINS: Final[tuple[tuple[str, float, float], ...]] = (
    ("red", 345.0, 48.0),
    ("orange", 48.0, 65.0),
    ("gold", 65.0, 110.0),
    ("green", 110.0, 205.0),
    ("blue", 205.0, 302.0),
    ("purple", 302.0, 345.0),
)


def strip_non_colourway_phrases(text: str, phrases: Sequence[str]) -> str:
    """Remove technical feature idioms (``blue fin``, ``silver ion``, ``red switch``)
    before extracting product colour words from a title."""
    cleaned = text
    for phrase in phrases:
        parts = [re.escape(part) for part in phrase.strip().split() if part]
        if len(parts) < 2:
            continue
        pattern = re.compile(r"(?<![A-Za-z0-9])" + r"[\s\-]+".join(parts) + r"(?![A-Za-z0-9])", re.IGNORECASE)
        cleaned = pattern.sub(" ", cleaned)
    return cleaned


def rgb_to_lch(rgb_u8: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Convert an ``(H, W, 3)`` uint8 RGB array into CIELAB ``(L*, C*, h°)`` float32 arrays."""
    lab = cv2.cvtColor(rgb_u8, cv2.COLOR_RGB2LAB).astype(np.float32)
    l_star = lab[:, :, 0] * (100.0 / 255.0)
    a_star = lab[:, :, 1] - 128.0
    b_star = lab[:, :, 2] - 128.0
    chroma = np.hypot(a_star, b_star)
    hue = np.degrees(np.arctan2(b_star, a_star)) % 360.0
    return l_star, chroma, hue


def _hue_in_arc_mask(hue: np.ndarray, start: float, end: float) -> np.ndarray:
    """Vectorized circular hue inclusion mask on ``[0, 360)``."""
    if start <= end:
        return np.asarray((hue >= start) & (hue <= end), dtype=bool)
    return np.asarray((hue >= start) | (hue <= end), dtype=bool)


def _region_mask(
    l_star: np.ndarray,
    chroma: np.ndarray,
    hue: np.ndarray,
    region: ColorRegion,
) -> np.ndarray:
    """Boolean mask of pixels falling inside a 3D CIELAB ``ColorRegion``."""
    mask = np.asarray(
        (l_star >= region.l_min)
        & (l_star <= region.l_max)
        & (chroma >= region.c_min)
        & (chroma <= region.c_max),
        dtype=bool,
    )
    if region.hue_arc is not None:
        mask = mask & _hue_in_arc_mask(hue, region.hue_arc[0], region.hue_arc[1])
    return mask


def _to_rgb_white_bg(image: Image.Image) -> Image.Image:
    """Flatten transparency onto white studio background before sampling."""
    if image.mode == "RGB":
        return image
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return image.convert("RGB")


def studio_foreground_mask(
    rgb_u8: np.ndarray,
    l_star: np.ndarray,
    chroma: np.ndarray,
) -> np.ndarray:
    """Identify product foreground pixels via edge-connected studio backdrop flood-fill.

    When the 4 corners agree on a bright neutral studio backdrop (pure white, off-white,
    or flat light grey ``#f2f2f2``), edge-connected pixels matching that backdrop are
    removed cleanly without diluting slender products or hollow interiors.  When corners
    are not a bright neutral studio ground (e.g. tinted studio plate or dark ground),
    returns the central 76% crop minus pure white pixels.
    """
    h, w = rgb_u8.shape[:2]
    edge = max(4, min(h, w) // 16)
    corners_rgb = np.concatenate(
        [
            rgb_u8[:edge, :edge].reshape(-1, 3),
            rgb_u8[:edge, -edge:].reshape(-1, 3),
            rgb_u8[-edge:, :edge].reshape(-1, 3),
            rgb_u8[-edge:, -edge:].reshape(-1, 3),
        ],
        axis=0,
    )
    corners_l = np.concatenate(
        [
            l_star[:edge, :edge].ravel(),
            l_star[:edge, -edge:].ravel(),
            l_star[-edge:, :edge].ravel(),
            l_star[-edge:, -edge:].ravel(),
        ]
    )
    corners_c = np.concatenate(
        [
            chroma[:edge, :edge].ravel(),
            chroma[:edge, -edge:].ravel(),
            chroma[-edge:, :edge].ravel(),
            chroma[-edge:, -edge:].ravel(),
        ]
    )
    if float(np.median(corners_l)) >= 82.0 and float(np.median(corners_c)) <= 12.0:
        bg_rgb = np.median(corners_rgb.astype(np.float32), axis=0)
        near_bg = np.max(np.abs(rgb_u8.astype(np.float32) - bg_rgb), axis=-1) <= 16.0
        labels, count = ndimage.label(near_bg)
        if count > 0:
            border_labels = np.unique(
                np.concatenate([labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]])
            )
            border_labels = border_labels[border_labels > 0]
            outer_bg = np.isin(labels, border_labels)
            return np.asarray(~outer_bg, dtype=bool)

    central = np.zeros((h, w), dtype=bool)
    central[int(h * 0.12) : int(h * 0.88), int(w * 0.12) : int(w * 0.88)] = True
    pure_white = (l_star > 92.0) & (chroma < 6.0)
    return np.asarray(central & ~pure_white, dtype=bool)


def _resolve_target(target_color_word: str) -> tuple[str, str, ColorRegion | None]:
    """Map a target colour string to ``(normalized, family, ColorRegion)``."""
    norm = " ".join(target_color_word.strip().lower().split())
    if not norm:
        return "", "", None
    family = next(
        (fam for fam, synonyms in COLOUR_FAMILIES.items() if norm == fam or norm in synonyms),
        "",
    )
    if not family:
        return norm, "", None
    region = LAB_REGIONS.get(norm) or LAB_REGIONS.get(family)
    return norm, family, region


def _dominant_rival(
    hue: np.ndarray,
    chromatic_fg: np.ndarray,
    fg_count: int,
) -> tuple[str, float]:
    """Return ``(rival_family, share_of_foreground)`` for the strongest chromatic rival."""
    if fg_count <= 0 or not np.any(chromatic_fg):
        return "", 0.0
    best_name = ""
    best_count = 0
    for name, start, end in _RIVAL_HUE_BINS:
        count = int((chromatic_fg & _hue_in_arc_mask(hue, start, end)).sum())
        if count > best_count:
            best_name = name
            best_count = count
    return best_name, float(best_count / fg_count)


def _chroma_conflict_verdict(
    *,
    target_word: str,
    target_family: str,
    target_share: float,
    rival_name: str,
    chroma_share: float,
    min_chroma: float,
    official: bool,
    exempt_chroma_on_official: bool,
) -> ColorVerdict:
    """Build a saturated-colour conflict verdict, respecting the official-page exemption."""
    reason = (
        f"product area is a saturated colour ({chroma_share:.0%} at C* >= {min_chroma:.0f}) - "
        f"conflicts with target '{target_word}' (family {target_family})"
    )
    if official and exempt_chroma_on_official:
        return ColorVerdict(
            outcome="pass",
            target=target_word,
            target_family=target_family,
            target_share=target_share,
            dominant_rival=rival_name,
            rival_share=chroma_share,
            reason=(
                f"{reason}; not enforced - this is the brand's own page, whose hero "
                "defines the variant's colour"
            ),
        )
    return ColorVerdict(
        outcome="reject",
        target=target_word,
        target_family=target_family,
        target_share=target_share,
        dominant_rival=rival_name,
        rival_share=chroma_share,
        reason=reason,
    )


def _evaluate_white(
    *,
    target_word: str,
    l_star: np.ndarray,
    hue: np.ndarray,
    chromatic: np.ndarray,
    fg_mask: np.ndarray,
    central_mask: np.ndarray,
    central_mean_l: float,
    official: bool,
    exempt_chroma_on_official: bool,
    min_rival_dominance: float,
    min_chroma: float,
) -> ColorVerdict:
    """Evaluate white targets: reject dark bodies and dominant saturated colours."""
    if central_mean_l < 48.0:
        return ColorVerdict(
            outcome="reject",
            target=target_word,
            target_family="white",
            dominant_rival="dark",
            rival_share=1.0,
            reason=(
                f"product area is dark (L* {central_mean_l:.1f}) - "
                f"conflicts with target '{target_word}' (family white)"
            ),
        )
    central_count = max(1, int(central_mask.sum()))
    fg_central = fg_mask & central_mask
    fg_central_frac = float(fg_central.sum() / central_count)
    fg_mean_l = float(l_star[fg_central].mean()) if fg_central.any() else 100.0
    if fg_central_frac >= 0.30 and fg_mean_l < 36.0:
        return ColorVerdict(
            outcome="reject",
            target=target_word,
            target_family="white",
            dominant_rival="dark",
            rival_share=fg_central_frac,
            reason=(
                f"product area is dominated by a dark body "
                f"(coverage {fg_central_frac:.0%}, L* {fg_mean_l:.1f}) - "
                f"conflicts with target '{target_word}' (family white)"
            ),
        )
    chroma_cov = float((chromatic & central_mask).sum() / central_count)
    if chroma_cov >= min_rival_dominance:
        rival, _ = _dominant_rival(hue, chromatic & central_mask, central_count)
        return _chroma_conflict_verdict(
            target_word=target_word,
            target_family="white",
            target_share=0.0,
            rival_name=rival,
            chroma_share=chroma_cov,
            min_chroma=min_chroma,
            official=official,
            exempt_chroma_on_official=exempt_chroma_on_official,
        )
    return ColorVerdict(outcome="pass", target=target_word, target_family="white", target_share=1.0)


def _evaluate_black_or_grey(
    *,
    target_word: str,
    target_family: str,
    l_star: np.ndarray,
    chroma: np.ndarray,
    hue: np.ndarray,
    chromatic: np.ndarray,
    fg_mask: np.ndarray,
    central_mean_l: float,
    official: bool,
    exempt_chroma_on_official: bool,
    min_target_presence: float,
    min_rival_dominance: float,
    min_chroma: float,
) -> ColorVerdict:
    """Evaluate black and grey targets strictly over the foreground silhouette."""
    fg_count = int(fg_mask.sum())
    target_region = LAB_REGIONS[target_family]
    target_share = (
        float((_region_mask(l_star, chroma, hue, target_region) & fg_mask).sum() / fg_count)
        if fg_count > 0
        else 0.0
    )
    chromatic_fg = chromatic & fg_mask
    chromatic_share = float(chromatic_fg.sum() / fg_count) if fg_count > 0 else 0.0
    rival_name, _ = _dominant_rival(hue, chromatic_fg, fg_count)

    if chromatic_share >= min_rival_dominance and target_share < min_target_presence:
        return _chroma_conflict_verdict(
            target_word=target_word,
            target_family=target_family,
            target_share=target_share,
            rival_name=rival_name,
            chroma_share=chromatic_share,
            min_chroma=min_chroma,
            official=official,
            exempt_chroma_on_official=exempt_chroma_on_official,
        )

    if target_family == "black" and target_share < min_target_presence and central_mean_l > 72.0:
        return ColorVerdict(
            outcome="reject",
            target=target_word,
            target_family="black",
            target_share=target_share,
            dominant_rival="bright",
            rival_share=1.0 - target_share,
            reason=(
                f"product area is bright (L* {central_mean_l:.1f}, "
                f"product dark {target_share:.0%}) - "
                f"conflicts with target '{target_word}' (family black)"
            ),
        )
    return ColorVerdict(
        outcome="pass",
        target=target_word,
        target_family=target_family,
        target_share=target_share,
    )


def _evaluate_achromatic(
    *,
    target_word: str,
    target_family: str,
    l_star: np.ndarray,
    chroma: np.ndarray,
    hue: np.ndarray,
    fg_mask: np.ndarray,
    central_mask: np.ndarray,
    official: bool,
    exempt_chroma_on_official: bool,
    min_target_presence: float,
    min_rival_dominance: float,
    min_chroma: float,
) -> ColorVerdict:
    """Dispatch black, white, and grey targets using perceptual lightness and chroma."""
    central_l = l_star[central_mask]
    central_mean_l = float(central_l.mean()) if central_l.size else 100.0
    chromatic = (chroma >= min_chroma) & (l_star < _MAX_CHROMATIC_L)
    if target_family == "white":
        return _evaluate_white(
            target_word=target_word,
            l_star=l_star,
            hue=hue,
            chromatic=chromatic,
            fg_mask=fg_mask,
            central_mask=central_mask,
            central_mean_l=central_mean_l,
            official=official,
            exempt_chroma_on_official=exempt_chroma_on_official,
            min_rival_dominance=min_rival_dominance,
            min_chroma=min_chroma,
        )
    return _evaluate_black_or_grey(
        target_word=target_word,
        target_family=target_family,
        l_star=l_star,
        chroma=chroma,
        hue=hue,
        chromatic=chromatic,
        fg_mask=fg_mask,
        central_mean_l=central_mean_l,
        official=official,
        exempt_chroma_on_official=exempt_chroma_on_official,
        min_target_presence=min_target_presence,
        min_rival_dominance=min_rival_dominance,
        min_chroma=min_chroma,
    )


def evaluate_image_color(
    image: Image.Image,
    target_color_word: str,
    *,
    official: bool = False,
    exempt_chroma_on_official: bool = True,
    min_target_presence: float = 0.12,
    min_rival_dominance: float = 0.45,
    min_chroma: float = 16.0,
) -> ColorVerdict:
    """Evaluate whether ``image`` contradicts ``target_color_word`` in perceptual CIELAB space."""
    norm, family, region = _resolve_target(target_color_word)
    if not family or region is None:
        return ColorVerdict(outcome="pass", target=target_color_word, target_family="")

    rgb_img = _to_rgb_white_bg(image).resize((_SAMPLE_SIDE, _SAMPLE_SIDE), Image.Resampling.BOX)
    rgb_u8 = np.asarray(rgb_img, dtype=np.uint8)
    l_star, chroma, hue = rgb_to_lch(rgb_u8)

    central_mask = np.zeros((_SAMPLE_SIDE, _SAMPLE_SIDE), dtype=bool)
    lo = int(_SAMPLE_SIDE * 0.12)
    hi = int(_SAMPLE_SIDE * 0.88)
    central_mask[lo:hi, lo:hi] = True
    fg_mask = studio_foreground_mask(rgb_u8, l_star, chroma)

    if family in ("black", "white", "grey"):
        return _evaluate_achromatic(
            target_word=target_color_word,
            target_family=family,
            l_star=l_star,
            chroma=chroma,
            hue=hue,
            fg_mask=fg_mask,
            central_mask=central_mask,
            official=official,
            exempt_chroma_on_official=exempt_chroma_on_official,
            min_target_presence=min_target_presence,
            min_rival_dominance=min_rival_dominance,
            min_chroma=min_chroma,
        )

    fg_count = int(fg_mask.sum())
    if fg_count < 16:
        return ColorVerdict(outcome="pass", target=target_color_word, target_family=family)

    # 1. Positive evidence: if meaningful foreground matches the target's 3D CIELAB volume, pass.
    target_share = float((_region_mask(l_star, chroma, hue, region) & fg_mask).sum() / fg_count)
    if target_share >= min_target_presence:
        return ColorVerdict(
            outcome="pass",
            target=target_color_word,
            target_family=family,
            target_share=target_share,
        )

    # 2. Negative evidence: reject only when a mutually exclusive chromatic rival dominates.
    chromatic_fg = (chroma >= min_chroma) & (l_star < _MAX_CHROMATIC_L) & fg_mask
    rival_name, rival_share = _dominant_rival(hue, chromatic_fg, fg_count)
    banned = CONFLICTS.get(family, frozenset())
    if rival_name in banned and rival_share >= min_rival_dominance:
        return ColorVerdict(
            outcome="reject",
            target=target_color_word,
            target_family=family,
            target_share=target_share,
            dominant_rival=rival_name,
            rival_share=rival_share,
            reason=(
                f"foreground colour ({rival_name}, {rival_share:.0%}; target '{norm}' "
                f"{target_share:.0%}) conflicts with target '{target_color_word}' (family {family})"
            ),
        )

    return ColorVerdict(
        outcome="pass",
        target=target_color_word,
        target_family=family,
        target_share=target_share,
        dominant_rival=rival_name,
        rival_share=rival_share,
    )

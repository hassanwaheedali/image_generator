"""Byte-level and pixel-level image gates (S6, S7, S8, S9, S13).

Pure functions: they take bytes or a PIL image plus explicit thresholds, so every
gate is testable offline with synthetic images.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterable
from typing import Any

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from config import PipelineConfig

LOG = logging.getLogger("pipeline.quality")

#: Content types that must never be treated as a catalog photo.
_FORBIDDEN_CONTENT_TYPES: frozenset[str] = frozenset(
    {"image/svg+xml", "image/svg", "text/html", "application/json", "text/xml"}
)


class ImageValidationError(Exception):
    """S13 failure: the bytes are not a usable raster image."""

    def __init__(self, rule_id: str, detail: str) -> None:
        super().__init__(f"{rule_id}: {detail}")
        self.rule_id = rule_id
        self.detail = detail


# --------------------------------------------------------------------------- #
# S13 - bytes must really be an image
# --------------------------------------------------------------------------- #
def validate_image_bytes(
    content: bytes,
    content_type: str = "",
    *,
    max_bytes: int,
    max_pixels: int,
) -> Image.Image:
    """Safeguard S13: size, content type, real decode, bomb guard, EXIF rotation."""
    if not content:
        raise ImageValidationError("S13", "empty body")
    if len(content) > max_bytes:
        raise ImageValidationError(
            "S13", f"{len(content)} bytes exceeds max_image_bytes={max_bytes}"
        )
    declared = content_type.split(";")[0].strip().lower()
    if declared in _FORBIDDEN_CONTENT_TYPES:
        raise ImageValidationError("S13", f"content-type {declared} is not a raster image")
    if declared and not declared.startswith("image/"):
        raise ImageValidationError("S13", f"content-type {declared} is not an image")
    if content.lstrip()[:5].lower().startswith(b"<svg") or b"<svg" in content[:200].lower():
        raise ImageValidationError("S13", "SVG payload")

    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        with Image.open(io.BytesIO(content)) as probe:
            width, height = probe.size
            if width * height > max_pixels:
                raise ImageValidationError(
                    "S13", f"{width}x{height} exceeds max_image_pixels={max_pixels}"
                )
            if probe.format == "GIF" and getattr(probe, "n_frames", 1) > 1:
                raise ImageValidationError("S13", "animated GIF")
            probe.load()
            image = ImageOps.exif_transpose(probe)
    except ImageValidationError:
        raise
    except Image.DecompressionBombError as exc:
        raise ImageValidationError("S13", f"decompression bomb guard: {exc}") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageValidationError("S13", f"Pillow could not decode: {exc}") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit
    return image


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def dimensions_from_header(data: bytes) -> tuple[int, int] | None:
    """Width/height read from the first bytes of a JPEG/PNG/WebP/GIF, else ``None``.

    Used by the 64 KB ``Range`` probe (plan.md 11.5 step 3): a 60x60 thumbnail (S8)
    or a 3000x100 strip (S6) is rejected after ~2 KB instead of after a full download.
    """
    if len(data) < 12:
        return None
    if data[:2] == b"\xff\xd8":                                   # JPEG
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            if marker == 0xDA:                                     # start of scan
                return None
            if index + 4 > len(data):
                return None
            length = int.from_bytes(data[index + 2 : index + 4], "big")
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
                if index + 9 > len(data):
                    return None
                height = int.from_bytes(data[index + 5 : index + 7], "big")
                width = int.from_bytes(data[index + 7 : index + 9], "big")
                return (width, height)
            if length < 2:
                return None
            index += 2 + length
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:       # PNG
        return (
            int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big"),
        )
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":               # WebP
        chunk = data[12:16]
        if chunk == b"VP8X" and len(data) >= 30:
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return (width, height)
        if chunk == b"VP8 " and len(data) >= 30:
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
            if width and height:
                return (width, height)
        return None
    if data[:3] == b"GIF" and len(data) >= 10:                      # GIF
        return (
            int.from_bytes(data[6:8], "little"),
            int.from_bytes(data[8:10], "little"),
        )
    return None


def probe_verdict(
    dimensions: tuple[int, int] | None,
    *,
    min_short_side: int,
    min_long_side: int,
    max_aspect_ratio: float,
) -> tuple[str, str]:
    """Gate a *probe* dimension pair: returns ``("ok", "")`` or ``("S6"/"S8", detail)``."""
    if dimensions is None:
        return "ok", ""
    width, height = dimensions
    if width <= 0 or height <= 0:
        return "ok", ""
    short_side, long_side = sorted((width, height))
    if short_side < min_short_side or long_side < min_long_side:
        return "S8", (
            f"too small {width}x{height} (need short>={min_short_side}, "
            f"long>={min_long_side})"
        )
    if short_side > 0 and long_side / short_side > max_aspect_ratio:
        return "S6", f"banner shape {width}x{height} (max/min > {max_aspect_ratio})"
    return "ok", ""


def to_rgb(image: Image.Image) -> Image.Image:
    """Flatten onto white and return an RGB image."""
    if image.mode == "RGB":
        return image
    if image.mode in ("RGBA", "LA", "P"):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    return image.convert("RGB")


def _rgb_array(image: Image.Image) -> np.ndarray:
    return np.asarray(to_rgb(image), dtype=np.float64)


# --------------------------------------------------------------------------- #
# S8 / S6
# --------------------------------------------------------------------------- #
def is_large_enough(
    image: Image.Image, *, min_short_side: int, min_long_side: int
) -> bool:
    """Safeguard S8: short side >= min_short_side and long side >= min_long_side."""
    width, height = image.size
    short, long = (width, height) if width <= height else (height, width)
    return short >= min_short_side and long >= min_long_side


def has_extreme_aspect(image: Image.Image, *, max_aspect_ratio: float) -> bool:
    """Safeguard S6: banner-shaped images (ratio above the limit) are rejected."""
    width, height = image.size
    if width <= 0 or height <= 0:
        return True
    ratio = max(width, height) / max(1, min(width, height))
    return ratio > max_aspect_ratio


# --------------------------------------------------------------------------- #
# S7
# --------------------------------------------------------------------------- #
def _ring_bands(array: np.ndarray, width_px: int) -> list[np.ndarray]:
    top = array[:width_px, :, :]
    bottom = array[-width_px:, :, :]
    left = array[width_px:-width_px, :width_px, :]
    right = array[width_px:-width_px, -width_px:, :]
    return [band.reshape(-1, 3) for band in (top, bottom, left, right) if band.size]


def border_is_clean(
    image: Image.Image,
    *,
    ring_ratio: float,
    ring_min_px: int,
    tolerance: float,
    std_max: float,
    min_fraction: float,
) -> bool:
    """Safeguard S7: the whole border ring must be one calm background colour.

    The ring (not just the corners) is measured, so a photo that fills the frame
    with a busy scene fails even when its corners happen to be white.
    """
    array = _rgb_array(image)
    height, width = array.shape[:2]
    ring_px = max(ring_min_px, int(min(width, height) * ring_ratio))
    ring_px = max(1, min(ring_px, max(1, min(width, height) // 2)))
    bands = _ring_bands(array, ring_px)
    if not bands:
        return False
    pixels = np.concatenate(bands, axis=0)
    median = np.median(pixels, axis=0)
    deviation = np.abs(pixels - median).max(axis=1)
    fraction = float(np.mean(deviation <= tolerance))
    if fraction < min_fraction:
        return False
    return all(
        band.shape[0] <= 8 or float(band.std(axis=0).max()) <= std_max for band in bands
    )


def corner_patch_stats(image: Image.Image, ratio: float) -> list[tuple[float, float]]:
    """Per-corner (brightness, saturation) of the mean colour, 0..1 each."""
    array = _rgb_array(image)
    height, width = array.shape[:2]
    size = max(2, int(min(width, height) * ratio))
    patches = [
        array[:size, :size],
        array[:size, -size:],
        array[-size:, :size],
        array[-size:, -size:],
    ]
    stats: list[tuple[float, float]] = []
    for patch in patches:
        mean = patch.reshape(-1, 3).mean(axis=0)
        maximum = float(mean.max()) / 255.0
        minimum = float(mean.min()) / 255.0
        saturation = 0.0 if maximum <= 0 else (maximum - minimum) / maximum
        stats.append((maximum, saturation))
    return stats


def neutral_corner_count(
    image: Image.Image, ratio: float, min_value: float, max_saturation: float
) -> int:
    """Count corners that are bright and near-neutral (white-ish background)."""
    return sum(
        1
        for value, saturation in corner_patch_stats(image, ratio)
        if value >= min_value and saturation <= max_saturation
    )


# --------------------------------------------------------------------------- #
# S9
# --------------------------------------------------------------------------- #
def is_qr_or_barcode(image: Image.Image, url: str = "", *, extra_keywords: Iterable[str] = ()) -> bool:
    """Safeguard S9: filename token **and** a real decode attempt.

    Decoding needs OpenCV; if it is unavailable the filename check still applies and
    the caller sees the reason in the log.
    """
    from url_filters import has_bad_keyword

    if url and has_bad_keyword(url, extra_keywords=tuple(extra_keywords)):
        tokens = set(url.lower().replace("_", "-").split("/"))
        if any("qr" in token or "barcode" in token for token in tokens):
            return True
    try:
        import cv2
    except Exception:                             # pragma: no cover - optional dep
        return False
    gray = np.asarray(image.convert("L"), dtype=np.uint8)
    try:
        data, _points, _straight = cv2.QRCodeDetector().detectAndDecode(gray)
        if data:
            return True
    except Exception:                             # pragma: no cover - cv2 internals
        pass
    detector_cls: Any = getattr(getattr(cv2, "barcode", None), "BarcodeDetector", None)
    if detector_cls is not None:
        try:
            ok, _info, _types, _points = detector_cls().detectAndDecode(gray)
            if ok:
                return True
        except Exception:                         # pragma: no cover - cv2 internals
            pass
    return False


# --------------------------------------------------------------------------- #
# gate runner
# --------------------------------------------------------------------------- #
def _scan_promo_band(
    array: np.ndarray,
    config: PipelineConfig,
) -> tuple[str, int, int] | None:
    """Locate one unambiguous promo strip.  Returns ``(side, band_edge, band_inner)``.

    ``band_edge`` is the outermost strip row, ``band_inner`` the first row beyond the strip
    on the inside.  ``None`` means "leave the image alone".
    """
    height = array.shape[0]
    white = array.min(axis=2) >= config.promo_band_white_level
    row_white = white.mean(axis=1)
    row_mean = array.mean(axis=(1, 2))
    band_row = (row_white <= 1.0 - config.promo_band_min_width_ratio) & (
        row_mean < config.promo_band_dark_max_mean
    )
    max_band = int(height * config.promo_band_max_ratio)

    for side, step, first in (("bottom", -1, height - 1), ("top", 1, 0)):
        index = first
        margin = 0
        while (
            0 <= index < height
            and row_white[index] >= 0.98
            and margin < config.promo_band_edge_margin_max_px
        ):
            index += step
            margin += 1
        if not (0 <= index < height) or not band_row[index]:
            continue
        band_edge = index
        while 0 <= index < height and band_row[index]:
            index += step
        band_inner = index                      # first row past the strip (inside)
        if not 0 <= band_inner < height:
            continue
        band_height = abs(band_inner - band_edge)
        if not config.promo_band_min_height_px <= band_height <= max_band:
            continue
        # the rows just beyond the strip must be bright background: soft shadows are fine,
        # a dark product body is not - that is what stops the cut from eating the product.
        gap = config.promo_band_background_gap_px
        if side == "bottom":
            window = array[max(0, band_inner - gap + 1) : band_inner + 1]
        else:
            window = array[band_inner : band_inner + gap]
        if window.shape[0] < gap:
            continue
        if float(window.mean(axis=(1, 2)).min()) < config.promo_band_gap_min_mean:
            continue
        return side, band_edge, band_inner
    return None


def strip_promo_band(
    image: Image.Image, config: PipelineConfig
) -> tuple[Image.Image, tuple[str, int] | None]:
    """Cut an unambiguous shop promo strip off the product shot (rule id ``S7.band``).

    Shops glue strips like "PWM Single Tub 1250 SA" onto an otherwise clean white product
    photo.  Those strips cross the S7 border ring, so the photo used to be thrown away even
    though the product itself is perfect.  A strip is removed only when *all* of this holds:

    * it sits at the top or bottom edge (a white outer margin of at most
      ``promo_band_edge_margin_max_px`` is allowed),
    * every strip row is at least ``promo_band_min_width_ratio`` non-white across the width
      and darker than ``promo_band_dark_max_mean`` on average,
    * it is at least ``promo_band_min_height_px`` tall and at most ``promo_band_max_ratio``
      of the image,
    * the ``promo_band_background_gap_px`` rows on its inner side are bright
      (``promo_band_gap_min_mean``), so the cut cannot slice through the product.

    Anything less clear-cut is left untouched and S7 rejects it exactly as before.
    Returns ``(image, None)`` when nothing was removed, else ``(cropped, (side, pixels))``.
    """
    if not config.promo_band_strip_enabled:
        return image, None
    width, height = image.size
    if height < 4 * config.promo_band_min_height_px:
        return image, None
    array = np.asarray(image.convert("RGB"), dtype=np.uint8)
    found = _scan_promo_band(array, config)
    if found is None:
        return image, None
    side, _band_edge, band_inner = found
    if side == "bottom":
        cropped = image.crop((0, 0, width, band_inner + 1))
    else:
        cropped = image.crop((0, band_inner, width, height))
    if cropped.size[1] == height:
        return image, None
    return cropped, (side, height - cropped.size[1])


def _scan_seam(
    non_white: np.ndarray, config: PipelineConfig, orientation: str
) -> tuple[str, int, int] | None:
    """Find one trimmable seam in ``orientation`` (``"vertical"`` = columns).

    Returns ``(kind, cut, removed)`` where ``cut`` is the new edge position measured from the
    side being trimmed (so the seam itself is always outside the crop), or ``None``.
    """
    height, width = non_white.shape
    if orientation == "vertical":
        counts = non_white.sum(axis=0)          # per column
        span, size = height, width
    else:
        counts = non_white.sum(axis=1)          # per row
        span, size = width, height
    marked = counts >= config.seam_min_span_ratio * span
    limit = int(size * (1.0 - config.seam_keep_min_ratio))

    start = None
    for index in range(len(marked) + 1):
        inside = index < len(marked) and marked[index]
        if inside and start is None:
            start = index
        elif not inside and start is not None:
            run = index - start
            if run <= config.seam_max_width_px and run <= limit:
                keep_before = non_white[:, :start] if orientation == "vertical" else non_white[:start, :]
                keep_after = non_white[:, index:] if orientation == "vertical" else non_white[index:, :]
                # an empty side counts as zero content (the seam may sit right on the edge)
                before_content = float(keep_before.mean()) if start > 0 else 0.0
                after_content = float(keep_after.mean()) if index < size else 0.0
                if min(before_content, after_content) <= config.seam_offcut_max_content_ratio:
                    # cut away the emptier side - the product decides which side that is.
                    # The seam columns/rows sit between ``start`` and ``index`` and are always
                    # part of what is removed.
                    if before_content <= after_content:
                        return f"{orientation}-start", index, index
                    return f"{orientation}-end", start, size - start
            start = None
    return None


def trim_edge_seams(
    image: Image.Image, config: PipelineConfig
) -> tuple[Image.Image, list[tuple[str, int]]]:
    """Trim thin layout seams that touch two edges (rule id ``S7.seam``).

    Some shop assets carry a divider line (1-8 px) from top to bottom or left to right.  It
    crosses the S7 border ring, so a flawless product photo used to be thrown away.  A seam
    is only trimmed when it spans at least ``seam_min_span_ratio`` of the picture, is at most
    ``seam_max_width_px`` wide, the sliver beyond it is almost pure background (at most
    ``seam_offcut_max_content_ratio`` non-white) and the result keeps at least
    ``seam_keep_min_ratio`` of the original picture (checked across all trims together).
    Otherwise the image is returned untouched.  Returns ``(image, [(kind, pixels), ...])``.
    """
    if not config.seam_trim_enabled:
        return image, []
    original_size = image.size
    current = image
    trims: list[tuple[str, int]] = []

    for _ in range(config.seam_max_passes):
        width, height = current.size
        array = np.asarray(current.convert("RGB"), dtype=np.uint8)
        non_white = array.min(axis=2) < config.promo_band_white_level
        found = _scan_seam(non_white, config, "vertical") or _scan_seam(
            non_white, config, "horizontal"
        )
        if found is None:
            break
        kind, cut, removed = found
        cropped = (
            current.crop((cut, 0, width, height))
            if kind == "vertical-start"
            else current.crop((0, 0, cut, height))
            if kind == "vertical-end"
            else current.crop((0, cut, width, height))
            if kind == "horizontal-start"
            else current.crop((0, 0, width, cut))
        )
        if cropped.size == current.size:
            break
        kept = (cropped.size[0] * cropped.size[1]) / (original_size[0] * original_size[1])
        if kept < config.seam_keep_min_ratio:
            break                                # too much would be lost - leave it alone
        current = cropped
        trims.append((kind, removed))
    if not trims:
        return image, []
    return current, trims


def official_gallery_pass(image: Image.Image, config: PipelineConfig) -> bool:
    """S7 acceptance for an image from a verified official page's curated gallery.

    The strict S7 test demands a clean border ring, which a manufacturer's own studio shot
    fails whenever the product fills the frame (top view, open lid).  Such an image is still
    a genuine white-background product photo, so it is accepted when its corners are neutral
    studio background - the corners are the evidence that this is not a lifestyle or banner
    picture.  Retail pages never get this acceptance: it is scoped by the caller to an
    official brand domain *and* a curated gallery source.
    """
    if not config.official_gallery_relaxed_s7:
        return False
    corners = neutral_corner_count(
        image,
        config.corner_patch_ratio,
        config.neutral_bright_min_value,
        config.neutral_bright_max_saturation,
    )
    return corners >= config.official_gallery_min_neutral_corners


def studio_background_pass(image: Image.Image, config: PipelineConfig) -> bool:
    """Universal S7 rescue: a studio shot whose product touches the frame edges.

    The strict border-ring test assumes the outer ring is pure background, which a genuine
    white-background photo violates whenever the product reaches the top/bottom edges (a
    top-down view, a tall appliance).  The four corners are the reliable evidence that the
    backdrop is white studio rather than a lifestyle or banner scene, so when all of them are
    neutral the image is accepted no matter which host served it.  A busy frame with only
    tiny white corner patches never reaches ``studio_rescue_min_neutral_corners`` at
    ``corner_patch_ratio``, so lifestyle and banner images stay rejected.
    """
    if not config.studio_background_rescue:
        return False
    corners = neutral_corner_count(
        image,
        config.corner_patch_ratio,
        config.neutral_bright_min_value,
        config.neutral_bright_max_saturation,
    )
    return corners >= config.studio_rescue_min_neutral_corners


def run_quality_gates(
    image: Image.Image,
    config: PipelineConfig,
    *,
    official_gallery: bool = False,
) -> tuple[str, str]:
    """Run the geometric gates in the required order; stop at the first failure.

    Order (spec 7.2): S13 (already done on the bytes) -> S8 -> S6 -> S9 -> S7.
    ``official_gallery`` relaxes **only** the S7 border-ring test, and only through
    :func:`official_gallery_pass`; every other gate is unchanged.  Returns
    ``("ok", "")`` or ``(rule_id, detail)``.
    """
    if not is_large_enough(
        image, min_short_side=config.min_short_side, min_long_side=config.min_long_side
    ):
        detail = (
            f"too small {image.size[0]}x{image.size[1]} "
            f"(need short>={config.min_short_side}, long>={config.min_long_side})"
        )
        return ("S8", detail)
    if has_extreme_aspect(image, max_aspect_ratio=config.max_aspect_ratio):
        width, height = image.size
        ratio = max(width, height) / max(1, min(width, height))
        return ("S6", f"aspect {ratio:.2f} > {config.max_aspect_ratio}")
    if is_qr_or_barcode(image):
        return "S9", "QR/barcode detected"
    ring_clean = border_is_clean(
        image,
        ring_ratio=config.border_ring_ratio,
        ring_min_px=config.border_ring_min_px,
        tolerance=config.border_bg_tolerance,
        std_max=config.border_std_max,
        min_fraction=config.border_clean_min_fraction,
    )
    relaxed_shot = (official_gallery and official_gallery_pass(image, config)) or (
        studio_background_pass(image, config)
    )
    if not ring_clean and not relaxed_shot:
        return "S7", "border ring is not a clean background"
    corners = neutral_corner_count(
        image,
        config.corner_patch_ratio,
        config.neutral_bright_min_value,
        config.neutral_bright_max_saturation,
    )
    if corners < config.min_neutral_corners:
        return "S7", f"only {corners} neutral corner(s), need {config.min_neutral_corners}"
    return "ok", ""

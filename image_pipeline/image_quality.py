"""Byte-level and pixel-level image gates (S6, S7, S8, S9, S13).

Pure functions: they take bytes or a PIL image plus explicit thresholds, so every
gate is testable offline with synthetic images.
"""

from __future__ import annotations

import io
import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass
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
    """S13 failure: the bytes are not a usable raster image.

    ``salvageable`` marks the *only* two rejects that are a quality judgement rather
    than a broken payload: the lossy byte floor and the bpp floor.  The image decoded
    fine - it is merely compressed.  The pipeline may publish such an image as a last
    resort when nothing better exists (owner decision 2026-10-08: "jab kuch na bachey
    toh kam quality wali hi le lon").
    """

    def __init__(self, rule_id: str, detail: str, salvageable: bool = False) -> None:
        super().__init__(f"{rule_id}: {detail}")
        self.rule_id = rule_id
        self.detail = detail
        self.salvageable = salvageable


# --------------------------------------------------------------------------- #
# S13 - bytes must really be an image
# --------------------------------------------------------------------------- #
def _looks_like_raster(content: bytes) -> bool:
    """Magic-byte sniff for servers that mislabel rasters (``webp``, ``binary``)."""
    head = content[:16]
    if head[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return True
    return head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8", b"GIF8", b"BM"))


def validate_image_bytes(
    content: bytes,
    content_type: str = "",
    *,
    max_bytes: int,
    max_pixels: int,
    min_lossy_bytes: int = 0,
    min_lossy_bytes_per_pixel: float = 0.0,
    blur_sharpness_floor: float = 0.0,
) -> Image.Image:
    """Safeguard S13: size, content type, real decode, bomb guard, EXIF rotation.

    For lossy formats (JPEG, WebP), also validates minimum payload size and byte
    density (bytes per pixel) to reject heavily-compressed, blurry thumbnails - but
    only when the picture is *actually* blurry: a smooth studio gradient compresses
    extremely well, so a below-floor payload that still scores at or above
    ``blur_sharpness_floor`` on :func:`sharpness_score` passes (owner 2026-10-09:
    "jahan blur hay wahin lagana hay" - the golden Haier hero at bpp 0.018 is crisp).
    """
    if not content:
        raise ImageValidationError("S13", "empty body")
    if len(content) > max_bytes:
        raise ImageValidationError(
            "S13", f"{len(content)} bytes exceeds max_image_bytes={max_bytes}"
        )
    declared = content_type.split(";")[0].strip().lower()
    if declared in _FORBIDDEN_CONTENT_TYPES:
        raise ImageValidationError("S13", f"content-type {declared} is not a raster image")
    if declared and not declared.startswith("image/") and not _looks_like_raster(content):
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
            lossy_detail = ""
            if probe.format in ("JPEG", "WEBP"):
                if min_lossy_bytes > 0 and len(content) < min_lossy_bytes:
                    lossy_detail = (
                        f"lossy payload {len(content)} bytes below minimum {min_lossy_bytes}"
                    )
                elif min_lossy_bytes_per_pixel > 0.0 and width > 0 and height > 0:
                    bpp = len(content) / float(width * height)
                    if bpp < min_lossy_bytes_per_pixel:
                        lossy_detail = (
                            f"over-compressed blurry image "
                            f"(bpp={bpp:.4f} < {min_lossy_bytes_per_pixel:.4f})"
                        )
            probe.load()
            # Flattened here, once, because `RGBA.convert("RGB")` composites transparency onto
            # BLACK and every studio gate then reads the page's CSS plate as a dark product body.
            # Brand cut-outs (Orient, Haier, Dawlance PNGs) were judged against the wrong ground -
            # see to_rgb's note.
            image = to_rgb(ImageOps.exif_transpose(probe))
            if (
                lossy_detail
                and blur_sharpness_floor > 0.0
                and sharpness_score(image) >= blur_sharpness_floor
            ):
                return image
            if lossy_detail:
                raise ImageValidationError("S13", lossy_detail, salvageable=True)
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
    """Flatten onto white and return an RGB image.

    The only correct way to drop a mode for the studio gates.  ``RGBA.convert("RGB")``
    composites transparency onto **black**, and a brand cut-out PNG (Orient's
    ``15-ton-platinium-matt-white-dc-inverter-230fb832.png`` of 2026-10-10, whose plate colour
    ``#e5e5e5`` lives in the page's CSS) then measures as "product area is dark (mean 0.33) -
    conflicts with target 'white'": S16 rejected the right picture, and the same mistake fed
    black edges into S14/S20/S21, the promo-band trim and the marketing-panel cut.
    """
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
        band.shape[0] <= 8 or float(np.max(band.std(axis=0))) <= std_max for band in bands
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
        maximum = float(np.max(mean)) / 255.0
        minimum = float(np.min(mean)) / 255.0
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
# S14 - monochrome logo / emblem gate
# --------------------------------------------------------------------------- #
def is_monochrome_emblem(image: Image.Image, config: PipelineConfig) -> bool:
    """Safeguard S14: a flat monochrome badge on a non-white ground is a logo.

    Live 2026-10-09: Haier's registry gallery mixed the brand's "AI + leaf" emblem -
    dark-grey strokes and a ring on a light-grey disc - into the product images, and
    it sailed through S7 because its border ring *is* one calm colour and its four
    corners are neutral.  The signals that together name it: the picture is entirely
    achromatic (no chroma anywhere, unlike any appliance photo), its ground is not
    the white studio backdrop (corner value below the white level), it carries
    stroke-share dark ink (not a product body's coverage), and - the live false
    positive that same day, Kenwood's perfect white unit on a grey studio ground -
    it has *nothing brighter than the ground*, which no real product photo lacks.
    """
    if not config.logo_gate_enabled:
        return False
    thumb = to_rgb(image).resize((128, 128), Image.Resampling.BOX)
    arr = np.asarray(thumb, dtype=np.float32) / 255.0
    maximum = arr.max(axis=-1)
    delta = maximum - arr.min(axis=-1)
    if float((delta < 0.08).mean()) < config.logo_achromatic_min_fraction:
        return False
    # The ground is what every corner shows; a single white corner already proves a
    # studio backdrop (a promo strip may darken two corners, an emblem darkens none).
    corner = max(
        float(maximum[:16, :16].mean()),
        float(maximum[:16, -16:].mean()),
        float(maximum[-16:, :16].mean()),
        float(maximum[-16:, -16:].mean()),
    )
    if corner >= config.logo_background_max_value:
        return False
    # A real product on a grey ground always carries a body *brighter* than that
    # ground (the white Kenwood unit on the grey studio shot, live 2026-10-09 -
    # the first S14 draft ate exactly that perfect official picture).  An emblem
    # has nothing brighter than its disc.
    bright = float((maximum > corner + 0.05).mean())
    if bright >= config.logo_bright_max_fraction:
        return False
    ink = float((maximum < config.logo_ink_max_value).mean())
    if ink < config.logo_ink_min_fraction:
        return False
    # An emblem's ink is stroke-work (a few percent to a quarter of the frame);
    # a dark appliance body covers far more and is a product, not a logo.
    return ink <= config.logo_ink_max_fraction


# --------------------------------------------------------------------------- #
# S9
# --------------------------------------------------------------------------- #
_DETECTOR_TLS = threading.local()


def _get_qr_detector() -> Any:
    """Thread-isolated QRCodeDetector instance."""
    if not hasattr(_DETECTOR_TLS, "qr_detector"):
        detector: Any = None
        try:
            import cv2

            detector = cv2.QRCodeDetector()
        except Exception:
            pass
        _DETECTOR_TLS.qr_detector = detector
    return _DETECTOR_TLS.qr_detector


def _get_barcode_detector() -> Any:
    """Thread-isolated BarcodeDetector instance with missing module sentinel caching."""
    if not hasattr(_DETECTOR_TLS, "barcode_detector"):
        detector: Any = None
        try:
            import cv2

            detector_cls: Any = getattr(getattr(cv2, "barcode", None), "BarcodeDetector", None)
            if detector_cls is not None:
                detector = detector_cls()
        except Exception:
            pass
        _DETECTOR_TLS.barcode_detector = detector
    return _DETECTOR_TLS.barcode_detector


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

    qr_detector = _get_qr_detector()
    barcode_detector = _get_barcode_detector()
    if qr_detector is None and barcode_detector is None:
        return False

    gray = np.asarray(image.convert("L"), dtype=np.uint8)
    if qr_detector is not None:
        try:
            data, _points, _straight = qr_detector.detectAndDecode(gray)
            if data:
                return True
        except Exception:                             # pragma: no cover - cv2 internals
            pass
    if barcode_detector is not None:
        try:
            ok, _info, _types, _points = barcode_detector.detectAndDecode(gray)
            if ok:
                return True
        except Exception:                             # pragma: no cover - cv2 internals
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
        if float(np.min(window.mean(axis=(1, 2)))) < config.promo_band_gap_min_mean:
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
    array = np.asarray(to_rgb(image), dtype=np.uint8)
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
        array = np.asarray(to_rgb(current), dtype=np.uint8)
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
        config.official_neutral_bright_min_value,
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


# --------------------------------------------------------------------------- #
# S19 - branded template guard (Option 2)
# --------------------------------------------------------------------------- #
def branded_template_reason(image: Image.Image, config: PipelineConfig) -> str | None:
    """Safeguard S19: a store's own themed template is not a product photo.

    Shops such as iShopping serve "template" art - the product pasted onto the
    shop's brand colours with its logo and design added.  Compositing those breaks
    isolation and puts one shop's branding inside another shop's template, so they
    must be skipped while clean product photos pass.

    The evidence is the background only: the four corner patches, the same window
    the S7 studio rescue trusts.  A product photo's corners are white or light
    neutral (saturation ~0.01-0.05); a theme frame shows saturated brand colour in
    its corners.  The product body is never measured - a colourful product on a
    white sheet passes, and a product that fills the frame is judged by the S7
    corner bar exactly as in Option 1, never here.  Returns ``None`` when the
    image passes, or a one-sentence reason a human can act on.
    """
    if not config.branded_template_guard_enabled:
        return None
    saturated = sum(
        1
        for _value, saturation in corner_patch_stats(image, config.corner_patch_ratio)
        if saturation >= config.branded_corner_min_saturation
    )
    if saturated < config.branded_min_saturated_corners:
        return None
    return (
        f"store template: {saturated} of 4 corners are saturated brand colour "
        f"(themed shop background, not a clean product photo)"
    )


def run_quality_gates(
    image: Image.Image,
    config: PipelineConfig,
    *,
    official_gallery: bool = False,
    panel_cut: tuple[str, int] | None = None,
) -> tuple[str, str]:
    """Run the geometric gates in the required order; stop at the first failure.

    Order (spec 7.2): S13 (already done on the bytes) -> S8 -> S6 -> S9 -> S7.
    ``official_gallery`` relaxes **only** the S7 border-ring test, and only through
    :func:`official_gallery_pass`; every other gate is unchanged.  ``after_panel_cut`` skips
    only S20's painted-artwork branch, for an image whose marketing panel this pipeline itself
    just cut off (see :func:`is_marketing_layout`).  Returns
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
    if is_monochrome_emblem(image, config):
        return "S14", "monochrome logo/emblem on a grey ground - not a product photo"
    if is_marketing_layout(image, config, panel_cut=panel_cut):
        is_studio_product = False
        if official_gallery:
            _ink_frac, _span, _texture = layout_metrics(image)
            if _texture < config.marketing_max_texture:
                ring_clean = border_is_clean(
                    image,
                    ring_ratio=config.border_ring_ratio,
                    ring_min_px=config.border_ring_min_px,
                    tolerance=config.border_bg_tolerance,
                    std_max=config.border_std_max,
                    min_fraction=config.border_clean_min_fraction,
                )
                if ring_clean:
                    from compositing import isolate_main_product
                    isolated, degraded = isolate_main_product(image, config)
                    iw, ih = isolated.size
                    ratio = max(iw, ih) / max(1, min(iw, ih))
                    if not degraded and ratio <= config.max_aspect_ratio and min(iw, ih) >= config.min_short_side:
                        is_studio_product = True
        if not is_studio_product:
            return "S20", marketing_layout_reason(image, config)
    diagram = diagram_sheet_reason(image, config)
    if diagram:
        return "S21", diagram
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
    if corners < config.min_neutral_corners and official_gallery:
        # Official studios print light-grey grounds (~0.85) where retail shots are
        # white; the page already proved the model (S1), so the brand's own gallery
        # gets the calmer corner bar (owner 2026-10-09).
        corners = neutral_corner_count(
            image,
            config.corner_patch_ratio,
            config.official_neutral_bright_min_value,
            config.neutral_bright_max_saturation,
        )
    if corners < config.min_neutral_corners:
        return "S7", f"only {corners} neutral corner(s), need {config.min_neutral_corners}"
    return "ok", ""


# --------------------------------------------------------------------------- #
# S16 - Visual Color Gate (Colorway Verification)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ForegroundColorProfile:
    """Foreground chromaticity sampled from a studio product shot."""

    is_chromatic: bool
    chromatic_ratio: float
    median_hue: float
    mean_saturation: float
    mean_value: float
    dominant_family: str


def extract_foreground_color_profile(image: Image.Image) -> ForegroundColorProfile:
    """Sample foreground chromaticity in < 2ms using a 96x96 downsample and vectorized HSV."""
    thumb = image.resize((96, 96), Image.Resampling.BOX)
    arr = np.asarray(to_rgb(thumb), dtype=np.float32) / 255.0

    max_c = arr.max(axis=-1)
    min_c = arr.min(axis=-1)
    delta = max_c - min_c

    # Studio background mask (bright and near-neutral)
    bg_mask = (max_c > 0.90) & (delta < 0.08)

    h, w = arr.shape[:2]
    central_mask = np.zeros((h, w), dtype=bool)
    central_mask[int(h * 0.12) : int(h * 0.88), int(w * 0.12) : int(w * 0.88)] = True

    fg_mask = (~bg_mask) & central_mask
    if not np.any(fg_mask):
        return ForegroundColorProfile(
            is_chromatic=False,
            chromatic_ratio=0.0,
            median_hue=0.0,
            mean_saturation=0.0,
            mean_value=1.0,
            dominant_family="neutral",
        )

    fg = arr[fg_mask]
    fg_max = fg.max(axis=-1)
    fg_min = fg.min(axis=-1)
    fg_delta = fg_max - fg_min

    chromatic_mask = fg_delta > 0.10
    chromatic_ratio = float(np.mean(chromatic_mask))
    mean_val = float(np.mean(fg_max))

    r, g, b = fg[:, 0], fg[:, 1], fg[:, 2]
    hues = np.zeros_like(r)
    nz = fg_delta > 0
    idx_r = (r == fg_max) & nz
    hues[idx_r] = ((g[idx_r] - b[idx_r]) / fg_delta[idx_r]) % 6.0
    idx_g = (g == fg_max) & nz
    hues[idx_g] = ((b[idx_g] - r[idx_g]) / fg_delta[idx_g]) + 2.0
    idx_b = (b == fg_max) & nz
    hues[idx_b] = ((r[idx_b] - g[idx_b]) / fg_delta[idx_b]) + 4.0
    hues = hues * 60.0

    if chromatic_ratio >= 0.15:
        med_hue = float(np.median(hues[chromatic_mask]))
        mean_sat = float(np.mean(fg_delta[chromatic_mask]))
        if 260.0 <= med_hue <= 330.0:
            dom = "purple"
        elif med_hue >= 335.0 or med_hue <= 20.0:
            dom = "red"
        elif 25.0 <= med_hue <= 65.0:
            dom = "gold"
        elif 180.0 <= med_hue <= 255.0:
            dom = "blue"
        elif 75.0 <= med_hue <= 160.0:
            dom = "green"
        else:
            dom = "chromatic"
        return ForegroundColorProfile(
            is_chromatic=True,
            chromatic_ratio=chromatic_ratio,
            median_hue=med_hue,
            mean_saturation=mean_sat,
            mean_value=mean_val,
            dominant_family=dom,
        )

    return ForegroundColorProfile(
        is_chromatic=False,
        chromatic_ratio=chromatic_ratio,
        median_hue=0.0,
        mean_saturation=float(np.mean(fg_delta)),
        mean_value=mean_val,
        dominant_family="neutral",
    )


def body_shape(
    image: Image.Image, *, sample: int = 256, close_px: int = 9, out_size: int = 64
) -> np.ndarray | None:
    """The product's largest solid body, as a silhouette normalised to its own box.

    A grey-patch correlation compares *appearance*, so it cannot tell a re-served copy
    from a second angle: a 600px and an 800px copy of one photo correlate 0.97, while
    the same AC with its louver flap open correlates 0.45.  The body *shape* does
    separate them - and because only the largest connected body is kept, a coloured badge
    pasted into the corner (the ``T3`` sticker that made a duplicate look different) is
    outside the comparison instead of inside it.

    Returns ``None`` when there is no measurable body, so the caller falls back to the
    weaker signal rather than guessing.
    """
    try:
        from scipy import ndimage
    except Exception:                                       # pragma: no cover - optional dep
        return None
    array = np.asarray(
        to_rgb(image).resize((sample, sample), Image.Resampling.LANCZOS), dtype=np.float32
    ) / 255.0
    edge = max(4, sample // 18)
    corners = np.concatenate(
        [
            array[:edge, :edge].reshape(-1, 3),
            array[:edge, -edge:].reshape(-1, 3),
            array[-edge:, :edge].reshape(-1, 3),
            array[-edge:, -edge:].reshape(-1, 3),
        ],
        axis=0,
    )
    background = np.median(corners, axis=0)
    mask = np.asarray(np.abs(array - background).max(axis=2) > 0.06, dtype=bool)
    if int(mask.sum()) < 200:
        return None
    structure = np.ones((close_px, close_px), dtype=bool)
    solid = ndimage.binary_fill_holes(ndimage.binary_closing(mask, structure=structure))
    labels, count = ndimage.label(solid)
    if count == 0:
        return None
    sizes = ndimage.sum(solid, labels, index=range(1, count + 1))
    body = labels == (int(np.argmax(sizes)) + 1)
    rows, cols = np.nonzero(body)
    crop = body[int(rows.min()) : int(rows.max()) + 1, int(cols.min()) : int(cols.max()) + 1]
    if crop.size == 0:
        return None
    small = Image.fromarray((crop * 255).astype(np.uint8)).resize(
        (out_size, out_size), Image.Resampling.BILINEAR
    )
    return np.asarray(small, dtype=np.float32) / 255.0


def shape_overlap(one: np.ndarray | None, other: np.ndarray | None) -> float:
    """Intersection over union of two normalised silhouettes; 0.0 when either is unknown."""
    if one is None or other is None or one.shape != other.shape:
        return 0.0
    left = one > 0.5
    right = other > 0.5
    union = int((left | right).sum())
    return float((left & right).sum() / union) if union else 0.0


def sharpness_score(image: Image.Image) -> float:
    """Contrast-normalised Laplacian variance on a 256px grey downsample.

    Blur detector for the last-resort salvage path only.  Normalising by the
    image's own std is what makes it a *blur* metric instead of a contrast
    metric: a pale champagne fridge on a white background is low-contrast but
    sharp, and must not read as blurred (first attempt failed exactly this -
    raw Laplacian variance scored gold 68 vs maroon 523).  Measured margins:
    crisp studio shots 0.04-0.06 (even at jpeg q12), upscaled thumbnails and
    smeared shots 0.00-0.01, so the 0.025 floor sits 2-4x away from both sides
    (owner 2026-10-08: salvaged Haier pictures were "puri blurred").
    """
    grey = np.asarray(
        image.convert("L").resize((256, 256), Image.Resampling.BOX), dtype=np.float64
    )
    std = grey.std()
    if std < 1e-6:
        return 0.0
    grey = (grey - grey.mean()) / std
    laplacian = (
        grey[1:-1, 2:]
        + grey[1:-1, :-2]
        + grey[2:, 1:-1]
        + grey[:-2, 1:-1]
        - 4 * grey[1:-1, 1:-1]
    )
    return float(laplacian.var())


def is_canvas_blurry(
    canvas_image: Image.Image,
    config: PipelineConfig,
) -> tuple[bool, float, float]:
    """Safeguard S13.blur: catch upscaled/blurry images whose high-frequency spectral energy is depleted.

    Evaluates the FFT spectral energy in the upper frequencies (r > 0.35 * min(h, w))
    and product edge gradient sharpness.
    """
    if not config.canvas_blur_guard_enabled:
        return False, 1.0, 100.0

    arr = np.asarray(canvas_image.convert("L"), dtype=np.float64)
    h, w = arr.shape
    prod = arr < 250
    if prod.sum() < 500:
        return False, 1.0, 100.0

    f = np.fft.fftshift(np.abs(np.fft.fft2(arr)))
    r_high = min(h, w) * 0.35
    y, x = np.ogrid[:h, :w]
    high_mask = (x - w // 2) ** 2 + (y - h // 2) ** 2 > r_high ** 2
    total_energy = float(f.sum())
    if total_energy <= 1e-6:
        return False, 1.0, 100.0
    hf_energy = float(f[high_mask].sum() / total_energy)

    gy, gx = np.gradient(arr)
    grad = np.sqrt(gx ** 2 + gy ** 2)
    edge_pixels = prod & (grad > 20)
    edge_sharpness = float(grad[edge_pixels].mean()) if edge_pixels.sum() > 50 else 0.0

    is_blurry = bool(
        hf_energy < config.canvas_blur_hf_threshold
        and edge_sharpness < config.canvas_blur_edge_threshold
    )
    return is_blurry, hf_energy, edge_sharpness


def is_hue_in_arc(hue: float, start: float, end: float) -> bool:
    """Whether hue in degrees (0-360) falls within circular arc [start, end]."""
    hue = float(hue) % 360.0
    start = float(start) % 360.0
    end = float(end) % 360.0
    if start <= end:
        return start <= hue <= end
    return hue >= start or hue <= end


def verify_image_color(
    image: Image.Image,
    target_color_word: str,
    *,
    official: bool = False,
    exempt_chroma_on_official: bool = True,
    min_saturation: float = 0.22,
    wash_max_value: float = 0.95,
) -> tuple[bool, str]:
    """Safeguard S16: does the image's foreground chromaticity contradict the target colour?

    Fails open (returns True) when target_color_word is empty or not a known colour.
    Returns (False, reason) when a strong chromatic foreground contradicts the target.

    ``official`` marks a page owned by the brand itself (``host_is_official``).  A brand's
    own hero is the definitive picture of that exact variant, so on such a page S16 keeps
    the two signals that actually catch a wrong-colour sibling - the brightness conflict
    and the dark-body conflict, both measured on the product area - and gives up only the
    third one, *chromatic dominance*, which cannot tell a coloured product from a coloured
    studio wash.  ``exempt_chroma_on_official=False`` restores the old, stricter behaviour.
    A non-empty detail paired with ``True`` means "would have been rejected, exempted
    because this is the brand's page" - so the run can log it instead of going silent.
    """
    if not target_color_word or not target_color_word.strip():
        return True, ""

    from variant_terms import COLOUR_FAMILIES

    target_norm = target_color_word.strip().lower()
    target_family = next(
        (fam for fam, synonyms in COLOUR_FAMILIES.items() if target_norm in synonyms or target_norm == fam),
        "",
    )
    if not target_family:
        return True, ""

    # Achromatic targets (black/white/grey rows) are judged by the product area
    # itself, not the hue family table: the foreground mask throws the near-white
    # body away as "background", so a white/gold unit on a black row slipped
    # through the old chroma-only rule (EcoStar Duke).  Two signals, both
    # measured over the central region: (1) brightness conflict - a bright unit
    # on a black row, a dark unit on a white row; (2) chromatic dominance - the
    # product area is *mostly* a saturated colour (a green fridge on a white
    # row).  A small accent (blue control panel on a white dispenser) covers
    # too little of the centre to fire either rule.
    if target_family in ("black", "white", "grey"):
        thumb = to_rgb(image).resize((64, 64), Image.Resampling.BOX)
        arr = np.asarray(thumb, dtype=np.float32) / 255.0
        central = arr[7:57, 7:57]
        if central.size == 0:
            return True, ""
        c_max = central.max(axis=-1)
        c_delta = c_max - central.min(axis=-1)
        central_mean = float(c_max.mean())
        bg = (c_max > 0.90) & (c_delta < 0.08)
        product = ~bg
        product_frac = float(np.mean(product))
        product_mean = float(c_max[product].mean()) if product.any() else 1.0
        dark_frac = float(np.mean(c_max < 0.45))
        # A colour, not a tint.  The old test was "channel spread > 0.10", which cannot
        # separate a coloured product from a coloured studio background: the owner's Orient
        # Frost hero of 2026-10-10 - a white unit on a cool plate - measured 60 %
        # "chromatic" and the brand's own picture was thrown away.  Saturation (spread /
        # value) plus a brightness cap is what separates them: a studio wash is bright and
        # weakly saturated (0.01 of the product area on a #c9d9f2 plate, where the old test
        # said 0.45), a coloured body is neither (0.61-0.62 for purple/red/blue/green/gold).
        # The bar is 0.22 and not higher because a golden body sits at saturation 0.24 - a
        # 0.30 bar re-opens the gold-on-white hole this gate was written for.
        saturation = np.divide(c_delta, np.maximum(c_max, 1e-6))
        chromatic = (saturation > min_saturation) & (c_max < wash_max_value)
        chromatic_coverage = float(np.mean(chromatic & product))
        if target_family == "black" and central_mean > 0.75 and dark_frac < 0.08:
            # No dark body anywhere in the product area - a white/bright unit on a
            # black row.  The dark_frac guard keeps a *correct* small black unit
            # (few dark pixels, lots of white background) from being rejected.
            return False, (
                f"product area is bright (mean {central_mean:.2f}, "
                f"dark coverage {dark_frac:.0%}) - "
                f"conflicts with target '{target_color_word}' (family black)"
            )
        if target_family == "white":
            if central_mean < 0.45:
                return False, (
                    f"product area is dark (mean {central_mean:.2f}) - "
                    f"conflicts with target '{target_color_word}' (family white)"
                )
            if product_frac >= 0.30 and product_mean < 0.30:
                # A large, near-black body dominates the frame (slim wide unit on a
                # white row survives the plain mean test because white background
                # outnumbers it).
                return False, (
                    f"product area is dominated by a dark body "
                    f"(coverage {product_frac:.0%}, mean {product_mean:.2f}) - "
                    f"conflicts with target '{target_color_word}' (family white)"
                )
        if chromatic_coverage >= 0.45:
            reason = (
                f"product area is a saturated colour ({chromatic_coverage:.0%} of it at "
                f"saturation > {min_saturation:.2f}) - conflicts with target "
                f"'{target_color_word}' (family {target_family})"
            )
            if official and exempt_chroma_on_official:
                # Only this branch.  A dark body on a white row, or a bright body on a
                # black row, is a wrong *product* and still rejects on a brand page too -
                # that is how the Haier official-CDN dark-body catch of 2026-10-09 stays
                # alive.  What a brand page is never judged on is wash dominance, because
                # the tint lives in the brand's own studio setup, not in the variant.
                return True, (
                    f"{reason}; not enforced - this is the brand's own page, whose hero "
                    "defines the variant's colour"
                )
            return False, reason
        return True, ""

    profile = extract_foreground_color_profile(image)
    if not profile.is_chromatic:
        return True, ""

    # A small chromatic accent must not condemn the whole image (a gold handle on
    # a maroon fridge): a family conflict needs the foreground to be *mostly*
    # that colour.
    if profile.chromatic_ratio < 0.45:
        return True, ""

    # Conflicting chromatic pairings between distinct coloured appliance variants
    # Covering all vocabulary families from variant_terms.COLOUR_FAMILIES
    conflicts: dict[str, set[str]] = {
        "gold": {"purple", "red", "blue", "green", "pink"},
        "red": {"purple", "gold", "blue", "green", "yellow", "orange"},
        "purple": {"red", "gold", "blue", "green", "yellow", "orange"},
        "blue": {"red", "purple", "gold", "green", "yellow", "orange"},
        "green": {"red", "purple", "gold", "blue", "yellow", "orange"},
        "white": {"purple", "red", "blue", "green", "gold", "orange", "yellow", "pink", "brown", "chromatic"},
        "black": {"purple", "red", "blue", "green", "gold", "orange", "yellow", "pink", "brown", "chromatic"},
        "grey": {"purple", "red", "blue", "green", "gold", "orange", "yellow", "pink", "brown", "chromatic"},
        "beige": {"purple", "red", "blue", "green", "pink"},
        "brown": {"purple", "blue", "green"},
        "yellow": {"purple", "red", "blue", "green"},
        "orange": {"purple", "blue", "green"},
        "pink": {"blue", "green", "gold"},
    }

    # Dynamic continuous circular hue arcs for chromatic appliance families.
    # Instead of brittle discrete bins and hardcoded one-off string checks, this models
    # the continuous physical spectrum of appliances and studio lighting:
    # - Green smoothly spans olive (70°) through forest, mint, emerald, and cyan/teal sheen (210°)
    # - Red wraps circularly (305° to 25°, or 285° on official brand heroes) spanning wine,
    #   burgundy, maroon, crimson, and red
    # - Purple spans violet (255°) through magenta and warm studio glass reflections (up to 25°)
    # - Blue spans cyan/teal (180°) through sky blue, cobalt, and deep navy (260°)
    # - Gold/Yellow spans champagne, bronze, gold, and warm amber (25° to 65°)
    # Standard W3C/chromatic shade centroids on the 360° color wheel:
    # Allows any known shade (emerald, burgundy, mint, teal, maroon, copper, etc.) to dynamically
    # anchor its own natural hue spectrum without brittle one-off string checks.
    SHADE_CENTROIDS: dict[str, float] = {
        "burgundy": 310.0,
        "wine": 310.0,
        "maroon": 335.0,
        "ruby": 350.0,
        "crimson": 345.0,
        "cherry": 350.0,
        "rose": 330.0,
        "coral": 15.0,
        "emerald": 165.0,
        "emerald green": 160.0,
        "teal": 180.0,
        "mint": 150.0,
        "olive": 80.0,
        "cyan": 180.0,
        "navy": 230.0,
        "sky blue": 205.0,
        "azure": 210.0,
        "amber": 40.0,
        "copper": 25.0,
        "bronze": 35.0,
        "champagne": 45.0,
    }

    target_centroid = next(
        (cent for shade, cent in SHADE_CENTROIDS.items() if shade in target_norm),
        None,
    )
    if target_centroid is not None:
        diff = abs(profile.median_hue - target_centroid) % 360.0
        if min(diff, 360.0 - diff) <= 55.0:
            return True, ""

    red_min = 285.0 if official else 305.0
    chromatic_arcs: dict[str, tuple[float, float]] = {
        "green": (70.0, 210.0),
        "blue": (180.0, 260.0),
        "purple": (255.0, 25.0),
        "red": (red_min, 25.0),
        "gold": (25.0, 65.0),
        "yellow": (25.0, 65.0),
        "orange": (15.0, 45.0),
        "pink": (305.0, 355.0),
    }

    if target_family in chromatic_arcs:
        arc_start, arc_end = chromatic_arcs[target_family]
        if is_hue_in_arc(profile.median_hue, arc_start, arc_end):
            return True, ""

    banned = conflicts.get(target_family, set())
    if profile.dominant_family in banned:
        return False, (
            f"foreground colour ({profile.dominant_family}, hue {profile.median_hue:.0f}°) "
            f"conflicts with target '{target_color_word}' (family {target_family})"
        )

    return True, ""


# --------------------------------------------------------------------------- #
# S20 - full-bleed marketing artwork (the white-ground sibling of S14)
# --------------------------------------------------------------------------- #
#: How far a pixel must sit from pure studio white to count as artwork.
_LAYOUT_INK_TOLERANCE = 6
#: An artwork box must be this close to the whole frame to be called full-bleed.
_LAYOUT_FULL_BLEED_SPAN = 0.98
#: S20 speaks only about a white canvas: the brightest corner must be at least this
#: close to white.  A lifestyle photo of a room fills its frame too, but it is not an
#: emblem on studio white - S7 (or its official-gallery rescue) owns that case.
_LAYOUT_WHITE_GROUND_MIN = 0.90
#: S7.panel: how close a band's pixel must sit to the band's median shade to count as
#: flat background.  Badges and captions are artwork *on* the panel, not the panel.
_PANEL_BG_TOLERANCE = 35.0


def layout_metrics(image: Image.Image) -> tuple[float, float, float]:
    """Return ``(ink fraction, ink bbox span, coarse texture)`` of a studio image.

    All three read the same white-background assumption S7 uses, and each describes a
    different thing about what fills the frame:

    * ``ink fraction`` - share of pixels that are not white.  Product photos of split
      ACs on studio white measure 0.22-0.44 across this pipeline's accepted set; a
      marketing poster that paints its whole canvas covers 0.95.
    * ``ink bbox span`` - area of the smallest box touching all artwork, over the
      frame area.  A product floats inside its canvas (0.21-0.27); a poster bleeds to
      all four edges (1.00).
    * ``coarse texture`` - mean absolute grey-level change on a 64x64 resample.
      Photographs measure 3-10 (a full-bleed Midea shot reaches 10.3); a logo or
      emblem sheet is dense line art (37-55); an AC wearing a strip of feature badges
      sits at 23-26.
    """
    rgb = np.asarray(to_rgb(image), dtype=float)
    grey = np.asarray(image.convert("L").resize((64, 64)), dtype=float)
    ink = np.abs(rgb - 255.0).max(axis=2) > _LAYOUT_INK_TOLERANCE
    rows = np.flatnonzero(ink.any(axis=1))
    cols = np.flatnonzero(ink.any(axis=0))
    if rows.size == 0 or cols.size == 0:
        return 0.0, 0.0, 0.0
    span = float((rows[-1] - rows[0] + 1) * (cols[-1] - cols[0] + 1)) / float(ink.size)
    texture = float(
        np.abs(np.diff(grey, axis=0)).mean() + np.abs(np.diff(grey, axis=1)).mean()
    )
    return float(ink.mean()), span, texture


def is_marketing_layout(
    image: Image.Image,
    config: PipelineConfig,
    *,
    panel_cut: tuple[str, int] | None = None,
) -> bool:
    """Safeguard **S20**: artwork that fills its canvas edge to edge is not a photo.

    This is the hole the owner caught twice (2026-10-10, Haier): "this link clearly
    show a emblem ai label add picture why this script keep downloading and not
    rejecting".  The page's *own* gallery held ``logo_white_bg.png`` - the brand's
    AI-and-laurel emblem - and it passed every gate because each one asked a question
    the emblem happens to answer: it is square (S8), crisp (S6), it has no banned word
    in its URL (S5), no other model's name (S4), it is white on white (S7) and it is
    monochrome.  **S14 was the gate written for exactly this emblem, and it gave up on
    the first test**: it requires a *non-white* ground (``corner >=
    logo_background_max_value`` -> keep), because the live case that made it was the
    grey-disc variant of 2026-10-09.  These files are the same emblem on a WHITE
    ground, so the corner check bailed them out.  S20 is that sibling: it never asks
    about the ground colour, it asks whether anything *is* a product - a real unit
    leaves its studio whitespace to the edges, an emblem or a poster does not.
    """
    if not config.marketing_layout_enabled:
        return False
    ink_fraction, span, texture = layout_metrics(image)
    if span < _LAYOUT_FULL_BLEED_SPAN:
        return False
    if panel_cut is not None and panel_cut[1] >= 8:
        # The "painted artwork around the product" verdict was already acted on: the flat
        # marketing panel has been cut off this exact image by ``marketing_panel_split``, so
        # what is left is the studio area, and its near-white wash still measures as ink (the
        # Dawlance file's remaining area reads 100 % faint-ink because Orient and Dawlance
        # shoot on a light grey plate, not pure white).  Raising the tolerance globally is not
        # the answer - measured on the owner's run, that would let all four Orient marketing
        # posters back in, since their wash is the same light grey.  The line-art branch and
        # S21 stay in force here: they describe the product area itself, not the panel.
        return texture >= config.marketing_max_texture
    rgb = np.asarray(to_rgb(image).resize((128, 128), Image.Resampling.BOX), dtype=float)
    corner = max(
        float(rgb[:16, :16].max(axis=2).mean()),
        float(rgb[:16, -16:].max(axis=2).mean()),
        float(rgb[-16:, :16].max(axis=2).mean()),
        float(rgb[-16:, -16:].max(axis=2).mean()),
    )
    if corner < _LAYOUT_WHITE_GROUND_MIN * 255.0:
        return False
    return (
        ink_fraction >= config.marketing_max_ink_fraction
        or texture >= config.marketing_max_texture
    )


def marketing_layout_reason(image: Image.Image, config: PipelineConfig) -> str:
    """One sentence naming which S20 signal fired, for ``decisions.jsonl``."""
    ink_fraction, _span, texture = layout_metrics(image)
    if ink_fraction >= config.marketing_max_ink_fraction:
        return (
            f"painted artwork covers {ink_fraction:.0%} of the frame and reaches all "
            "four edges (poster/banner, not a product photo)"
        )
    return (
        f"line art of texture {texture:.1f} reaches every edge of a full-bleed frame "
        "(logo/emblem/badge sheet, not a product photo)"
    )

# --------------------------------------------------------------------------- #
# S21 - caption-sheet diagrams (in-box contents, manual pages, how-to pages)
# --------------------------------------------------------------------------- #
def body_layout(image: Image.Image, *, min_part_share: float = 0.02) -> tuple[int, float]:
    """Return ``(how many disconnected parts the ink forms, the largest one's share)``.

    A photograph of a product is one body: even "unit + remote + copper roll" leaves one
    part clearly dominant.  A contents sheet - "Indoor Unit / Outdoor Unit / AAA battery /
    Remote Control / User Manual / Copper Piping" with a caption under each icon - is a
    grid of comparable small parts with no leader at all.  Both are flat black on white,
    so colour and sharpness say nothing; the *arrangement* does.

    ``min_part_share`` ignores specks (dust, an antialiased letter) so a photo is not
    turned into a "many-part" image by its own text.
    """
    try:
        from scipy import ndimage
    except Exception:                                       # pragma: no cover - optional dep
        return 0, 1.0
    rgb = np.asarray(to_rgb(image), dtype=float)
    ink = np.abs(rgb - 255.0).max(axis=2) > _LAYOUT_INK_TOLERANCE
    if not ink.any():
        return 0, 1.0
    labels, count = ndimage.label(ink, structure=np.ones((3, 3)))
    if count <= 0:
        return 0, 1.0
    sizes = np.bincount(labels.ravel(), minlength=count + 1)[1:].astype(float)
    total = float(sizes.sum())
    parts = int((sizes / total >= min_part_share).sum())
    return parts, float(sizes.max() / total)


def diagram_sheet_reason(image: Image.Image, config: PipelineConfig) -> str:
    """Say why this image is a caption-sheet diagram rather than a photo of the product."""
    if not config.diagram_sheet_enabled:
        return ""
    parts, dominant = body_layout(image)
    if parts < config.diagram_min_parts or dominant > config.diagram_max_dominant_share:
        return ""
    return (
        f"layout of {parts} comparable parts with no dominant body (largest covers "
        f"{dominant:.0%} of the artwork) - an in-box/manual caption sheet, not the product"
    )


# --------------------------------------------------------------------------- #
# S7.panel - a pasted marketing panel below (or above) the product
# --------------------------------------------------------------------------- #
def marketing_panel_split(
    image: Image.Image, config: PipelineConfig
) -> tuple[Image.Image, tuple[str, int] | None]:
    """Cut a flat marketing panel off the edge of an image, keeping the product.

    The owner's Dawlance file of 2026-10-10 is the case: the top half is a normal studio
    shot of the indoor unit, and the bottom half is a **pasted black panel** carrying feature
    badges, a "1.5 Ton" pill and a warranty block.  Their instruction was explicit and it is
    the same rule they gave for Orient's hero - "it should download but on isolated it cuts
    bottom labels, advertisement marketing labels, and add only product".

    ``strip_promo_band`` cannot see this: it caps a promo strip at ``promo_band_max_ratio`` and
    demands a bright gap row on the inner side, while this panel covers ~45 % of the frame and
    sits directly under the unit's dark lip.  Refusing to widen that rule was measured, not
    assumed - a "cut any dark bottom band" rule truncates real photographs, because 3 of the
    owner's own accepted files are exactly that shape (Orient Titanium olive-black, EcoStar
    Duke x2).  What separates a *pasted panel* from a *dark product* is that a panel is dead
    flat and its edge is a hard line across the whole width:

    ==========================  ==========  ==============  ======  ==========
                                band mean   band row std    jump    edge flatness
    ==========================  ==========  ==============  ======  ==========
    pasted black panel              28.5          23.7       238       1.00
    Orient Titanium olive-black    178.7          91.2        56       0.88
    EcoStar Duke (x2)          163.8 / 181.4   105.7 / 88.9   79 / 82  0.67 / 0.72
    ==========================  ==========  ==============  ======  ==========

    All five conditions have to hold, and a scan of the owner's 72 published files with these
    thresholds finds **no other image to cut**.  The calm-rows condition is a disjunction since
    LOW_11 (2026-10-10): badges, captions and a warranty block make the rows uneven, but they
    sit on one flat black background, so a band mostly made of its own median shade qualifies
    too - a dark *product* never is.  Returns ``(image, None)`` when there is no panel, else
    ``(cropped, (side, pixels))``.
    """
    if not config.marketing_panel_split_enabled:
        return image, None
    grey = np.asarray(image.convert("L"), dtype=float)
    height, width = grey.shape[0], grey.shape[1]
    if height < 64 or width < 64:
        return image, None
    row_mean = grey.mean(axis=1)
    # The panel's border is the single hardest horizontal line in the picture: search for it
    # rather than walking in from the edge, because a panel is black *with bright badges in it*,
    # so its individual rows are not all dark.
    lo = max(int(0.28 * height), 1)
    hi = min(int(0.90 * height), height - 1)
    if hi <= lo:
        return image, None
    jump, y = max(
        (abs(float(row_mean[i] - row_mean[i - 1])), i) for i in range(lo, hi)
    )
    if jump < config.panel_min_jump:
        return image, None
    downward = float(row_mean[y]) < float(row_mean[y - 1])
    band = grey[y:] if downward else grey[:y]
    rest = row_mean[:y] if downward else row_mean[y + 1 :]
    ratio = band.shape[0] / height
    if not config.panel_min_ratio <= ratio <= config.panel_max_ratio:
        return image, None
    if float(band.mean()) > config.panel_band_max_mean:
        return image, None      # a dark *product* is not a flat black panel
    if float(band.mean(axis=1).std()) > config.panel_max_row_std:
        # The rows of a panel carrying real artwork (badges, captions, a warranty block)
        # are not calm - LOW_11 measured 53 where the flat replica measured 24.  The
        # artwork sits on one dead-flat background, though, while a photograph is body
        # and gradient, so a band whose pixels mostly sit on one flat shade still counts.
        background = float(np.mean(np.abs(band - np.median(band)) <= _PANEL_BG_TOLERANCE))
        if background < config.panel_bg_flat_min:
            return image, None  # highlights and shadows: a photograph, not pasted artwork
    if rest.size == 0 or float(rest.mean()) < config.panel_side_min_mean:
        return image, None      # what is kept must be a bright studio area
    inner, outer = (y - 1, y) if downward else (y, y - 1)
    flat = float(np.mean(np.abs(grey[inner] - grey[outer]) > 0.5 * jump))
    if flat < config.panel_edge_flat_min:
        return image, None      # the seam must run straight across the whole width
    edge = y
    cropped = image.crop((0, 0, width, edge)) if downward else image.crop(
        (0, edge, width, height)
    )
    if cropped.size[1] == height:
        return image, None
    side = "bottom" if downward else "top"
    return cropped, (side, int(band.shape[0]))

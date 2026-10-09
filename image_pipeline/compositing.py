"""Product isolation, square normalisation, light sharpening and templates.

Everything here works on PIL images and explicit thresholds from ``PipelineConfig``.
No network, no filesystem, no hidden state.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFilter

from config import PipelineConfig
from image_quality import to_rgb

LOG = logging.getLogger("pipeline.compositing")


# --------------------------------------------------------------------------- #
# background / isolation (spec 7.2)
# --------------------------------------------------------------------------- #
def detect_background_color(image: Image.Image, *, patch_ratio: float) -> tuple[int, int, int]:
    """Median colour of the four corner patches - the assumed studio background."""
    array = np.asarray(to_rgb(image), dtype=float)
    height, width = array.shape[:2]
    size = max(2, int(min(width, height) * patch_ratio))
    patches = [
        array[:size, :size],
        array[:size, -size:],
        array[-size:, :size],
        array[-size:, -size:],
    ]
    stacked = np.concatenate([patch.reshape(-1, 3) for patch in patches], axis=0)
    median = np.median(stacked, axis=0)
    return (int(median[0]), int(median[1]), int(median[2]))


def white_fallback_bbox(
    image: Image.Image, tolerance: int
) -> tuple[int, int, int, int] | None:
    """Safeguard for white product on white background (spec 7.2).

    Bounding box of every pixel that differs from pure white by more than
    ``tolerance`` (soft shadows included).  Returns ``None`` when nothing differs.
    """
    array = np.asarray(to_rgb(image), dtype=int)
    distance = (255 - array).max(axis=2)
    mask = distance > tolerance
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    if rows.size == 0 or cols.size == 0:
        return None
    return (int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1)


def _foreground_mask(rgb: Image.Image, config: PipelineConfig) -> np.ndarray:
    """Pixels far enough from the studio background colour to be called foreground."""
    array = np.asarray(rgb, dtype=float)
    background = np.array(detect_background_color(rgb, patch_ratio=config.corner_patch_ratio), dtype=float)
    return np.asarray(np.abs(array - background).max(axis=2) > config.isolation_bg_distance, dtype=bool)


def _product_silhouette(mask: np.ndarray, config: PipelineConfig) -> np.ndarray:
    """The mask made solid: hairline gaps bridged, enclosed background filled.

    Two failures of the raw difference mask were what cut products apart.  A white
    shell on a white background differs from the background by only a few levels, so
    the body is *hollow* - its outline breaks wherever the shading is soft, and the
    component that survives is a strip.  A frame that holds several objects (an
    "in-box items" diagram, a promo banner with a logo) has several components and
    unioning their boxes crops to whichever ones happened to be close together.
    Closing then filling turns a hollow body into one solid region, which is what a
    product actually is.
    """
    try:
        from scipy import ndimage
    except Exception:                                       # pragma: no cover - optional dep
        return mask
    radius = max(1, round(min(mask.shape) * config.isolation_merge_radius_ratio))
    structure = np.ones((2 * radius + 1, 2 * radius + 1), dtype=bool)
    return np.asarray(ndimage.binary_fill_holes(ndimage.binary_closing(mask, structure=structure)), dtype=bool)


def _solid_bodies(mask: np.ndarray, config: PipelineConfig) -> tuple[list[tuple[int, int, int, int]], list[float]]:
    """Boxes and areas of the silhouette's connected bodies, largest first."""
    try:
        from scipy import ndimage
    except Exception:                                       # pragma: no cover - optional dep
        return [], []
    labels, count = ndimage.label(mask)
    if count == 0:
        return [], []
    slices = ndimage.find_objects(labels)
    sizes = ndimage.sum(mask, labels, index=range(1, count + 1))
    boxes: list[tuple[int, int, int, int]] = []
    areas: list[float] = []
    for rank in np.argsort(sizes)[::-1]:
        index = int(rank)
        area = float(sizes[index])
        if area <= 0:
            continue
        box = _slice_to_box(slices[index])
        if box is not None:
            boxes.append(box)
            areas.append(area)
    return boxes, areas


def _is_composite_frame(boxes: list[tuple[int, int, int, int]], areas: list[float], config: PipelineConfig) -> bool:
    """True when the frame holds several separate objects, so cropping is meaningless.

    An "in-box items" diagram, a spec sheet with a logo, or a promo banner is not one
    product on a background: the largest body is a fraction of the whole, and the next
    body is comparable to it.  Cropping such a frame to the largest component throws the
    rest of the picture away and publishes a fragment (live 2026-10-09: the Inspire Pro
    row's "In-box items" diagram was cropped to its single line-art indoor unit, and the
    LG remote shot was cropped to a grey blob).  The safe answer is the full frame.
    """
    if len(boxes) < 2 or not areas:
        return False
    largest = areas[0]
    if largest <= 0:
        return False
    # A layout, not a product with a badge: several bodies that are each a fair share of
    # the largest one and sit far from it (a diagram's cells, a logo beside a photo).
    comparable = [
        (box, area)
        for box, area in zip(boxes[1:], areas[1:], strict=True)
        if area >= largest * config.isolation_multi_body_ratio
    ]
    if len(comparable) < 2:
        return False
    main_width = boxes[0][2] - boxes[0][0]
    return any(
        _box_distance(box, boxes[0]) > min(main_width, box[2] - box[0]) * 0.5 for box, _area in comparable
    )


def _white_on_white_crop(rgb: Image.Image, config: PipelineConfig) -> tuple[Image.Image, bool]:
    """Crop a product the difference mask cannot see, or publish the frame untouched.

    Both halves of the decision matter.  Near-white bodies *are* real - their edges only
    sit a few levels from the background - so the box is padded generously, or the crop
    shaves the product's own side panels.  But when the only pixels that differ from white
    are dark parts of a *layout* (vents along one edge, a logo, an "in-box items" diagram),
    the honest answer is the whole frame: the background is already the white the template
    wants, and a crop of a fragment is strictly worse than the uncut picture.
    """
    fallback = white_fallback_bbox(rgb, config.white_fallback_tolerance)
    if fallback is None:
        return rgb, True
    # The near-white body edges sit *inside* the tolerance, so pad far more generously
    # than for a chromatic product - cropping to the dark parts alone shaves the
    # product's own edges (vent slivers, cut side panels).
    generous = config.padding_px + max(8, min(rgb.width, rgb.height) // 25)
    box = _pad_box(fallback, generous, rgb.size)
    area_ratio = ((box[2] - box[0]) * (box[3] - box[1])) / float(rgb.width * rgb.height)
    if area_ratio < config.white_fallback_min_area_ratio:
        # Only a sliver of dark pixels (vents, logo, a remote's screen): the product
        # itself is invisible to the mask.  Publish the full frame.
        return rgb, True
    boxes, areas = _solid_bodies(_product_silhouette(_fallback_mask(rgb, config), config), config)
    if boxes and _is_composite_frame(boxes, areas, config):
        return rgb, True
    crop = rgb.crop(box)
    if crop.width < 2 or crop.height < 2:
        return rgb, True
    return crop, True


def _fallback_mask(rgb: Image.Image, config: PipelineConfig) -> np.ndarray:
    """Pixels differing from pure white by more than the fallback tolerance."""
    array = np.asarray(rgb, dtype=int)
    return np.asarray((255 - array).max(axis=2) > config.white_fallback_tolerance, dtype=bool)


def _single_body_box(mask: np.ndarray, config: PipelineConfig) -> tuple[int, int, int, int] | None:
    """Bbox of the product body: the largest silhouette plus its attached companions."""
    boxes, areas = _solid_bodies(mask, config)
    if not boxes:
        return None
    largest = areas[0]
    kept = [boxes[0]]
    for index in range(1, len(boxes)):
        if areas[index] < largest * config.isolation_min_component_ratio:
            continue
        if _box_distance(boxes[index], boxes[0]) <= config.isolation_neighbor_distance_px:
            kept.append(boxes[index])
    return (
        min(box[0] for box in kept),
        min(box[1] for box in kept),
        max(box[2] for box in kept),
        max(box[3] for box in kept),
    )


def isolate_main_product(
    image: Image.Image, config: PipelineConfig
) -> tuple[Image.Image, bool]:
    """Crop the product out of its studio background.

    Returns ``(image, degraded)``.  ``degraded`` is True when a guard fired: the
    degenerate crop guard (use the uncropped image) or the white-on-white fallback.
    Never raises and never returns an empty image.
    """
    rgb = to_rgb(image)
    mask = _foreground_mask(rgb, config)
    fraction = float(mask.mean()) if mask.size else 0.0

    # A body too faint for the difference mask is measured against pure white instead,
    # and that path owns its own guards - it has to run before the silhouette work, whose
    # mask here holds nothing but a few dark parts.
    if fraction < config.isolation_min_foreground_fraction:
        return _white_on_white_crop(rgb, config)

    # Work on the *silhouette*, not on raw difference components: a body whose shading is
    # softer than the threshold is hollow, and a hollow body is where products got cut.
    silhouette = _product_silhouette(mask, config)
    boxes, areas = _solid_bodies(silhouette, config)
    if not boxes:
        return rgb, True

    # Several comparable, spread-out objects = a diagram or a banner, not a product on a
    # background.  Cropping it publishes one fragment of it, which is worse than the
    # whole picture, so the frame goes through as it is.
    if _is_composite_frame(boxes, areas, config):
        return rgb, True

    body = _single_body_box(silhouette, config)
    if body is None:
        return rgb, True
    box = _pad_box(body, config.padding_px, rgb.size)
    if config.isolation_crop_closure:
        closed = _ink_closed_box(rgb, box, config)
        if closed != box:                       # only a repaired crop gains a second pad
            box = _pad_box(closed, config.padding_px, rgb.size)
    area_ratio = ((box[2] - box[0]) * (box[3] - box[1])) / float(rgb.width * rgb.height)
    if area_ratio < config.degenerate_crop_ratio:
        return rgb, True
    crop = rgb.crop(box)
    if crop.width < 2 or crop.height < 2:
        return rgb, True
    return crop, False


def _ink_closed_box(
    rgb: Image.Image, box: tuple[int, int, int, int], config: PipelineConfig
) -> tuple[int, int, int, int]:
    """Grow a crop until its border stops cutting visible ink.  Never shrinks it.

    ``isolation_bg_distance`` is strict on purpose, and the strict mask is what picks
    the body to crop - so the box ends wherever a white shell's shading softens below
    the threshold, and the product is cut there (owner run 2026-10-10: 40 of 72
    accepted images, e.g. ``Candy By Haier 0.8 Ton`` 1342x695 -> 1342x195).  Here the
    same frame is measured against *pure white* with a loose scan, its connected bodies
    are labelled, and only bodies that actually touch the crop border are pulled in.
    A mark standing away from the product - a corner logo, a price label - is a
    separate body and never widens the crop.
    """
    try:
        from scipy import ndimage
    except Exception:                                       # pragma: no cover - optional dep
        return box
    array = np.asarray(rgb, dtype=float)
    height, width = array.shape[:2]
    ink = np.asarray(
        (255.0 - array).max(axis=2) > config.isolation_ink_scan_distance, dtype=bool
    )
    if not ink.any():
        return box
    labels, count = ndimage.label(ink, structure=np.ones((3, 3), dtype=bool))
    if count == 0:                                           # pragma: no cover - defensive
        return box
    closed = box
    for _ in range(3):
        left, top, right, bottom = closed
        left = max(0, min(left, width - 1))
        top = max(0, min(top, height - 1))
        right = max(left + 1, min(right, width))
        bottom = max(top + 1, min(bottom, height))
        border = np.zeros(ink.shape, dtype=bool)
        border[top:bottom, left] = True
        border[top:bottom, right - 1] = True
        border[top, left:right] = True
        border[bottom - 1, left:right] = True
        ids = np.unique(labels[np.logical_and(border, ink)])
        ids = ids[ids > 0]
        if ids.size == 0:
            return closed
        rows, cols = np.where(np.isin(labels, ids))
        grown = (
            min(closed[0], int(cols.min())),
            min(closed[1], int(rows.min())),
            max(closed[2], int(cols.max()) + 1),
            max(closed[3], int(rows.max()) + 1),
        )
        if grown == closed:
            return closed
        closed = grown
    return closed


def _pad_box(
    box: tuple[int, int, int, int], padding: int, size: tuple[int, int]
) -> tuple[int, int, int, int]:
    left, top, right, bottom = box
    width, height = size
    return (
        max(0, left - padding),
        max(0, top - padding),
        min(width, right + padding),
        min(height, bottom + padding),
    )


def _slice_to_box(slice_tuple: tuple[slice, slice] | None) -> tuple[int, int, int, int] | None:
    if slice_tuple is None:
        return None
    vertical, horizontal = slice_tuple
    return (horizontal.start, vertical.start, horizontal.stop, vertical.stop)


def _box_distance(left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> float:
    """Distance between two rectangles (0 when they overlap)."""
    dx = max(left[0] - right[2], right[0] - left[2], 0)
    dy = max(left[1] - right[3], right[1] - left[3], 0)
    return float((dx * dx + dy * dy) ** 0.5)


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #
def to_square_canvas(
    image: Image.Image,
    side: int,
    background: tuple[int, int, int] = (255, 255, 255),
    *,
    fill_ratio: float = 0.92,
    max_upscale: float = 0.0,
) -> Image.Image:
    """Scale to fit inside a square canvas, pad with the background colour, centre.

    ``max_upscale`` (when > 0) caps how far a small image may be blown up: a
    120px remote control scaled to fill a 1200px canvas is 10x mush, better
    small and sharp in the middle of the canvas than giant and unreadable.
    """
    rgb = to_rgb(image)
    if rgb.width <= 0 or rgb.height <= 0:
        return Image.new("RGB", (side, side), background)
    scale = min(side / rgb.width, side / rgb.height) * fill_ratio
    if max_upscale > 0:
        scale = min(scale, max_upscale)
    scale = max(scale, 1e-6)
    target = (max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale)))
    resized = rgb.resize(target, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (side, side), background)
    canvas.paste(resized, ((side - target[0]) // 2, (side - target[1]) // 2))
    return canvas


def sharpen(image: Image.Image, *, radius: float, percent: int, threshold: int) -> Image.Image:
    """Light UnsharpMask only - no colour or contrast changes."""
    return image.filter(ImageFilter.UnsharpMask(radius=radius, percent=percent, threshold=threshold))


# --------------------------------------------------------------------------- #
# templates
# --------------------------------------------------------------------------- #
def _non_background_mask(template: Image.Image) -> np.ndarray:
    """Pixels that are neither transparent nor near-white."""
    rgba = template.convert("RGBA")
    array = np.asarray(rgba)
    alpha = array[:, :, 3]
    rgb = array[:, :, :3]
    opaque = alpha > 8
    colourful = rgb.min(axis=2) < 245
    # asarray keeps the declared ndarray return type (mypy --strict) at zero cost
    return np.asarray(opaque & colourful, dtype=bool)


def header_safe_margin(template: Image.Image, config: PipelineConfig) -> int:
    """Height of the template's header band, found by scanning (no hard-coded logo).

    Scans the top ``template_top_scan_ratio`` of the template for the last row that
    has non-transparent / non-white pixels followed by ``template_header_gap_px``
    blank rows.  Falls back to ``template_default_top_margin_px`` when the band has
    no detectable end.
    """
    mask = _non_background_mask(template)
    if mask.size == 0:
        return 0
    height = int(mask.shape[0])
    band = max(1, int(height * config.template_top_scan_ratio))
    rows = np.asarray(mask[:band].any(axis=1), dtype=bool)
    if not bool(rows.any()):
        return 0
    last_content = -1
    blank_run = 0
    for row in range(band):
        if bool(rows[row]):
            last_content = row
            blank_run = 0
        else:
            blank_run += 1
            if last_content >= 0 and blank_run >= config.template_header_gap_px:
                return int(min(height, last_content + config.template_header_gap_px))
    if last_content >= 0:
        return int(
            min(
                height,
                max(
                    last_content + config.template_header_gap_px,
                    config.template_default_top_margin_px,
                ),
            )
        )
    return 0


def template_content_box(
    template: Image.Image, config: PipelineConfig
) -> tuple[int, int, int, int]:
    """Safe content box: side margins, header band and bottom margin excluded."""
    width, height = template.size
    left = int(width * config.template_side_margin_ratio)
    right = width - left
    top = header_safe_margin(template, config)
    bottom = height - int(height * config.template_bottom_margin_ratio)
    if right - left < 8 or bottom - top < 8:
        top = 0
        left = 0
        right = width
        bottom = height
    return (left, top, right, bottom)


def product_content_bbox(
    image: Image.Image, *, white_level: int, min_ratio: float
) -> tuple[int, int, int, int]:
    """Tight box around the product inside its canvas (white = background).

    Used for sizing: the *product* must fill the template's box, not the padded square it
    travels in.  Falls back to the whole canvas when the measurement looks unreliable (a
    product that is almost entirely white, or an empty canvas).
    """
    rgb = to_rgb(image)
    width, height = rgb.size
    array = np.asarray(rgb, dtype=np.uint8)
    content = array.min(axis=2) < white_level
    if not bool(content.any()):
        return (0, 0, width, height)
    rows = np.where(content.any(axis=1))[0]
    cols = np.where(content.any(axis=0))[0]
    box_w = int(cols[-1]) - int(cols[0]) + 1
    box_h = int(rows[-1]) - int(rows[0]) + 1
    if box_w < min_ratio * width or box_h < min_ratio * height:
        return (0, 0, width, height)
    return (int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1)


def fit_product_size(
    content_w: int,
    content_h: int,
    box_w: int,
    box_h: int,
    *,
    fill_ratio: float,
) -> float:
    """Scale factor that makes ``content`` fill ``fill_ratio`` of its tightest box edge."""
    if content_w <= 0 or content_h <= 0:
        return 1.0
    return min(box_w * fill_ratio / content_w, box_h * fill_ratio / content_h)


def template_has_transparency(template: Image.Image, *, min_alpha: int) -> bool:
    """True only when the template really uses translucency.

    An alpha *channel* is not the same thing as transparency: brand PNGs are routinely
    exported with an all-opaque alpha channel.  Such a template must still get its white
    knocked out, otherwise it paints over the product and every final image is the bare
    template.  ``min_alpha`` is the configurable line between the two cases.
    """
    alpha = np.asarray(template.convert("RGBA"), dtype=np.uint8)[:, :, 3]
    return bool(np.min(alpha) < min_alpha)


def make_white_transparent(
    template: Image.Image,
    cutoff: int,
    *,
    box: tuple[int, int, int, int] | None = None,
) -> Image.Image:
    """Turn near-white pixels into transparency (for opaque, white-backed templates).

    ``box`` restricts the knockout to the product area.  The logo band and the footer are
    deliberately left alone: a strictly byte-identical logo band is the customer guarantee,
    and knocking out its near-white anti-aliased edges (values just above ``cutoff``) would
    quietly brighten the brand art even though the product never goes near it.
    """
    rgba = template.convert("RGBA")
    array = np.asarray(rgba, dtype=np.uint8).copy()
    white = array[:, :, :3].min(axis=2) >= cutoff
    if box is not None:
        left, top, right, bottom = box
        inside = np.zeros_like(white)
        inside[top:bottom, left:right] = True
        white &= inside
    array[:, :, 3] = np.where(white, 0, array[:, :, 3])
    return Image.fromarray(array, mode="RGBA")


@dataclass(frozen=True)
class PreparedTemplate:
    """Immutable precomputed overlay and bounding content box for a template."""

    content_box: tuple[int, int, int, int]
    base: Image.Image


def prepare_template(template: Image.Image, config: PipelineConfig) -> PreparedTemplate:
    """Precompute template content box and transparency mask once per template asset."""
    box = template_content_box(template, config)
    base = template.convert("RGBA")
    if config.template_make_white_transparent and not template_has_transparency(
        template, min_alpha=config.template_transparency_min_alpha
    ):
        base = make_white_transparent(template, config.template_white_cutoff, box=box)
    return PreparedTemplate(content_box=box, base=base)


def apply_template(
    product_square: Image.Image,
    template: Image.Image,
    config: PipelineConfig,
    *,
    bbox: tuple[int, int, int, int] | None = None,
    prepared: PreparedTemplate | None = None,
) -> Image.Image:
    """Composite the product into the template and put the overlay back on top."""
    if prepared is not None:
        left, top, right, bottom = prepared.content_box
        base = prepared.base
    else:
        left, top, right, bottom = template_content_box(template, config)
        base = template.convert("RGBA")
        if config.template_make_white_transparent and not template_has_transparency(
            template, min_alpha=config.template_transparency_min_alpha
        ):
            # Knock white out of the product area only - never out of the logo band or footer.
            base = make_white_transparent(
                template, config.template_white_cutoff, box=(left, top, right, bottom)
            )

    box_w, box_h = right - left, bottom - top

    # Size from the product's own content box, so a wide shot fills the width and a tall
    # shot fills the height - the padded square is only a carrier.
    canvas_w, canvas_h = product_square.size
    if bbox is None:
        bbox = product_content_bbox(
            product_square,
            white_level=config.product_content_white_level,
            min_ratio=config.product_content_min_ratio,
        )
    content_w, content_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    scale = fit_product_size(
        content_w, content_h, box_w, box_h, fill_ratio=config.product_fill_ratio
    )
    target = (max(1, round(canvas_w * scale)), max(1, round(canvas_h * scale)))
    product = to_rgb(product_square).resize(target, Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", template.size, (255, 255, 255, 0))
    # Align the *content*, not the carrier: the padded square is bigger than the product,
    # so centring the carrier could push its padding outside the content box and over the
    # logo band.  PIL clips negative origins, which trims that padding safely.
    content_target_w = max(1, round(content_w * scale))
    content_target_h = max(1, round(content_h * scale))
    bias = int(box_h * config.optical_center_bias)
    content_x = left + (box_w - content_target_w) // 2
    content_y = top + (box_h - content_target_h) // 2 + bias
    # Paste the product itself (its content box), never the white padding around it -
    # that padding is what used to creep over the logo band.
    crop_left = round(bbox[0] * scale)
    crop_top = round(bbox[1] * scale)
    content = product.crop(
        (crop_left, crop_top, crop_left + content_target_w, crop_top + content_target_h)
    )
    canvas.paste(content, (content_x, content_y))
    return Image.alpha_composite(canvas, base)


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #
def to_jpeg_bytes(
    image: Image.Image, *, quality: int, background: tuple[int, int, int] = (255, 255, 255)
) -> bytes:
    """Flatten onto ``background`` and encode as JPEG (used for local files + upload)."""
    rgb = image.convert("RGB") if image.mode == "RGB" else to_rgb(image)
    if rgb.mode != "RGB":
        rgb = rgb.convert("RGB")
    buffer = io.BytesIO()
    rgb.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=True)
    return buffer.getvalue()

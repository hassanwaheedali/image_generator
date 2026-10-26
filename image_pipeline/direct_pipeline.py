"""Option 2: direct image URLs, no scraping.

The sheet row supplies the product's own image URLs; the row trusts them.  Per row:
URL de-duplication -> download -> bytes validation -> furniture cleanup ->
S19 branded-template guard -> white-background gates -> colour rule -> pixel
dedup -> isolate/square/sharpen -> every selected template -> upload -> CSVs.

Every stage calls the Option 1 machinery (nothing is forked); the only new
logic is about the blur check after downloading the picture, gate order and the S19 call.
There is deliberately no page verification (S1-S3), no gallery extraction, no fallback search
(S15) and no URL-heuristic filtering (S4/S5/S12): those exist to prove scraped pages, and
Option 2 never scrapes.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from dataclasses import replace
from pathlib import Path

from compositing import apply_template, isolate_main_product, sharpen, to_square_canvas
from config import PipelineConfig
from csv_io import hash_source_urls
from dedup import ProductDedup, compute_fingerprint
from fetch import FetchError, PermanentAPIError
from image_quality import (
    ImageValidationError,
    branded_template_reason,
    is_canvas_blurry,
    run_quality_gates,
    sharpness_score,
    strip_promo_band,
    trim_edge_seams,
    validate_image_bytes,
    verify_image_color,
)
from model_matching import build_model_keys, is_display_product
from models import AcceptedImage, ImageCandidate, ProductResult, ProductTask, Status

# ``_compose_and_save`` and ``_row_is_done`` carry the Option 1 compose/target
# contract; Option 2 calls them instead of re-implementing that contract.
from pipeline import (
    PipelineDeps,
    _compose_and_save,
    _dump_rejected,
    _load_prepared_template,
    _row_is_done,
    finalize_and_upload,
    note_gallery_colour_conflict,
)
from url_filters import dedup_key, detect_brand, display_brand

LOG = logging.getLogger("pipeline.direct")


def _blur_reference_template(config: PipelineConfig, deps: PipelineDeps) -> Path | None:
    """The artwork S13.blur composites its preview on (``config.canvas_blur_reference``).

    The guard's two thresholds were measured against one template's bands, and a template
    changes the reading (its own edges are part of the FFT energy and the gradient mean),
    so the reference is a named platform rather than whatever the plan happens to lead
    with.  A run without that platform still judges on its first platform - skipping the
    gate because a shop was not selected would be the wrong way to fail open.
    """
    platform = deps.platforms.by_key(config.canvas_blur_reference)
    if platform is None and deps.platforms.platforms:
        platform = deps.platforms.platforms[0]
    return platform.template_path if platform else None


def process_direct_product(
    task: ProductTask,
    deps: PipelineDeps,
    config: PipelineConfig,
    cancel_event: threading.Event | None = None,
) -> tuple[ProductResult, list[AcceptedImage]]:
    """The Option 2 per-row flow: one direct image URL at a time.

    Same signature and contract as ``process_product`` so ``BatchRunner`` can run
    either flow.  Every accept and every reject leaves one ``decisions.jsonl``
    line with its rule id, exactly as in Option 1.
    """
    brand = detect_brand(task.source_urls[0] if task.source_urls else "", task.title)
    base = ProductResult(
        task_index=task.index,
        title=task.title,
        status=Status.FAILED,
        brand=display_brand(brand, task.title),
        source_url_hash=hash_source_urls(task.source_urls),
    )
    # Colours only: direct rows trust their links, so a title without a model
    # code is fine here (there is no page to prove).  S16 fails open on "".
    keys = build_model_keys(
        task.title,
        allow_title_only=config.allow_title_only_verification,
        unbalanced_bracket_keeps_text=config.unbalanced_bracket_keeps_text,
        bracketed_key_fallback=config.use_bracketed_keys_as_fallback,
        brand_match_max_extra_tokens=config.brand_match_max_extra_tokens,
    )
    dedup = ProductDedup(
        hard_threshold=config.dedup_hamming_threshold,
        soft_threshold=config.dedup_hamming_soft_threshold,
        corr_threshold=config.dedup_corr_threshold,
        hard_corr_threshold=config.dedup_hard_corr_threshold,
        shape_iou_min=config.dedup_shape_iou_min,
        colour_min_agreement=config.gallery_colour_min_agreement,
    )
    accepted: list[AcceptedImage] = []
    seen_keys: set[str] = set()
    rejections: Counter[str] = Counter()
    LOG.info("[row %s] direct | %s | %d image url(s)", task.index, task.title, len(task.source_urls))

    for url in task.source_urls:
        if cancel_event is not None and cancel_event.is_set():
            break
        if _row_is_done(accepted, config):
            break

        # Option 1's S10-key guard, before any network: the same file is never
        # downloaded twice for one product.
        key = dedup_key(url)
        if key in seen_keys:
            rejections["S10"] += 1
            deps.decisions.reject(
                title=task.title, url=url, rule_id="S10", detail="same file already queued for this product"
            )
            continue

        binary = None
        try:
            binary = deps.image_fetcher.fetch_image(url, referer=url)
        except (PermanentAPIError, FetchError) as exc:
            rejections["S13.fetch"] += 1
            deps.decisions.reject(title=task.title, url=url, rule_id="S13.fetch", detail=str(exc))
            continue

        try:
            image = validate_image_bytes(
                binary.content,
                binary.content_type,
                max_bytes=config.max_image_bytes,
                max_pixels=config.max_image_pixels,
                min_lossy_bytes=config.min_lossy_image_bytes,
                min_lossy_bytes_per_pixel=config.min_lossy_bytes_per_pixel,
                blur_sharpness_floor=config.salvage_min_sharpness,
            )
        except ImageValidationError as exc:
            rejections[exc.rule_id] += 1
            deps.decisions.reject(title=task.title, url=url, rule_id=exc.rule_id, detail=exc.detail)
            continue

        # Blur guard (Option 2): reject genuinely blurry / smeared images.
        # Clean product shots (sharpness >= 0.02) pass; smeared/blurry images are rejected.
        blur_score = sharpness_score(image)
        if blur_score < config.salvage_min_sharpness:
            rejections["S13.blur"] += 1
            deps.decisions.reject(
                title=task.title,
                url=url,
                rule_id="S13.blur",
                detail=f"image is too blurry (sharpness {blur_score:.3f} < {config.salvage_min_sharpness:.3f})",
            )
            if config.keep_rejected_images:
                _dump_rejected(image, config, task.title, url, "S13.blur")
            continue

        # The same furniture cleanup Option 1 runs before the ring test: a shop
        # promo strip glued to an edge is cut, not a reason to lose the photo.
        # Kept for the audit: ``raw/`` is what the URL served, ``clean/`` what survived.
        downloaded = image
        image, band = strip_promo_band(image, config)
        if band is not None:
            side, pixels = band
            deps.decisions.keep(
                title=task.title,
                url=url,
                rule_id="S7.band",
                detail=f"removed a {pixels}px promo strip from the {side} edge before the S7 ring test",
            )
        image, seams = trim_edge_seams(image, config)
        for kind, pixels in seams:
            deps.decisions.keep(
                title=task.title,
                url=url,
                rule_id="S7.seam",
                detail=f"trimmed a {pixels}px {kind} layout seam before the S7 ring test",
            )

        # S19: a store's own themed template (brand colours, logo) never composes.
        template_reason = branded_template_reason(image, config)
        if template_reason is not None:
            rejections["S19"] += 1
            deps.decisions.reject(title=task.title, url=url, rule_id="S19", detail=template_reason)
            if config.keep_rejected_images:
                _dump_rejected(image, config, task.title, url, "S19")
            continue

        # The white-background gates (S8/S6/S9/S14/S7 + studio rescue): "we need
        # only clear pictures".  Same order and thresholds as Option 1.
        rule_id, detail = run_quality_gates(image, config, official_gallery=True)
        if rule_id != "ok":
            rejections[rule_id] += 1
            deps.decisions.reject(title=task.title, url=url, rule_id=rule_id, detail=detail)
            if config.keep_rejected_images:
                _dump_rejected(image, config, task.title, url, rule_id)
            continue
        if detail:
            # Accepted, not silent (the S16.curated pattern): the gate would have refused
            # this picture on a scraped page; in his folder it is his choice, and the
            # audit trail says so.
            deps.decisions.keep(
                title=task.title, url=url, rule_id="S7.curated", detail=detail
            )

        if (
            config.variant_separation_enabled
            and config.color_gate_enabled
            and keys.colour_word
            and not is_display_product(task.title)
            # Strictly the first image (owner 2026-10-21): a detail shot's hue
            # cannot say whether it is the right product from another angle or a
            # wrong colourway, so only the hero is colour-judged.
            and not accepted
        ):
            color_ok, color_detail = verify_image_color(
                image,
                keys.colour_word,
                official=True,
                exempt_chroma_on_official=config.color_gate_official_chroma_exempt,
                min_saturation=config.color_chroma_min_saturation,
                wash_max_value=config.color_chroma_max_value,
            )
            remaining_urls = [u for u in task.source_urls if dedup_key(u) not in seen_keys and u != url]
            if not color_ok and config.local_images_mode:
                # An owner-curated folder is the one source the run never doubts: it
                # fetches nothing, his hand-named files ARE the row, and he picked the
                # order.  No hue rule can tell a wrong colourway from a correct view of
                # the right product - the Floral Brown CW1's side views frame only its
                # blue-grey cabinet (100 % blue pixels, and correct; owner 2026-10-21) -
                # so even the first picture's verdict is recorded, not enforced.
                deps.decisions.keep(
                    title=task.title, url=url, rule_id="S16.curated", detail=color_detail
                )
            elif not color_ok and remaining_urls:
                rejections["S16"] += 1
                deps.decisions.reject(title=task.title, url=url, rule_id="S16", detail=color_detail)
                if config.keep_rejected_images:
                    _dump_rejected(image, config, task.title, url, "S16")
                continue

        note_gallery_colour_conflict(
            image,
            dedup,
            config=config,
            deps=deps,
            title=task.title,
            url=url,
        )
        fingerprint = compute_fingerprint(image)
        duplicate = dedup.duplicate_reason(image, fingerprint=fingerprint)
        if duplicate:
            rejections["S10"] += 1
            deps.decisions.reject(title=task.title, url=url, rule_id="S10", detail=duplicate)
            if config.keep_rejected_images:
                _dump_rejected(image, config, task.title, url, "S10")
            continue
        review_note = dedup.review_shape_match(image, fingerprint=fingerprint)
        if review_note:
            deps.decisions.keep(title=task.title, url=url, rule_id="S10.review", detail=review_note)

        # Blur guard (Option 2): reject images that lack high-frequency details when composited on canvas.
        # Calibrated against platform template frames in download mode; local curated images and --no-template skip it.
        if config.canvas_blur_guard_enabled and not (config.local_images_mode or config.no_template):
            isolated, _ = isolate_main_product(image, config)
            square = to_square_canvas(
                isolated,
                config.canonical_side,
                fill_ratio=config.product_fill_ratio,
                max_upscale=config.max_upscale,
            )
            square = sharpen(
                square,
                radius=config.unsharp_radius,
                percent=config.unsharp_percent,
                threshold=config.unsharp_threshold,
            )
            ref_path = _blur_reference_template(config, deps)
            if ref_path is not None:
                reference_prepared = _load_prepared_template(config, ref_path)
                preview = apply_template(square, reference_prepared, config)
                blurry, hf_val, edge_val = is_canvas_blurry(preview, config)
                if blurry:
                    rejections["S13.blur"] += 1
                    deps.decisions.reject(
                        title=task.title,
                        url=url,
                        rule_id="S13.blur",
                        detail=(
                            f"image lacks high-frequency detail after canvas scaling "
                            f"(hf={hf_val:.4f} < {config.canvas_blur_hf_threshold}, "
                            f"edge={edge_val:.1f} < {config.canvas_blur_edge_threshold})"
                        ),
                    )
                    if config.keep_rejected_images:
                        _dump_rejected(image, config, task.title, url, "S13.blur")
                    continue

        # Same as Option 1's ``_consume_candidates``: register the URL key on
        # accept so a repeated link is caught by the S10-key check before download.
        seen_keys.add(key)
        _compose_and_save(
            task=task,
            keys=keys,
            candidate=ImageCandidate(
                url=url, page_url="", source="direct", order=len(accepted) + 1, matches_model=True
            ),
            used_url=url,
            source_label="direct",
            image=image,
            downloaded=downloaded,
            config=config,
            deps=deps,
            dedup=dedup,
            accepted=accepted,
            rejections=rejections,
            fingerprint=fingerprint,
        )

    notes = ["direct mode: user-supplied image URLs, no scraping"]
    if rejections:
        summary = ", ".join(f"{rule} x{count}" for rule, count in rejections.most_common(4))
        notes.append(f"images rejected: {summary}")
    note = "; ".join(notes)

    if not accepted:
        reason = (
            "no direct image passed the gates"
            if rejections
            else "no image could be downloaded from the supplied URLs"
        )
        LOG.info("[direct] %s | failed: %s", task.title, reason)
        return replace(base, reason=reason, note=note), []

    result = replace(base, status=Status.PARTIAL, images_accepted=len(accepted), note=note)
    result = finalize_and_upload(task, result, accepted, deps, config, keys)
    LOG.info(
        "[direct] %s | %d accepted, status %s", task.title, len(accepted), result.status.value
    )
    return result, accepted if result.status is not Status.FAILED else []

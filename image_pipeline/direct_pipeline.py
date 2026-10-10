"""Option 2: direct image URLs, no scraping.

The sheet row supplies the product's own image URLs; the row trusts them.  Per row:
URL de-duplication -> download -> bytes validation -> furniture cleanup ->
S19 branded-template guard -> white-background gates -> colour rule -> pixel
dedup -> isolate/square/sharpen -> both templates -> upload -> CSVs.

Every stage calls the Option 1 machinery (nothing is forked); the only new
logic is the gate order and the S19 call.  There is deliberately no page
verification (S1-S3), no gallery extraction, no fallback search (S15) and no
URL-heuristic filtering (S4/S5/S12): those exist to prove scraped pages, and
Option 2 never scrapes.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from dataclasses import replace

from config import PipelineConfig
from csv_io import hash_source_urls
from dedup import ProductDedup, compute_fingerprint
from fetch import FetchError, PermanentAPIError
from image_quality import (
    ImageValidationError,
    branded_template_reason,
    run_quality_gates,
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
    _row_is_done,
    finalize_and_upload,
)
from url_filters import dedup_key, detect_brand, display_brand

LOG = logging.getLogger("pipeline.direct")


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

        # The same furniture cleanup Option 1 runs before the ring test: a shop
        # promo strip glued to an edge is cut, not a reason to lose the photo.
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
            continue

        # The white-background gates (S8/S6/S9/S14/S7 + studio rescue): "we need
        # only clear pictures".  Same order and thresholds as Option 1.
        rule_id, detail = run_quality_gates(image, config)
        if rule_id != "ok":
            rejections[rule_id] += 1
            deps.decisions.reject(title=task.title, url=url, rule_id=rule_id, detail=detail)
            continue

        if (
            config.variant_separation_enabled
            and config.color_gate_enabled
            and keys.colour_word
            and not is_display_product(task.title)
        ):
            color_ok, color_detail = verify_image_color(image, keys.colour_word)
            if not color_ok:
                rejections["S16"] += 1
                deps.decisions.reject(title=task.title, url=url, rule_id="S16", detail=color_detail)
                continue

        fingerprint = compute_fingerprint(image)
        duplicate = dedup.duplicate_reason(image, fingerprint=fingerprint)
        if duplicate:
            rejections["S10"] += 1
            deps.decisions.reject(title=task.title, url=url, rule_id="S10", detail=duplicate)
            continue
        review_note = dedup.review_shape_match(image, fingerprint=fingerprint)
        if review_note:
            deps.decisions.keep(title=task.title, url=url, rule_id="S10.review", detail=review_note)

        # Same as Option 1's ``_consume_candidates``: register the URL key on
        # accept so a repeated link is caught by the S10-key check before download.
        seen_keys.add(key)
        _compose_and_save(
            task=task,
            candidate=ImageCandidate(
                url=url, page_url="", source="direct", order=len(accepted) + 1, matches_model=True
            ),
            used_url=url,
            source_label="direct",
            image=image,
            config=config,
            deps=deps,
            dedup=dedup,
            accepted=accepted,
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
    result = finalize_and_upload(task, result, accepted, deps, config)
    LOG.info(
        "[direct] %s | %d accepted, status %s", task.title, len(accepted), result.status.value
    )
    return result, accepted if result.status is not Status.FAILED else []

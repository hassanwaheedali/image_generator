"""Per-product pipeline, upload gate (S14) and the batch runner.

Shared mutable state is limited to: the decision log (own lock), the banner cache
(own lock) and the outputs written by the runner under one lock after each product.
Per-product state - including the ``ProductDedup`` instance and the accepted image
list - is created inside :func:`process_product` and never shared (S10).
"""

from __future__ import annotations

import csv
import io
import logging
import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from PIL import Image

from compositing import (
    PreparedTemplate,
    apply_template,
    isolate_main_product,
    prepare_template,
    product_content_bbox,
    sharpen,
    to_jpeg_bytes,
    to_square_canvas,
)
from config import (
    FAILED_HEADER,
    OUTPUT_HEADER,
    REPORT_HEADER,
    REVIEW_HEADER,
    PipelineConfig,
)
from csv_io import (
    append_checkpoint,
    atomic_write_csv,
    atomic_write_text,
    hash_source_urls,
    safe_slug,
    save_banner_cache,
    save_image_atomic,
    sha256_bytes,
    timestamp_now,
)
from decisions import DecisionLog
from dedup import ProductDedup
from fetch import FetchError, PermanentAPIError
from gallery_extractors import ExtractionContext, extract_candidates, shopify_json, woo_json
from image_quality import (
    ImageValidationError,
    dimensions_from_header,
    official_gallery_pass,
    probe_verdict,
    run_quality_gates,
    sharpness_score,
    strip_promo_band,
    trim_edge_seams,
    validate_image_bytes,
    verify_image_color,
)
from model_matching import (
    build_model_keys,
    is_display_product,
    near_miss_codes,
    relaxed_page_match,
    url_belongs_to_other_model,
)
from models import (
    AcceptedImage,
    CheckpointEntry,
    ImageCandidate,
    ImageRole,
    ModelKeys,
    PageCandidate,
    PageMatchResult,
    ProductResult,
    ProductTask,
    SourceSearchReport,
    Status,
)
from page_verification import (
    MatchSettings,
    page_body_text,
    strip_unwanted_regions,
    verify_page,
)
from protocols import BinaryResponse, ImageFetcher, ImageHost, PageFetcher, SourceSearcher
from url_filters import (
    dedup_key,
    detect_brand,
    display_brand,
    has_bad_keyword,
    has_image_extension,
    host_is_official,
    is_excluded_host,
    is_fetchable_product_url,
    prioritize_source_urls,
    upscale_candidates,
    url_filename,
)
from variant_terms import identity_alpha_tail

LOG = logging.getLogger("pipeline")


# --------------------------------------------------------------------------- #
# dependencies
# --------------------------------------------------------------------------- #
@dataclass
class PipelineDeps:
    """Everything the pipeline needs from the outside world (all injectable)."""

    page_fetcher: PageFetcher
    image_fetcher: ImageFetcher
    image_host: ImageHost
    decisions: DecisionLog
    banner_cache: dict[str, str]
    banner_cache_lock: threading.Lock
    #: Optional: finds candidate pages when the sheet's URLs did not solve a row.
    #: ``None`` (or ``config.fallback_enabled=False``) keeps the original behaviour.
    source_searcher: SourceSearcher | None = None


class DryRunHost:
    """``ImageHost`` used with ``--dry-run``: writes nothing, returns local paths."""

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir
        self.calls = 0

    def upload(self, data: bytes, filename: str) -> str:
        self.calls += 1
        return f"file://{self._output_dir / 'images' / filename}"


#: Gallery sources that come from the page's own curated product gallery.  ``whole_page``
#: is deliberately absent: that fallback scrapes every image on the page, so it must keep
#: passing the strict tests.  Used for the official-gallery S7 acceptance (see image_quality).
CURATED_GALLERY_SOURCES: frozenset[str] = frozenset(
    {
        "gallery_container",
        "registry:dawlance",
        "registry:haier",
        "registry:magento",
        "json_ld",
        "embedded_json",
        "state_json",
        "dom_audit",
    }
)

#: The subset that really is the product's own gallery, used for the download allowance:
#: ``dom_audit`` is deliberately absent - those nodes exist only so that S2/S3 can reject
#: them in the log, so they must never enlarge a page's download budget.
GALLERY_IMAGE_SOURCES: frozenset[str] = CURATED_GALLERY_SOURCES - {"dom_audit"}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _image_dir(config: PipelineConfig, title: str, role: ImageRole) -> Path:
    return config.images_dir / safe_slug(title) / role.value


def _save_local(image: Image.Image, config: PipelineConfig, title: str, role: ImageRole, name: str) -> Path:
    path = _image_dir(config, title, role) / name
    save_image_atomic(image, path, jpeg_quality=config.jpeg_quality)
    return path


# --------------------------------------------------------------------------- #
# S14 - the upload gate
# --------------------------------------------------------------------------- #
def is_public_url(url: str) -> bool:
    """True for a real ``http(s)`` URL.  A live run may never publish a local path."""
    return url.startswith(("http://", "https://"))


def finalize_and_upload(
    task: ProductTask,
    result: ProductResult,
    images: list[AcceptedImage],
    deps: PipelineDeps,
    config: PipelineConfig,
) -> ProductResult:
    """Safeguard S14: composite, then upload every image to **both** platforms.

    Nothing is uploaded until the product's final selection is complete, and the
    product either publishes on both platforms or on neither: any upload failure
    discards the product instead of publishing a half-filled row.
    """
    if not images:
        return replace(
            result,
            status=Status.FAILED,
            digi_urls=(),
            alfa_urls=(),
            reason=result.reason or "no image passed the quality gates",
        )

    digi_urls: list[str] = []
    alfa_urls: list[str] = []
    try:
        for index, image in enumerate(images, start=1):
            stem = f"{safe_slug(task.title)}-{index:02d}"
            if config.dry_run:
                digi_urls.append(f"file://{image.digi_path.resolve()}")
                alfa_urls.append(f"file://{image.alfa_path.resolve()}")
                continue
            digi_urls.append(
                deps.image_host.upload(
                    image.digi_path.read_bytes(), f"{stem}-digimall.jpg"
                )
            )
            alfa_urls.append(
                deps.image_host.upload(
                    image.alfa_path.read_bytes(), f"{stem}-alfamall.jpg"
                )
            )
    except PermanentAPIError:
        # Invalid key / quota / forbidden: retrying cannot help and nothing may be
        # published.  The runner flushes the checkpoint and aborts the whole run.
        LOG.error("[fatal] image host rejected the upload permanently for %r", task.title)
        raise
    except (FetchError, OSError) as exc:
        LOG.error("[fatal] upload failed for %r: %s", task.title, exc)
        return replace(
            result,
            status=Status.FAILED,
            digi_urls=(),
            alfa_urls=(),
            reason="image host upload failed after retries",
        )

    if not digi_urls or not alfa_urls:
        return replace(result, status=Status.FAILED, digi_urls=(), alfa_urls=(), reason="upload produced no URL")

    try:
        digi_urls.append(ensure_banner_url(config, deps, "digimall"))
        alfa_urls.append(ensure_banner_url(config, deps, "alfamall"))
    except PermanentAPIError:
        raise
    except (FetchError, OSError) as exc:
        LOG.error("[fatal] banner upload failed for %r: %s", task.title, exc)
        return replace(
            result,
            status=Status.FAILED,
            digi_urls=(),
            alfa_urls=(),
            reason="banner upload failed after retries",
        )

    if not config.dry_run and not all(is_public_url(url) for url in (*digi_urls, *alfa_urls)):
        # Defence in depth: a local path must never reach an import CSV.  If it does, the
        # row is discarded instead of publishing something the shop cannot fetch.
        LOG.error("[fatal] refusing to publish a local path for %r", task.title)
        return replace(
            result,
            status=Status.FAILED,
            digi_urls=(),
            alfa_urls=(),
            reason="a live run produced a non-http image URL",
        )

    status = Status.OK if len(images) >= config.min_required_images else Status.PARTIAL
    return replace(
        result,
        status=status,
        digi_urls=tuple(digi_urls),
        alfa_urls=tuple(alfa_urls),
        images_accepted=len(images),
        reason="",
    )


def _banner_cache_key(config: PipelineConfig, digest: str) -> str:
    """Cache key for a banner upload - scoped to the run mode.

    A ``--dry-run`` returns ``file://`` paths, so its entries must never be reused by a real
    run (and vice versa).  Prefixing the key with the mode keeps the two apart in the shared
    ``state/banner_urls.json`` file.
    """
    return f"{'dry' if config.dry_run else 'live'}:{digest}"


def ensure_banner_url(config: PipelineConfig, deps: PipelineDeps, role: str) -> str:
    """Uploaded closing-banner URL, cached in ``state/banner_urls.json`` by content hash."""
    asset_key = "digimall_banner" if role == "digimall" else "alfamall_banner"
    path = config.asset_paths[asset_key]
    data = path.read_bytes()
    digest = _banner_cache_key(config, sha256_bytes(data))
    with deps.banner_cache_lock:
        cached = deps.banner_cache.get(digest)
        if cached:
            return cached
        jpeg = to_jpeg_bytes(Image.open(io.BytesIO(data)), quality=config.jpeg_quality)
        url = deps.image_host.upload(jpeg, f"{role}-closing-banner.jpg")
        deps.banner_cache[digest] = url
        save_banner_cache(config.banner_cache_path, deps.banner_cache)
        return url


# --------------------------------------------------------------------------- #
# one product
# --------------------------------------------------------------------------- #
def relax_s2_when_description_only(
    candidates: list[ImageCandidate],
) -> list[ImageCandidate]:
    """Drop the S2 flag when a page has *nothing but* description-region images.

    Woo/shopify builds often render the whole gallery inside the description
    article; rejecting every image leaves the product empty while the owner can
    see five correct photos on screen (ES-24AR02W on surmawala).  A page that
    also has a normal gallery keeps S2 at full strength.
    """
    if candidates and all("S2" in candidate.dom_flags for candidate in candidates):
        return [
            replace(c, dom_flags=tuple(f for f in c.dom_flags if f != "S2"))
            for c in candidates
        ]
    return candidates


def _decide_candidate(
    candidate: ImageCandidate,
    keys: ModelKeys,
    seen_keys: set[str],
) -> tuple[str, str] | None:
    """Cheap URL/DOM filters (S5, S4, S2, S3, S10-key).  ``None`` means "go on"."""
    key = dedup_key(candidate.url)
    if key in seen_keys:
        return "S10", "same file already queued for this product"
    if not has_image_extension(candidate.url):
        return "S5", "URL does not have an image file extension"
    if has_bad_keyword(candidate.url):
        return "S5", "bad keyword in image URL"
    # Whole-token matching cannot see a logo inside a decorated file name
    # ("cropped-logooooooo.png" - a site branding image that passed S7 because
    # the logo itself sits on a clean white background).  Substring, not token.
    if "logo" in url_filename(candidate.url).lower():
        return "S5", "image filename contains 'logo' (site branding)"
    if url_belongs_to_other_model(candidate.url, keys):
        return "S4", "image URL belongs to another model"
    if "S2" in candidate.dom_flags:
        return "S2", "image sits inside a description/article region"
    if "S3" in candidate.dom_flags:
        return "S3", "image is linked to a different page"
    if candidate.source == "whole_page" and not candidate.matches_model:
        return "S12", "whole-page fallback needs the model key in the image URL"
    return None


def process_product(
    task: ProductTask,
    deps: PipelineDeps,
    config: PipelineConfig,
    cancel_event: threading.Event | None = None,
) -> tuple[ProductResult, list[AcceptedImage]]:
    """The per-product flow, one stage at a time.

    Stage 1 recognition -> 2 the sheet's own pages -> 3 the fallback chain (only when
    the row is still unsolved) -> 4 image gates -> 5 compose -> 6 upload -> 7 status.
    Every stage appends a line to ``stages`` so the log shows the whole step-by-step
    path of a row.
    """
    stages: list[str] = []
    keys = build_model_keys(
        task.title,
        allow_title_only=config.allow_title_only_verification,
        unbalanced_bracket_keeps_text=config.unbalanced_bracket_keeps_text,
        bracketed_key_fallback=config.use_bracketed_keys_as_fallback,
        brand_match_max_extra_tokens=config.brand_match_max_extra_tokens,
    )
    brand = detect_brand(task.source_urls[0] if task.source_urls else "", task.title)
    base = ProductResult(
        task_index=task.index,
        title=task.title,
        status=Status.FAILED,
        brand=display_brand(brand, task.title),
        source_url_hash=hash_source_urls(task.source_urls),
    )
    if not keys.all_keys and not keys.keywords:
        # Owner 2026-10-09: some products carry no model code at all - "Dawlance
        # Dual Pro 30 Inverter T3" *is* the identity.  Such rows continue with
        # keyword matching (S1 rule "keyword"); only a title with neither a code
        # nor usable keywords is unprocessable.
        deps.decisions.reject(
            title=task.title, url="", rule_id="4.1", detail="title has no model-like token"
        )
        return replace(base, reason="title has no model-like token"), []

    stages.append(f"1 recognition: keys={list(keys.tokens)} brand={base.brand or '-'}")
    LOG.info(
        "[row %s] start | %s | %d sheet url(s)",
        task.index, task.title, len(task.source_urls),
    )

    urls = prioritize_source_urls(task.source_urls, brand)
    settings = MatchSettings.from_config(config)
    dedup = ProductDedup(
        hard_threshold=config.dedup_hamming_threshold,
        soft_threshold=config.dedup_hamming_soft_threshold,
        corr_threshold=config.dedup_corr_threshold,
        hard_corr_threshold=config.dedup_hard_corr_threshold,
    )

    accepted: list[AcceptedImage] = []
    seen_keys: set[str] = set()
    notes: list[str] = []
    relaxed_notes: list[str] = []
    #: Pages that matched only through the tolerated spelling, kept for the last resort
    #: pass: (url, source label, was a fallback page).
    tolerated_pages: list[tuple[str, str, bool]] = []
    rejections: Counter[str] = Counter()
    pages_tried = 0
    pages_rejected = 0
    budget = _DownloadBudget(
        config.image_full_download_max_per_page,
        config.image_full_download_max_per_row,
    )
    salvage: list[SalvageOffer] = []

    # ---- stage 2 & 3: sheet URLs and fallback search -----------------------
    # Customer & Owner rules:
    # 1. Official brand pages always take priority over third-party marketplace links
    #    (e.g. Daraz, AlfaMall). When sheet URLs are non-official, query official brand
    #    sources first for high-quality studio photos.
    # 2. Multi-image quota: a product requires at least `min_required_images` (2+).
    #    Never stop early when only 1 image has been found; keep searching fallback
    #    sources until the required quota is reached.
    official_sheet_urls = [u for u in urls if host_is_official(u, brand)]
    non_official_sheet_urls = [u for u in urls if not host_is_official(u, brand)]
    sheet_pages_tried = 0
    sheet_pages_rejected = 0
    visited_page_urls: set[str] = set()

    def _process_candidate_url(
        page_url: str,
        source_label: str,
        is_fallback: bool,
    ) -> bool:
        nonlocal pages_tried, pages_rejected, sheet_pages_tried, sheet_pages_rejected
        if cancel_event is not None and cancel_event.is_set():
            return False
        if _row_is_done(accepted, config):
            return False
        if page_url in visited_page_urls:
            return True
        visited_page_urls.add(page_url)

        # Dead hosts: the exclusion list already stops the fallback *search* from
        # proposing them, but a dead host pasted into the sheet itself used to burn
        # every retry x timeout before failing (live: fahadelectronics.pk in the
        # 98C8L row, 4 attempts x 5s connect).  Refuse it before any fetch.
        if is_excluded_host(urlsplit(page_url).netloc.lower(), config.excluded_hosts):
            pages_rejected += 1
            if not is_fallback:
                sheet_pages_rejected += 1
            deps.decisions.reject(
                title=task.title,
                url=page_url,
                rule_id="S15" if is_fallback else "S11",
                detail="host is on the excluded list (known unreachable) - not fetched",
            )
            return True

        if not is_fetchable_product_url(
            page_url,
            block_collections_without_products=config.block_collections_without_products,
        ):
            pages_rejected += 1
            if not is_fallback:
                sheet_pages_rejected += 1
            rule_id = "S15" if is_fallback else "S11"
            detail = (
                "fallback candidate is not a product page URL"
                if is_fallback
                else "not a product page URL"
            )
            deps.decisions.reject(
                title=task.title, url=page_url, rule_id=rule_id, detail=detail
            )
            return True

        pages_tried += 1
        if not is_fallback:
            sheet_pages_tried += 1
        outcome = _visit_page(
            task=task,
            page_url=page_url,
            source_label=source_label,
            is_fallback=is_fallback,
            keys=keys,
            brand=brand,
            settings=settings,
            config=config,
            deps=deps,
            dedup=dedup,
            seen_keys=seen_keys,
            accepted=accepted,
            rejections=rejections,
            notes=notes,
            budget=budget,
            salvage=salvage,
            defer_tolerated=True,
        )
        if outcome.deferred:
            tolerated_pages.append((outcome.page_url, source_label, is_fallback))
        elif outcome.tolerated:
            relaxed_notes.append(outcome.tolerated)
        if not outcome.fetched or not outcome.matched:
            pages_rejected += 1
            if not is_fallback:
                sheet_pages_rejected += 1
        if outcome.relaxed:
            relaxed_notes.append(outcome.relaxed)
        return True

    fallback_found: list[PageCandidate] | None = None
    fallback_report: SourceSearchReport | None = None
    fallback_used = False

    def _ensure_fallback_searched() -> tuple[list[PageCandidate], SourceSearchReport | None]:
        nonlocal fallback_found, fallback_report, fallback_used
        if fallback_found is None:
            if config.fallback_enabled and deps.source_searcher is not None:
                LOG.info("[row %s] fallback search across retailer sources", task.index)
                found, report = _search_sources(task=task, brand=brand, keys=keys, deps=deps)
                fallback_found = found
                fallback_report = report
                fallback_used = bool(found)
                if report is not None:
                    if report.note:
                        notes.append(report.note)
                    if report.tried:
                        notes.append("sources tried: " + ", ".join(report.tried))
            else:
                fallback_found = []
                fallback_report = None
        return fallback_found, fallback_report

    # Path 1: If sheet contains official manufacturer URLs, visit them first.
    if official_sheet_urls:
        for url in official_sheet_urls:
            if not _process_candidate_url(url, "sheet", is_fallback=False):
                break
        if len(accepted) < config.min_required_images and not _row_is_done(accepted, config):
            for url in non_official_sheet_urls:
                if not _process_candidate_url(url, "sheet", is_fallback=False):
                    break
        if len(accepted) < config.min_required_images and not _row_is_done(accepted, config):
            found_candidates, _ = _ensure_fallback_searched()
            for candidate in found_candidates:
                if not _process_candidate_url(
                    candidate.url, f"fallback:{candidate.origin}", is_fallback=True
                ):
                    break
    else:
        # Path 2: All sheet URLs are non-official marketplaces (or empty).
        # Query official brand sources first!
        found_candidates, _ = _ensure_fallback_searched()
        official_candidates = [c for c in found_candidates if host_is_official(c.url, brand)]
        other_candidates = [c for c in found_candidates if not host_is_official(c.url, brand)]

        for candidate in official_candidates:
            if not _process_candidate_url(
                candidate.url, f"fallback:{candidate.origin}", is_fallback=True
            ):
                break

        # If official candidates yielded fewer than min_required_images, try non-official sheet URLs
        if len(accepted) < config.min_required_images and not _row_is_done(accepted, config):
            for url in non_official_sheet_urls:
                if not _process_candidate_url(url, "sheet", is_fallback=False):
                    break

        # If still fewer than min_required_images, try other fallback candidates (retailers)
        if len(accepted) < config.min_required_images and not _row_is_done(accepted, config):
            for candidate in other_candidates:
                if not _process_candidate_url(
                    candidate.url, f"fallback:{candidate.origin}", is_fallback=True
                ):
                    break

    stages.append(
        f"2 sheet pages: {sheet_pages_tried} tried, {sheet_pages_rejected} rejected, "
        f"{len(accepted)} image(s)"
    )
    if fallback_found is not None:
        stages.append(f"3 fallback search: {len(fallback_found)} candidate page(s)")
    else:
        stages.append("3 fallback search: skipped (row already solved or disabled)")

    # ---- stage 3b: the tolerated spelling, only if nothing better exists -----
    # The sheet's link is sometimes the wrong page for the model in its title (live:
    # DW-7500C row pointing at a page printing DW-7500).  Such a page is published with a
    # flag rather than thrown away - but ONLY when every source above found nothing.  If any
    # exact-match page already yielded even one picture, publishing that single correct image
    # (PARTIAL, flagged) beats topping the row up with a different-variant page's photos:
    # fewer correct images are always better than a mixed, wrong gallery (owner 2026-10-07).
    for page_url, label, is_fallback in tolerated_pages:
        if cancel_event is not None and cancel_event.is_set():
            break
        if accepted:
            break
        pages_tried += 1
        outcome = _visit_page(
            task=task,
            page_url=page_url,
            source_label=label,
            is_fallback=is_fallback,
            keys=keys,
            brand=brand,
            settings=settings,
            config=config,
            deps=deps,
            dedup=dedup,
            seen_keys=seen_keys,
            accepted=accepted,
            rejections=rejections,
            notes=notes,
            budget=budget,
            salvage=salvage,
            defer_tolerated=False,
        )
        if outcome.tolerated:
            relaxed_notes.append(outcome.tolerated)
            stages.append(f"3b tolerated spelling used: {outcome.page_url}")
        if not outcome.fetched or not outcome.matched:
            pages_rejected += 1

    # ---- stage 3c: last-resort salvage (owner decision 2026-10-08) ----------
    # "2 image hi chahye leken jab mil hi nahi rahi toh 1 hi kafi hay": when the row
    # is still below min_required_images, the best already-downloaded picture that
    # only missed a *quality* floor (never identity: wrong model, banner, wrong
    # colour) becomes a published image.  Live case: the Homage HRF-47662-GD golden
    # hero, 9772 bytes against a 12000-byte floor, was the ONLY photo of the right
    # model+colour anywhere - the row failed 0/0/0 while the correct image sat in
    # the reject pile.
    if config.last_resort_salvage_enabled and len(accepted) < config.min_required_images:
        _salvage_row(
            task=task,
            keys=keys,
            config=config,
            deps=deps,
            dedup=dedup,
            seen_keys=seen_keys,
            accepted=accepted,
            rejections=rejections,
            notes=notes,
            stages=stages,
            offers=salvage,
        )

    # ---- stage 4/5 summary -------------------------------------------------
    if rejections:
        summary = ", ".join(
            f"{rule} x{count}" for rule, count in rejections.most_common(4)
        )
        reason_hint = ""
        if rejections.get("S7") and not accepted:
            reason_hint = " (gallery is not white-background)"
        notes.append(f"images rejected: {summary}{reason_hint}")
    note = "; ".join(dict.fromkeys(notes))
    if fallback_used:
        note = f"{note}; fallback used: yes" if note else "fallback used: yes"
    stages.append(f"4 images: {len(accepted)} accepted, {sum(rejections.values())} rejected")

    if not accepted:
        reason = _failure_reason(pages_tried, pages_rejected)
        LOG.info("[stages] %s | %s", task.title, " -> ".join(stages))
        return (
            replace(
                base,
                reason=reason,
                pages_tried=pages_tried,
                pages_rejected=pages_rejected,
                note=note,
            ),
            [],
        )

    result = replace(
        base,
        status=Status.PARTIAL,
        pages_tried=pages_tried,
        pages_rejected=pages_rejected,
        images_accepted=len(accepted),
        note=note,
    )
    stages.append(f"5 compose: {len(accepted)} final set(s) on both templates")
    result = finalize_and_upload(task, result, accepted, deps, config)
    if relaxed_notes and result.status is Status.OK:
        # A relaxed fallback match is published, never silently "ok": the row goes to
        # review_products.csv and to the report with its flag.
        result = replace(result, status=Status.PARTIAL)
    stages.append(
        f"6 upload: {len(result.digi_urls)} digimall / {len(result.alfa_urls)} alfamall URL(s)"
        if result.status is not Status.FAILED
        else "6 upload: failed"
    )
    stages.append(f"7 status: {result.status.value}")
    LOG.info("[stages] %s | %s", task.title, " -> ".join(stages))
    return result, accepted if result.status is not Status.FAILED else []


# --------------------------------------------------------------------------- #
# stage helpers
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _PageOutcome:
    """What one visited page produced (the caller keeps the counters)."""

    matched: bool
    fetched: bool
    page_url: str
    #: Set when a *fallback* page was accepted with the relaxed rule; the reason is what the
    #: run report shows and why the row is listed in review_products.csv.
    relaxed: str = ""
    #: Set when the page matched only through the dropped-trailing-letter tolerance and was
    #: **not** consumed: the row keeps searching, this URL is the last resort.
    deferred: bool = False
    #: Set when such a page *was* consumed (the chain found nothing better): the reason goes
    #: on the row report and into review_products.csv, exactly like ``relaxed``.
    tolerated: str = ""


@dataclass(frozen=True)
class SalvageOffer:
    """A downloaded image that only missed a *quality* floor (S13 lossy payload / bpp).

    Kept aside while the row is still hunting; if the row ends below
    ``min_required_images`` the best offers publish as a last resort (owner decision
    2026-10-08).  ``score`` is bytes-per-pixel: among compressed candidates the least
    compressed one wins.
    """

    score: float
    candidate: ImageCandidate
    used_url: str
    source_label: str
    image: Image.Image


def _offer_salvage(
    offers: list[SalvageOffer],
    offer: SalvageOffer,
    cap: int,
) -> None:
    """Insert an offer best-first, never keeping more than ``cap`` (best survive)."""
    offers.append(offer)
    offers.sort(key=lambda item: item.score, reverse=True)
    del offers[cap:]


def _maybe_offer_salvage(
    *,
    salvage: list[SalvageOffer],
    binary: BinaryResponse,
    candidate: ImageCandidate,
    used_url: str,
    source_label: str,
    config: PipelineConfig,
    task: ProductTask,
    deps: PipelineDeps,
    rejections: Counter[str],
) -> None:
    """Keep a quality-floor reject aside; stage 3c may still need it.

    Re-decodes without the lossy floors (bomb guard and decode still apply).  A
    payload that will not decode is not salvageable by definition, and neither is
    a visibly blurred picture: salvage publishes "kam quality", never "puri
    blurred" (owner 2026-10-08).
    """
    try:
        image = validate_image_bytes(
            binary.content,
            binary.content_type,
            max_bytes=config.max_image_bytes,
            max_pixels=config.max_image_pixels,
        )
    except ImageValidationError:
        return
    score = sharpness_score(image)
    if score < config.salvage_min_sharpness:
        rejections["S8.salvage"] += 1
        deps.decisions.reject(
            title=task.title,
            url=used_url,
            rule_id="S8.salvage",
            detail=(
                f"too blurred for last-resort publication "
                f"(sharpness {score:.3f} < {config.salvage_min_sharpness:.3f})"
            ),
        )
        return
    pixels = max(1, image.size[0] * image.size[1])
    _offer_salvage(
        salvage,
        SalvageOffer(
            score=len(binary.content) / float(pixels),
            candidate=candidate,
            used_url=used_url,
            source_label=source_label,
            image=image,
        ),
        cap=config.target_images_per_product,
    )


def _salvage_row(
    *,
    task: ProductTask,
    keys: ModelKeys,
    config: PipelineConfig,
    deps: PipelineDeps,
    dedup: ProductDedup,
    seen_keys: set[str],
    accepted: list[AcceptedImage],
    rejections: Counter[str],
    notes: list[str],
    stages: list[str],
    offers: list[SalvageOffer],
) -> None:
    """Stage 3c: publish quality-floor rejects when nothing better exists.

    Identity is still non-negotiable: a wrong-colour sibling from the same site is
    exactly what must NOT slip in (owner 2026-10-08: the safeguard "should only reject
    wrong product color and pick one correct color image"), so the colour check runs
    here even when the S16 gate is off.  Everything else already passed its page's
    model match - only compression stood between it and the row.
    """
    before = len(accepted)
    for offer in offers:
        if _row_is_done(accepted, config):
            break
        if dedup.is_duplicate(offer.image):
            continue
        if keys.colour_word and not is_display_product(task.title):
            color_ok, color_detail = verify_image_color(offer.image, keys.colour_word)
            if not color_ok:
                rejections["S16.salvage"] += 1
                deps.decisions.reject(
                    title=task.title,
                    url=offer.used_url,
                    rule_id="S16.salvage",
                    detail=f"wrong colour for the last-resort slot too - {color_detail}",
                )
                continue
        deps.decisions.keep(
            title=task.title,
            url=offer.used_url,
            rule_id="S13.salvage",
            detail=(
                "below the quality floor but the only real photo of this product found "
                "- published as last resort (owner rule 2026-10-08)"
            ),
        )
        seen_keys.add(dedup_key(offer.candidate.url))
        _compose_and_save(
            task=task,
            candidate=offer.candidate,
            used_url=offer.used_url,
            source_label=offer.source_label,
            image=offer.image,
            config=config,
            deps=deps,
            dedup=dedup,
            accepted=accepted,
        )
    salvaged = len(accepted) - before
    if salvaged:
        notes.append(
            f"last-resort salvage: {salvaged} below-floor image(s) published "
            "- no better photo of this product exists"
        )
        stages.append(f"3c salvage: {salvaged} below-floor image(s) published")


class _DownloadBudget:
    """Caps on full image downloads: per *page* and per *row* (plan.md 11.5 step 4).

    Two levels on purpose.  A page that serves nothing but dark marketing renders must
    not consume the row's whole allowance and starve the next candidate page - found
    live on Dawlance, where the brand page hid the retail candidates completely.

    A brand's *own* verified page is the documented exception: ``_page_download_allowance``
    raises that single page's cap to the size of its curated gallery, because those are the
    manufacturer's own product shots and a gallery routinely holds more than the per-page
    default (live: the DW 7500 TWIN TUB CLEAR LID (WHITE) page publishes 10 photos and the
    old 6-download ceiling silently ignored four of them).
    """

    def __init__(self, per_page: int, per_row: int) -> None:
        self._per_page = max(1, per_page)
        self._row_left = max(1, per_row)
        self._page_left: dict[str, int] = {}

    def grant(self, page_url: str, allowance: int) -> None:
        """Raise this page's allowance (never lower it; the row cap still applies).

        Called once per page, before any of its candidates is downloaded.
        """
        key = page_url or "-"
        self._page_left[key] = max(self._page_left.get(key, 0), max(1, allowance))

    def take(self, page_url: str) -> bool:
        """Consume one download slot for this page; ``False`` when a cap is reached."""
        if self._row_left <= 0:
            return False
        key = page_url or "-"
        left = self._page_left.setdefault(key, self._per_page)
        if left <= 0:
            return False
        self._page_left[key] = left - 1
        self._row_left -= 1
        return True

    def page_left(self, page_url: str) -> int:
        return self._page_left.setdefault(page_url or "-", self._per_page)

    def page_exhausted(self, page_url: str) -> bool:
        """This page has spent its allowance - move on to the next candidate page."""
        return self.page_left(page_url) <= 0

    @property
    def exhausted(self) -> bool:
        """The whole row has spent its allowance - stop downloading for this row."""
        return self._row_left <= 0


def _row_is_done(accepted: list[AcceptedImage], config: PipelineConfig) -> bool:
    """Enough images for this row: the customer target (3), capped by the maximum."""
    goal = min(config.target_images_per_product, config.max_images_per_product)
    return len(accepted) >= max(1, goal)


def _page_download_allowance(
    candidates: list[ImageCandidate],
    config: PipelineConfig,
) -> int:
    """How many full downloads one page may spend.

    Ordinary scraped pages keep ``image_full_download_max_per_page``: a page of dark
    marketing renders must not eat the row's allowance and starve the next candidate
    page (found live on Dawlance).

    A page that offers a *curated gallery* (a real gallery container, the brand's own
    extractor, JSON-LD or an embedded product payload) is different - those are the
    product's own pictures, and a gallery routinely holds more of them than the
    per-page default (live: the Dawlance DW 7500 TWIN TUB CLEAR LID (WHITE) page
    publishes 10 photos, Surmawala's DW-7500 page carries its own set; a 6-download
    ceiling silently ignored the rest).  There the gallery is read in full, never more
    than the row may publish and never more than the row's own download budget.
    """
    curated = sum(1 for candidate in candidates if candidate.source in GALLERY_IMAGE_SOURCES)
    if curated <= 0:
        return config.image_full_download_max_per_page
    return max(
        config.image_full_download_max_per_page,
        min(curated, config.max_images_per_product, config.image_full_download_max_per_row),
    )


def _search_sources(
    *,
    task: ProductTask,
    brand: str | None,
    keys: ModelKeys,
    deps: PipelineDeps,
) -> tuple[list[PageCandidate], SourceSearchReport | None]:
    """Stage 3 lookup.  Never raises: a broken source must not break the row."""
    if deps.source_searcher is None:
        return [], None
    try:
        found, report = deps.source_searcher.find_candidates(
            title=task.title, brand=brand, keys=keys
        )
    except Exception as exc:                              # pragma: no cover - defensive
        LOG.warning("fallback search failed for %r: %s", task.title, exc)
        deps.decisions.reject(
            title=task.title, url="", rule_id="S15", detail=f"fallback search failed: {exc}"
        )
        return [], None
    return list(found), report


def _visit_page(
    *,
    task: ProductTask,
    page_url: str,
    source_label: str,
    is_fallback: bool,
    keys: ModelKeys,
    brand: str | None,
    settings: MatchSettings,
    config: PipelineConfig,
    deps: PipelineDeps,
    dedup: ProductDedup,
    seen_keys: set[str],
    accepted: list[AcceptedImage],
    rejections: Counter[str],
    notes: list[str],
    budget: _DownloadBudget,
    salvage: list[SalvageOffer],
    defer_tolerated: bool = False,
) -> _PageOutcome:
    """Fetch one page, verify it (S1, or S15 for a fallback candidate), extract, gate.

    ``defer_tolerated`` is set while the row is still looking for a page that *prints* the
    model: a page matching only through the dropped-trailing-letter tolerance is then
    returned untouched, so the caller can come back to it as a flagged last resort.
    """
    LOG.info("[row %s] fetching %s", task.index, page_url)
    try:
        response = deps.page_fetcher.fetch_page(page_url)
    except (PermanentAPIError, FetchError) as exc:
        rule = "S15.fetch" if is_fallback else "S11.fetch"
        rejections[rule] += 1
        deps.decisions.reject(
            title=task.title, url=page_url, rule_id=rule, detail=str(exc)
        )
        return _PageOutcome(False, False, page_url)

    resolved_url = response.url or page_url
    soup = strip_unwanted_regions(_parse(response.text), resolved_url)
    match = verify_page(resolved_url, response.text, keys, brand=brand, settings=settings)
    relaxed = ""
    if _relaxed_rescue_applies(match, is_fallback):
        # Owner decision 2026-10-06: a fallback page that names the brand and carries the
        # model's own number publishes its pictures and flags the row for manual review,
        # instead of failing.  Sheet URLs never come here.
        relaxed = _relaxed_fallback_note(
            page_url=resolved_url, soup=soup, keys=keys, brand=brand, config=config
        )
        if relaxed:
            deps.decisions.keep(
                title=task.title,
                url=resolved_url,
                rule_id="S15.relaxed",
                detail=f"fallback page accepted with a flag - {relaxed}",
            )
            match = replace(match, strength=max(1, match.strength), detail=relaxed, match_rule="relaxed")
    if not match.accepted:
        rejections["S15" if is_fallback else "S1"] += 1
        detail = f"page strength 0 - {match.detail}"
        if is_fallback:
            detail = f"fallback candidate rejected - {match.detail}"
        deps.decisions.reject(
            title=task.title, url=resolved_url, rule_id="S15" if is_fallback else "S1",
            detail=detail,
        )
        return _PageOutcome(False, True, resolved_url)

    tolerated_reason = ""
    if match.tolerated and config.tolerated_spelling_as_last_resort:
        tolerated_reason = _tolerated_spelling_reason(match.page_name, keys)
        if defer_tolerated:
            # Live case (2026-10-06): the sheet row says DW-7500C and the link it carries
            # points at a page printing DW-7500 - the wrong page for that model.  The page
            # is kept for later, and the row goes on to look for one that prints the model.
            deps.decisions.keep(
                title=task.title,
                url=resolved_url,
                rule_id="S15.tolerated",
                detail=f"deferred as last resort - {tolerated_reason}",
            )
            return _PageOutcome(True, True, resolved_url, deferred=True)

    notes.extend(match.variant_notes)
    if relaxed:
        notes.append(f"flagged: relaxed fallback match - {relaxed}")
    if tolerated_reason:
        notes.append(f"flagged: tolerated spelling - {tolerated_reason}")
        deps.decisions.keep(
            title=task.title,
            url=resolved_url,
            rule_id="S15.tolerated",
            detail=f"published as last resort - {tolerated_reason}",
        )
    if is_fallback:
        notes.append(f"fallback page: {source_label}")
    context = ExtractionContext(
        page_url=resolved_url,
        soup=soup,
        keys=keys,
        strength=match.strength,
        brand=brand,
        raw_html=response.text,
        match_rule=match.match_rule,
        brand_match_min_length=config.brand_match_min_length,
        brand_match_min_digits=config.brand_match_min_digits,
    )
    candidates = extract_candidates(context)
    if len(candidates) < config.target_images_per_product:
        candidates = _extend_with_structured_gallery(
            page_url=resolved_url,
            html=response.text,
            soup=soup,
            candidates=candidates,
            keys=keys,
            match=match,
            config=config,
            deps=deps,
        )
    if not candidates:
        deps.decisions.reject(
            title=task.title,
            url=resolved_url,
            rule_id="S12",
            detail="no gallery and no model-bearing image URL on this page",
        )
        return _PageOutcome(True, True, resolved_url)

    _consume_candidates(
        task=task,
        page_url=resolved_url,
        candidates=candidates,
        keys=keys,
        config=config,
        deps=deps,
        dedup=dedup,
        seen_keys=seen_keys,
        accepted=accepted,
        rejections=rejections,
        official_page=host_is_official(resolved_url, brand),
        source_label=source_label,
        budget=budget,
        salvage=salvage,
    )
    return _PageOutcome(True, True, resolved_url, relaxed, tolerated=tolerated_reason)


def _parse(html: str) -> BeautifulSoup:
    """Parse with lxml (the fast backend); kept in one place for auditability."""
    return BeautifulSoup(html, "lxml")


def _extend_with_structured_gallery(
    *,
    page_url: str,
    html: str,
    soup: BeautifulSoup,
    candidates: list[ImageCandidate],
    keys: ModelKeys,
    match: PageMatchResult,
    config: PipelineConfig,
    deps: PipelineDeps,
) -> list[ImageCandidate]:
    """Top up a thin gallery from the product's own JSON (Shopify / WooCommerce).

    Live reason: a Shopify search result carries **one** ``featured_image``, and a
    lazy-loaded HTML gallery can be empty, so a rescued row published a single picture
    where the shop had six.  The product JSON is authoritative and cheap (one request),
    and it is fetched only when the page's own gallery came up short of the target.
    Failures are silent by design - a missing JSON must never fail a row that already
    has candidate pictures.
    """
    if not config.structured_gallery_enabled:
        return candidates
    context = ExtractionContext(
        page_url=page_url,
        soup=soup,
        keys=keys,
        strength=match.strength,
        brand=None,
        raw_html=html,
        match_rule=match.match_rule,
    )
    extra: list[ImageCandidate] = []
    host = urlsplit(page_url).netloc.lower()

    if config.platform_of(f"https://{host}") == "shopify" or shopify_json.looks_like_shopify(page_url, html):
        url = shopify_json.shopify_product_json_url(page_url)
        if url:
            extra = _fetch_and_parse_json(deps, url, shopify_json.parse_product_json, context)
    elif config.platform_of(f"https://{host}") == "woo" or woo_json.looks_like_woo(page_url, html):
        context = replace(context, brand=None)
        for url in woo_json.woo_store_api_urls(page_url):
            extra = _fetch_and_parse_json(deps, url, woo_json.parse_store_products, context)
            if extra:
                break
    if not extra:
        return candidates

    merged = {dedup_key(candidate.url): candidate for candidate in candidates if dedup_key(candidate.url)}
    added = 0
    for candidate in extra:
        key = dedup_key(candidate.url)
        if not key or key in merged:
            continue
        merged[key] = candidate
        added += 1
    if added:
        deps.decisions.keep(
            title="",
            url=page_url,
            rule_id="source",
            detail=(
                f"product JSON supplied {added} more image URL(s) for this page "
                f"(HTML gallery had {len(candidates)})"
            ),
        )
    ordered = sorted(merged.values(), key=lambda candidate: candidate.order)
    return ordered


def _fetch_and_parse_json(
    deps: PipelineDeps,
    url: str,
    parser: Callable[[str, ExtractionContext], list[ImageCandidate]],
    context: ExtractionContext,
) -> list[ImageCandidate]:
    """Fetch a JSON body through the page boundary and parse it.  Never raises."""
    try:
        response = deps.page_fetcher.fetch_page(url)
    except (PermanentAPIError, FetchError):
        return []
    except Exception:                                     # pragma: no cover - defensive
        LOG.debug("unexpected error fetching %s", url, exc_info=True)
        return []
    return parser(response.text, context)


def _relaxed_rescue_applies(match: PageMatchResult, is_fallback: bool) -> bool:
    """May the relaxed fallback tier rescue this rejected page?

    Never for a colour rejection: another colourway is another product (owner decision
    2026-10-07), so the row must keep looking instead of publishing that gallery.
    """
    return not match.accepted and is_fallback and match.match_rule != "colour_conflict"


def _variant_tails(identities: tuple[str, ...]) -> list[str]:
    """The letter tails the model codes carry, e.g. ``["ifga"]`` for ``HRF-538 IFGA``."""
    return [tail for _, tail in map(identity_alpha_tail, identities) if tail]


def _variant_tail_difference(page_name: str, keys: ModelKeys) -> str:
    """``"page shows variant IFPA, sheet model is IFGA"`` when the tails differ.

    A letter tail is the part of a model code that names the variant (``IFGA`` vs
    ``IFPA``), so the reviewer has to see both spellings - a generic "tolerated
    spelling" note would hide the only interesting difference.
    """
    sheet_tails = _variant_tails(keys.identities)
    page_tails = _variant_tails(build_model_keys(page_name).identities)
    other = sorted({tail.upper() for tail in page_tails} - {tail.upper() for tail in sheet_tails})
    if not sheet_tails or not other:
        return ""
    return f"page shows variant {'/'.join(other)}, sheet model is {sheet_tails[0].upper()}"


def _tolerated_spelling_reason(page_name: str, keys: ModelKeys) -> str:
    """One sentence for the report and the review CSV: what the page shows vs the sheet.

    Two shapes only, and both are spelled out for the reviewer:
    * another variant tail - the page shows ``IFPA`` for an ``IFGA`` row (2026-10-07);
    * a dropped trailing letter - the page shows ``DW-7500`` for a ``DW-7500C`` row
      (live case, 2026-10-06).
    """
    tail_difference = _variant_tail_difference(page_name, keys)
    if tail_difference:
        return tail_difference
    shows = near_miss_codes(page_name, keys)
    page_code = shows[0].upper() if shows else "the model without its trailing letter"
    #: A row with neither a model token nor a glued identity (a pure keyword row such as
    #: ``Frost 18W Classic White``) has no code to name.  Indexing ``identities[0]`` there
    #: raised ``tuple index out of range`` and turned the row into a crash instead of a
    #: verdict (live 2026-10-09: the whole Frost row died in the note that explains it).
    if keys.tokens:
        sheet_code = keys.tokens[0]
    elif keys.identities:
        sheet_code = keys.identities[0].upper()
    else:
        return f"page shows {page_code}, sheet model is the product name itself"
    return f"page shows {page_code}, sheet model is {sheet_code}"


def _relaxed_fallback_note(
    *,
    page_url: str,
    soup: BeautifulSoup,
    keys: ModelKeys,
    brand: str | None,
    config: PipelineConfig,
) -> str:
    """Loose acceptance for a *fallback* page: brand + the model's own number.

    Owner decision 2026-10-06: a fallback page that is close enough should publish its
    pictures with a flag for manual review instead of failing the row ("I just need
    images").  Never used for the sheet's own URLs, which stay strict S1.
    """
    if not config.relaxed_fallback_matching or not keys.identities:
        return ""
    text = page_body_text(soup)
    ok, reason = relaxed_page_match(page_url, text, keys, brand)
    return reason if ok else ""


def _failure_reason(pages_tried: int, pages_rejected: int) -> str:
    if pages_tried == 0:
        return "all source URLs rejected as non-product pages"
    if pages_rejected >= pages_tried:
        return "no page matched this model"
    return "pages matched but no image passed quality gates"


def _consume_candidates(
    *,
    task: ProductTask,
    page_url: str,
    candidates: list[ImageCandidate],
    keys: ModelKeys,
    config: PipelineConfig,
    deps: PipelineDeps,
    dedup: ProductDedup,
    seen_keys: set[str],
    accepted: list[AcceptedImage],
    rejections: Counter[str],
    source_label: str,
    budget: _DownloadBudget,
    salvage: list[SalvageOffer],
    official_page: bool = False,
) -> None:
    """Cheap checks, a header probe, one download, the gates, then compose.

    Order matters: URL/DOM filters cost nothing, the 64 KB probe rejects S6/S8 before
    a real download, and only an image that passed every gate is composed and saved.

    The page's download allowance is opened first: a curated gallery may be read in
    full (bounded by the row budget) instead of stopping at the per-page default and
    leaving the rest of the product's own photos unexamined.
    """
    budget.grant(page_url, _page_download_allowance(candidates, config))
    candidates = relax_s2_when_description_only(candidates)
    for candidate in candidates:
        # Stop at the customer *target* (3 by default), not the hard maximum (10):
        # downloading 9-11 images per page when 3 publish was the single biggest
        # per-row time sink (owner run 2026-10-08: ~2 min per product).  Rows that
        # need more pictures raise ``target_images_per_product`` instead.
        if _row_is_done(accepted, config):
            break
        verdict = _decide_candidate(candidate, keys, seen_keys)
        if verdict is not None:
            rule, detail = verdict
            rejections[rule] += 1
            deps.decisions.reject(
                title=task.title, url=candidate.url, rule_id=rule, detail=detail
            )
            continue

        # (a) header-only probe: kills thumbnails and banners without downloading them.
        # Probe the LARGEST rewrite, not the URL as found: Shopify pages list a tiny
        # ``_288x`` variant whose ``_1024x`` sibling is the real asset, and probing the small
        # one rejected the product at S8 before the large form was ever requested.
        if config.image_probe_enabled:
            probe_url = (
                upscale_candidates(candidate.url, official_page)[0]
                if config.url_rewrite_enabled
                else candidate.url
            )
            dimensions = _probe_dimensions(deps, probe_url, page_url, config)
            rule, detail = probe_verdict(
                dimensions,
                min_short_side=config.min_short_side,
                min_long_side=config.min_long_side,
                max_aspect_ratio=config.max_aspect_ratio,
            )
            if rule != "ok":
                rejections[rule] += 1
                deps.decisions.reject(
                    title=task.title,
                    url=candidate.url,
                    rule_id=rule,
                    detail=f"{detail} (header probe, no download)",
                )
                continue

        # (b) the real download: largest rewrite first, then the URL as given
        attempts = (
            upscale_candidates(candidate.url, official_page)
            if config.url_rewrite_enabled
            else (candidate.url,)
        )
        # One allowance slot per *asset*, not per HTTP attempt: the attempts below are the
        # same picture at another size, and charging for each of them starved the next real
        # gallery image (live case: Dawlance DW-7500C official gallery, 4 photos, the last
        # one never downloaded).
        if not budget.take(page_url):
            last_error = (
                "page download allowance spent"
                if budget.page_exhausted(page_url)
                else "row download allowance spent"
            )
            rejections["S13.fetch"] += 1
            deps.decisions.reject(
                title=task.title, url=candidate.url, rule_id="S13.fetch", detail=last_error
            )
            if budget.exhausted or budget.page_exhausted(page_url):
                break
            continue

        binary = None
        used_url = candidate.url
        last_error = ""
        for attempt_url in attempts:
            try:
                binary = deps.image_fetcher.fetch_image(attempt_url, referer=page_url)
                used_url = attempt_url
                break
            except (PermanentAPIError, FetchError) as exc:
                last_error = str(exc)
                continue
        if binary is None:
            rejections["S13.fetch"] += 1
            deps.decisions.reject(
                title=task.title, url=candidate.url, rule_id="S13.fetch", detail=last_error
            )
            if budget.exhausted or budget.page_exhausted(page_url):
                # one line per page, not one per remaining candidate
                break
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
            deps.decisions.reject(
                title=task.title, url=used_url, rule_id=exc.rule_id, detail=exc.detail
            )
            if exc.salvageable and config.last_resort_salvage_enabled:
                _maybe_offer_salvage(
                    salvage=salvage,
                    binary=binary,
                    candidate=candidate,
                    used_url=used_url,
                    source_label=source_label,
                    config=config,
                    task=task,
                    deps=deps,
                    rejections=rejections,
                )
            continue
        # (c) page furniture that is not the product: a shop promo strip glued to an edge,
        # or a thin layout seam touching two edges.  Both cross the S7 ring and used to
        # reject a perfect product photo.  Each cut is recorded with its own rule id, and
        # every gate below still runs on the cleaned image - a coloured background or a
        # lifestyle shot is rejected exactly as before.
        image, band = strip_promo_band(image, config)
        if band is not None:
            side, pixels = band
            deps.decisions.keep(
                title=task.title,
                url=used_url,
                rule_id="S7.band",
                detail=f"removed a {pixels}px promo strip from the {side} edge before the S7 ring test",
            )
        image, seams = trim_edge_seams(image, config)
        for kind, pixels in seams:
            deps.decisions.keep(
                title=task.title,
                url=used_url,
                rule_id="S7.seam",
                detail=f"trimmed a {pixels}px {kind} layout seam before the S7 ring test",
            )

        # Brand-owned pages get the calmer studio bar on every gallery source, not
        # just the curated ones (owner 2026-10-09: "picture verification should not
        # be too strict only in brand sites because they are official" - the grey
        # Kenwood studio shot died on the retail corner bar).  The acceptance still
        # demands neutral corners, and S1 already proved the model on the page.
        official_gallery = official_page
        rule_id, detail = run_quality_gates(image, config, official_gallery=official_gallery)
        if rule_id == "S7" and official_gallery and official_gallery_pass(image, config):
            deps.decisions.keep(
                title=task.title,
                url=used_url,
                rule_id="S7.official",
                detail=(
                    f"official {candidate.source} gallery image for this model; product fills "
                    "the frame but the corners are neutral studio background"
                ),
            )
            rule_id = "ok"
        if rule_id != "ok":
            rejections[rule_id] += 1
            deps.decisions.reject(
                title=task.title, url=used_url, rule_id=rule_id, detail=detail
            )
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
                deps.decisions.reject(
                    title=task.title, url=used_url, rule_id="S16", detail=color_detail
                )
                continue
        if dedup.is_duplicate(image):
            rejections["S10"] += 1
            deps.decisions.reject(
                title=task.title,
                url=used_url,
                rule_id="S10",
                detail="near-duplicate of an already accepted angle",
            )
            continue

        seen_keys.add(dedup_key(candidate.url))
        _compose_and_save(
            task=task,
            candidate=candidate,
            used_url=used_url,
            source_label=source_label,
            image=image,
            config=config,
            deps=deps,
            dedup=dedup,
            accepted=accepted,
        )


def _probe_dimensions(
    deps: PipelineDeps,
    url: str,
    page_url: str,
    config: PipelineConfig,
) -> tuple[int, int] | None:
    """Sheet 11.5 step 3: dimensions from the first bytes only.  Failures are silent."""
    prober = getattr(deps.image_fetcher, "probe_image", None)
    if prober is None:
        return None
    try:
        probed = prober(url, referer=page_url, max_bytes=config.image_probe_bytes)
    except Exception:                                      # pragma: no cover - defensive
        return None
    if probed is None:
        return None
    return dimensions_from_header(probed.content)


def _compose_and_save(
    *,
    task: ProductTask,
    candidate: ImageCandidate,
    used_url: str,
    source_label: str,
    image: Image.Image,
    config: PipelineConfig,
    deps: PipelineDeps,
    dedup: ProductDedup,
    accepted: list[AcceptedImage],
) -> None:
    """Stage 5: isolate, 1:1 centre, light unsharp, both templates, save locally."""
    isolated, degraded = isolate_main_product(image, config)
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
    index = len(accepted) + 1
    raw_path = _save_local(image, config, task.title, ImageRole.RAW, f"{index:02d}-raw.jpg")
    square_path = _save_local(
        square, config, task.title, ImageRole.ISOLATED, f"{index:02d}-square.jpg"
    )
    bbox = product_content_bbox(
        square,
        white_level=config.product_content_white_level,
        min_ratio=config.product_content_min_ratio,
    )
    digi_prepared = _load_prepared_template(config, "digimall_template")
    alfa_prepared = _load_prepared_template(config, "alfamall_template")
    digi_path = _save_local(
        apply_template(square, digi_prepared.base, config, bbox=bbox, prepared=digi_prepared),
        config,
        task.title,
        ImageRole.FINAL_DIGIMALL,
        f"{index:02d}-digimall.jpg",
    )
    alfa_path = _save_local(
        apply_template(square, alfa_prepared.base, config, bbox=bbox, prepared=alfa_prepared),
        config,
        task.title,
        ImageRole.FINAL_ALFAMALL,
        f"{index:02d}-alfamall.jpg",
    )
    accepted.append(
        AcceptedImage(
            source_url=candidate.url,
            dedup_key=dedup_key(candidate.url),
            raw_path=raw_path,
            square_path=square_path,
            digi_path=digi_path,
            alfa_path=alfa_path,
        )
    )
    dedup.add(image, candidate.url)
    deps.decisions.keep(
        title=task.title,
        url=candidate.url,
        rule_id="accept",
        detail=(
            f"source={source_label} gallery={candidate.source} "
            f"degraded_isolation={degraded} downloaded={used_url}"
        ),
    )
    LOG.info("[accept] %s | %s | %s", task.title, source_label, candidate.source)


_ASSET_CACHE: dict[str, Image.Image] = {}
_ASSET_LOCK = threading.Lock()
_PREPARED_TEMPLATE_CACHE: dict[tuple[str, float, int, int], PreparedTemplate] = {}
_PREPARED_LOCK = threading.Lock()


def _load_asset(config: PipelineConfig, key: str) -> Image.Image:
    """Load a template once per run.  Templates are read-only, never per-product data."""
    path = config.asset_paths[key]
    cache_key = str(path)
    with _ASSET_LOCK:
        cached = _ASSET_CACHE.get(cache_key)
        if cached is None:
            cached = Image.open(path).convert("RGBA")
            cached.load()
            _ASSET_CACHE[cache_key] = cached
        return cached


def _load_prepared_template(config: PipelineConfig, key: str) -> PreparedTemplate:
    """Load and prepare a template once per run with double-checked locking."""
    path = config.asset_paths[key]
    cache_key = (
        str(path),
        config.template_top_scan_ratio,
        config.template_header_gap_px,
        config.template_white_cutoff,
    )
    cached = _PREPARED_TEMPLATE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    with _PREPARED_LOCK:
        cached = _PREPARED_TEMPLATE_CACHE.get(cache_key)
        if cached is None:
            raw = _load_asset(config, key)
            cached = prepare_template(raw, config)
            _PREPARED_TEMPLATE_CACHE[cache_key] = cached
        return cached


# --------------------------------------------------------------------------- #
# outputs
# --------------------------------------------------------------------------- #
def write_output_csvs(
    results: list[ProductResult],
    config: PipelineConfig,
    *,
    include_platform_csvs: bool = True,
) -> None:
    """Write the platform CSVs, the failed CSV and the run report (all atomic).

    Rows always follow input order.  ``ok``/``partial`` rows go to the platform
    CSVs, only ``failed`` rows go to ``failed_products.csv``.
    """
    ordered = sorted(results, key=lambda result: result.task_index)
    if include_platform_csvs:
        atomic_write_csv(
            config.digimall_csv,
            OUTPUT_HEADER,
            (
                (result.title, result.brand, "|".join(result.digi_urls))
                for result in ordered
                if result.digi_urls
            ),
        )
        atomic_write_csv(
            config.alfamall_csv,
            OUTPUT_HEADER,
            (
                (result.title, result.brand, "|".join(result.alfa_urls))
                for result in ordered
                if result.alfa_urls
            ),
        )
        atomic_write_csv(
            config.failed_csv,
            FAILED_HEADER,
            ((result.title, result.reason) for result in ordered if result.status is Status.FAILED),
        )
    # The manual-review queue: rows that published something but carry a flag
    # (relaxed fallback match, colour/variant difference).  Written on every run,
    # including --dry-run, because it is a report.
    atomic_write_csv(
        config.review_csv,
        REVIEW_HEADER,
        (
            (result.title, result.status.value, result.note)
            for result in ordered
            if "flagged" in result.note.lower()
        ),
    )
    _write_output_readme(ordered, config, include_platform_csvs=include_platform_csvs)
    atomic_write_csv(
        config.report_csv,
        REPORT_HEADER,
        (
            (
                result.title,
                result.status.value,
                str(result.pages_tried),
                str(result.pages_rejected),
                str(result.images_accepted),
                result.note,
            )
            for result in ordered
        ),
    )


#: Plain-English map of the run folder.  Written on every run so the folder explains
#: itself to whoever opens it next (no tribal knowledge required).
_OUTPUT_README = """# Run output - what is in here
{dry_note}
Generated by `main.py`.  Everything is rewritten from scratch on every run, so this
folder is always the result of the **last** run - never a mix of runs.

**Import files** (give these to the shops):
- `digimall_product_images.csv` - import file for DigiMall: `Title,Brand,Image_Urls`,
  URLs pipe-separated, the last one is always the closing banner.
- `alfamall_product_images.csv` - the same for AlfaMall, with its own image set.

**Reports** (read these, do not import them):
- `failed_products.csv` - rows that produced nothing usable: `Title,Reason`.
- `run_report.csv` - one line per input row: status (`ok`/`partial`/`failed`),
  pages tried, pages rejected, images accepted, note.
- `review_products.csv` - the manual-review queue: rows that published something but
  carry a flag (a relaxed fallback match, a colour/variant difference): `Title,Status,Note`.
- `decisions.jsonl` - the audit trail: one JSON object per accept/reject with the
  rule id (S1..S15) that decided it.
- `pipeline_run.log` - the full log of the run.

**Pictures** - `images/<product>/`, one folder per row:
- `raw/` - the photos exactly as downloaded (proof of what the source showed);
- `isolated/` - after background knock-out, 1:1 squaring and sharpening (1080x1080);
- `final_digimall/` - the final DigiMall picture (product on the DigiMall template);
- `final_alfamall/` - the final AlfaMall picture.

`<product>` is a slug of the row title (`dawlance-semi-automatic-washing-machine-10kg-dw-7500c`),
so the picture sets can be matched back to the CSVs by eye.

## This run

- rows: {rows} | ok: {ok} | partial: {partial} | failed: {failed}
- pictures published: {images} per platform, plus the closing banner
- status meanings: **ok** = published with at least {min_images} image(s), **partial** = published but
  flagged (fewer images than target, or a human should glance at the note column),
  **failed** = nothing published for that row (see `failed_products.csv`).

A row that is *failed* is deliberate, not a crash: the pipeline refuses to publish a
picture it cannot prove belongs to that exact model (see `README.md`, rule S1).
"""


def _write_output_readme(
    results: list[ProductResult],
    config: PipelineConfig,
    *,
    include_platform_csvs: bool,
) -> None:
    """Write ``output/README.md``: the run folder explains itself."""
    ok = sum(1 for result in results if result.status is Status.OK)
    partial = sum(1 for result in results if result.status is Status.PARTIAL)
    failed = sum(1 for result in results if result.status is Status.FAILED)
    # every row's last URL is the banner, so the product pictures are one fewer per published row
    images = sum(max(0, len(result.digi_urls) - 1) for result in results)
    dry_note = (
        ""
        if include_platform_csvs
        else "\n**This folder was written by a --dry-run:** no uploads happened and the import "
        "CSVs below are left as they were by the last real run.\n"
    )
    try:
        atomic_write_text(
            config.output_dir / "README.md",
            _OUTPUT_README.format(
                rows=len(results),
                ok=ok,
                partial=partial,
                failed=failed,
                images=images,
                dry_note=dry_note,
                min_images=config.min_required_images,
            ),
        )
    except OSError as exc:                              # pragma: no cover - disk
        LOG.warning("could not write output/README.md: %s", exc)


# --------------------------------------------------------------------------- #
# batch runner
# --------------------------------------------------------------------------- #
class BatchRunner:
    """Bounded thread pool with resume, one writer lock and graceful interrupts."""

    def __init__(
        self,
        config: PipelineConfig,
        deps: PipelineDeps,
        tasks: list[ProductTask],
        checkpoint: dict[str, CheckpointEntry],
    ) -> None:
        self._config = config
        self._deps = deps
        self._tasks = tasks
        self._checkpoint = checkpoint
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._results: dict[int, ProductResult] = {}
        self._redo = {title.strip().lower() for title in config.redo_titles}

    # ------------------------------------------------------------------ #
    def run(self) -> list[ProductResult]:
        """Process every pending product, writing outputs after each one."""
        pending: list[ProductTask] = []
        with self._lock:
            for task in self._tasks:
                if self._should_skip(task):
                    entry = self._checkpoint[task.title.strip().lower()]
                    self._results[task.index] = ProductResult(
                        task_index=task.index,
                        title=task.title,
                        # a checkpoint written before the brand field existed carries none,
                        # so fall back to reading the brand out of the title
                        brand=entry.brand or display_brand(None, task.title),
                        status=entry.status,
                        digi_urls=entry.digi_urls,
                        alfa_urls=entry.alfa_urls,
                        reason=entry.reason,
                        note="resumed from checkpoint",
                        source_url_hash=entry.source_url_hash,
                    )
                else:
                    pending.append(task)
            self._write_outputs()

        if not pending:
            LOG.info("nothing to do: every product is already published")
            return self._sorted_results()

        interrupted = False
        try:
            with ThreadPoolExecutor(max_workers=self._config.workers) as pool:
                futures = {
                    pool.submit(self._run_one, task): task for task in pending
                }
                for future in as_completed(futures):
                    task = futures[future]
                    try:
                        result, _images = future.result()
                    except PermanentAPIError:
                        self._cancel.set()
                        for pending_future in futures:
                            pending_future.cancel()
                        raise
                    except Exception as exc:            # unexpected: never lose a row
                        LOG.exception("unexpected error for %r", task.title)
                        result = ProductResult(
                            task_index=task.index,
                            title=task.title,
                            status=Status.FAILED,
                            reason=f"unexpected error: {exc}",
                            source_url_hash=hash_source_urls(task.source_urls),
                        )
                    self._record(result)
        except KeyboardInterrupt:
            interrupted = True
            self._cancel.set()
            LOG.warning("interrupted: flushing checkpoint and outputs")
        except PermanentAPIError:
            LOG.error("[fatal] permanent image-host error: flushing checkpoint and aborting")
            self._write_outputs()
            raise
        finally:
            self._write_outputs()
        if interrupted:
            LOG.warning("run stopped early; rerun without --redo to continue")
        return self._sorted_results()

    # ------------------------------------------------------------------ #
    def _should_skip(self, task: ProductTask) -> bool:
        key = task.title.strip().lower()
        if key in self._redo:
            return False
        entry = self._checkpoint.get(key)
        if entry is None:
            return False
        return entry.is_resume_done(hash_source_urls(task.source_urls))

    def _run_one(self, task: ProductTask) -> tuple[ProductResult, list[AcceptedImage]]:
        return process_product(task, self._deps, self._config, self._cancel)

    def _record(self, result: ProductResult) -> None:
        """One writer: checkpoint append + CSV regeneration, under the lock."""
        with self._lock:
            self._results[result.task_index] = result
            try:
                append_checkpoint(self._config.checkpoint_path, result, timestamp=timestamp_now())
            except OSError as exc:                      # pragma: no cover - disk issues
                LOG.error("could not append checkpoint: %s", exc)
            self._write_outputs()

    def _write_outputs(self) -> None:
        write_output_csvs(
            list(self._results.values()),
            self._config,
            include_platform_csvs=not self._config.dry_run,
        )

    def _sorted_results(self) -> list[ProductResult]:
        with self._lock:
            return [self._results[index] for index in sorted(self._results)]


# --------------------------------------------------------------------------- #
# small helpers used by main
# --------------------------------------------------------------------------- #
def csv_escape(value: str) -> str:
    """Small helper kept public for tests that build expected CSV strings."""
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerow([value])
    return buffer.getvalue().strip()



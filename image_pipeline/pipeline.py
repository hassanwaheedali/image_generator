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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path

from bs4 import BeautifulSoup
from PIL import Image

from compositing import (
    apply_template,
    isolate_main_product,
    sharpen,
    to_jpeg_bytes,
    to_square_canvas,
)
from config import FAILED_HEADER, OUTPUT_HEADER, REPORT_HEADER, PipelineConfig
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
from gallery_extractors import ExtractionContext, extract_candidates
from image_quality import (
    ImageValidationError,
    dimensions_from_header,
    official_gallery_pass,
    probe_verdict,
    run_quality_gates,
    strip_promo_band,
    trim_edge_seams,
    validate_image_bytes,
)
from model_matching import build_model_keys, url_belongs_to_other_model
from models import (
    AcceptedImage,
    CheckpointEntry,
    ImageCandidate,
    ImageRole,
    ModelKeys,
    PageCandidate,
    ProductResult,
    ProductTask,
    SourceSearchReport,
    Status,
)
from page_verification import MatchSettings, strip_unwanted_regions, verify_page
from protocols import ImageFetcher, ImageHost, PageFetcher, SourceSearcher
from url_filters import (
    dedup_key,
    detect_brand,
    display_brand,
    has_bad_keyword,
    host_is_official,
    is_fetchable_product_url,
    prioritize_source_urls,
    upscale_candidates,
)

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
        "dom_audit",
    }
)


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
    with deps.banner_cache_lock:
        deps.banner_cache[digest] = url
        save_banner_cache(config.banner_cache_path, deps.banner_cache)
    return url


# --------------------------------------------------------------------------- #
# one product
# --------------------------------------------------------------------------- #
def _decide_candidate(
    candidate: ImageCandidate,
    keys: ModelKeys,
    seen_keys: set[str],
) -> tuple[str, str] | None:
    """Cheap URL/DOM filters (S5, S4, S2, S3, S10-key).  ``None`` means "go on"."""
    key = dedup_key(candidate.url)
    if key in seen_keys:
        return "S10", "same file already queued for this product"
    if has_bad_keyword(candidate.url):
        return "S5", "bad keyword in image URL"
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
    if not keys.all_keys:
        deps.decisions.reject(
            title=task.title, url="", rule_id="4.1", detail="title has no model-like token"
        )
        return replace(base, reason="title has no model-like token"), []

    stages.append(f"1 recognition: keys={list(keys.tokens)} brand={base.brand or '-'}")

    urls = prioritize_source_urls(task.source_urls, brand)
    settings = MatchSettings.from_config(config)
    dedup = ProductDedup(
        hard_threshold=config.dedup_hamming_threshold,
        soft_threshold=config.dedup_hamming_soft_threshold,
        corr_threshold=config.dedup_corr_threshold,
    )

    accepted: list[AcceptedImage] = []
    seen_keys: set[str] = set()
    notes: list[str] = []
    rejections: Counter[str] = Counter()
    pages_tried = 0
    pages_rejected = 0
    budget = _DownloadBudget(
        config.image_full_download_max_per_page,
        config.image_full_download_max_per_row,
    )

    # ---- stage 2: the sheet's own URLs, in the given order -----------------
    for url in urls:
        if cancel_event is not None and cancel_event.is_set():
            break
        if _row_is_done(accepted, config):
            break
        if not is_fetchable_product_url(
            url,
            block_collections_without_products=config.block_collections_without_products,
        ):
            pages_rejected += 1
            deps.decisions.reject(
                title=task.title, url=url, rule_id="S11", detail="not a product page URL"
            )
            continue
        pages_tried += 1
        outcome = _visit_page(
            task=task,
            page_url=url,
            source_label="sheet",
            is_fallback=False,
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
        )
        if not outcome.fetched or not outcome.matched:
            pages_rejected += 1
    stages.append(
        f"2 sheet pages: {pages_tried} tried, {pages_rejected} rejected, "
        f"{len(accepted)} image(s)"
    )

    # ---- stage 3: the fallback chain, only when the row is still unsolved --
    fallback_used = False
    if (
        config.fallback_enabled
        and deps.source_searcher is not None
        and not accepted
        and not _row_is_done(accepted, config)
    ):
        found, report = _search_sources(task=task, brand=brand, keys=keys, deps=deps)
        fallback_used = bool(found)
        if report is not None:
            if report.note:
                notes.append(report.note)
            if report.tried:
                notes.append("sources tried: " + ", ".join(report.tried))
        stages.append(f"3 fallback search: {len(found)} candidate page(s)")
        for candidate in found:
            if cancel_event is not None and cancel_event.is_set():
                break
            if _row_is_done(accepted, config):
                break
            if not is_fetchable_product_url(
                candidate.url,
                block_collections_without_products=config.block_collections_without_products,
            ):
                deps.decisions.reject(
                    title=task.title,
                    url=candidate.url,
                    rule_id="S15",
                    detail="fallback candidate is not a product page URL",
                )
                continue
            pages_tried += 1
            outcome = _visit_page(
                task=task,
                page_url=candidate.url,
                source_label=f"fallback:{candidate.origin}",
                is_fallback=True,
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
            )
            if not outcome.fetched or not outcome.matched:
                pages_rejected += 1
    else:
        stages.append("3 fallback search: skipped (row already solved or disabled)")

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


class _DownloadBudget:
    """Caps on full image downloads: per *page* and per *row* (plan.md 11.5 step 4).

    Two levels on purpose.  A page that serves nothing but dark marketing renders must
    not consume the row's whole allowance and starve the next candidate page - found
    live on Dawlance, where the brand page hid the retail candidates completely.
    """

    def __init__(self, per_page: int, per_row: int) -> None:
        self._per_page = max(1, per_page)
        self._row_left = max(1, per_row)
        self._page_left: dict[str, int] = {}

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
) -> _PageOutcome:
    """Fetch one page, verify it (S1, or S15 for a fallback candidate), extract, gate."""
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

    notes.extend(match.variant_notes)
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
    )
    return _PageOutcome(True, True, resolved_url)


def _parse(html: str) -> BeautifulSoup:
    """Parse with lxml (the fast backend); kept in one place for auditability."""
    return BeautifulSoup(html, "lxml")


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
    official_page: bool = False,
) -> None:
    """Cheap checks, a header probe, one download, the gates, then compose.

    Order matters: URL/DOM filters cost nothing, the 64 KB probe rejects S6/S8 before
    a real download, and only an image that passed every gate is composed and saved.
    """
    for candidate in candidates:
        if len(accepted) >= config.max_images_per_product:
            break
        verdict = _decide_candidate(candidate, keys, seen_keys)
        if verdict is not None:
            rule, detail = verdict
            rejections[rule] += 1
            deps.decisions.reject(
                title=task.title, url=candidate.url, rule_id=rule, detail=detail
            )
            continue

        # (a) header-only probe: kills thumbnails and banners without downloading them
        if config.image_probe_enabled:
            dimensions = _probe_dimensions(deps, candidate.url, page_url, config)
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
            upscale_candidates(candidate.url)
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
            )
        except ImageValidationError as exc:
            rejections[exc.rule_id] += 1
            deps.decisions.reject(
                title=task.title, url=used_url, rule_id=exc.rule_id, detail=exc.detail
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

        # Only a curated gallery on the brand's own verified page gets the S7 acceptance.
        official_gallery = official_page and candidate.source in CURATED_GALLERY_SOURCES
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
    digi_path = _save_local(
        apply_template(square, _load_asset(config, "digimall_template"), config),
        config,
        task.title,
        ImageRole.FINAL_DIGIMALL,
        f"{index:02d}-digimall.jpg",
    )
    alfa_path = _save_local(
        apply_template(square, _load_asset(config, "alfamall_template"), config),
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



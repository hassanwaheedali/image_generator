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
import shutil
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
    MAX_PREFIX_CHARS,
    append_checkpoint,
    atomic_write_csv,
    atomic_write_text,
    filename_prefix,
    guard_windows_name,
    hash_source_urls,
    safe_slug,
    save_banner_cache,
    save_image_atomic,
    sha256_bytes,
    timestamp_now,
)
from decisions import DecisionLog
from dedup import ImageFingerprint, ProductDedup, compute_fingerprint
from fetch import FetchError, PermanentAPIError
from gallery_extractors import ExtractionContext, extract_candidates, shopify_json, woo_json
from image_quality import (
    ImageValidationError,
    dimensions_from_header,
    marketing_panel_split,
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
    url_anchored_to_row,
    url_belongs_to_other_model,
    url_names_foreign_brand,
    url_names_other_capacity_code,
    url_names_other_product_line,
    url_names_sibling_product,
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
from platforms import PLATFORM_KEYS, PlatformPlan, ResolvedPlatform
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
from variant_terms import colour_word_conflict, identity_alpha_tail

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
    #: Which platforms this run publishes and what each does with its banner, decided
    #: once at startup.  Workers read it; nobody may change it mid-run.
    platforms: PlatformPlan
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
        "shopify_json",
        "woo_json",
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
def _final_path_overhead(config: PipelineConfig) -> int:
    """Characters every final path spends outside the product folder name.

    Measured against the widest platform key in the table and the longest filename the
    pipeline can write (``<48-char prefix>_99.jpg``), so the budget holds for a platform
    that is not enabled in this run too.
    """
    widest_key = max(PLATFORM_KEYS, key=len)
    longest_name = 1 + MAX_PREFIX_CHARS + len("_99.jpg")
    return len(str(config.images_dir)) + 1 + len(widest_key) + 1 + longest_name + 1


def _product_folder(config: PipelineConfig, title: str) -> str:
    """Per-product folder name, shortened to keep the deepest path inside the budget.

    Only the slug shrinks, and it keeps a hash tail so two long titles cannot collide on
    one truncated name.  The model-code filename prefix is never touched - that is the
    string the owner reads in the CSV, while the folder is only a place to look.
    """
    slug = safe_slug(title)
    budget = config.max_output_path_chars - _final_path_overhead(config)
    if len(slug) <= budget:
        return slug
    digest = sha256_bytes(title.encode("utf-8"))[:8]
    keep = max(8, budget - len(digest) - 1)
    return guard_windows_name(f"{slug[:keep]}-{digest}")


def _audit_dir(config: PipelineConfig, title: str, role: ImageRole) -> Path:
    return config.audit_dir / _product_folder(config, title) / role.value


def _platform_dir(config: PipelineConfig, title: str, platform_key: str) -> Path:
    return config.images_dir / platform_key / _product_folder(config, title)


def clear_stale_product_files(config: PipelineConfig, deps: PipelineDeps, title: str) -> None:
    """Delete this product's pictures from an earlier run before the row starts.

    A row that published five pictures last time and publishes three now leaves two
    files behind in ``images/<platform>/<product>/``.  The import sheet names three, the
    folder holds five, and the owner's zip-and-import mode ships pictures the sheet never
    listed - with the audit tree rotting the same way.  Cleaning at row start is what
    keeps "what is on disk" equal to "what this run published".

    Only folders named from *this* title are touched, and only for the platforms this run
    publishes: a ``--platforms digi`` re-run must not erase Alfamall's delivered pictures.
    A file that refuses to delete (Windows locks one while a viewer has it open) is not
    worth losing the row over, because every file this run publishes is overwritten by name.
    """
    shutil.rmtree(config.audit_dir / _product_folder(config, title), ignore_errors=True)
    for platform in deps.platforms.platforms:
        shutil.rmtree(_platform_dir(config, title, platform.key), ignore_errors=True)


def _layout_rescue_verdict(
    image: Image.Image, config: PipelineConfig, rule_id: str, detail: str
) -> tuple[bool, str]:
    """Can this S20/S21 layout be published as the product *inside* it?

    Returns ``(rescued, why)``.  The rescue is deliberately indirect: the pipeline does not
    decide that a full-bleed layout is fine (it is not - on a retailer page it stays
    rejected), it asks whether the picture contains one clean product body, isolates it, and
    then lets **every gate re-judge that crop**.  An emblem or an in-box caption sheet answers
    "no": isolation is degraded, or the crop is still full-bleed line art and trips S20/S21
    again.  A photograph of the unit that merely wears a promo strip or a tinted marketing
    panel answers "yes", and what gets published is the crop, not the panel - the owner's rule
    of 2026-10-10: "cut the bottom labels, advertisement marketing labels, and add only product".

    Two safeguards keep this from being a loophole: the trigger is limited to the painted-panel
    branch (line art and caption sheets are refused outright, so the Haier emblem class can
    never come back through here), and S20, S21, S14, S8, S6 and S9 all have to pass again on
    the isolated body.  Only S7 - the border ring - is forgiven, because that ring is the
    marketing panel compose paints over with studio white anyway.

    Measured on the owner's 16 published junk files (2026-10-10 run): **all 16 stay rejected**,
    because every one of them is line art, a caption sheet, or an ad whose isolated body is
    still artwork.  A tinted hero card with a real photograph inside it is the case this exists
    for, and it is rescued.

    """
    if not config.official_layout_rescue:
        return False, ""
    # Only the "painted artwork around the product" branch may be rescued.  S20 fires for two
    # different things and they need opposite answers: a marketing *panel* with a real photo
    # inside it (crop the panel, publish the unit) versus line art - an emblem, a badge sheet
    # - where the artwork IS the whole picture and there is nothing to cut.  S21's caption
    # sheets are in the second group by definition.
    if rule_id != "S20" or not detail.startswith("painted artwork"):
        return False, ""
    isolated, degraded = isolate_main_product(image, config)
    if degraded:
        return False, ""
    # The crop, not the padded canvas, is judged.  The canvas measurement is what taught
    # this the hard way: `to_square_canvas` pads the body out to 1200px with 6px of white,
    # which drops the ink span under S20's full-bleed threshold and rescued 12 of the owner's
    # 16 junk files (every Haier emblem among them).  On the tight crop the content rules keep
    # their teeth - line art stays line art, a caption sheet stays a caption sheet - and the
    # only verdict allowed to be forgiven is S7, the border-ring test, because the ring there
    # is the marketing panel that compose paints over with studio white anyway.
    # No `panel_cut` here: nothing was cut in this path, so S20's painted-artwork branch stays
    # in force - which is what keeps a full-bleed ad render from being rescued.
    rule, _detail = run_quality_gates(isolated, config, official_gallery=True)
    if rule not in ("ok", "S7"):
        return False, ""
    return True, (
        f"official gallery: the {rule_id} layout is marketing artwork around the product, "
        "so the isolated product was judged instead and it passes every gate"
    )


def _dump_rejected(
    image: Image.Image, config: PipelineConfig, title: str, url: str, rule_id: str
) -> None:
    """Keep the bytes of a pixel-gate reject, named after the rule that killed it.

    Owner request, 2026-10-10 - "if rejected, where can we find [it]": a candidate that a
    pixel gate refuses leaves nothing on disk, so "the official product image was never
    scraped" cannot be checked by eye and the argument turns into reasoning about code.
    Written to ``output/audit/<product>/rejected/<rule>-<hash>.jpg``.  Off by default:
    this is a debugging aid, never delivery output.
    """
    folder = _audit_dir(config, title, ImageRole.REJECTED)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{rule_id}-{dedup_key(url)[:10]}.jpg"
    save_image_atomic(image, folder / name, jpeg_quality=config.jpeg_quality)


def _save_audit(
    image: Image.Image, config: PipelineConfig, title: str, role: ImageRole, name: str
) -> Path:
    """Write one evidence file under ``audit/<product>/<role>/``."""
    path = _audit_dir(config, title, role) / name
    save_image_atomic(image, path, jpeg_quality=config.jpeg_quality)
    return path


def _save_final(
    image: Image.Image, config: PipelineConfig, title: str, platform_key: str, name: str
) -> Path:
    """Write one publishable picture under ``images/<platform>/<product>/``."""
    path = _platform_dir(config, title, platform_key) / name
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
    keys: ModelKeys,
) -> ProductResult:
    """Safeguard S14: upload every accepted picture to **all** selected platforms.

    Nothing is uploaded until the product's final selection is complete, and the product
    either publishes on every platform the run selected or on none: any upload failure
    discards the row instead of leaving a shop with half the set.  An empty plan is the
    same "none" - there is no such thing as a row that publishes to zero platforms.
    """
    platforms = deps.platforms.platforms
    if not images or not platforms:
        return _discard(
            result, result.reason or "no image passed the quality gates"
        )
    # The stem the pictures were written under.  Recomputing it here rather than
    # reading it back off the paths keeps the sheet's ``images`` column and the files
    # on disk derived from one call, so they cannot drift apart.
    prefix = filename_prefix(keys, task.title)

    try:
        per_platform = _upload_finals(task, images, deps, config, platforms)
    except PermanentAPIError:
        # Invalid key / quota / forbidden: retrying cannot help and nothing may be
        # published.  The runner flushes the checkpoint and aborts the whole run.
        LOG.error("[fatal] image host rejected the upload permanently for %r", task.title)
        raise
    except (FetchError, OSError) as exc:
        LOG.error("[fatal] upload failed for %r: %s", task.title, exc)
        return _discard(result, "image host upload failed after retries")

    if not all(per_platform.values()):
        return _discard(result, "upload produced no URL")

    try:
        _append_banners(task, prefix, images, deps, config, platforms, per_platform)
    except PermanentAPIError:
        LOG.error("[fatal] image host rejected the closing banner permanently for %r", task.title)
        raise
    except (FetchError, OSError) as exc:
        LOG.error("[fatal] banner upload failed for %r: %s", task.title, exc)
        return _discard(result, "banner upload failed after retries")

    published = tuple((platform.key, tuple(per_platform[platform.key])) for platform in platforms)
    urls = tuple(url for _, per_platform in published for url in per_platform)
    if not config.dry_run and not all(is_public_url(url) for url in urls):
        # Defence in depth: a local path must never reach an import CSV.  If it does, the
        # row is discarded instead of publishing something the shop cannot fetch.
        LOG.error("[fatal] refusing to publish a local path for %r", task.title)
        return _discard(result, "a live run produced a non-http image URL")

    status = Status.OK if len(images) >= config.min_required_images else Status.PARTIAL
    return replace(
        result,
        status=status,
        platform_urls=published,
        name_prefix=prefix,
        images_accepted=len(images),
        reason="",
    )


def _discard(result: ProductResult, reason: str) -> ProductResult:
    """S14's "none": the row publishes nothing, so no platform keeps a URL."""
    return replace(result, status=Status.FAILED, platform_urls=(), reason=reason)


def _upload_finals(
    task: ProductTask,
    images: list[AcceptedImage],
    deps: PipelineDeps,
    config: PipelineConfig,
    platforms: tuple[ResolvedPlatform, ...],
) -> dict[str, list[str]]:
    """Upload one product's whole set on every platform, keeping the platforms apart.

    ``--dry-run`` records the local path instead of calling the host, so the CSVs and the
    picture folders are still the ones a real run would have produced.
    """
    per_platform: dict[str, list[str]] = {platform.key: [] for platform in platforms}
    for index, image in enumerate(images, start=1):
        stem = f"{safe_slug(task.title)}-{index:02d}"
        for platform in platforms:
            final = image.final_path(platform.key)
            if config.dry_run:
                per_platform[platform.key].append(f"file://{final.resolve()}")
            else:
                # The *display* name, not the folder key: this string is what the host and
                # therefore the shop sees, and it has always read "digimall", not "digi".
                name = f"{stem}-{platform.display_name.lower()}.jpg"
                per_platform[platform.key].append(deps.image_host.upload(final.read_bytes(), name))
    return per_platform


def _append_banners(
    task: ProductTask,
    prefix: str,
    images: list[AcceptedImage],
    deps: PipelineDeps,
    config: PipelineConfig,
    platforms: tuple[ResolvedPlatform, ...],
    per_platform: dict[str, list[str]],
) -> None:
    """Close each platform's set with its banner: the shared URL, and a copy in the folder.

    One upload serves every product (the URL is cached by content hash), but the folder of
    each product has to contain the picture the CSV points at, so the bytes are written per
    row under the same ``<code>_N.jpg`` stem - the banner is simply the last index.
    """
    name = f"{prefix}_{len(images) + 1}.jpg"
    for platform in platforms:
        banner_path = platform.banner_path if platform.appends_banner else None
        if banner_path is None:
            continue
        per_platform[platform.key].append(
            ensure_banner_url(config, deps, platform.display_name.lower(), banner_path)
        )
        with Image.open(banner_path) as banner:
            _save_final(banner, config, task.title, platform.key, name)


def _banner_cache_key(config: PipelineConfig, digest: str) -> str:
    """Cache key for a banner upload - scoped to the run mode.

    A ``--dry-run`` returns ``file://`` paths, so its entries must never be reused by a real
    run (and vice versa).  Prefixing the key with the mode keeps the two apart in the shared
    ``state/banner_urls.json`` file.
    """
    return f"{'dry' if config.dry_run else 'live'}:{digest}"


def ensure_banner_url(
    config: PipelineConfig, deps: PipelineDeps, role: str, banner_path: Path
) -> str:
    """Uploaded closing-banner URL, cached in ``state/banner_urls.json`` by content hash."""
    data = banner_path.read_bytes()
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
        shape_iou_min=config.dedup_shape_iou_min,
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
    #: Owner rule 2026-10-11 ("when found in brand official just download done"): once a
    #: page on the brand's own host has delivered an accepted picture, the row publishes
    #: what the brand gave it and searches nothing more - no retailer sheet URL, no
    #: fallback chain.  Every wrong image of that run (Innova for Pulse, Blaze, Glide)
    #: arrived *after* the official page had already delivered; fewer correct images
    #: beat a bigger mixed gallery (owner 2026-10-07), and a PARTIAL status flags the
    #: row for review instead of silently topping it up with another product.
    official_solved = False
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
    # 3. Owner override 2026-10-11: rule 2 stands only while the brand's own site has
    #    delivered nothing.  Once an official page has supplied a picture the row is
    #    solved - the hunt that followed it in the live run is what downloaded Innovia
    #    for Pulse, so no retailer page and no further source is visited after it.
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
        nonlocal official_solved
        if cancel_event is not None and cancel_event.is_set():
            return False
        if _row_is_done(accepted, config):
            return False
        # Owner rule 2026-10-11: the brand's own site already delivered, so nothing else
        # is searched - the retailer hunt that follows a solved row is exactly what
        # downloaded another product in the live run.
        if official_solved:
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
        accepted_before = len(accepted)
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
        if (
            not official_solved
            and len(accepted) > accepted_before
            and host_is_official(outcome.page_url, brand)
        ):
            # The brand's own page delivered - the row is solved (owner 2026-10-11).
            official_solved = True
            stages.append(f"official page solved the row: {outcome.page_url}")
        if config.early_stop_when_image_found and len(accepted) >= 1:
            stages.append(f"early stop: {len(accepted)} image(s) accepted from {outcome.page_url}")
            return False
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
        # Owner rule 2026-10-11: the brand's own site already solved the row, so the
        # retailer hunt never even starts - no source query, no candidate list.
        if official_solved or _row_is_done(accepted, config):
            return [], None

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
    stages.append(f"5 compose: {len(accepted)} final set(s) on every selected template")
    result = finalize_and_upload(task, result, accepted, deps, config, keys)
    if relaxed_notes and result.status is Status.OK:
        # A relaxed fallback match is published, never silently "ok": the row goes to
        # review_products.csv and to the report with its flag.
        result = replace(result, status=Status.PARTIAL)
    stages.append(
        "6 upload: "
        + ", ".join(f"{len(urls)} {key}" for key, urls in result.platform_urls)
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
            keys=keys,
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
    """Enough images for this row: early stop if configured, or customer target capped by max."""
    if config.early_stop_when_image_found and len(accepted) >= 1:
        return True
    goal = min(config.target_images_per_product, config.max_images_per_product)
    return len(accepted) >= max(1, goal)


def _page_publish_goal(candidates: list[ImageCandidate], config: PipelineConfig) -> int:
    """How many images this one page may fill.

    Normally the row target (3): the customer asks for three pictures per product, and
    every extra download is run time.  A page carrying its **own curated gallery** is a
    different thing, and the owner's two reviews of 2026-10-10 showed why the target must
    not be a hard stop there: Haier publishes 6 views of a unit, and Dawlance publishes 11
    slides whose good photos sit *after* the manual sheets in DOM order (their markup runs
    ``_1, _10, _11, _2, _3, …``).  Stopping at the 3rd accept therefore published the
    in-box/manual sheets and never reached the product, which is exactly the complaint -
    "showing 6 images but script download only 1 … remove this hardcoded concept".

    So for such a page the whole gallery is published, bounded by the row's hard cap
    ``max_images_per_product``.  Other pages (a retailer's stray ``<img>`` set) keep the
    target, and :func:`_row_is_done` still decides whether more *pages* are visited, so a
    full official gallery does not invite a retailer top-up.
    """
    goal = min(config.target_images_per_product, config.max_images_per_product)
    if config.limit_gallery_iteration or not config.gallery_publishes_all:
        return goal
    curated = sum(1 for candidate in candidates if candidate.source in GALLERY_IMAGE_SOURCES)
    if curated >= config.gallery_publishes_all_min:
        return config.max_images_per_product
    return goal


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
    if config.limit_gallery_iteration:
        return min(
            config.image_full_download_max_per_page,
            max(config.target_images_per_product * 2, 2),
        )
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
    # Count only candidates that survive the cheap URL/DOM pre-filters. Junk
    # found in the HTML (site logos, social icons, other-model images, links to other pages)
    # all inflate this count and were previously suppressing the Shopify/WooCommerce JSON
    # fetch even though every one of them would be rejected moments later. Using the
    # canonical ``_decide_candidate`` check gives an accurate picture of how many *usable*
    # candidates the HTML produced, without touching performance (no network I/O, pure in-memory ops).
    usable_candidates = [
        c
        for c in candidates
        if c.source != "dom_audit" and _decide_candidate(c, keys, seen_keys) is None
    ]
    if len(usable_candidates) < config.target_images_per_product:
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
        brand=brand,
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
    Never for a brand rejection either (owner 2026-10-11): a page that shows another
    maker's product is another product, full stop - the relaxed "brand + number" route
    must not bring a Panasonic page back for an Orient row.
    """
    return (
        not match.accepted
        and is_fallback
        and match.match_rule not in ("colour_conflict", "other_brand_product")
    )


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
    brand: str | None,
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
        if len(accepted) >= _page_publish_goal(candidates, config):
            # Not `_row_is_done`: this page's own gallery is read to the end (see
            # ``_page_publish_goal``), so a third manual sheet cannot spend the last slot.
            break

        verdict = _decide_candidate(candidate, keys, seen_keys)
        if (
            verdict is None
            and config.capacity_code_gate_enabled
            and url_names_other_capacity_code(candidate.url, keys)
        ):
            # S4's key comparison cannot see a row whose only model code is a capacity
            # pair ("18X"), so the wrong-product file ("…Super-18G-EVA-…") slipped into
            # the gallery.  Checked here rather than inside _decide_candidate because
            # that function is deliberately config-free.
            verdict = (
                "S4",
                "image URL names another capacity code than this row (different model)",
            )
        if (
            verdict is None
            and url_names_sibling_product(candidate.url, task.sibling_terms)
            and not url_anchored_to_row(
                candidate.url, keys, brand=brand, own_terms=task.own_terms
            )
        ):
            # S4's third limb, sheet knowledge (Orient run 2026-10-10): the Pulse row's
            # tolerated page also carried the Innova file, and neither URL prints the
            # row's key - but the sheet itself names the difference.  A word that names
            # exactly one *other* row of this sheet names another product, whatever the
            # page's status is; a wrong product must never be downloaded.  The limb stands
            # down when the file is anchored to *this* row (its own key or its own series
            # word in the name): live 2026-10-11 the Hyper row's own "…Hyper-18X-T3-
            # Super-White-Turbo-Pro.webp" was killed by the word "turbo" beside its key.
            verdict = (
                "S4",
                "image URL names another product from this sheet",
            )
        if (
            verdict is None
            and url_names_other_product_line(candidate.url, keys)
            and not url_anchored_to_row(
                candidate.url, keys, brand=brand, own_terms=task.own_terms
            )
        ):
            # S4's fourth limb, no sheet needed (owner run 2026-10-11): the Pulse row
            # downloaded "15-ton-innova-ultra-white-dc-inverter" although the sheet has
            # no Innova row - the sibling limb was silent, the file prints no code to
            # compare, but its own name says another product line.  A word the row does
            # not own (no brand, no category, no colour, no commerce word) names another
            # product; a file anchored to this row by its own key or line word stays.
            verdict = (
                "S4",
                "image URL names another product line",
            )
        foreign_maker = (
            url_names_foreign_brand(candidate.url, brand) if verdict is None else None
        )
        if verdict is None and foreign_maker:
            # S4's fifth limb, the maker itself (owner run 2026-10-12): the Megatron
            # row published ``panasonic.jpg`` renders.  The page gate judges the page's
            # title, but a degraded fetch may print no maker and a direct image URL
            # carries no page at all - the file's own name is the last word, and a file
            # named after another known maker is never this product.  Judged on the
            # file name alone (retail CDN paths host every brand), never anchored: no
            # key of this row can make another maker's file ours.
            verdict = (
                "S4",
                f"image URL names another maker ({foreign_maker})",
            )
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
                upscale_candidates(
                    candidate.url,
                    official_page,
                    sibling_ext=config.url_rewrite_cross_extension_enabled,
                )[0]
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
            upscale_candidates(
                candidate.url,
                official_page,
                sibling_ext=config.url_rewrite_cross_extension_enabled,
            )
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
        image, panel = marketing_panel_split(image, config)
        if panel is not None:
            # The owner's Dawlance file of 2026-10-10 - a studio shot of the unit with a pasted
            # black badge/warranty panel below it - was rejected outright, and they asked for the
            # opposite treatment: "it should download but on isolated it cuts bottom labels
            # advertisement marketing labels and add only product".  Cutting happens here, before
            # the gates, so the picture the gates judge (and the customer receives) is the product
            # alone; S20/S21 then still run on it, so cutting a panel cannot smuggle in an ad.
            side, pixels = panel
            deps.decisions.keep(
                title=task.title,
                url=used_url,
                rule_id="S7.panel",
                detail=(
                    f"cut a {pixels}px flat marketing panel off the {side} edge; the product "
                    "area was kept and re-judged by every gate"
                ),
            )

        # Brand-owned pages get the calmer studio bar on every gallery source, not
        # just the curated ones (owner 2026-10-09: "picture verification should not
        # be too strict only in brand sites because they are official" - the grey
        # Kenwood studio shot died on the retail corner bar).  The acceptance still
        # demands neutral corners, and S1 already proved the model on the page.
        official_gallery = official_page
        rule_id, detail = run_quality_gates(
            image, config, official_gallery=official_gallery, panel_cut=panel
        )
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
        if rule_id in ("S20", "S21") and official_gallery:
            rescued, reason = _layout_rescue_verdict(image, config, rule_id, detail)
            if rescued:
                deps.decisions.keep(
                    title=task.title, url=used_url, rule_id=f"{rule_id}.official", detail=reason
                )
                rule_id = "ok"
        if rule_id != "ok":
            rejections[rule_id] += 1
            deps.decisions.reject(
                title=task.title, url=used_url, rule_id=rule_id, detail=detail
            )
            if config.keep_rejected_images:
                _dump_rejected(image, config, task.title, used_url, rule_id)
            continue
        if (
            config.variant_separation_enabled
            and config.color_gate_enabled
            and keys.colour_word
            and not is_display_product(task.title)
        ):
            # The brand-page exemption yields when the brand's *own file name* names a
            # different colourway.  Orient-style assets are per variant
            # ("...classic-white-dc-inverter.png"), so a contradiction there is a real
            # sibling mix-up and S16 keeps its teeth even on the official page.
            exempt_chroma = config.color_gate_official_chroma_exempt and not colour_word_conflict(
                keys.colour_word, used_url.rsplit("/", 1)[-1]
            )
            color_ok, color_detail = verify_image_color(
                image,
                keys.colour_word,
                official=official_gallery,
                exempt_chroma_on_official=exempt_chroma,
                min_saturation=config.color_chroma_min_saturation,
                wash_max_value=config.color_chroma_max_value,
            )
            if not color_ok:
                rejections["S16"] += 1
                deps.decisions.reject(
                    title=task.title, url=used_url, rule_id="S16", detail=color_detail
                )
                if config.keep_rejected_images:
                    _dump_rejected(image, config, task.title, used_url, "S16")
                continue
            if color_detail:
                # Exempted, not silent: the owner reads this folder to find out why a
                # picture was or was not used, so the measurement stays on the record.
                deps.decisions.keep(
                    title=task.title, url=used_url, rule_id="S16.official", detail=color_detail
                )
        # One fingerprint per candidate, reused by the gate and by the bookkeeping after
        # compose - the same 32x32 resample of the same 1200px image, once instead of twice.
        fingerprint = compute_fingerprint(image)
        duplicate = dedup.duplicate_reason(image, fingerprint=fingerprint)
        if duplicate:
            rejections["S10"] += 1
            deps.decisions.reject(
                title=task.title, url=used_url, rule_id="S10", detail=duplicate
            )
            continue

        # Ambiguity is recorded, not silently published or silently dropped: a pair that
        # looks like a repeat but cannot be proven one goes to the log for a human glance
        # (see dedup.duplicate_reason for why the thresholds cannot be tightened).
        review_note = dedup.review_shape_match(image, fingerprint=fingerprint)
        if review_note:
            deps.decisions.keep(
                title=task.title, url=used_url, rule_id="S10.review", detail=review_note
            )

        seen_keys.add(dedup_key(candidate.url))
        _compose_and_save(
            task=task,
            keys=keys,
            candidate=candidate,
            used_url=used_url,
            source_label=source_label,
            image=image,
            config=config,
            deps=deps,
            dedup=dedup,
            accepted=accepted,
            fingerprint=fingerprint,
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
    keys: ModelKeys,
    candidate: ImageCandidate,
    used_url: str,
    source_label: str,
    image: Image.Image,
    config: PipelineConfig,
    deps: PipelineDeps,
    dedup: ProductDedup,
    accepted: list[AcceptedImage],
    fingerprint: ImageFingerprint | None = None,
) -> None:
    """Stage 5: isolate, 1:1 centre, light unsharp, every selected template, save locally."""
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
    raw_path = _save_audit(image, config, task.title, ImageRole.RAW, f"{index:02d}-raw.jpg")
    square_path = _save_audit(
        square, config, task.title, ImageRole.ISOLATED, f"{index:02d}-square.jpg"
    )
    bbox = product_content_bbox(
        square,
        white_level=config.product_content_white_level,
        min_ratio=config.product_content_min_ratio,
    )
    # One stem shared by every platform: the same photograph is then comparable across
    # ``digi/``, ``alfa/`` and ``website/`` by filename alone (owner decision 2026-10-13).
    name = f"{filename_prefix(keys, task.title)}_{index}.jpg"
    final_paths: list[tuple[str, Path]] = []
    for platform in deps.platforms.platforms:
        prepared = _load_prepared_template(config, platform.template_path)
        composite = apply_template(square, prepared.base, config, bbox=bbox, prepared=prepared)
        final_paths.append(
            (platform.key, _save_final(composite, config, task.title, platform.key, name))
        )
    accepted.append(
        AcceptedImage(
            source_url=candidate.url,
            dedup_key=dedup_key(candidate.url),
            raw_path=raw_path,
            square_path=square_path,
            final_paths=tuple(final_paths),
        )
    )
    dedup.add(image, candidate.url, fingerprint=fingerprint)
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


def _load_asset(path: Path) -> Image.Image:
    """Load a template once per run.  Templates are read-only, never per-product data."""
    cache_key = str(path)
    with _ASSET_LOCK:
        cached = _ASSET_CACHE.get(cache_key)
        if cached is None:
            cached = Image.open(path).convert("RGBA")
            cached.load()
            _ASSET_CACHE[cache_key] = cached
        return cached


def _load_prepared_template(config: PipelineConfig, path: Path) -> PreparedTemplate:
    """Load and prepare a template once per run with double-checked locking."""
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
            cached = prepare_template(_load_asset(path), config)
            _PREPARED_TEMPLATE_CACHE[cache_key] = cached
        return cached


# --------------------------------------------------------------------------- #
# outputs
# --------------------------------------------------------------------------- #
def _platform_csv_path(config: PipelineConfig, platform: ResolvedPlatform) -> Path:
    """One import sheet per shop, in the shop's own folder under the run folder."""
    return config.output_dir / platform.key / platform.csv_filename


def _image_column(prefix: str, count: int) -> str:
    """The bare filenames one row published, as the CSV-only import mode needs them.

    The count comes from the URL list, because the two are produced by the same write:
    one URL is exactly one file, and the closing banner is simply the last index.  So the
    ``images`` column can never claim a picture the ``Image_Urls`` column does not have.
    """
    if not prefix or count <= 0:
        return ""
    return ", ".join(f"{prefix}_{index}.jpg" for index in range(1, count + 1))


def _platform_row(
    result: ProductResult, key: str, *, dry_run: bool
) -> tuple[str, str, str, str]:
    """One output sheet line.  A dry run has nothing hosted, so its URL column is empty
    while the filenames it did write are still listed."""
    urls = result.urls_for(key)
    return (
        result.title,
        result.brand,
        "" if dry_run else "|".join(urls),
        _image_column(result.name_prefix, len(urls)),
    )


def write_output_csvs(
    results: list[ProductResult],
    config: PipelineConfig,
    platforms: PlatformPlan,
) -> None:
    """Write one import sheet per publishing platform, the failed CSV and the report.

    Rows always follow input order.  ``ok``/``partial`` rows go to the platform
    CSVs, only ``failed`` rows go to ``failed_products.csv``.  A ``--dry-run`` writes
    the sheets too (owner decision 2026-10-13): the ``images`` column names the files it
    did produce, and ``Image_Urls`` stays empty because nothing was uploaded.
    """
    ordered = sorted(results, key=lambda result: result.task_index)
    for platform in platforms.platforms:
        atomic_write_csv(
            _platform_csv_path(config, platform),
            OUTPUT_HEADER,
            (
                _platform_row(result, platform.key, dry_run=config.dry_run)
                for result in ordered
                if result.urls_for(platform.key)
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
    _write_output_readme(ordered, config, platforms)
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
#: itself to whoever opens it next (no tribal knowledge required).  The platform blocks
#: are generated from the plan, so a fourth platform documents itself without an edit here.
_OUTPUT_README = """# Run output - what is in here
{dry_note}
Generated by `main.py`.  Everything is rewritten from scratch on every run, so this
folder is always the result of the **last** run - never a mix of runs.

{import_block}

**Reports** (read these, do not import them):
- `failed_products.csv` - rows that produced nothing usable: `Title,Reason`.
- `run_report.csv` - one line per input row: status (`ok`/`partial`/`failed`),
  pages tried, pages rejected, images accepted, note.
- `review_products.csv` - the manual-review queue: rows that published something but
  carry a flag (a relaxed fallback match, a colour/variant difference): `Title,Status,Note`.
- `decisions.jsonl` - the audit trail: one JSON object per accept/reject with the
  rule id (`S1`..`S21`, plus scoped sub-rules like `S15.relaxed`) that decided it.
- `pipeline_run.log` - the full log of the run.

{picture_block}

`<product>` is a slug of the row title (`dawlance-semi-automatic-washing-machine-10kg-dw-7500c`),
and the pictures are named `<MODEL_CODE>_1.jpg`, `_2.jpg`, ... so the same photograph is
the same filename in every platform folder and can be matched back to the CSVs by eye.

## This run

- rows: {rows} | ok: {ok} | partial: {partial} | failed: {failed}
- pictures published: {published}
- status meanings: **ok** = published with at least {min_images} image(s), **partial** = published but
  flagged (fewer images than target, or a human should glance at the note column),
  **failed** = nothing published for that row (see `failed_products.csv`).

A row that is *failed* is deliberate, not a crash: the pipeline refuses to publish a
picture it cannot prove belongs to that exact model (see `README.md`, rule S1).
"""


def _import_block(platforms: PlatformPlan) -> str:
    """The import-sheet bullets - one per platform that actually published."""
    lines = ["**Import files** (give these to the shops):"]
    if not platforms.platforms:
        lines.append("- none: this run published on no platform (see the terminal notices).")
        return "\n".join(lines)
    columns = ", ".join(OUTPUT_HEADER)
    for platform in platforms.platforms:
        banner = "the last entry is its closing banner" if platform.appends_banner else "no closing banner"
        lines.append(
            f"- `{platform.key}/{platform.csv_filename}` - {platform.display_name}: "
            f"`{columns}`, URLs pipe-separated, {banner}."
        )
    return "\n".join(lines)


def _picture_block(platforms: PlatformPlan) -> str:
    """The publishable folders this run filled, then the evidence tree it never publishes."""
    lines = ["**Pictures**:"]
    lines.extend(
        f"- `images/{platform.key}/<product>/` - {platform.display_name}'s final "
        "pictures on its own template.  These are the publishable set; a folder can be "
        "zipped and handed over as it stands."
        for platform in platforms.platforms
    )
    lines.append(
        "- `audit/<product>/` - the evidence that never goes to a shop: `raw/` the photos "
        "exactly as downloaded (proof of what the source showed) and `isolated/` after "
        "background knock-out, 1:1 squaring and sharpening (1080x1080)."
    )
    return "\n".join(lines)


def _published_counts(results: list[ProductResult], platforms: PlatformPlan) -> str:
    """Product pictures per platform, banner excluded - the count the shops import against."""
    if not platforms.platforms:
        return "nothing (no platform was selected)"
    return ", ".join(
        f"{platform.display_name} "
        f"{sum(max(0, len(r.urls_for(platform.key)) - (1 if platform.appends_banner else 0)) for r in results)}"
        for platform in platforms.platforms
    )


def _write_output_readme(
    results: list[ProductResult],
    config: PipelineConfig,
    platforms: PlatformPlan,
) -> None:
    """Write ``output/README.md``: the run folder explains itself."""
    ok = sum(1 for result in results if result.status is Status.OK)
    partial = sum(1 for result in results if result.status is Status.PARTIAL)
    failed = sum(1 for result in results if result.status is Status.FAILED)
    dry_note = (
        "\n**This folder was written by a `--dry-run`:** nothing was uploaded, so every import "
        "sheet here has an empty `Image_Urls` column.  A dry run overwrites the last real run's "
        "sheets - do not hand this one to a shop.\n"
        if config.dry_run
        else ""
    )
    try:
        atomic_write_text(
            config.output_dir / "README.md",
            _OUTPUT_README.format(
                rows=len(results),
                ok=ok,
                partial=partial,
                failed=failed,
                published=_published_counts(results, platforms),
                dry_note=dry_note,
                import_block=_import_block(platforms),
                picture_block=_picture_block(platforms),
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
        *,
        processor: Callable[
            [ProductTask, PipelineDeps, PipelineConfig, threading.Event | None],
            tuple[ProductResult, list[AcceptedImage]],
        ]
        | None = None,
    ) -> None:
        self._config = config
        self._deps = deps
        self._tasks = tasks
        self._checkpoint = checkpoint
        #: Option 2 swaps the per-row processor; the default keeps Option 1 exact.
        self._processor = processor if processor is not None else process_product
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
                        platform_urls=entry.platform_urls,
                        name_prefix=entry.name_prefix,
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
        return entry.is_resume_done(hash_source_urls(task.source_urls), self._deps.platforms.keys)

    def _run_one(self, task: ProductTask) -> tuple[ProductResult, list[AcceptedImage]]:
        # Rows skipped by the checkpoint never reach here, so a resumed product keeps
        # the pictures its completed run published.
        clear_stale_product_files(self._config, self._deps, task.title)
        return self._processor(task, self._deps, self._config, self._cancel)

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
            list(self._results.values()), self._config, self._deps.platforms
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



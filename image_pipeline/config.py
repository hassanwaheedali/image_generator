"""Single frozen configuration object for the whole pipeline.

Rule: every threshold mentioned in the specification lives here, never as a
magic number inside an algorithm.  ``__post_init__`` validates ranges so that a
mis-typed threshold fails at startup instead of silently accepting bad images.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

# --------------------------------------------------------------------------- #
# Asset file names (defaults; all overridable through PipelineConfig)
# --------------------------------------------------------------------------- #
DEFAULT_DIGIMALL_TEMPLATE = "DigiMall.png"
DEFAULT_ALFAMALL_TEMPLATE = "AlfaMall.png"
DEFAULT_DIGIMALL_BANNER = "digi_last.png"
DEFAULT_ALFAMALL_BANNER = "alfa_last.png"

# Output CSV headers.  Changing the header is a one-constant change.
OUTPUT_HEADER: tuple[str, ...] = ("Title", "Brand", "Image_Urls")
FAILED_HEADER: tuple[str, str] = ("Title", "Reason")
REPORT_HEADER: tuple[str, ...] = (
    "Title",
    "Status",
    "Pages_Tried",
    "Pages_Rejected",
    "Images_Accepted",
    "Note",
)
#: Rows that published something but carry a flag a human should read (relaxed fallback
#: match, colour/variant difference) - owner request 2026-10-06: "we will manually review
#: it, I just need images".
REVIEW_HEADER: tuple[str, str, str] = ("Title", "Status", "Note")


class ConfigError(ValueError):
    """Raised when the configuration is internally inconsistent or unusable."""


#: Secrets file names searched, in order.  ``.env`` is hidden on most systems, so a plainly
#: visible ``secrets.env`` is searched as well.  A file with real values always wins over
#: an unfilled template, wherever it sits.
ENV_FILE_NAMES: tuple[str, ...] = (".env", "secrets.env", "env.txt")

#: Folder that holds this package, and the folder above it.  The one above is searched so a
#: key pasted at the top of a workspace/repository is picked up when the program is run from
#: inside the project folder.
_PROJECT_DIR: Path = Path(__file__).resolve().parent

#: NAME=value, tolerating an optional ``export`` and surrounding spaces.
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
_ENV_COMMENT_RE = re.compile(r"\s+#")


def parse_env_text(text: str) -> dict[str, str]:
    """KEY=VALUE pairs from a `.env` body (pure function, unit-tested).

    Rules: blank lines and ``#`` comments are ignored; a value may be quoted with
    single or double quotes (quotes are stripped); an unquoted trailing ``# comment``
    is removed; ``export KEY=value`` is tolerated.
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE_RE.match(line)
        if match is None:
            continue
        key, value = match.group(1), match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = _ENV_COMMENT_RE.split(value, maxsplit=1)[0].strip()
        if value:
            values[key] = value
    return values


def default_search_dirs() -> list[Path]:
    """Folders searched for a secrets file when ``--env-file`` is not given.

    Order: the current working directory, the project folder, then the folder above the
    project (handy when you run the program from inside ``image_pipeline/`` but keep the
    key in the folder that contains it).  Duplicates are dropped.
    """
    return list(
        dict.fromkeys([Path.cwd(), _PROJECT_DIR, _PROJECT_DIR.parent])
    )


def load_env_file(
    path: Path | None = None,
    *,
    search_dirs: Sequence[Path] | None = None,
) -> Path | None:
    """Load a secrets file into ``os.environ`` without ever overriding a real env var.

    Search order (first existing wins):

    1. the explicit ``path`` (``--env-file``);
    2. ``.env``, ``secrets.env``, ``env.txt`` in the working directory;
    3. the same names next to this file (so it works from any folder).

    Returns the file that was used, or ``None``.  Values are **never** logged.
    """
    candidates: list[Path] = []
    if path is not None:
        candidates.append(path)
    else:
        folders = list(search_dirs) if search_dirs is not None else default_search_dirs()
        for folder in folders:
            candidates.extend(folder / name for name in ENV_FILE_NAMES)

    first_seen: Path | None = None
    contributed: Path | None = None
    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        if first_seen is None:
            first_seen = candidate
        values = parse_env_text(text)
        if not values:
            continue                      # e.g. an empty template - keep looking
        for key, value in values.items():
            os.environ.setdefault(key, value)   # a real env var always wins
        if contributed is None:
            contributed = candidate
    return contributed or first_seen


#: Default endpoint used when ``FREEIMAGE_UPLOAD_URL`` is not set.
DEFAULT_UPLOAD_URL: Final[str] = "https://freeimage.host/api/1/upload"


def _read_upload_url() -> str:
    """Upload endpoint supplied by ``FREEIMAGE_UPLOAD_URL`` (blank uses the default)."""
    return os.environ.get("FREEIMAGE_UPLOAD_URL", "").strip() or DEFAULT_UPLOAD_URL


def _read_api_key() -> str:
    """Image-host API key, read from the environment at call time (no default).

    Supplied by ``FREEIMAGE_API_KEY`` - exported in the shell, set as a Colab secret, or
    put in a ``.env`` file (see :func:`load_env_file`, called by ``main``).
    """
    return os.environ.get("FREEIMAGE_API_KEY", "").strip()


@dataclass(frozen=True)
class PipelineConfig:
    """Immutable run configuration.  One instance is shared by all workers."""

    # ----- paths -----------------------------------------------------------
    assets_dir: Path = Path("assets")
    output_dir: Path = Path("output")
    state_dir: Path = Path("state")
    input_dir: Path = Path("input")

    # ----- asset file names ------------------------------------------------
    digimall_template_name: str = DEFAULT_DIGIMALL_TEMPLATE
    alfamall_template_name: str = DEFAULT_ALFAMALL_TEMPLATE
    digimall_banner_name: str = DEFAULT_DIGIMALL_BANNER
    alfamall_banner_name: str = DEFAULT_ALFAMALL_BANNER

    # ----- run control -----------------------------------------------------
    dry_run: bool = False
    workers: int = 4
    limit: int | None = None
    redo_titles: tuple[str, ...] = ()
    resume: bool = True

    # ----- quotas ----------------------------------------------------------
    # Customer decision 2026-10-06: aim for 3 images per product when the site has
    # them; fewer than `min_required_images` accepted -> status `partial`.
    target_images_per_product: int = 3
    min_required_images: int = 2
    # Owner request 2026-10-06: a brand's own gallery routinely holds more than 5
    # usable product shots (live: the Dawlance DW 7500 TWIN TUB CLEAR LID (WHITE) page
    # publishes 10), and publishing only 5 of them silently threw the rest away.  The
    # *target* still stops the search after 3 images; this is only the hard ceiling on
    # how many may be published.  `--max-images` overrides it per run.
    max_images_per_product: int = 10
    allow_title_only_verification: bool = False
    unbalanced_bracket_keeps_text: bool = True
    use_bracketed_keys_as_fallback: bool = True

    # ----- simplified matching (customer decisions 2026-10-05, plan §3.6) ---
    # Loose about recognizing the product, strict about publishing a wrong image.
    # --- product recognition: two plain rules (see model_matching) ----------
    allow_key_truncation_match: bool = True      # DW-7500C sheet vs DW-7500 page
    brand_match_enabled: bool = True             # Haier + "538 IFGA" == Haier HRF-538 IFGA
    brand_match_min_length: int = 5              # code characters, e.g. "538ifga"
    brand_match_min_digits: int = 3
    brand_match_max_extra_tokens: int = 2        # glue "HRF-538 IFGA" -> hrf538ifga
    brand_match_reject_other_brand_domain: bool = True   # never on samsung.com for Dawlance

    # --- flags: they annotate run_report.csv, they never reject a photo ------
    check_colour_match: bool = True              # title colour vs page colour
    flag_variant_mismatch: bool = True           # size/type mismatch -> publish + flag

    # ----- fallback source chain (plan.md section 11, customer-approved) ----
    # Runs ONLY when the sheet's own URLs did not solve the row (no matched page,
    # or a matched page yielded no usable image).  Every candidate page found here
    # is re-verified with the sheet's model before any image is considered (S15).
    fallback_enabled: bool = True
    refresh_sitemaps: bool = False
    excluded_hosts: tuple[str, ...] = (
        "etech.pk",           # HTTP 403 to every attempt
        "symbios.pk",         # broken TLS chain
        "zeesolstore.pk",     # domain dead / no JSON
        "zeesol.com.pk",
    )
    #: base URL -> platform, measured (plan.md 11.2).  Add a shop = add one line.
    source_platforms: dict[str, str] = field(
        default_factory=lambda: {
            "www.haier.com/pk": "sitemap",
            "haiermall.pk": "none",           # sitemap.xml is an HTML page there
            "www.dawlance.com.pk": "sitemap",
            "www.tcl.com/pk": "none",         # marketing site, JS gallery
            "eshop.pel.com.pk": "shopify",
            "homage.com.pk": "woo",
            "www.surmawala.pk": "shopify",
            "modernelectronics.pk": "shopify",
            "www.telex.pk": "shopify",
            "www.alfatah.com.pk": "woo",
            "www.almumtaz.com.pk": "woo",
            "hadielectronics.com.pk": "woo",
            "www.subhanelectronics.pk": "woo",
        }
    )
    #: brand -> its own sources, tried before any retail shop.
    brand_sources: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: {
            "haier": ("https://haiermall.pk", "https://www.haier.com/pk"),
            "dawlance": ("https://www.dawlance.com.pk",),
            "tcl": ("https://www.tcl.com/pk",),
            "pel": ("https://eshop.pel.com.pk",),
            "homage": ("https://homage.com.pk",),
        }
    )
    retail_shopify_sources: tuple[str, ...] = (
        "https://www.surmawala.pk",
        "https://modernelectronics.pk",
        "https://www.telex.pk",
    )
    retail_woo_sources: tuple[str, ...] = (
        "https://www.alfatah.com.pk",
        "https://www.almumtaz.com.pk",
        "https://hadielectronics.com.pk",
        "https://www.subhanelectronics.pk",
    )
    #: Structured product JSON (Shopify ``/products/<handle>.json``, WooCommerce Store API):
    #: the HTML gallery is sometimes lazy-loaded or holds a single picture, while the JSON
    #: carries the product's whole image list.  Fetched only when the page's own gallery
    #: yielded fewer than ``target_images_per_product`` candidates.
    structured_gallery_enabled: bool = True
    #: A *fallback* page (never a sheet URL) that fails strict S1 may still be used when it
    #: names the brand and carries the model's own number: the row is published with a
    #: ``flagged:`` note and listed in ``review_products.csv``.  Owner decision 2026-10-06:
    #: images are wanted with a flag rather than a failed row.
    relaxed_fallback_matching: bool = True
    #: A page that only matches through the dropped-trailing-letter tolerance (``DW-7500``
    #: printed for the sheet's ``DW-7500C``) is not treated as the answer: the row keeps
    #: looking - brand, then Surmawala and the other shops - for a page that prints the
    #: model, and revisits the tolerated page as a **flagged** last resort only when the
    #: chain found nothing.  Owner decision 2026-10-06: the sheet's link is sometimes the
    #: wrong page for the model in the title, so the model wins over the link.
    tolerated_spelling_as_last_resort: bool = True
    fallback_max_pages_per_row: int = 6
    #: Per *source* share of that budget.  Without it the first source that answers
    #: (a brand sitemap lists the whole model family) fills the row budget and the
    #: retail tier is never even queried - live: a Dawlance row returned six sitemap
    #: pages, Surmawala was never searched, and the row failed with "no page matched"
    #: although the shop had the exact product.
    fallback_max_pages_per_source: int = 3
    fallback_max_search_per_source: int = 1
    source_search_timeout_s: float = 12.0
    sitemap_ttl_days: int = 7
    sitemap_max_urls: int = 20000
    sitemap_max_child_maps: int = 12
    respect_robots: bool = True

    # ----- image download economy (plan.md 11.5) ---------------------------
    image_probe_enabled: bool = True
    image_probe_bytes: int = 65536
    #: Per *page* cap: one page of dark marketing renders must never eat the row's
    #: whole allowance and starve the next candidate page (found live on Dawlance).
    #: A brand-official page offering a *curated gallery* is exempt and may be read
    #: in full, bounded by the row cap below (see ``_page_download_allowance``).
    image_full_download_max_per_page: int = 6
    image_full_download_max_per_row: int = 18
    url_rewrite_enabled: bool = True              # largest-size rewrite before fetch

    # ----- page / image fetching ------------------------------------------
    request_timeout_s: float = 25.0
    max_retries: int = 3
    backoff_base_s: float = 0.8
    backoff_max_s: float = 8.0
    domain_min_interval_s: float = 1.2
    impersonate: str = "chrome120"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    accept_language: str = "en-US,en;q=0.9"

    # ----- URL filtering ---------------------------------------------------
    block_collections_without_products: bool = True

    # ----- byte-level image validation (S13) ------------------------------
    max_image_bytes: int = 15 * 1024 * 1024
    max_image_pixels: int = 40_000_000

    # ----- geometric gates (S6/S8) ----------------------------------------
    min_short_side: int = 180
    min_long_side: int = 350
    max_aspect_ratio: float = 3.5

    # ----- background cleanliness (S7) ------------------------------------
    border_ring_ratio: float = 0.06
    border_ring_min_px: int = 3
    border_clean_min_fraction: float = 0.92
    border_bg_tolerance: float = 28.0
    border_std_max: float = 22.0
    neutral_bright_min_value: float = 0.86
    neutral_bright_max_saturation: float = 0.14
    min_neutral_corners: int = 2
    #: Official brand pages are curated: the images in their product gallery are the
    #: manufacturer's own studio shots, and many of them fill the frame (a top-down view,
    #: an open-lid interior).  The strict "clean border ring" test throws those away even
    #: though the background is white - live case: Dawlance DW-7500C official gallery had
    #: 4 images and only 1 survived, so the row came out with a single picture.
    #: When the image came from the *curated gallery of a verified official page for this
    #: model*, S7 accepts it if the corners are still neutral studio background.
    official_gallery_relaxed_s7: bool = True
    #: Neutral corners required for that acceptance (the corners prove a studio backdrop).
    official_gallery_min_neutral_corners: int = 2
    #: Universal studio-background rescue (S7).  A clean white-background product photo whose
    #: product legitimately touches the frame edges (top-down view, tall appliance) fails the
    #: strict border-ring test even though the backdrop is white.  When all four corners are
    #: neutral studio white, the image is accepted regardless of which host served it - the
    #: corners are the evidence it is a studio shot, not a lifestyle scene.  Live case:
    #: Surmawala DW-7500 1200x1200 white shot rejected with "border ring is not a clean
    #: background" despite 4/4 neutral corners.  Bounded: a busy frame with only tiny white
    #: corner patches never reaches four neutral corners at ``corner_patch_ratio``.
    studio_background_rescue: bool = True
    #: Neutral corners required for the universal rescue - deliberately the full four,
    #: stricter than the official-gallery acceptance because it applies to every host.
    studio_rescue_min_neutral_corners: int = 4
    corner_patch_ratio: float = 0.12

    # ----- near-duplicate removal (S10) -----------------------------------
    dedup_hamming_threshold: int = 6
    dedup_hamming_soft_threshold: int = 14
    dedup_corr_threshold: float = 0.995

    # ----- compositing -----------------------------------------------------
    canonical_side: int = 1200
    padding_px: int = 6
    isolation_bg_distance: float = 32.0
    isolation_min_foreground_fraction: float = 0.03
    isolation_min_component_ratio: float = 0.02
    isolation_neighbor_distance_px: float = 35.0
    degenerate_crop_ratio: float = 0.03
    white_fallback_tolerance: int = 10
    unsharp_radius: float = 0.75
    unsharp_percent: int = 30
    unsharp_threshold: int = 4
    #: How much of the template's content box the product may fill (0-1).  This is the
    #: "make it big" knob: the product is scaled so its *content* (not its square canvas)
    #: reaches this share of the box along its longest side.
    product_fill_ratio: float = 0.95
    #: What counts as "not the product" when measuring its content box (white background).
    product_content_white_level: int = 246
    #: Safety net: if the measured content box is smaller than this share of the canvas the
    #: measurement is distrusted and the whole canvas is used instead.
    product_content_min_ratio: float = 0.2
    template_side_margin_ratio: float = 0.04
    template_bottom_margin_ratio: float = 0.03
    template_top_scan_ratio: float = 0.35
    template_header_gap_px: int = 8
    template_default_top_margin_px: int = 90
    #: Promo strips ("PWM Single Tub 1250 SA" style) that shops glue to the bottom or top
    #: edge of an otherwise clean product shot cross the S7 border ring and would fail a
    #: perfectly good photo.  When the strip is unambiguous it is cut off *before* the
    #: background check; the S7 ring test then runs on what is left, so a lifestyle image
    #: (coloured background) is still rejected exactly as before.
    promo_band_strip_enabled: bool = True
    #: A band may not take more than this share of the image height (taller = not a strip).
    promo_band_max_ratio: float = 0.30
    #: Shortest strip worth cutting, in pixels.
    promo_band_min_height_px: int = 24
    #: A band row must be at least this non-white across its width.
    promo_band_min_width_ratio: float = 0.80
    #: ...and this dark on average (promo strips are dark/coloured, not pale artwork).
    promo_band_dark_max_mean: int = 200
    #: Rows of bright background required between the product and the strip, so the cut
    #: can never slice through the product.
    promo_band_background_gap_px: int = 6
    #: What counts as "bright background" for that gap (soft product shadows are allowed,
    #: a dark product body is not).
    promo_band_gap_min_mean: int = 200
    #: White margin tolerated on the outside of the strip before giving up.
    promo_band_edge_margin_max_px: int = 80
    #: What counts as "white" when measuring the gap and the margin (S7 uses the same idea).
    promo_band_white_level: int = 245
    #: Shops also ship assets with a thin layout seam (a 1-8 px divider line touching two
    #: edges) that crosses the S7 ring and rejects an otherwise perfect photo.  Such a seam
    #: is trimmed off only when it spans the picture and the sliver beyond it is empty.
    seam_trim_enabled: bool = True
    #: A seam must run across at least this share of the image (height for vertical seams).
    seam_min_span_ratio: float = 0.95
    #: Widest line still considered a seam rather than picture content.
    seam_max_width_px: int = 12
    #: The sliver between the seam and the edge must be at most this busy (non-white share).
    seam_offcut_max_content_ratio: float = 0.03
    #: Never trim more than this share of the picture away.
    seam_keep_min_ratio: float = 0.5
    #: How many seams may be trimmed (one per pass), e.g. a side line plus an edge line.
    seam_max_passes: int = 3
    template_white_cutoff: int = 250
    template_make_white_transparent: bool = True
    #: A template counts as "already transparent" only when it really contains translucent
    #: pixels (alpha below this value).  Exported PNGs often carry an alpha channel that is
    #: 255 everywhere; treating that as transparency would hide the product behind an opaque
    #: sheet, so the white knockout below runs whenever no pixel is this transparent.
    template_transparency_min_alpha: int = 250
    #: 0.0 = perfectly centred product (the customer wants "bilkul centre");
    #: positive moves it down, negative up.
    optical_center_bias: float = 0.0
    jpeg_quality: int = 95

    # ----- image host ------------------------------------------------------
    api_key: str = field(default_factory=_read_api_key)
    #: Upload endpoint - ``FREEIMAGE_UPLOAD_URL``, defaulting to freeimage.host.
    upload_url: str = field(default_factory=_read_upload_url)
    upload_min_interval_s: float = 0.6

    # ------------------------------------------------------------------ #
    # validation
    # ------------------------------------------------------------------ #
    def __post_init__(self) -> None:
        self._validate_quotas()
        self._validate_run_control()
        self._validate_fetch()
        self._validate_image_gates()
        self._validate_compositing()
        if not self.dry_run and not self.api_key:
            raise ConfigError(
                "FREEIMAGE_API_KEY is not set (and --dry-run was not requested). "
                "Put it in the .env file next to main.py (FREEIMAGE_API_KEY=...) or export "
                "it in the shell before a real upload run."
            )

    def sources_for(self, brand: str | None) -> tuple[str, ...]:
        """The fallback chain for one row: the brand's own sites first, then retail.

        Order is the feature: official brand sources come before any marketplace.
        """
        ordered: list[str] = []
        if brand:
            ordered.extend(self.brand_sources.get(brand.lower(), ()))
        ordered.extend(self.retail_shopify_sources)
        ordered.extend(self.retail_woo_sources)
        seen: set[str] = set()
        out: list[str] = []
        for base in ordered:
            host = base.lower().rstrip("/")
            if host and host not in seen:
                seen.add(host)
                out.append(base)
        return tuple(out)

    def platform_of(self, base: str) -> str:
        """Declared platform for a source base URL (``""`` when unknown)."""
        host = base.lower().split("//")[-1].rstrip("/")
        return self.source_platforms.get(host, "none")

    def _validate_quotas(self) -> None:
        if self.min_required_images < 1:
            raise ConfigError("min_required_images must be >= 1")
        if self.max_images_per_product < self.min_required_images:
            raise ConfigError(
                "max_images_per_product must be >= min_required_images "
                f"({self.max_images_per_product} < {self.min_required_images})"
            )
        if self.max_images_per_product > 20:
            raise ConfigError("max_images_per_product must be <= 20")
        if self.target_images_per_product < 1:
            raise ConfigError("target_images_per_product must be >= 1")
        if self.min_required_images > self.max_images_per_product:
            raise ConfigError("min_required_images must be <= max_images_per_product")
        if self.image_probe_bytes < 4096:
            raise ConfigError("image_probe_bytes must be >= 4096")
        if self.fallback_max_pages_per_source < 1:
            raise ConfigError("fallback_max_pages_per_source must be >= 1")
        if self.fallback_max_pages_per_source > self.fallback_max_pages_per_row:
            raise ConfigError(
                "fallback_max_pages_per_source must be <= fallback_max_pages_per_row "
                f"({self.fallback_max_pages_per_source} > {self.fallback_max_pages_per_row})"
            )
        if self.image_full_download_max_per_page < 1:
            raise ConfigError("image_full_download_max_per_page must be >= 1")
        if self.image_full_download_max_per_page > self.image_full_download_max_per_row:
            raise ConfigError(
                "image_full_download_max_per_page must be <= image_full_download_max_per_row"
            )
        if self.fallback_max_pages_per_row < 1:
            raise ConfigError("fallback_max_pages_per_row must be >= 1")
        if self.brand_match_min_length < 3:
            raise ConfigError("brand_match_min_length must be >= 3")
        if self.brand_match_min_digits < 1:
            raise ConfigError("brand_match_min_digits must be >= 1")
        if not 0 <= self.brand_match_max_extra_tokens <= 5:
            raise ConfigError("brand_match_max_extra_tokens must be in [0, 5]")

    def _validate_run_control(self) -> None:
        if self.workers < 1:
            raise ConfigError("workers must be >= 1")
        if self.limit is not None and self.limit < 1:
            raise ConfigError("limit must be >= 1 when given")

    def _validate_fetch(self) -> None:
        if self.request_timeout_s <= 0:
            raise ConfigError("request_timeout_s must be > 0")
        if self.max_retries < 1:
            raise ConfigError("max_retries must be >= 1")
        if self.backoff_base_s <= 0 or self.backoff_max_s < self.backoff_base_s:
            raise ConfigError("backoff_base_s must be > 0 and <= backoff_max_s")
        if self.domain_min_interval_s < 0:
            raise ConfigError("domain_min_interval_s must be >= 0")

    def _validate_image_gates(self) -> None:
        if self.max_image_bytes <= 0:
            raise ConfigError("max_image_bytes must be > 0")
        if self.max_image_pixels <= 0:
            raise ConfigError("max_image_pixels must be > 0")
        if self.min_short_side < 1 or self.min_long_side < self.min_short_side:
            raise ConfigError("require 1 <= min_short_side <= min_long_side")
        if self.max_aspect_ratio <= 1.0:
            raise ConfigError("max_aspect_ratio must be > 1.0")
        for name, value in (
            ("border_ring_ratio", self.border_ring_ratio),
            ("border_clean_min_fraction", self.border_clean_min_fraction),
            ("corner_patch_ratio", self.corner_patch_ratio),
        ):
            if not 0.0 < value <= 1.0:
                raise ConfigError(f"{name} must be in (0, 1]")
        if self.border_std_max <= 0:
            raise ConfigError("border_std_max must be > 0")
        if not 0 <= self.min_neutral_corners <= 4:
            raise ConfigError("min_neutral_corners must be in [0, 4]")
        if not 0 <= self.official_gallery_min_neutral_corners <= 4:
            raise ConfigError("official_gallery_min_neutral_corners must be in [0, 4]")
        if not 0 <= self.studio_rescue_min_neutral_corners <= 4:
            raise ConfigError("studio_rescue_min_neutral_corners must be in [0, 4]")
        for name, value in (
            ("dedup_hamming_threshold", self.dedup_hamming_threshold),
            ("dedup_hamming_soft_threshold", self.dedup_hamming_soft_threshold),
        ):
            if not 0 <= value <= 64:
                raise ConfigError(f"{name} must be in [0, 64]")
        if self.dedup_hamming_soft_threshold < self.dedup_hamming_threshold:
            raise ConfigError(
                "dedup_hamming_soft_threshold must be >= dedup_hamming_threshold"
            )
        if not 0.0 < self.dedup_corr_threshold <= 1.0:
            raise ConfigError("dedup_corr_threshold must be in (0, 1]")

    def _validate_compositing(self) -> None:
        if self.canonical_side < 64:
            raise ConfigError("canonical_side must be >= 64")
        if self.padding_px < 0:
            raise ConfigError("padding_px must be >= 0")
        if self.isolation_bg_distance <= 0:
            raise ConfigError("isolation_bg_distance must be > 0")
        for name, value in (
            ("isolation_min_foreground_fraction", self.isolation_min_foreground_fraction),
            ("isolation_min_component_ratio", self.isolation_min_component_ratio),
            ("degenerate_crop_ratio", self.degenerate_crop_ratio),
        ):
            if not 0.0 < value <= 1.0:
                raise ConfigError(f"{name} must be in (0, 1]")
        if self.isolation_neighbor_distance_px < 0:
            raise ConfigError("isolation_neighbor_distance_px must be >= 0")
        if not 0 <= self.white_fallback_tolerance <= 64:
            raise ConfigError("white_fallback_tolerance must be in [0, 64]")
        if self.unsharp_radius <= 0 or not 0 < self.unsharp_percent <= 500:
            raise ConfigError("unsharp_radius > 0 and 0 < unsharp_percent <= 500")
        if self.unsharp_threshold < 0:
            raise ConfigError("unsharp_threshold must be >= 0")
        if not 0 <= self.product_content_white_level <= 255:
            raise ConfigError("product_content_white_level must be in [0, 255]")
        if not 0.0 < self.product_content_min_ratio <= 1.0:
            raise ConfigError("product_content_min_ratio must be in (0, 1]")
        for name, value in (
            ("product_fill_ratio", self.product_fill_ratio),
            ("template_side_margin_ratio", self.template_side_margin_ratio),
            ("template_bottom_margin_ratio", self.template_bottom_margin_ratio),
            ("template_top_scan_ratio", self.template_top_scan_ratio),
        ):
            if not 0.0 < value < 1.0:
                raise ConfigError(f"{name} must be in (0, 1)")
        for name, value in (
            ("promo_band_max_ratio", self.promo_band_max_ratio),
            ("promo_band_min_width_ratio", self.promo_band_min_width_ratio),
        ):
            if not 0.0 < value <= 1.0:
                raise ConfigError(f"{name} must be in (0, 1]")
        for name, value_int in (
            ("promo_band_min_height_px", self.promo_band_min_height_px),
            ("promo_band_dark_max_mean", self.promo_band_dark_max_mean),
            ("promo_band_background_gap_px", self.promo_band_background_gap_px),
            ("promo_band_edge_margin_max_px", self.promo_band_edge_margin_max_px),
        ):
            if value_int < 0:
                raise ConfigError(f"{name} must be >= 0")
        if not 0 <= self.promo_band_white_level <= 255:
            raise ConfigError("promo_band_white_level must be in [0, 255]")
        if not 0 <= self.promo_band_gap_min_mean <= 255:
            raise ConfigError("promo_band_gap_min_mean must be in [0, 255]")
        for name, value in (
            ("seam_min_span_ratio", self.seam_min_span_ratio),
            ("seam_offcut_max_content_ratio", self.seam_offcut_max_content_ratio),
            ("seam_keep_min_ratio", self.seam_keep_min_ratio),
        ):
            if not 0.0 < value <= 1.0:
                raise ConfigError(f"{name} must be in (0, 1]")
        if self.seam_max_width_px < 1:
            raise ConfigError("seam_max_width_px must be >= 1")
        if self.seam_max_passes < 1:
            raise ConfigError("seam_max_passes must be >= 1")
        if not -0.25 <= self.optical_center_bias <= 0.25:
            raise ConfigError("optical_center_bias must be in [-0.25, 0.25]")
        if not 1 <= self.jpeg_quality <= 100:
            raise ConfigError("jpeg_quality must be in [1, 100]")
        if self.upload_min_interval_s < 0:
            raise ConfigError("upload_min_interval_s must be >= 0")

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @property
    def asset_paths(self) -> dict[str, Path]:
        """The four fixed asset files, keyed by role, as resolved paths."""
        return {
            "digimall_template": self.assets_dir / self.digimall_template_name,
            "alfamall_template": self.assets_dir / self.alfamall_template_name,
            "digimall_banner": self.assets_dir / self.digimall_banner_name,
            "alfamall_banner": self.assets_dir / self.alfamall_banner_name,
        }

    @property
    def images_dir(self) -> Path:
        return self.output_dir / "images"

    @property
    def decisions_path(self) -> Path:
        return self.output_dir / "decisions.jsonl"

    @property
    def log_path(self) -> Path:
        return self.output_dir / "pipeline_run.log"

    @property
    def digimall_csv(self) -> Path:
        return self.output_dir / "digimall_product_images.csv"

    @property
    def alfamall_csv(self) -> Path:
        return self.output_dir / "alfamall_product_images.csv"

    @property
    def failed_csv(self) -> Path:
        return self.output_dir / "failed_products.csv"

    @property
    def report_csv(self) -> Path:
        return self.output_dir / "run_report.csv"

    @property
    def review_csv(self) -> Path:
        """Rows published with a flag - the manual-review queue."""
        return self.output_dir / "review_products.csv"

    @property
    def checkpoint_path(self) -> Path:
        return self.state_dir / "checkpoint.jsonl"

    @property
    def banner_cache_path(self) -> Path:
        return self.state_dir / "banner_urls.json"

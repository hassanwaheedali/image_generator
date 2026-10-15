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
from typing import Any, Final

# Output CSV headers.  Changing the header is a one-constant change.
#: ``Image_Urls`` serves the hosted-import mode; ``images`` serves the CSV-only mode
#: where the owner zips ``images/<platform>/`` and imports the bare filenames
#: (owner decision D3: one sheet carries both).
OUTPUT_HEADER: tuple[str, ...] = ("Title", "Brand", "Image_Urls", "images")
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


def load_config_file(
    path: Path | None = None,
    *,
    search_dirs: Sequence[Path] | None = None,
) -> dict[str, Any]:
    """Load configuration dictionary from a JSON file (e.g. pipeline_config.json)."""
    import json
    candidates: list[Path] = []
    if path is not None:
        candidates.append(path)
    else:
        folders = list(search_dirs) if search_dirs is not None else default_search_dirs()
        for folder in folders:
            candidates.append(folder / "pipeline_config.json")
            candidates.append(folder / "config.json")
    for candidate in candidates:
        if candidate.is_file():
            try:
                with candidate.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except (OSError, ValueError):
                pass
    return {}



#: Default endpoint used when ``FREEIMAGE_UPLOAD_URL`` is not set.
DEFAULT_UPLOAD_URL: Final[str] = "https://freeimage.host/api/1/upload"
DEFAULT_IMGBB_UPLOAD_URL: Final[str] = "https://api.imgbb.com/1/upload"
DEFAULT_APILAYER_UPLOAD_URL: Final[str] = "https://api.apilayer.com/image_upload/upload"



def _read_upload_url() -> str:
    """Upload endpoint supplied by ``FREEIMAGE_UPLOAD_URL`` (blank uses the default)."""
    return os.environ.get("FREEIMAGE_UPLOAD_URL", "").strip() or DEFAULT_UPLOAD_URL


def _read_imgbb_upload_url() -> str:
    """Upload endpoint supplied by ``IMGBB_UPLOAD_URL`` (blank uses the default)."""
    return os.environ.get("IMGBB_UPLOAD_URL", "").strip() or DEFAULT_IMGBB_UPLOAD_URL


def _read_api_key() -> str:
    """Image-host API key, read from the environment at call time (no default).

    Supplied by ``FREEIMAGE_API_KEY``
    put in a ``.env`` file (see :func:`load_env_file`, called by ``main``).
    """
    return os.environ.get("FREEIMAGE_API_KEY", "").strip()


def _read_imgbb_api_key() -> str:
    """ImgBB API key for fallback/primary upload, read from ``IMGBB_API_KEY``."""
    return os.environ.get("IMGBB_API_KEY", "").strip()


def _read_apilayer_api_key() -> str:
    """APILayer API key for fallback/primary upload, read from ``APILAYER_API_KEY``."""
    return os.environ.get("APILAYER_API_KEY", "").strip()


def _read_apilayer_upload_url() -> str:
    """Upload endpoint supplied by ``APILAYER_UPLOAD_URL`` (blank uses the default)."""
    return os.environ.get("APILAYER_UPLOAD_URL", "").strip() or DEFAULT_APILAYER_UPLOAD_URL


#: The two roots a run is written under when the caller pins no folder.  Relative on
#: purpose: the owner launches from the repo root and reads these names in the file
#: explorer, and ``main.name_run_folders`` puts each sheet's own folder inside them.
DEFAULT_OUTPUT_ROOT = Path("output")
DEFAULT_STATE_ROOT = Path("state")


@dataclass(frozen=True)
class PipelineConfig:
    """Immutable run configuration.  One instance is shared by all workers."""

    # ----- paths -----------------------------------------------------------
    assets_dir: Path = Path("assets")
    output_dir: Path = DEFAULT_OUTPUT_ROOT
    state_dir: Path = DEFAULT_STATE_ROOT
    input_dir: Path = Path("input")
    #: Windows MAX_PATH is 260 and Excel's own limit is lower in practice.  The deepest
    #: path a run writes is ``output/<run>/images/<platform>/<slug>/<prefix>_NN.jpg``;
    #: when composing it would pass this budget the *slug* is shortened (with a hash
    #: suffix, so it stays unique) and the model-code prefix is left alone - the prefix
    #: is what the owner reads in the CSV.
    max_output_path_chars: int = 220

    # ----- platforms -------------------------------------------------------
    #: Comma-separated platform keys, in publish order.  ``--platforms`` and the
    #: startup prompt override this.  Keys and their assets live in ``platforms.py``.
    enabled_platforms: str = "website,alfa,digi"
    #: Website drops the closing banner by rule (the site has its own closing slot).
    #: Owner asked for the rule to stay switchable rather than be baked into the table.
    website_keep_banner: bool = False

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
    #: A page that carries its own curated gallery (the brand's ``.sd_imgs`` / swiper
    #: slides, JSON-LD product payload, Shopify/Woo/Magento product payload) is read to
    #: the end instead of stopping at ``target_images_per_product``.  Reason, from the
    #: owner's review of 2026-10-10: Haier shows 6 views and Dawlance 11 slides whose
    #: good photos come AFTER two manual sheets in DOM order, so "stop at 3 accepted"
    #: published the manuals and never reached the product.  Bounded by
    #: ``max_images_per_product``, and only pages offering at least
    #: ``gallery_publishes_all_min`` curated images qualify, so a retailer page with one
    #: stray ``<img>`` cannot bulk-publish.
    #: S21 - reject caption sheets (in-box contents, manual and how-to pages).  Dawlance
    #: publishes them inside the *product* gallery, and they are flat black-on-white like a
    #: white appliance photo, so only the layout tells: many comparable parts, no leader.
    #: Measured on the owner's run: the two live sheets form 9 parts with the largest at 12 %
    #: of the ink, while the most a real photo forms is 3 parts with one at 94 %.
    diagram_sheet_enabled: bool = True
    diagram_min_parts: int = 4
    diagram_max_dominant_share: float = 0.20
    #: On an *official* product page, a full-bleed layout (S20) or a caption-sheet layout
    #: (S21) is not fatal on its own: the pipeline isolates the product inside the artwork
    #: and re-runs every gate on that crop, publishing the crop.  Owner rule of 2026-10-10:
    #: "cut the bottom labels, advertisement marketing labels, and add only product".
    #: S7.panel - cut a *pasted* marketing panel (badges, capacity pill, warranty block)
    #: off the top or bottom of an image.  Only a dead-flat dark block with a hard straight
    #: edge qualifies, so a dark product on a dark ground is left alone (measured on the
    #: owner's run: 3 accepted photos are that shape and none of them qualifies - their
    #: band means run 163-181, far above ``panel_band_max_mean``).
    #:
    #: 2026-10-10, LOW_11: the real panel carries four badges, captions, a pill and a
    #: warranty block, and that artwork pushed the band's row-mean variance past the old
    #: flat bar (measured 53 on the fixture, the old bar was 45), so the detector refused
    #: the very file it was written for.  What separates pasted artwork from a dark
    #: product is not the variance of the rows but the *background*: badges sit on a dead
    #: black ground, a photograph is body and gradient.  A band now qualifies when its
    #: rows are calm **or** most of its pixels sit on one flat background shade
    #: (``panel_bg_flat_min`` of them within ``_PANEL_BG_TOLERANCE`` of the band median).
    marketing_panel_split_enabled: bool = True
    panel_band_max_mean: float = 95.0
    panel_side_min_mean: float = 170.0
    panel_min_jump: float = 150.0
    panel_max_row_std: float = 45.0
    panel_bg_flat_min: float = 0.55
    panel_min_ratio: float = 0.12
    panel_max_ratio: float = 0.72
    panel_edge_flat_min: float = 0.95
    official_layout_rescue: bool = True
    #: Write rejected pixels to ``<product>/rejected/<rule>-<hash>.jpg`` so a refusal can be
    #: inspected instead of argued about.  Debugging aid; off by default.
    keep_rejected_images: bool = False
    gallery_publishes_all: bool = True
    gallery_publishes_all_min: int = 4
    allow_title_only_verification: bool = False
    unbalanced_bracket_keeps_text: bool = True
    use_bracketed_keys_as_fallback: bool = True

    # ----- performance & iteration control ---------------------------------
    #: When True, stop searching subsequent pages and fallback sources as soon as
    #: at least 1 valid image is accepted for the product.
    early_stop_when_image_found: bool = False

    #: When True, stop inspecting candidates on a page as soon as target_images_per_product
    #: is reached, rather than inspecting all remaining candidates in large galleries.
    limit_gallery_iteration: bool = False

    #: Extra generic / category words that must not trigger a series substitution veto.
    extra_generic_name_words: tuple[str, ...] = ()

    #: Catalogue CSV (columns ``Brand`` and ``Original_Title``) whose sibling rows teach
    #: the matcher which tier words separate different products (``Avante`` vs
    #: ``Avante+``).  Relative paths resolve against the pipeline directory; "" disables
    #: the external corpus (learning then uses the run's own sheet only).
    series_learning_corpus: str = (
        "tests/fixtures/test_files/"
        "Classified_Stock_Inventory_Master - KiaChahiye 12-10-2026 - Classified Stock.csv"
    )
    #: Tier words the owner wants in force regardless of what the corpus proves.
    extra_series_differentiators: tuple[str, ...] = ()

    # ----- simplified matching (customer decisions 2026-10-05, plan §3.6) ---

    # Loose about recognizing the product, strict about publishing a wrong image.
    # --- product recognition: two plain rules (see model_matching) ----------
    allow_key_truncation_match: bool = True      # DW-7500C sheet vs DW-7500 page
    brand_match_enabled: bool = True             # Haier + "538 IFGA" == Haier HRF-538 IFGA
    brand_match_min_length: int = 5              # code characters, e.g. "538ifga"
    brand_match_min_digits: int = 3
    brand_match_max_extra_tokens: int = 2        # glue "HRF-538 IFGA" -> hrf538ifga
    brand_match_reject_other_brand_domain: bool = True   # never on samsung.com for Dawlance

    # --- variant separation: one switch, two signals (plan: variant-separation)
    # A page printing another colourway is NOT this product, so it is rejected (S1);
    # a page that only spells the code's letter tail differently is kept but flagged,
    # because those tails are printed inconsistently by the brands themselves.
    variant_separation_enabled: bool = True

    # --- flags: they annotate run_report.csv, they never reject a photo ------
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
        "telex.pk",           # port 443 unresponsive (connect timeout)
        "www.telex.pk",
        "fahadelectronics.pk",# port 443 unresponsive (connect timeout)
        "www.fahadelectronics.pk",
    )
    #: base URL -> platform, measured (plan.md 11.2).  Add a shop = add one line.
    source_platforms: dict[str, str] = field(
        default_factory=lambda: {
            "www.haier.com/pk": "sitemap",
            "haiermall.pk": "none",           # sitemap.xml is an HTML page there
            "www.dawlance.com.pk": "sitemap",
            "www.tcl.com/pk": "sitemap",
            "www.orient.com.pk": "sitemap",
            "eshop.pel.com.pk": "shopify",
            "homage.com.pk": "woo",
            "www.kenwoodpakistan.com": "woo",
            "www.kenwoodpakistan.pk": "woo",
            "www.surmawala.pk": "shopify",
            "friendshome.pk": "shopify",
            "www.alfatah.com.pk": "woo",
            "subhanelectronics.pk": "woo",
            "sohailelectronics.com": "woo",
            "pkelectronics.pk": "woo",
            "jalalelectronics.com": "shopify",
            "modernelectronics.pk": "shopify",
            "shandaarbuy.pk": "shopify",
            "electrociti.pk": "shopify",
            "whitehouse.com.pk": "shopify",
            "japanelectronics.com.pk": "shopify",
            "www.almumtaz.com.pk": "woo",
            "hadielectronics.com.pk": "woo",
            "cityelectronics.com.pk": "woo",
            "yasirelectronics.com": "woo",
            "umarelectronics.pk": "woo",
            "pakref.com": "woo",
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
            "kenwood": (
                "https://www.kenwoodpakistan.com",
                "https://www.kenwoodpakistan.pk",
            ),
            "orient": ("https://www.orient.com.pk",),
        }
    )
    retail_shopify_sources: tuple[str, ...] = (
        "https://www.surmawala.pk",
        "https://modernelectronics.pk",
        "https://friendshome.pk",
        "https://shandaarbuy.pk",
        "https://jalalelectronics.com",
        "https://electrociti.pk",
        "https://whitehouse.com.pk",
        "https://japanelectronics.com.pk",
    )
    retail_woo_sources: tuple[str, ...] = (
        "https://www.alfatah.com.pk",
        "https://www.almumtaz.com.pk",
        "https://hadielectronics.com.pk",
        "https://pkelectronics.pk",
        "https://www.subhanelectronics.pk",
        "https://sohailelectronics.com",
        "https://cityelectronics.com.pk",
        "https://yasirelectronics.com",
        "https://umarelectronics.pk",
        "https://pakref.com",
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
    #: A page that prints no key of its own may still be the row, written shortly: the
    #: **complete** tail of the sheet's code plus the series word the sheet put after
    #: the code plus the row's tonnage (see ``model_matching.tail_chunk_match``).  A
    #: dead-end rescue only - it arms where today's row dies, publishes flagged
    #: (``flagged: tail-chunk match``) and never preempts exact, tolerated or relaxed
    #: evidence.  Live run website_ac_clean 2026-10-15: three Go Easy rows died while
    #: mabdullah sold them as ``18HRFN1 QC6 … GO EASY 1.5 Ton``.
    tail_chunk_match_enabled: bool = True
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
    #: Cross-extension rewrites (Haier ``X_60.jpg`` -> ``X.png``). OFF until the owner
    #: confirms on haier.com that the sibling really exists: a rewrite is only ever
    #: *tried*, and the download step takes the first candidate that answers, so a
    #: guessed URL that happens to resolve to some other file could outrank the URL the
    #: page itself named. Same-family substitution is the one failure this repo must
    #: never ship, so the rung stays dark until it is proven by a live probe.
    url_rewrite_cross_extension_enabled: bool = False

    # ----- page / image fetching ------------------------------------------
    connect_timeout_s: float = 5.0
    request_timeout_s: float = 25.0
    max_retries: int = 3
    backoff_base_s: float = 0.8
    backoff_max_s: float = 8.0
    domain_min_interval_s: float = 1.2
    impersonate: str = "chrome120"
    #: Force IPv4 for every fetch.  Some PK hosts sit behind an IPv6 record that black-holes
    #: on crawler networks (browser still works), producing curl 28 connect timeouts while the
    #: IPv4 address is fine.  IPv4-only is universally reachable for these retail domains.
    fetch_force_ipv4: bool = True
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
    #: Minimum payload and byte density for lossy (JPEG/WebP) product images.
    #: Rejects heavily compressed, blurry, and low-fidelity thumbnails (e.g. Daraz 9.6KB 470x556).
    min_lossy_image_bytes: int = 12_000
    min_lossy_bytes_per_pixel: float = 0.025

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
    #: Official brand sites get a calmer corner bar (owner 2026-10-09: "picture
    #: verification should not be too strict only in brand sites because they are
    #: official"): their studio grounds run light grey (~0.85) where retail shots
    #: are white, and the model match (S1) already proves the page.  Retail keeps
    #: the strict value.
    official_neutral_bright_min_value: float = 0.80
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
    #: The hard hamming path also needs a near-perfect grey-patch correlation: two
    #: catalogue angles of the same appliance hash close together, but only a
    #: re-served copy correlates ~1.0 (live 2026-10-09: a correct second angle was
    #: rejected as "near-duplicate").
    dedup_hard_corr_threshold: float = 0.985
    #: Inside the hard hamming radius, how much of the product silhouette two images must
    #: share to be called the same angle.  Calibrated on the owner's 33-product run
    #: (2026-10-09): re-served copies measure IoU 0.985, the best genuinely different pair
    #: 0.754 - so 0.90 sits in the gap rather than in the middle of a guess.
    dedup_shape_iou_min: float = 0.90

    # ----- monochrome logo / emblem gate (S14) ------------------------------
    #: Official galleries mix marketing badges (the Haier "AI + leaf" emblem on a
    #: grey disc, live 2026-10-09) into the product image list.  A flat achromatic
    #: graphic on a non-white ground is a logo, not a product photo.
    logo_gate_enabled: bool = True
    logo_achromatic_min_fraction: float = 0.97
    logo_background_max_value: float = 0.90
    logo_ink_min_fraction: float = 0.06
    logo_ink_max_value: float = 0.60
    #: A body brighter than the grey ground proves a product (white unit on a grey
    # studio shot) - emblems carry nothing brighter than their disc.
    logo_bright_max_fraction: float = 0.10
    #: Emblem ink is stroke-work; a dark appliance body covers far more.
    logo_ink_max_fraction: float = 0.30

    #: S20 - the white-ground sibling of S14 (owner's Haier review, 2026-10-10): the
    #: brand's own gallery mixed its AI/laurel emblem into the product images and S14
    #: skipped it because it only fires on a grey ground.  A full-bleed frame is judged
    #: by what covers it: a poster paints most of the canvas, an emblem is dense line
    #: art.  Measured on the 72 accepted raws of that run, real product photos sit at
    #: ink 0.22-0.44 and texture 3-10 (a full-bleed Midea shot reaches 10.3), and an
    #: AC wearing a strip of feature badges sits at texture 23-26 - the owner's own
    #: LOW_1 measured 25.9.  Emblems and line art start at 37 and reach 55.  The
    #: texture bar therefore sits in the gap between the two bands (32.0), not inside
    #: the product band: the original 18.0 rejected the owner's clean LOW_1 photo on
    #: 2026-10-10 ("S20 rejected … texture 25.9"), the same night S20 was born.
    #: S4 half: reject a file whose capacity code contradicts the row's (row "18X" vs
    #: file "18G").  Only runs when both sides carry such a code and they share none.
    capacity_code_gate_enabled: bool = True
    marketing_layout_enabled: bool = True
    marketing_max_ink_fraction: float = 0.75
    marketing_max_texture: float = 32.0

    # ----- visual color verification (S16) --------------------------------
    #: Owner decision 2026-10-08: keep it narrow.  It false-rejected a white Dawlance
    #: WD-1051 dispenser whose blue accent panel read as hue 213 deg ("conflicts with
    #: target 'white'"), and on LED TVs the screen shows arbitrary content, so a
    #: picture-based colour check can never be trusted there; display products stay exempt
    #: regardless.  The switch has been on since the base commit (the comment used to claim
    #: otherwise), which is why it also ate the owner's Orient Frost hero on 2026-10-10.
    #: What S16 does now: on a *retailer* page it may reject a wrong colour, on the brand's
    #: own page only the two identity-strength signals run (see
    #: ``color_gate_official_chroma_exempt``), and the "is it a colour or is it a wash"
    #: test is saturation-based instead of raw channel spread.
    color_gate_enabled: bool = True
    #: Skip the chromatic-dominance branch on a brand-owned page.  The brand's hero *is*
    #: the definitive picture of that variant, and a coloured studio plate around a white
    #: unit is not a colour conflict; a dark body on a white row is, and that branch stays
    #: in force on every page.  Set False to restore the pre-2026-10-10 behaviour.
    color_gate_official_chroma_exempt: bool = True
    #: A product-area pixel counts as "a colour" only above this HSV saturation *and* below
    #: this value, because a studio wash is bright and low-saturation while a coloured body
    #: is neither.  Measured on 64x64 centre crops, share of the product area: pale cool plate
    #: #e9eefb 0.01, mid cool plate #c9d9f2 0.01 (the old channel-spread test said 0.45 -
    #: that is the number that ate the owner's Orient hero), purple/red/blue/green bodies
    #: 0.61-0.62, a **golden** body 0.61 (a 0.30 or 0.35 bar drops gold to 0.00 and re-opens
    #: the gold-on-white hole the tests guard), white/black/silver bodies 0.00.
    color_chroma_min_saturation: float = 0.22
    color_chroma_max_value: float = 0.95

    # ----- Option 2: direct mode + branded template guard (S19) -----------
    #: Option 2 run mode: the sheet's URL column holds direct image URLs.  No pages
    #: are fetched, nothing is scraped or model-proven; the row trusts its links and
    #: everything from the download onward reuses the Option 1 machinery.
    direct_mode: bool = False

    #: Safeguard S19: store theme templates (iShopping's yellow frame, promo art with
    #: the shop's own logo and design) break isolation and must never be composited.
    #: The evidence is the background only - the four corner patches.  A product
    #: photo's corners are white or light neutral; a theme template's corners are
    #: saturated brand colours.  The product body itself is never measured, so a
    #: product that fills the frame stays safe (that case belongs to the S7 studio
    #: rescue, exactly as in Option 1).
    branded_template_guard_enabled: bool = True
    #: A corner counts as "themed" when its mean-patch saturation exceeds this.
    #: White studio corners sit at ~0.01-0.05, warm grey at ~0.03; brand yellows,
    #: reds and blues sit above 0.4, so the bar is lenient by a wide margin.
    branded_corner_min_saturation: float = 0.30
    #: How many themed corners make the whole picture a store template.  3 of 4: one
    #: accidental coloured corner (a cable, a shadow, a price tag) never fires S19.
    branded_min_saturated_corners: int = 3

    # ----- last-resort salvage --------------------------------------------
    #: Owner decision 2026-10-08 ("jab kuch na bachey toh kam quality wali hi le lon"):
    #: when a row would otherwise end with 0 (or below ``min_required_images``) images,
    #: publish the best already-downloaded picture that only missed a *quality* floor
    #: (S13 lossy payload / bpp) - never an identity reject (wrong model, banner, wrong
    #: colour).  1 salvaged image beats a failed row.
    last_resort_salvage_enabled: bool = True

    #: Blur floor for salvaged images (contrast-normalised Laplacian variance, see
    #: image_quality.sharpness_score): even flat low-contrast sharp shots score
    #: ~0.029+, textured real photos higher, visibly blurred thumbnails <= 0.01.
    #: Salvage must never publish "puri blurred" pictures (owner 2026-10-08);
    #: the normal path is untouched.
    salvage_min_sharpness: float = 0.02

    # ----- canvas blur guard (Option 2) -----------------------------------
    #: Reject upscaled, low-fidelity images whose high-frequency spectral energy
    #: and edge gradients are depleted after compositing onto the square canvas.
    canvas_blur_guard_enabled: bool = True
    #: Which platform's artwork the guard composites its preview on.  The two
    #: thresholds below were calibrated against that template's bands, so the
    #: reference is named instead of assumed.  A run that does not include it
    #: falls back to its first platform rather than skipping the gate.
    canvas_blur_reference: str = "digi"
    canvas_blur_hf_threshold: float = 0.102
    canvas_blur_edge_threshold: float = 48.0

    # ----- compositing -----------------------------------------------------
    canonical_side: int = 1200
    padding_px: int = 6
    isolation_bg_distance: float = 32.0
    isolation_min_foreground_fraction: float = 0.03
    isolation_min_component_ratio: float = 0.02
    isolation_neighbor_distance_px: float = 35.0
    #: Re-join a product's own parts up to this share of the body's short side.  The gap
    #: that splits them is pale studio wall the difference mask reads as background, so it
    #: scales with the photograph - a fixed pixel count cannot serve a 300px thumbnail and a
    #: 4000px hero shot at once.  Owner sample 2026-10-15: an air cooler's grille and its
    #: black water tank sat 39px apart in a 990px frame, 4px outside the fixed reach above,
    #: and the crop published the top half of the product alone.
    isolation_neighbor_size_ratio: float = 0.15
    #: A body this far from the main one is only kept in the crop if it is this share of the
    #: frame - big enough to be a second product standing beside the first, so a shot of
    #: three units keeps all three.  A corner badge or an energy sticker is a fraction of
    #: this and must not widen the crop: the owner's cooler sample carries a 1.2% logo.
    isolation_product_frame_ratio: float = 0.04
    #: Largest per-channel step that counts as the product's own edge, blocking the studio
    #: flood.  A wall fades slowly across a frame; the line where it meets the product is
    #: sharp.  Needed because a tolerance wide enough to span a studio gradient also spans a
    #: pale body - ``dawlance-single-door-deep-freezer-df-400-eds`` has its white body 16
    #: levels from its wall, and colour alone cannot separate them.  0 disables the barrier.
    studio_edge_barrier: float = 3.0
    #: How far the studio key reaches from the wall colour, in levels.  Wide enough to span
    #: a lit wall and its gradient; it is also what lets the key step onto a pale body when
    #: the wall colour was measured on that body in the first place.
    studio_clean_tolerance: float = 30.0
    #: Pixels of the product's own edge taken with the wall.  A photograph has no hard
    #: boundary: the last row of wall and the first row of product are a blend of the two,
    #: and leaving that ramp is what makes a cut outline read as soft and ragged (owner
    #: review 2026-10-15).  One pixel removes the blend; more starts to thin the product.
    studio_defringe_px: int = 1
    #: Blur applied to the wall matte before it is taken back at half strength, which is
    #: what evens out the cut without moving it.  0 keeps the raw flood boundary.
    studio_matte_sigma: float = 1.5
    #: How close to pure white the wall must be for the cleaner to call it already clean and
    #: stand down.  Keying is for the rare coloured studio wall - the owner's Super Asia
    #: cooler sits 25 levels from white - never for a picture already on white, where it can
    #: only damage the product.  Measured 2026-10-15: 726 of 726 image-01 raws stand down.
    studio_already_white_levels: float = 20.0
    #: Radius that bridges a product's own soft outline before the body is measured, as a
    #: share of the image's short side.  A white shell on a white background is *hollow*
    #: to a difference mask (its shading is below the threshold), and a hollow body splits
    #: into strips - which is how an AC lost its top and kept its vents.
    isolation_merge_radius_ratio: float = 0.012
    #: A second body this close in area to the largest one means the frame is a layout
    #: (an "in-box items" diagram, a banner with a logo), not one product: crop nothing.
    isolation_multi_body_ratio: float = 0.30
    #: Grow-only crop closure (S18).  The strict difference mask above decides *which*
    #: body to crop; this decides that the crop may not *cut* it.  Measured on the owner
    #: run of 2026-10-10: 40 of the 72 accepted images had their product clipped by the
    #: crop border (`Candy By Haier 0.8 Ton`: content 1342x695, crop kept 1342x195).
    isolation_crop_closure: bool = True
    #: Ink scan for that closure: how many levels below pure white still counts as
    #: "something is drawn here".  Deliberately far looser than the foreground mask,
    #: because a white shell on a white background fades through 243, 235, 250.
    #: Calibrated on the 72 accepted raws: the ink box is stable from 2 to 24 levels
    #: (median 39.1% -> 37.6% of the frame), so JPEG noise on white cannot inflate it.
    isolation_ink_scan_distance: int = 6
    degenerate_crop_ratio: float = 0.03
    white_fallback_tolerance: int = 10
    #: White-on-white fallback crops must cover at least this share of the frame;
    #: below it only dark slivers (vents, a remote's screen) were detected and the
    #: full frame is published instead of a tight crop around nothing.
    white_fallback_min_area_ratio: float = 0.20
    #: Ceiling on blow-up inside ``to_square_canvas``: small sources (remote
    #: controls, thumbnails that slipped through) stay small and sharp on the
    #: canvas instead of becoming 10x mush.  0 disables the cap.
    max_upscale: float = 2.0
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
    #: How deep inside the kept picture to look when judging where a cut would land, as a
    #: share of the frame.  A shop's layout divider has blank space on both sides of itself.
    seam_kept_band_ratio: float = 0.01
    #: How much product may sit in that band before the trim is refused.  Measured on the
    #: live assets of 2026-10-15: the two cuts that sliced a product scored 0.90 and 0.94,
    #: the two that removed only blank space scored 0.10 and 0.33.
    seam_kept_max_content_ratio: float = 0.5
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
    #: Fallback/alternative upload host - ``IMGBB_API_KEY``.
    imgbb_api_key: str = field(default_factory=_read_imgbb_api_key)
    #: Fallback/alternative upload host - ``APILAYER_API_KEY``.
    apilayer_api_key: str = field(default_factory=_read_apilayer_api_key)
    #: Upload endpoint - ``FREEIMAGE_UPLOAD_URL``, defaulting to freeimage.host.
    upload_url: str = field(default_factory=_read_upload_url)
    #: Upload endpoint - ``IMGBB_UPLOAD_URL``, defaulting to api.imgbb.com.
    imgbb_upload_url: str = field(default_factory=_read_imgbb_upload_url)
    #: Upload endpoint - ``APILAYER_UPLOAD_URL``, defaulting to api.apilayer.com.
    apilayer_upload_url: str = field(default_factory=_read_apilayer_upload_url)
    upload_min_interval_s: float = 0.6

    # ------------------------------------------------------------------ #
    # validation
    # ------------------------------------------------------------------ #
    def __post_init__(self) -> None:
        self._validate_quotas()
        self._validate_run_control()
        self._validate_platforms()
        self._validate_fetch()
        self._validate_image_gates()
        self._validate_compositing()
        if not self.dry_run and not self.api_key and not self.imgbb_api_key and not self.apilayer_api_key:
            raise ConfigError(
                "FREEIMAGE_API_KEY (or IMGBB_API_KEY / APILAYER_API_KEY) is not set "
                "(and --dry-run was not requested). "
                "Put it in secrets.env (FREEIMAGE_API_KEY=..., IMGBB_API_KEY=..., or APILAYER_API_KEY=...) or export "
                "it in the shell before a real upload run."
            )
        if self.extra_generic_name_words:
            from model_matching import register_generic_name_words

            register_generic_name_words(self.extra_generic_name_words)


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
        if not 0.0 < self.diagram_max_dominant_share < 1.0:
            raise ConfigError("diagram_max_dominant_share must be in (0, 1)")
        if self.diagram_min_parts < 2:
            raise ConfigError("diagram_min_parts must be >= 2")
        if self.gallery_publishes_all_min < 1:
            raise ConfigError("gallery_publishes_all_min must be >= 1")
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

    def _validate_platforms(self) -> None:
        # Imported lazily: platforms.py reads ConfigError from this module, so a
        # module-level import here would be circular.  The same trick is used below
        # for model_matching.
        from platforms import PLATFORM_KEYS, parse_platform_keys

        if not parse_platform_keys(self.enabled_platforms):
            raise ConfigError(
                "enabled_platforms must name at least one of: " + ", ".join(PLATFORM_KEYS)
            )
        if self.canvas_blur_reference not in PLATFORM_KEYS:
            raise ConfigError(
                "canvas_blur_reference must name one of: " + ", ".join(PLATFORM_KEYS)
            )
        if self.max_output_path_chars < 120:
            raise ConfigError("max_output_path_chars must be >= 120")

    def _validate_fetch(self) -> None:
        if self.connect_timeout_s <= 0:
            raise ConfigError("connect_timeout_s must be > 0")
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
        if not 0.0 <= self.branded_corner_min_saturation <= 1.0:
            raise ConfigError("branded_corner_min_saturation must be in [0, 1]")
        if not 0 <= self.branded_min_saturated_corners <= 4:
            raise ConfigError("branded_min_saturated_corners must be in [0, 4]")
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
        if not 0.0 < self.dedup_shape_iou_min <= 1.0:
            raise ConfigError("dedup_shape_iou_min must be in (0, 1]")

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
            ("marketing_max_ink_fraction", self.marketing_max_ink_fraction),
        ):
            if not 0.0 < value <= 1.0:
                raise ConfigError(f"{name} must be in (0, 1]")
        if self.isolation_neighbor_distance_px < 0:
            raise ConfigError("isolation_neighbor_distance_px must be >= 0")
        if not 0.0 <= self.isolation_neighbor_size_ratio <= 1.0:
            raise ConfigError("isolation_neighbor_size_ratio must be in [0, 1]")
        if not 0.0 < self.isolation_product_frame_ratio <= 1.0:
            raise ConfigError("isolation_product_frame_ratio must be in (0, 1]")
        if self.studio_edge_barrier < 0:
            raise ConfigError("studio_edge_barrier must be >= 0")
        if not 0.0 < self.studio_clean_tolerance <= 128.0:
            raise ConfigError("studio_clean_tolerance must be in (0, 128]")
        if not 0 <= self.studio_defringe_px <= 8:
            raise ConfigError("studio_defringe_px must be in [0, 8]")
        if not 0.0 <= self.studio_matte_sigma <= 8.0:
            raise ConfigError("studio_matte_sigma must be in [0, 8]")
        if not 0.0 <= self.studio_already_white_levels <= 128.0:
            raise ConfigError("studio_already_white_levels must be in [0, 128]")
        if not 0.0 < self.isolation_merge_radius_ratio <= 0.25:
            raise ConfigError("isolation_merge_radius_ratio must be in (0, 0.25]")
        if not 0.0 < self.isolation_multi_body_ratio <= 1.0:
            raise ConfigError("isolation_multi_body_ratio must be in (0, 1]")
        if not 0.0 < self.canvas_blur_hf_threshold <= 1.0:
            raise ConfigError("canvas_blur_hf_threshold must be in (0, 1]")
        if self.canvas_blur_edge_threshold <= 0.0:
            raise ConfigError("canvas_blur_edge_threshold must be > 0")
        if self.marketing_max_texture <= 0:
            raise ConfigError("marketing_max_texture must be > 0")
        if not 0 < self.isolation_ink_scan_distance < 64:
            raise ConfigError("isolation_ink_scan_distance must be in (0, 64)")
        if not 0 <= self.white_fallback_tolerance <= 64:
            raise ConfigError("white_fallback_tolerance must be in [0, 64]")
        if not 0.0 < self.white_fallback_min_area_ratio <= 1.0:
            raise ConfigError("white_fallback_min_area_ratio must be in (0, 1]")
        if self.max_upscale < 0:
            raise ConfigError("max_upscale must be >= 0 (0 disables the cap)")
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
            ("seam_kept_band_ratio", self.seam_kept_band_ratio),
            ("seam_kept_max_content_ratio", self.seam_kept_max_content_ratio),
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
    def images_dir(self) -> Path:
        """Publishable pictures only: ``images/<platform key>/<product>/``.

        Kept free of audit artefacts so a platform folder can be zipped and handed over
        with no filtering, and so a product that slugs to ``digi`` cannot collide with the
        platform folder of the same name on Windows' case-insensitive filesystem.
        """
        return self.output_dir / "images"

    @property
    def audit_dir(self) -> Path:
        """The never-published evidence tree: ``audit/<product>/{raw,isolated,rejected}/``."""
        return self.output_dir / "audit"

    @property
    def decisions_path(self) -> Path:
        return self.output_dir / "decisions.jsonl"

    @property
    def log_path(self) -> Path:
        return self.output_dir / "pipeline_run.log"

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

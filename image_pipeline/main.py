"""Command line entry point.

    python main.py --input "sample test 10 products images.csv" --dry-run
    FREEIMAGE_API_KEY=... python main.py --input sheet.xlsx --workers 4
    python main.py --check-image path/to/hero.png      # which gate stops this file, no crawl

Fail-fast order: config (incl. API key) -> sheet -> platform availability -> run.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
import threading
from collections.abc import Callable
from dataclasses import replace
from itertools import combinations
from pathlib import Path

from config import (
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_STATE_ROOT,
    ConfigError,
    PipelineConfig,
    load_env_file,
)
from csv_io import (
    atomic_write_csv,
    hash_source_urls,
    load_banner_cache,
    load_checkpoint,
    local_filename_prefix,
    merge_duplicate_titles,
    read_input,
)
from decisions import DecisionLog
from direct_pipeline import process_direct_product
from fetch import (
    CurlCffiImageFetcher,
    CurlCffiJsonHttp,
    CurlCffiPageFetcher,
    DomainRateLimiter,
    LocalImageFetcher,
    PermanentAPIError,
)
from image_host import APILayerHost, FallbackImageHost, FreeImageHost, ImgBBHost
from models import ProductResult, ProductTask, Status
from pipeline import (
    COLOUR_UNSTATED_NOTE,
    BatchRunner,
    DryRunHost,
    PipelineDeps,
    csv_escape,
    write_output_csvs,
)
from platforms import (
    DEFAULT_SPECS,
    PLATFORM_KEYS,
    PlatformPlan,
    parse_platform_keys,
    resolve_platforms,
)
from protocols import ImageHost
from sources import SourceChain

LOG = logging.getLogger("pipeline.main")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="image_pipeline",
        description="Verified product-gallery extraction, compositing and hosting.",
    )
    parser.add_argument(
        "--input",
        default=None,
        help="CSV or XLSX sheet with Title + source URL columns (if omitted, auto-discovers from input-dir)",
    )
    parser.add_argument(
        "--input-dir",
        default="input",
        help="enterprise input directory to look for sheets (default: input)",
    )
    parser.add_argument(
        "--keep-rejected",
        action="store_true",
        help="also write rejected images to <product>/rejected/<rule>-<hash>.jpg for inspection",
    )
    parser.add_argument(
        "--check-image",
        nargs="+",
        default=[],
        metavar="FILE",
        help="no crawl: run the real gates on these saved files and print what stops each one",
    )
    parser.add_argument(
        "--check-title",
        default="",
        metavar="TITLE",
        help="with --check-image: judge the row title too, so the colour gate is applied as in that row",
    )
    parser.add_argument(
        "--assets-dir",
        default="assets",
        help="folder with one subfolder per platform, each holding its template and closing banner",
    )
    parser.add_argument(
        "--platforms",
        default=None,
        metavar="KEYS",
        help="comma-separated platforms to publish (website, alfa, digi); skips the startup prompt",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="where the CSVs, images and logs go (default: output/<sheet>-<hash>)",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="checkpoint and banner cache folder (default: state/<sheet>-<hash>)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="do everything except upload; the import sheets list no URLs",
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Option 2: the sheet's URL column holds direct image URLs - download, template "
        "and publish them; no pages are fetched and nothing is scraped",
    )
    parser.add_argument(
        "--images-dir",
        default=None,
        metavar="PARENT",
        help="Option 3: PARENT folder with one subfolder per product (folder name = Title, "
        "its image files = the product's pictures); no sheet, no scraping, no downloading",
    )
    parser.add_argument("--redo", nargs="*", default=[], help="exact titles to force re-process")
    parser.add_argument("--limit", type=int, default=None, help="process at most N products")
    parser.add_argument("--workers", type=int, default=4, help="parallel workers (default 4)")
    parser.add_argument(
        "--early-stop",
        action="store_true",
        help="stop searching further pages once >= 1 valid image is accepted",
    )
    parser.add_argument(
        "--limit-gallery",
        action="store_true",
        help="stop gallery candidate evaluation once target image quota is reached",
    )
    parser.add_argument("--min-images", type=int, default=None, help="images required for status ok (default 2)")
    parser.add_argument("--max-images", type=int, default=None, help="images kept per product (default 10)")
    parser.add_argument("--no-resume", action="store_true", help="ignore the checkpoint entirely")
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help="do not search brand/retail sources when the sheet's URLs solve nothing",
    )
    parser.add_argument(
        "--refresh-sitemaps",
        action="store_true",
        help="ignore state/sitemaps/* caches and re-crawl the sitemap indexes",
    )
    parser.add_argument(
        "--target-images",
        type=int,
        default=None,
        help="images to aim for per product (default 3, hard cap --max-images)",
    )
    parser.add_argument("--log-level", default="INFO", help="DEBUG/INFO/WARNING")
    parser.add_argument(
        "--env-file",
        default=None,
        help="path to a .env file holding FREEIMAGE_API_KEY (default: ./.env)",
    )
    return parser


def configure_logging(config: PipelineConfig, level: str) -> None:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(config.log_path, encoding="utf-8"))
    except OSError as exc:                          # pragma: no cover - disk issues
        print(f"warning: cannot open log file {config.log_path}: {exc}", file=sys.stderr)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
        handlers=handlers,
        force=True,
    )


def build_config(args: argparse.Namespace) -> PipelineConfig:
    # Each sheet gets its own run folder, but naming it needs the *resolved* sheet path,
    early_stop = bool(getattr(args, "early_stop", False)) or PipelineConfig.early_stop_when_image_found
    limit_gallery = bool(getattr(args, "limit_gallery", False)) or PipelineConfig.limit_gallery_iteration
    extra_words = PipelineConfig.extra_generic_name_words
    gallery_publishes_all = PipelineConfig.gallery_publishes_all

    min_required_images = (
        int(args.min_images)
        if getattr(args, "min_images", None) is not None
        else PipelineConfig.min_required_images
    )
    max_images_per_product = (
        int(args.max_images)
        if getattr(args, "max_images", None) is not None
        else PipelineConfig.max_images_per_product
    )
    target_images_per_product = (
        int(args.target_images)
        if getattr(args, "target_images", None) is not None
        else PipelineConfig.target_images_per_product
    )
    fallback_max_pages_per_source = PipelineConfig.fallback_max_pages_per_source

    pinned_output = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_ROOT
    pinned_state = Path(args.state_dir) if args.state_dir else DEFAULT_STATE_ROOT

    return PipelineConfig(
        assets_dir=Path(args.assets_dir),
        output_dir=pinned_output,
        state_dir=pinned_state,
        input_dir=Path(args.input_dir),
        dry_run=bool(args.dry_run),
        direct_mode=bool(args.direct or args.images_dir),
        local_images_mode=bool(args.images_dir),
        workers=int(args.workers),
        limit=args.limit,
        redo_titles=tuple(args.redo or ()),
        resume=not args.no_resume,
        min_required_images=min_required_images,
        max_images_per_product=max_images_per_product,
        target_images_per_product=target_images_per_product,
        keep_rejected_images=bool(getattr(args, "keep_rejected", False)),
        fallback_enabled=not bool(args.no_fallback),
        refresh_sitemaps=bool(args.refresh_sitemaps),
        early_stop_when_image_found=early_stop,
        limit_gallery_iteration=limit_gallery,
        gallery_publishes_all=gallery_publishes_all,
        extra_generic_name_words=extra_words,
        fallback_max_pages_per_source=fallback_max_pages_per_source,
    )



def _discover_sheets_in_dir(directory: Path) -> list[Path]:
    """Find valid .csv and .xlsx files in directory, sorted newest first."""
    candidates: list[Path] = []
    for ext in ("*.csv", "*.xlsx"):
        for path in directory.glob(ext):
            if path.name.startswith("~") or path.name.startswith("."):
                continue
            candidates.append(path)
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)


def resolve_input_path(input_arg: str | None, input_dir: Path) -> Path:
    """Resolve the input sheet file path with enterprise input folder support."""
    if input_dir == Path("input") or str(input_dir) == "input":
        search_dirs = [
            input_dir,
            input_dir.parent / "input",
            Path("input"),
            Path("image_pipeline") / "input",
            Path("uploads"),
            Path("..") / "uploads",
            Path("image_pipeline") / "uploads",
        ]
    else:
        search_dirs = [input_dir]

    if input_arg:
        candidate = Path(input_arg)
        if candidate.is_file():
            return candidate
        # Search relative to search directories
        for directory in search_dirs:
            candidate_in_dir = directory / input_arg
            if candidate_in_dir.is_file():
                return candidate_in_dir
        if candidate.is_dir():
            sheets = _discover_sheets_in_dir(candidate)
            if sheets:
                return sheets[0]
            raise ConfigError(f"no .csv or .xlsx sheet found in directory {candidate.resolve()}")
        raise ConfigError(f"input sheet not found: {input_arg} (also searched in {input_dir.resolve()})")

    # When --input is omitted: auto-discover newest sheet from standard input folders
    seen: set[Path] = set()
    for directory in search_dirs:
        try:
            resolved = directory.resolve()
        except OSError:
            continue
        if resolved in seen or not directory.is_dir():
            continue
        seen.add(resolved)
        sheets = _discover_sheets_in_dir(directory)
        if sheets:
            return sheets[0]

    raise ConfigError(
        f"no input sheet specified and none found in '{input_dir}' or 'uploads/'. "
        "Place your .csv or .xlsx file in the 'input/' folder or specify --input <path>."
    )


# --------------------------------------------------------------------------- #
# Option 3 (--images-dir): a parent folder of product folders, no sheet, no URLs
# --------------------------------------------------------------------------- #
def _sortkey(path: Path) -> tuple[str, str]:
    return (path.name.lower(), path.name)


def _product_path(path: Path) -> str:
    """A file's POSIX path in the spelling every consumer of ``source_urls`` expects."""
    return path.resolve().as_posix()


def _folder_images(folder: Path) -> tuple[str, ...]:
    from url_filters import IMAGE_EXTENSIONS

    files = [p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")]
    images = [p for p in files if p.suffix.lower().lstrip(".") in IMAGE_EXTENSIONS]
    return tuple(_product_path(p) for p in sorted(images, key=_sortkey))


def _folder_products(folder: Path) -> list[tuple[str, tuple[str, ...]]]:
    """(title, images) for one product folder: its own images, or its colourway children."""
    images = _folder_images(folder)
    children = [p for p in _visible_children(folder) if p.is_dir()]
    if images and children:
        raise ValueError(
            f"{folder.name}: has both image files and subfolders, so the title is ambiguous - "
            "give it its own folder, or move the images into the colourway subfolders"
        )
    if images or not children:
        return [(folder.name, images)]
    return [
        (f"{folder.name} {child.name}", _colourway_images(folder.name, child))
        for child in children
    ]


def _colourway_images(model: str, colour: Path) -> tuple[str, ...]:
    """A colourway folder's pictures; folders *inside* it would hide a whole product."""
    images = _folder_images(colour)
    if any(p.is_dir() for p in _visible_children(colour)):
        raise ValueError(
            f"{colour.name} (inside {model}): has pictures and folders of its own - "
            "nesting goes one colour deep; give the inner folder a full product name"
        )
    return images


def _visible_children(parent: Path) -> list[Path]:
    return sorted((p for p in parent.iterdir() if not p.name.startswith(".")), key=_sortkey)


def _reject_stray_images(parent: Path, entries: list[Path]) -> None:
    from url_filters import IMAGE_EXTENSIONS

    stray = [p for p in entries if p.is_file() and p.suffix.lower().lstrip(".") in IMAGE_EXTENSIONS]
    if stray:
        names = f"{stray[0].name} and {len(stray) - 1} more" if len(stray) > 1 else stray[0].name
        raise ValueError(
            f"{parent} holds loose images ({names}) beside "
            "the product folders - did you point --images-dir at a product instead of its parent?"
        )


def discover_product_folders(parent: Path) -> list[ProductTask]:
    """One task per product folder; the folder name is the Title, verbatim.

    Two layouts are read and never mixed: a subfolder holding image files is one
    product; a subfolder holding *only* subfolders contributes ``Outer Inner``
    products (colourways).  The parent itself holds neither images nor loose files.
    """
    entries = _visible_children(parent)
    _reject_stray_images(parent, entries)
    children = [p for p in entries if p.is_dir()]
    if not children:
        raise ValueError(
            f"{parent} holds no product subfolders - give each product its own folder inside it"
        )
    products = [product for child in children for product in _folder_products(child)]
    return [
        ProductTask(index=index, title=title, source_urls=images)
        for index, (title, images) in enumerate(products)
    ]


def _build_tasks(config: PipelineConfig, input_path: Path) -> tuple[list[ProductTask], list[str]]:
    """The run's tasks: Option 3 reads folders, every other mode reads the sheet."""
    if config.local_images_mode:
        from model_matching import build_model_keys

        merged, warnings = merge_duplicate_titles(discover_product_folders(input_path))
        # merge_duplicate_titles rebuilds the tasks, so the stem is applied after it.
        # It keys on the lowercased title, so every spelling of one title shares it.
        return [
            replace(
                task,
                name_prefix_override=local_filename_prefix(build_model_keys(task.title), task.title),
            )
            for task in merged
        ], warnings
    return merge_duplicate_titles(read_input(input_path, direct=config.direct_mode))


def resolve_run_input(args: argparse.Namespace, config: PipelineConfig) -> Path:
    """What this run is named after: the sheet, or Option 3's parent folder."""
    if args.images_dir is None:
        return resolve_input_path(args.input, config.input_dir)
    for other, what in (("input", "the sheet"), ("direct", "direct image URLs")):
        if getattr(args, other):
            raise ConfigError(f"--images-dir replaces {what}; drop --{other} or use one mode")
    parent = Path(args.images_dir)
    if not parent.is_dir():
        raise ConfigError(f"--images-dir needs a folder of product folders: {args.images_dir}")
    return parent


def _mode_description(config: PipelineConfig) -> str:
    """One line a human reads at startup to know which of the three modes is running."""
    if config.local_images_mode:
        return "local image folders (no sheet, no scraping, no downloading)"
    if config.direct_mode:
        return "direct (user-supplied image URLs, no scraping)"
    return "scrape and verify"


def run_folder(input_path: Path) -> str:
    """This sheet's run folder name: ``website_ac-3f9a2c``.

    The stem alone made two sheets called ``website_ac.csv`` share one folder and so one
    ``checkpoint.jsonl``: the second run resumed the first one's rows as if they were its
    own.  Six hex characters of the *resolved* path keep them apart and stay deterministic,
    so re-running the same sheet still resumes.  ``resolve()`` is what makes the hash
    spelling-proof: on Windows it returns the on-disk case and separators, so
    ``--input INPUT/Website_AC.CSV`` and ``--input input/website_ac.csv`` are one folder.
    """
    identity = str(input_path.resolve())
    return f"{input_path.stem}-{hashlib.sha256(identity.encode()).hexdigest()[:6]}"


def name_run_folders(
    config: PipelineConfig, args: argparse.Namespace, input_path: Path
) -> PipelineConfig:
    """Put this run's output and state under ``<root>/<sheet>-<hash>``.

    Named from the resolved file rather than from the text after ``--input``, so
    ``website_ac.csv`` and ``input/website_ac.csv`` keep sharing one checkpoint.  An
    explicit ``--output-dir`` or ``--state-dir`` is used exactly as given.
    """
    folder = run_folder(input_path)
    if args.output_dir is None:
        config = replace(config, output_dir=DEFAULT_OUTPUT_ROOT / folder)
    if args.state_dir is None:
        config = replace(config, state_dir=DEFAULT_STATE_ROOT / folder)
        legacy = DEFAULT_STATE_ROOT / input_path.stem
        if (legacy / "checkpoint.jsonl").is_file():
            LOG.warning(
                "a previous run checkpointed in %s; this one reads %s, so its already "
                "published rows will be processed again unless you move that folder",
                legacy,
                config.state_dir,
            )
    return config


def _display_name(key: str) -> str:
    return next(spec.display_name for spec in DEFAULT_SPECS if spec.key == key)


def platform_groups() -> tuple[tuple[str, ...], ...]:
    """The menu's selectable groups: every platform, then each single, then each pair.

    Generated from the platform table rather than written out, so adding a fourth
    platform adds its menu lines by themselves.
    """
    groups: list[tuple[str, ...]] = [PLATFORM_KEYS]
    for size in range(1, len(PLATFORM_KEYS)):
        groups.extend(combinations(PLATFORM_KEYS, size))
    return tuple(groups)


def _group_label(keys: tuple[str, ...]) -> str:
    names = [_display_name(key) for key in keys]
    if len(keys) == len(PLATFORM_KEYS):
        return f"all ({', '.join(names)})"
    if len(names) == 1:
        return f"{names[0]} only"
    return " + ".join(names)


def select_platforms(
    raw: str | None,
    *,
    interactive: bool,
    emit: Callable[[str], None] = print,
    read: Callable[[], str] = input,
) -> tuple[str, ...] | None:
    """Which platforms this run publishes, or ``None`` to take the config default.

    ``--platforms`` wins outright; otherwise a terminal is asked once.  An empty or
    unparsable answer means **everything** (owner decision D6): the prompt must never
    be able to stop a run, and ``python main.py --input sheet.csv --workers 36`` has to
    keep working with nothing but an extra Enter.
    """
    if raw is not None:
        keys = parse_platform_keys(raw)
        if not keys:
            raise ConfigError("--platforms must name at least one of: " + ", ".join(PLATFORM_KEYS))
        return keys
    if not interactive:
        return None
    emit("Run for which platform(s)?")
    for number, group in enumerate(platform_groups(), start=1):
        hint = "   [Enter]" if number == 1 else ""
        emit(f"  {number} = {_group_label(group)}{hint}")
    try:
        answer = read().strip()
    except EOFError:
        return None
    if not answer.isdigit() or not 1 <= int(answer) <= len(groups := platform_groups()):
        return None
    return groups[int(answer) - 1]


def _build_image_host(config: PipelineConfig) -> ImageHost:
    """Build the configured image host, supporting dry-run, primary, fallback or failover."""
    if config.dry_run:
        return DryRunHost(config.output_dir)

    hosts: list[ImageHost] = []
    if config.api_key:
        hosts.append(
            FreeImageHost(
                api_key=config.api_key,
                endpoint=config.upload_url,
                min_interval_s=config.upload_min_interval_s,
                max_retries=config.max_retries,
                base_delay_s=config.backoff_base_s,
                max_delay_s=config.backoff_max_s,
            )
        )
    if config.imgbb_api_key:
        hosts.append(
            ImgBBHost(
                api_key=config.imgbb_api_key,
                endpoint=config.imgbb_upload_url,
                min_interval_s=config.upload_min_interval_s,
                max_retries=config.max_retries,
                base_delay_s=config.backoff_base_s,
                max_delay_s=config.backoff_max_s,
            )
        )
    if config.apilayer_api_key:
        hosts.append(
            APILayerHost(
                api_key=config.apilayer_api_key,
                endpoint=config.apilayer_upload_url,
                min_interval_s=config.upload_min_interval_s,
                max_retries=config.max_retries,
                base_delay_s=config.backoff_base_s,
                max_delay_s=config.backoff_max_s,
            )
        )

    if len(hosts) > 1:
        return FallbackImageHost(hosts=hosts)
    if len(hosts) == 1:
        return hosts[0]

    raise PermanentAPIError(
        "No upload host configured (FREEIMAGE_API_KEY, IMGBB_API_KEY, or APILAYER_API_KEY required)"
    )


def build_deps(
    config: PipelineConfig, decisions: DecisionLog, platforms: PlatformPlan
) -> PipelineDeps:
    limiter = DomainRateLimiter(config.domain_min_interval_s)
    host = _build_image_host(config)
    # Direct mode (Option 2) never searches: the row supplies its own images.
    searcher = (
        SourceChain(config, CurlCffiJsonHttp(config, limiter))
        if config.fallback_enabled and not config.direct_mode
        else None
    )
    return PipelineDeps(
        page_fetcher=CurlCffiPageFetcher(config, limiter),
        # Option 3 keeps the Option 2 processor untouched: the only difference is
        # where fetch_image's bytes come from - disk instead of the network.
        image_fetcher=(
            LocalImageFetcher(config.max_image_bytes)
            if config.local_images_mode
            else CurlCffiImageFetcher(config, limiter)
        ),
        image_host=host,
        decisions=decisions,
        banner_cache=load_banner_cache(config.banner_cache_path),
        banner_cache_lock=threading.Lock(),
        platforms=platforms,
        source_searcher=searcher,
    )


def empty_row_results(tasks: list[ProductTask]) -> list[ProductResult]:
    results: list[ProductResult] = []
    for task in tasks:
        reason = "empty title" if not task.title else "no source url"
        if not task.title or not task.source_urls:
            results.append(
                ProductResult(
                    task_index=task.index,
                    title=task.title or f"<row {task.index + 1}>",
                    status=Status.FAILED,
                    reason=reason,
                )
            )
    return results


def _check_local_images(
    paths: list[str], config: PipelineConfig, title: str = ""
) -> int:
    """Apply the pipeline's own image chain to saved files and print the verdicts.

    This exists because "will the official product image be published?" has two halves.
    The URL/DOM half can be replayed offline against a captured page, but the pixel half
    (S7 ring, S8 size, S20 marketing layout, S21 body layout, the panel cut, the official
    rescues) can only be answered for real bytes.  Rather than burning a whole crawl to ask
    it about one file, point this at the file.  The helpers are the pipeline's own on
    purpose: a checker that reimplemented the gates could drift from the run and lie.

    Returns 0 when every file would publish, 1 when a gate stops one, 2 when one cannot be
    opened at all.
    """
    from PIL import Image

    from compositing import isolate_main_product, to_square_canvas
    from image_quality import (
        marketing_panel_split,
        official_gallery_pass,
        run_quality_gates,
        strip_promo_band,
        to_rgb,
        trim_edge_seams,
        verify_image_color,
    )
    from model_matching import build_model_keys

    colour_word = build_model_keys(title).colour_word if title else ""
    from pipeline import _layout_rescue_verdict

    exit_code = 0
    for raw in paths:
        path = Path(raw)
        try:
            with Image.open(path) as opened:
                # load() first: to_rgb hands back the same object for an RGB file, and the
                # `with` closes the handle the lazy image still reads from.
                opened.load()
                # to_rgb, not convert("RGB"): a cut-out PNG must be judged on the white
                # ground the customer sees, not on the black PIL fills transparency with.
                image = to_rgb(opened)
        except (OSError, ValueError) as exc:
            print(f"{path.name}: cannot open ({exc})")
            exit_code = 2
            continue
        original = image.size
        # The same three cuts _visit_page applies before it judges anything.
        image, _band = strip_promo_band(image, config)
        image, _seams = trim_edge_seams(image, config)
        image, panel = marketing_panel_split(image, config)
        print(
            f"{path.name}: {original[0]}x{original[1]}"
            + (f" -> {image.size[0]}x{image.size[1]} after cutting the {panel[0]} marketing panel" if panel else "")
        )
        for label, official in (("brand-official page", True), ("retailer page", False)):
            rule_id, detail = run_quality_gates(
                image, config, official_gallery=official, panel_cut=panel
            )
            if rule_id == "S7" and official and official_gallery_pass(image, config):
                rule_id, detail = "ok", "saved by the official studio-corner pass"
            if rule_id in ("S20", "S21") and official:
                rescued, reason = _layout_rescue_verdict(image, config, rule_id, detail)
                if rescued:
                    rule_id, detail = "ok", f"saved by {reason}"
            if rule_id == "ok":
                print(f"  {label:19s}: PUBLISH")
            else:
                exit_code = exit_code or 1
                print(f"  {label:19s}: STOP {rule_id} {detail}")
        isolated, _degraded = isolate_main_product(image, config)
        square = to_square_canvas(
            isolated,
            config.canonical_side,
            fill_ratio=config.product_fill_ratio,
            max_upscale=config.max_upscale,
        )
        if colour_word:
            # S16 is judged on the same pixels the gates saw, per bar: the owner's Orient
            # row died here, not in the layout gates, and the brand-page bar is the one that
            # must not punish a coloured studio wash.
            print(f"  colour gate S16 for row '{title}' (colour word {colour_word!r}):")
            from variant_terms import colour_word_conflict

            # The pipeline's own carve-out, mirrored so this check cannot flatter a file:
            # a file name that names a different colourway cancels the brand-page exemption.
            name_conflict = colour_word_conflict(colour_word, path.name)
            if name_conflict:
                print("    note: the file name names a different colour - the brand-page "
                      "exemption is off for this file")
            for label, official in (("brand-official page", True), ("retailer page", False)):
                ok, why = verify_image_color(
                    image,
                    colour_word,
                    official=official,
                    exempt_chroma_on_official=(
                        config.color_gate_official_chroma_exempt and not name_conflict
                    ),
                    min_saturation=config.color_chroma_min_saturation,
                    wash_max_value=config.color_chroma_max_value,
                )
                if ok:
                    print(f"    {label:19s}: PUBLISH" + (f" - {why}" if why else ""))
                else:
                    exit_code = exit_code or 1
                    print(f"    {label:19s}: STOP S16 {why}")
        print(
            "  files the run would write: "
            f"raw {image.size[0]}x{image.size[1]}, square {square.size[0]}x{square.size[1]}"
        )
    return exit_code


def _learn_differentiators(config: PipelineConfig, tasks: list[ProductTask]) -> None:
    """Install the words the catalogue data itself proves (see model_matching).

    Two sets come out of one scan: the tier words that separate ``Avante`` from
    ``Avante+``, and the colourway words that separate ``Pearl Red`` from ``Ruby Red``.
    The tiers are learned first because the colourway reader counts series lines, and a
    series line is named by the tier-aware name words.
    """
    import csv

    from model_matching import learn_colourway_words, learn_series_differentiators
    from url_filters import detect_brand

    pairs: list[tuple[str | None, str]] = []
    corpus = config.series_learning_corpus.strip()
    if corpus:
        path = Path(corpus)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent / path
        try:
            with path.open(newline="", encoding="utf-8-sig") as handle:
                pairs.extend(
                    (row.get("Brand"), row.get("Original_Title") or "")
                    for row in csv.DictReader(handle)
                )
        except OSError as exc:
            LOG.warning("series learning corpus unreadable (%s): %s", path, exc)
    for task in tasks:
        first_url = task.source_urls[0] if task.source_urls else ""
        pairs.append((detect_brand(first_url, task.title), task.title))
    learned = learn_series_differentiators(pairs, extra=config.extra_series_differentiators)
    LOG.info(
        "series differentiators learned from %d title(s): %s",
        len(pairs), ", ".join(sorted(learned)) or "none",
    )
    colourways = learn_colourway_words(
        pairs,
        min_evidence=config.colourway_min_evidence,
        min_exclusivity=config.colourway_min_exclusivity,
    )
    LOG.info(
        "colourway words learned from %d title(s): %s",
        len(pairs), ", ".join(sorted(colourways)) or "none",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check_image and args.images_dir:
        # check-image judges single files by hand; it must not silently ignore a folder.
        print(
            "configuration error: --check-image judges files given on the command line; "
            "drop --images-dir",
            file=sys.stderr,
        )
        return 2
    if args.check_image:
        # This mode reads only local files: no sheet, no assets, no API key, no network.
        return _check_local_images(
            list(args.check_image), PipelineConfig(dry_run=True), str(args.check_title)
        )
    # Secrets come from the environment; a .env file is only a convenience wrapper.
    # A real exported variable always wins, and the value itself is never logged.
    env_path = load_env_file(Path(args.env_file) if args.env_file else None)
    try:
        config = build_config(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        input_path = resolve_run_input(args, config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    # Named before logging starts: the log file and the checkpoint both live in this
    # sheet's own folder, so a stray un-hashed folder must never be created for it.
    config = name_run_folders(config, args, input_path)

    configure_logging(config, args.log_level)
    if env_path is not None:
        LOG.info("secrets: loaded %s", env_path)
    LOG.info(
        "freeimage key: %s",
        "set" if config.api_key else ("not needed (dry-run)" if config.dry_run else "MISSING"),
    )
    LOG.info("input: %s", input_path.resolve())
    LOG.info(
        "mode: %s | dry-run: %s | workers: %d",
        _mode_description(config),
        config.dry_run,
        config.workers,
    )

    # Asked after the sheet is named, so the person choosing can see what they are choosing for.
    try:
        requested = select_platforms(args.platforms, interactive=sys.stdin.isatty())
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    platforms = resolve_platforms(config, requested)
    for notice in platforms.notices:
        LOG.info("%s", notice)
    if not platforms.platforms:
        raise SystemExit(
            "nothing to publish: no platform has its artwork in "
            f"{config.assets_dir.resolve()} - see the platform lines above for the exact files"
        )
    LOG.info("publishing on: %s", ", ".join(platform.display_name for platform in platforms.platforms))

    try:
        tasks, warnings = _build_tasks(config, input_path)
    except (OSError, ValueError) as exc:
        what = "product folders" if config.local_images_mode else "input sheet"
        print(f"cannot read {what}: {exc}", file=sys.stderr)
        return 2
    for warning in warnings:
        LOG.warning("%s", warning)

    # Sheet knowledge for S4's third limb: a file whose name carries another row's
    # series word belongs to another product, whatever the page's status (Orient run
    # 2026-10-10: the Pulse row published the Innova file).  Computed over the whole
    # sheet before ``--limit`` trims the view.
    # ``_learn_differentiators`` runs first: sibling rows prove which short words
    # (``+`` of ``Avante+``, ``Pro`` of ``Acce Pro``) separate two sold variants, and
    # those words must already be name words when the sibling terms are read out.
    _learn_differentiators(config, tasks)
    from model_matching import annotate_sibling_terms, annotate_tail_owners

    tasks = annotate_sibling_terms(tasks)
    tasks = annotate_tail_owners(tasks)

    checkpoint = {} if not config.resume else load_checkpoint(config.checkpoint_path)

    if config.limit is not None:
        redo = {title.strip().lower() for title in config.redo_titles}

        def _is_done(t: ProductTask) -> bool:
            key = t.title.strip().lower()
            if key in redo:
                return False
            entry = checkpoint.get(key)
            if entry is None:
                return False
            return entry.is_resume_done(
                hash_source_urls(t.source_urls), platforms.keys
            )

        pending_count = 0
        selected_tasks: list[ProductTask] = []
        for task in tasks:
            if _is_done(task):
                selected_tasks.append(task)
            elif pending_count < config.limit:
                selected_tasks.append(task)
                pending_count += 1
        tasks = selected_tasks

    LOG.info("rows to process: %d (limit: %s)", len(tasks), config.limit)

    bad_rows = [task for task in tasks if not task.title or not task.source_urls]
    good_rows = [task for task in tasks if task.title and task.source_urls]

    decisions = DecisionLog(config.decisions_path)
    deps = build_deps(config, decisions, platforms)
    runner = BatchRunner(
        config,
        deps,
        good_rows,
        checkpoint,
        processor=process_direct_product if config.direct_mode else None,
    )
    try:
        results = runner.run()
    except OSError as exc:
        # e.g. the output disk is full: the checkpoint keeps whatever finished, so a
        # rerun resumes instead of starting over.  Fail with a plain sentence.
        print(f"cannot write run outputs under {config.output_dir}: {exc}", file=sys.stderr)
        return 3
    except PermanentAPIError as exc:
        print(f"\n[FATAL] Image host error: {exc}", file=sys.stderr)
        print(
            "The image host (freeimage.host) refused the upload permanently.\n"
            "Common causes:\n"
            "  1. The API key in secrets.env has expired or been banned (e.g. error code 103).\n"
            "  2. Your IP address is temporarily blocked due to rate limits or VPN.\n"
            "\n"
            "How to proceed:\n"
            "  - Run with '--dry-run' to process and generate all images locally without uploading.\n"
            "  - Or get a fresh API key from https://freeimage.host/page/api and update secrets.env.\n"
            "All completed products have been preserved in the checkpoint file.",
            file=sys.stderr,
        )
        return 4

    everything = sorted([*results, *empty_row_results(bad_rows)], key=lambda item: item.task_index)
    write_output_csvs(everything, config, platforms)
    _write_failed_helper_csvs(everything, tasks, config)
    summarize(everything, config)
    _report_unstated_shades(tasks)
    _report_colour_variants(everything)
    return 0


def _report_colour_variants(results: list[ProductResult]) -> None:
    """S17's question: rows whose pages offered more than one colour and state none.

    The guard locks a gallery's colour only when two accepted pictures agree, so a row with
    one blue and one red picture is a real choice the matcher must not make (owner
    2026-10-16: a wrong colour picture costs a penalty).  Listing those rows once, with the
    families the pages actually showed, turns a silent mix into one word to write in the
    sheet - and nothing is printed for a row whose pictures agree.
    """
    asked = [
        result
        for result in results
        if COLOUR_UNSTATED_NOTE in result.note and result.images_accepted > 0
    ]
    if not asked:
        return
    print(
        f"\nCOLOUR NOT STATED ({len(asked)}) - the pages of these rows show more than one "
        "colour.\nWrite the colour into the row (e.g. 'Maroon', not nothing) and the other "
        "colours\nstop being accepted for it:"
    )
    for result in asked:
        detail = result.note.split(COLOUR_UNSTATED_NOTE, 1)[1].split(" - ")[0]
        print(f"  - {csv_escape(result.title)}  {detail.strip()}")


def _report_unstated_shades(tasks: list[ProductTask]) -> None:
    """S20.shade-unstated: rows whose own link names a shade the sheet leaves out.

    The matcher cannot invent a colourway the row never states, and a bare "Red" is three
    different fridges (owner 2026-10-16).  Listing the rows once at the end is what turns a
    wrong picture into one line of the sheet to fix.
    """
    from model_matching import shade_unstated_rows

    unstated = shade_unstated_rows(tasks)
    if not unstated:
        return
    print(
        f"\nSHADE NOT STATED ({len(unstated)}) - the row's own link names a colourway the "
        "title does not.\nWrite it into the title (e.g. 'Pearl Red', not 'Red') and the "
        "other shades of the same\ncolour stop being accepted for that row:"
    )
    for title, shade in unstated:
        print(f"  - {csv_escape(title)}  -> {shade}")


def _write_failed_helper_csvs(
    results: list[ProductResult], tasks: list[ProductTask], config: PipelineConfig
) -> None:
    """Generate ready-to-run CSVs for failed products so they can be re-run easily in Option 1 or Option 2."""
    task_by_index = {task.index: task for task in tasks}
    failed_results = [r for r in results if r.status is Status.FAILED]
    if failed_results:
        rows = [
            (r.title, " | ".join(task_by_index[r.task_index].source_urls) if r.task_index in task_by_index else "")
            for r in failed_results
        ]
        if config.direct_mode:
            # Option 2 re-run sheet: Title + Images (direct URLs)
            atomic_write_csv(
                config.output_dir / "failed_products_option2.csv",
                ("Title", "Images"),
                rows,
            )
        else:
            # Option 1 re-run sheet: Title + Source URL (page URLs)
            atomic_write_csv(
                config.output_dir / "failed_products_option1.csv",
                ("Title", "Source URL"),
                rows,
            )

    # 3. Complete CSV audit of all rejected images (blurry, template, color mismatch, etc.)
    if config.decisions_path.exists():
        import json
        rejected_rows = []
        try:
            for line in config.decisions_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("decision") == "reject":
                    rejected_rows.append((
                        entry.get("title", ""),
                        entry.get("url", ""),
                        entry.get("rule_id", ""),
                        entry.get("detail", ""),
                    ))
            if rejected_rows:
                atomic_write_csv(
                    config.output_dir / "rejected_images.csv",
                    ("Title", "URL", "Rule", "Detail"),
                    rejected_rows,
                )
        except Exception as exc:
            LOG.warning("could not write rejected_images.csv: %s", exc)



def summarize(results: list[ProductResult], config: PipelineConfig) -> None:
    ok = sum(1 for result in results if result.status is Status.OK)
    partial = sum(1 for result in results if result.status is Status.PARTIAL)
    failed = sum(1 for result in results if result.status is Status.FAILED)
    print("\n=== run summary ===")
    print(f"ok      : {ok}")
    print(f"partial : {partial}   (flagged in {config.report_csv.name})")
    print(f"failed  : {failed}   (see {config.failed_csv.name})")
    for result in results:
        if result.status is Status.FAILED:
            print(f"  - {csv_escape(result.title)} : {result.reason}")
    tail_rows = [r for r in results if "tail-chunk match" in r.note]
    if tail_rows:
        print(
            f"\nTAIL-CHUNK ROWS ({len(tail_rows)}) - the sheet's URLs never printed the full"
            " model, so a page carrying only its code tail was published as a last resort;"
            " confirm each (title, page and tier below) before trusting it:"
        )
        for result in tail_rows:
            note = next(
                (p for p in result.note.split("; ") if "tail-chunk match" in p), result.note
            )
            print(f"  - {csv_escape(result.title)}\n      {note}")
    print(f"report  : {config.report_csv}")
    print(f"decisions: {config.decisions_path}")


if __name__ == "__main__":
    raise SystemExit(main())

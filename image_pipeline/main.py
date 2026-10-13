"""Command line entry point.

    python main.py --input "sample test 10 products images.csv" --dry-run
    FREEIMAGE_API_KEY=... python main.py --input sheet.xlsx --workers 4
    python main.py --check-image path/to/hero.png      # which gate stops this file, no crawl

Fail-fast order: assets -> config (incl. API key) -> sheet -> checkpoint -> run.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path

from config import ConfigError, PipelineConfig, load_env_file
from csv_io import (
    atomic_write_csv,
    hash_source_urls,
    load_banner_cache,
    load_checkpoint,
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
    PermanentAPIError,
)
from image_host import FreeImageHost
from models import ProductResult, ProductTask, Status
from pipeline import (
    BatchRunner,
    DryRunHost,
    PipelineDeps,
    csv_escape,
    write_output_csvs,
)
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
    parser.add_argument("--assets-dir", default="assets", help="folder with the two templates and two banners")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="where the CSVs, images and logs go (default: output_<input file name>, or output)",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="checkpoint and banner cache folder (default: state_<input file name>, or state)",
    )
    parser.add_argument("--dry-run", action="store_true", help="do everything except uploads and platform CSVs")
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Option 2: the sheet's URL column holds direct image URLs - download, template "
        "and publish them; no pages are fetched and nothing is scraped",
    )
    parser.add_argument("--redo", nargs="*", default=[], help="exact titles to force re-process")
    parser.add_argument("--limit", type=int, default=None, help="process at most N products")
    parser.add_argument("--workers", type=int, default=4, help="parallel workers (default 4)")
    parser.add_argument("--min-images", type=int, default=2, help="images required for status ok")
    parser.add_argument("--max-images", type=int, default=10, help="images kept per product")
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
        default=3,
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
    # Each input sheet gets its own folders, so two sheets never mix their outputs/checkpoints.
    stem = Path(args.input).stem if args.input else ""
    output_dir = args.output_dir or (str(Path("output") / stem) if stem else "output")
    state_dir = args.state_dir or (str(Path("state") / stem) if stem else "state")
    return PipelineConfig(
        assets_dir=Path(args.assets_dir),
        output_dir=Path(output_dir),
        state_dir=Path(state_dir),
        input_dir=Path(args.input_dir),
        dry_run=bool(args.dry_run),
        direct_mode=bool(args.direct),
        workers=int(args.workers),
        limit=args.limit,
        redo_titles=tuple(args.redo or ()),
        resume=not args.no_resume,
        min_required_images=int(args.min_images),
        max_images_per_product=int(args.max_images),
        target_images_per_product=int(args.target_images),
        keep_rejected_images=bool(getattr(args, "keep_rejected", False)),
        fallback_enabled=not bool(args.no_fallback),
        refresh_sitemaps=bool(args.refresh_sitemaps),
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


def check_assets(config: PipelineConfig) -> None:
    """Fail fast, listing every missing asset and the folder that was searched."""
    missing = [path for path in config.asset_paths.values() if not path.exists()]
    if missing:
        listing = "\n".join(f"  - missing: {path}" for path in missing)
        raise SystemExit(
            f"asset check failed - searched in {config.assets_dir.resolve()}:\n{listing}\n"
            "expected files: "
            + ", ".join(
                path.name for path in config.asset_paths.values()
            )
        )


def build_deps(config: PipelineConfig, decisions: DecisionLog) -> PipelineDeps:
    limiter = DomainRateLimiter(config.domain_min_interval_s)
    host = (
        DryRunHost(config.output_dir)
        if config.dry_run
        else FreeImageHost(
            api_key=config.api_key,
            endpoint=config.upload_url,
            min_interval_s=config.upload_min_interval_s,
            max_retries=config.max_retries,
            base_delay_s=config.backoff_base_s,
            max_delay_s=config.backoff_max_s,
        )
    )
    # Direct mode (Option 2) never searches: the row supplies its own images.
    searcher = (
        SourceChain(config, CurlCffiJsonHttp(config, limiter))
        if config.fallback_enabled and not config.direct_mode
        else None
    )
    return PipelineDeps(
        page_fetcher=CurlCffiPageFetcher(config, limiter),
        image_fetcher=CurlCffiImageFetcher(config, limiter),
        image_host=host,
        decisions=decisions,
        banner_cache=load_banner_cache(config.banner_cache_path),
        banner_cache_lock=threading.Lock(),
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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

    configure_logging(config, args.log_level)
    if env_path is not None:
        LOG.info("secrets: loaded %s", env_path)
    LOG.info(
        "freeimage key: %s",
        "set" if config.api_key else ("not needed (dry-run)" if config.dry_run else "MISSING"),
    )
    check_assets(config)
    try:
        input_path = resolve_input_path(args.input, config.input_dir)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    LOG.info("input: %s", input_path.resolve())
    LOG.info(
        "mode: %s | dry-run: %s | workers: %d",
        "direct (user-supplied image URLs, no scraping)" if config.direct_mode else "scrape and verify",
        config.dry_run,
        config.workers,
    )

    try:
        tasks, warnings = merge_duplicate_titles(read_input(input_path, direct=config.direct_mode))
    except (OSError, ValueError) as exc:
        print(f"cannot read input sheet: {exc}", file=sys.stderr)
        return 2
    for warning in warnings:
        LOG.warning("%s", warning)

    # Sheet knowledge for S4's third limb: a file whose name carries another row's
    # series word belongs to another product, whatever the page's status (Orient run
    # 2026-10-10: the Pulse row published the Innova file).  Computed over the whole
    # sheet before ``--limit`` trims the view.
    from model_matching import annotate_sibling_terms

    tasks = annotate_sibling_terms(tasks)

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
            return entry.is_resume_done(hash_source_urls(t.source_urls))

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
    deps = build_deps(config, decisions)
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
    write_output_csvs(everything, config, include_platform_csvs=not config.dry_run)
    _write_failed_helper_csvs(everything, tasks, config)
    summarize(everything, config)
    return 0


def _write_failed_helper_csvs(
    results: list[ProductResult], tasks: list[ProductTask], config: PipelineConfig
) -> None:
    """Generate ready-to-run CSVs for failed products so they can be re-run easily in Option 1 or Option 2."""
    task_by_index = {task.index: task for task in tasks}
    failed_results = [r for r in results if r.status is Status.FAILED]
    if failed_results:
        # 1. Option 1 re-run sheet: Title + Source URL (page URLs)
        opt1_rows = [
            (r.title, " | ".join(task_by_index[r.task_index].source_urls) if r.task_index in task_by_index else "")
            for r in failed_results
        ]
        atomic_write_csv(
            config.output_dir / "failed_products_option1.csv",
            ("Title", "Source URL"),
            opt1_rows,
        )
        # 2. Option 2 re-run sheet: Title + Images (direct URLs)
        opt2_rows = [
            (r.title, " | ".join(task_by_index[r.task_index].source_urls) if r.task_index in task_by_index else "")
            for r in failed_results
        ]
        atomic_write_csv(
            config.output_dir / "failed_products_option2.csv",
            ("Title", "Images"),
            opt2_rows,
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
    print(f"report  : {config.report_csv}")
    print(f"decisions: {config.decisions_path}")


if __name__ == "__main__":
    raise SystemExit(main())

"""Command line entry point.

    python main.py --input "sample test 10 products images.csv" --dry-run
    FREEIMAGE_API_KEY=... python main.py --input sheet.xlsx --workers 4

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
    load_banner_cache,
    load_checkpoint,
    merge_duplicate_titles,
    read_input,
)
from decisions import DecisionLog
from fetch import (
    CurlCffiImageFetcher,
    CurlCffiJsonHttp,
    CurlCffiPageFetcher,
    DomainRateLimiter,
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
    parser.add_argument("--input", required=True, help="CSV or XLSX sheet with Title + source URL columns")
    parser.add_argument("--assets-dir", default="assets", help="folder with the two templates and two banners")
    parser.add_argument("--output-dir", default="output", help="where the CSVs, images and logs go")
    parser.add_argument("--state-dir", default="state", help="checkpoint and banner cache folder")
    parser.add_argument("--dry-run", action="store_true", help="do everything except uploads and platform CSVs")
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
    return PipelineConfig(
        assets_dir=Path(args.assets_dir),
        output_dir=Path(args.output_dir),
        state_dir=Path(args.state_dir),
        dry_run=bool(args.dry_run),
        workers=int(args.workers),
        limit=args.limit,
        redo_titles=tuple(args.redo or ()),
        resume=not args.no_resume,
        min_required_images=int(args.min_images),
        max_images_per_product=int(args.max_images),
        target_images_per_product=int(args.target_images),
        fallback_enabled=not bool(args.no_fallback),
        refresh_sitemaps=bool(args.refresh_sitemaps),
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
    searcher = (
        SourceChain(config, CurlCffiJsonHttp(config, limiter))
        if config.fallback_enabled
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
    LOG.info("input: %s", Path(args.input).resolve())
    LOG.info("dry-run: %s | workers: %d", config.dry_run, config.workers)

    try:
        tasks, warnings = merge_duplicate_titles(read_input(Path(args.input)))
    except (OSError, ValueError) as exc:
        print(f"cannot read input sheet: {exc}", file=sys.stderr)
        return 2
    for warning in warnings:
        LOG.warning("%s", warning)
    if config.limit is not None:
        tasks = tasks[: config.limit]
    LOG.info("rows to process: %d", len(tasks))

    bad_rows = [task for task in tasks if not task.title or not task.source_urls]
    good_rows = [task for task in tasks if task.title and task.source_urls]

    checkpoint = {} if not config.resume else load_checkpoint(config.checkpoint_path)
    decisions = DecisionLog(config.decisions_path)
    deps = build_deps(config, decisions)
    runner = BatchRunner(config, deps, good_rows, checkpoint)
    try:
        results = runner.run()
    except OSError as exc:
        # e.g. the output disk is full: the checkpoint keeps whatever finished, so a
        # rerun resumes instead of starting over.  Fail with a plain sentence.
        print(f"cannot write run outputs under {config.output_dir}: {exc}", file=sys.stderr)
        return 3

    everything = sorted([*results, *empty_row_results(bad_rows)], key=lambda item: item.task_index)
    write_output_csvs(everything, config, include_platform_csvs=not config.dry_run)
    summarize(everything, config)
    return 0


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

"""Pipeline behaviour with fakes only: S14, ordering, resume and abort semantics."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import (
    FakeImageFetcher,
    FakeImageHost,
    FakePageFetcher,
    FakeSourceSearcher,
    clean_product_image,
    load_fixture,
    make_assets,
    png_bytes,
    second_angle_image,
    third_angle_image,
)
from PIL import Image, ImageDraw

from config import PipelineConfig
from csv_io import (
    atomic_write_csv,
    hash_source_urls,
    load_checkpoint,
    merge_duplicate_titles,
    read_input,
    safe_slug,
)
from decisions import DecisionLog
from fetch import PermanentAPIError
from models import (
    AcceptedImage,
    CheckpointEntry,
    PageCandidate,
    ProductResult,
    ProductTask,
    SourceSearchReport,
    Status,
)
from pipeline import (
    BatchRunner,
    DryRunHost,
    PipelineDeps,
    ensure_banner_url,
    finalize_and_upload,
    process_product,
    write_output_csvs,
)

HAIER_PAGE = "https://www.haier.com/pk/hsu-12hfpaa.shtml"
NO_GALLERY_PAGE = "https://www.haier.com/pk/no-gallery"

IMG1 = "https://cdn.haier.com/p/hsu-12hfpaa-1.jpg"
IMG2 = "https://cdn.haier.com/p/hsu-12hfpaa-2.jpg"
IMG3 = "https://cdn.haier.com/p/hsu-12hfpaa-3.jpg"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def live_config(tmp_path: Path, **overrides: object) -> PipelineConfig:
    """A non-dry-run config (uploads go to the fake host, so nothing leaves the box)."""
    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    make_assets(assets)
    defaults: dict[str, object] = {
        "assets_dir": assets,
        "output_dir": tmp_path / "out",
        "state_dir": tmp_path / "state",
        "workers": 3,
        "domain_min_interval_s": 0.0,
        "upload_min_interval_s": 0.0,
        "api_key": "test-key",
    }
    defaults.update(overrides)
    return PipelineConfig(**defaults)  # type: ignore[arg-type]


def gallery_images() -> dict[str, bytes]:
    return {
        IMG1: png_bytes(clean_product_image(900, 900, "#2f5fa8")),
        IMG2: png_bytes(second_angle_image()),
        IMG3: png_bytes(third_angle_image()),
    }


def build_deps(
    tmp_path: Path,
    pages: dict[str, str],
    images: dict[str, bytes],
    host: FakeImageHost | None = None,
) -> tuple[PipelineDeps, FakePageFetcher, FakeImageFetcher, FakeImageHost]:
    page_fetcher = FakePageFetcher(pages=pages)
    image_fetcher = FakeImageFetcher(images=images)
    image_host = host or FakeImageHost()
    deps = PipelineDeps(
        page_fetcher=page_fetcher,
        image_fetcher=image_fetcher,
        image_host=image_host,
        decisions=DecisionLog(tmp_path / "out" / "decisions.jsonl"),
        banner_cache={},
        banner_cache_lock=threading.Lock(),
    )
    return deps, page_fetcher, image_fetcher, image_host


def task(index: int, title: str, urls: tuple[str, ...]) -> ProductTask:
    return ProductTask(index=index, title=title, source_urls=urls)


# --------------------------------------------------------------------------- #
# happy path, ordering, banners
# --------------------------------------------------------------------------- #
def test_ok_product_urls_are_real_images_then_banner(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    deps, pages, _, host = build_deps(
        tmp_path,
        {HAIER_PAGE: load_fixture("product_ok")},
        gallery_images(),
    )
    result, images = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    assert result.status is Status.OK
    assert len(images) == 3
    assert len(result.digi_urls) == 4                      # 3 images + closing banner
    assert result.digi_urls[-1].endswith("digimall-closing-banner.jpg")
    assert len(result.alfa_urls) == len(result.digi_urls)
    assert result.alfa_urls[-1].endswith("alfamall-closing-banner.jpg")
    assert host.uploads[-2].startswith("digimall-closing-banner")
    assert host.uploads[-1].startswith("alfamall-closing-banner")
    for accepted in images:
        assert accepted.digi_path.exists() and accepted.alfa_path.exists()
        assert accepted.raw_path.exists() and accepted.square_path.exists()


def test_partial_product_gets_one_banner_per_platform(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    html = """<html><head><title>Haier HSU-12HFPAA AC</title></head>
    <body><div class="product-gallery"><img src="https://cdn.haier.com/p/hsu-12hfpaa-1.jpg"></div>
    </body></html>"""
    deps, _, _, host = build_deps(tmp_path, {HAIER_PAGE: html},
                                  {IMG1: png_bytes(clean_product_image())})
    result, images = process_product(
        task(0, "Haier HSU-12HFPAA", (HAIER_PAGE,)), deps, config
    )
    assert result.status is Status.PARTIAL
    assert len(images) == 1
    assert len(result.digi_urls) == 2 and len(result.alfa_urls) == 2
    banner_uploads = [name for name in host.uploads if "closing-banner" in name]
    assert len(banner_uploads) == 2                        # one per platform


def test_failed_product_uploads_nothing(tmp_path: Path) -> None:
    """S14 + S12: a matched page without any usable gallery publishes nothing."""
    config = live_config(tmp_path)
    deps, _, _, host = build_deps(tmp_path, {NO_GALLERY_PAGE: load_fixture("no_gallery")}, {})
    result, images = process_product(
        task(0, "Haier HSU-12HFPAA", (NO_GALLERY_PAGE,)), deps, config
    )
    assert result.status is Status.FAILED
    assert result.reason == "pages matched but no image passed quality gates"
    assert result.digi_urls == () and result.alfa_urls == ()
    assert images == []
    assert host.uploads == []                              # zero upload calls


def test_pipeline_has_no_fallback_source(tmp_path: Path) -> None:
    """A valid model match with no gallery is never rescued by any fallback (S12)."""
    config = live_config(tmp_path)
    html = """<html><head><title>Haier HSU-12HFPAA AC</title></head><body>
    <img src="https://cdn.haier.com/site/hero.jpg">
    <img src="https://cdn.shop.example/images/random-photo.jpg"></body></html>"""
    deps, _, _, host = build_deps(tmp_path, {HAIER_PAGE: html},
                                  {})
    result, _ = process_product(task(0, "Haier HSU-12HFPAA", (HAIER_PAGE,)), deps, config)
    assert result.status is Status.FAILED
    assert host.uploads == []


def test_listing_page_is_rejected_by_s1(tmp_path: Path) -> None:
    """A fetchable page that never names the model: strength 0, zero extraction (S1)."""
    config = live_config(tmp_path)
    listing = "https://shop.example/product/gree-gs-18pja"
    deps, pages, image_fetcher, host = build_deps(tmp_path, {listing: load_fixture("listing_page")}, {})
    result, _ = process_product(task(0, "Haier HSU-12HFPAA", (listing,)), deps, config)
    assert result.status is Status.FAILED
    assert result.reason == "no page matched this model"
    assert image_fetcher.requested == []                   # S1: no extraction at all
    assert host.uploads == []


def test_non_product_and_dead_urls_never_fetched(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    deps, page_fetcher, _, _ = build_deps(tmp_path, {}, {})
    result, _ = process_product(
        task(0, "Haier HSU-12HFPAA", ("https://shop.example/blog/best-acs",)),
        deps,
        config,
    )
    assert result.reason == "all source URLs rejected as non-product pages"
    assert page_fetcher.requested == []                    # S11 runs before any fetch


def test_second_url_is_used_when_the_first_fails(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    deps, page_fetcher, _, _ = build_deps(
        tmp_path,
        {"https://www.haier.com/pk/alt.shtml": load_fixture("product_ok")},
        gallery_images(),
    )
    result, _ = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC",
             ("https://www.haier.com/pk/dead.shtml", "https://www.haier.com/pk/alt.shtml")),
        deps,
        config,
    )
    assert result.status is Status.OK
    assert page_fetcher.requested[1] == "https://www.haier.com/pk/alt.shtml"


def test_colour_conflict_is_a_flag_not_a_failure(tmp_path: Path) -> None:
    """Customer decision (2026-10-05): a colour difference is recorded, nothing more."""
    config = live_config(tmp_path)
    html = """<html><head><title>Haier HRF-538 IFGA Refrigerator Black</title></head>
    <body><h1>Haier HRF-538 IFGA Black</h1>
    <div class="product-gallery"><img src="https://cdn.haier.com/p/hrf-538-ifga-1.jpg"></div></body></html>"""
    url = "https://www.haier.com/pk/hrf-538"
    images = {"https://cdn.haier.com/p/hrf-538-ifga-1.jpg": png_bytes(clean_product_image(900, 900))}
    deps, _, _, host = build_deps(tmp_path, {url: html}, images)
    result, _ = process_product(
        task(0, "Haier Refrigerator HRF-538 IFGA Golden", (url,)), deps, config
    )
    assert result.status is Status.PARTIAL          # one image only, but published
    assert "colour mismatch" in result.note
    assert len(host.uploads) == 4                   # 1 image + banner on both platforms
    assert result.digi_urls[-1] == result.alfa_urls[-1].replace("alfa", "digi")
    assert result.digi_urls[:-1] and result.alfa_urls[:-1]


# --------------------------------------------------------------------------- #
# ordering + CSV writers
# --------------------------------------------------------------------------- #
def test_output_rows_preserve_input_order(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    results = [
        ProductResult(task_index=2, title="C", status=Status.FAILED, reason="no page matched this model"),
        ProductResult(task_index=0, title="A", status=Status.OK, digi_urls=("u1",), alfa_urls=("a1",)),
        ProductResult(task_index=1, title="B", status=Status.PARTIAL, digi_urls=("u2",), alfa_urls=("a2",)),
    ]
    write_output_csvs(results, config)
    lines = config.digimall_csv.read_text(encoding="utf-8").strip().splitlines()
    assert [line.split(",")[0] for line in lines] == ["Title", "A", "B"]
    failed = config.failed_csv.read_text(encoding="utf-8").strip().splitlines()
    assert len(failed) == 2 and failed[1].startswith("C")
    report = config.report_csv.read_text(encoding="utf-8").strip().splitlines()
    assert [line.split(",")[0] for line in report] == ["Title", "A", "B", "C"]


def test_runner_sorts_results_by_input_order(tmp_path: Path) -> None:
    config = live_config(tmp_path, workers=3)
    pages = {
        f"https://www.haier.com/pk/hsu-12hfpaa.shtml?p={index}": load_fixture("product_ok")
        for index in range(3)
    }
    deps, _, _, _ = build_deps(tmp_path, pages, gallery_images())
    tasks = [
        task(0, "Haier HSU-12HFPAA", ("https://www.haier.com/pk/hsu-12hfpaa.shtml?p=0",)),
        task(1, "Haier HSU-12HFPAA", ("https://www.haier.com/pk/hsu-12hfpaa.shtml?p=1",)),
        task(2, "Haier HSU-12HFPAA", ("https://www.haier.com/pk/hsu-12hfpaa.shtml?p=2",)),
    ]
    results = BatchRunner(config, deps, tasks, {}).run()
    assert [result.task_index for result in results] == [0, 1, 2]


# --------------------------------------------------------------------------- #
# resume
# --------------------------------------------------------------------------- #
def test_interrupt_then_resume_skips_done_retries_failed(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    good = task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,))
    bad = task(1, "Haier HSU-12HFPAA Broken", (NO_GALLERY_PAGE,))

    deps, _, _, _ = build_deps(
        tmp_path, {HAIER_PAGE: load_fixture("product_ok"), NO_GALLERY_PAGE: load_fixture("no_gallery")},
        gallery_images(),
    )
    first = BatchRunner(config, deps, [good, bad], {}).run()
    assert {result.status for result in first} == {Status.OK, Status.FAILED}
    checkpoint = load_checkpoint(config.checkpoint_path)
    assert len(checkpoint) == 2

    # second run: the good product must not be fetched again, the failed one must retry
    deps2, page_fetcher2, _, _ = build_deps(
        tmp_path, {HAIER_PAGE: load_fixture("product_ok")}, gallery_images()
    )
    second = BatchRunner(config, deps2, [good, bad], checkpoint).run()
    assert HAIER_PAGE not in page_fetcher2.requested      # done -> skipped
    assert NO_GALLERY_PAGE in page_fetcher2.requested     # failed -> retried
    by_index = {result.task_index: result for result in second}
    assert by_index[0].status is Status.OK
    assert by_index[0].note == "resumed from checkpoint"
    assert by_index[0].digi_urls                      # published URLs survive the resume
    assert by_index[1].status is Status.FAILED


def test_empty_urls_row_and_changed_url_are_retried(tmp_path: Path) -> None:
    data = tmp_path / "in.csv"
    atomic_write_csv(data, ["Title", "Images"],
                     [["", "https://x.example/p"], ["Haier HSU-12HFPAA", ""]])
    tasks = read_input(data)
    assert tasks[0].title == "" and tasks[1].source_urls == ()

    entry = CheckpointEntry(
        title="Haier HSU-12HFPAA",
        source_url_hash=hash_source_urls(("https://old.example/p",)),
        status=Status.OK,
        digi_urls=("https://img.example/a.jpg",),
        alfa_urls=("https://img.example/a.jpg",),
        reason="",
        timestamp="2026-01-01T00:00:00Z",
    )
    # same URLs -> done
    assert entry.is_resume_done(hash_source_urls(("https://old.example/p",))) is True
    # changed Source URL -> retry
    assert entry.is_resume_done(hash_source_urls(("https://new.example/p",))) is False
    # empty URL list with an ok status -> retry
    empty = CheckpointEntry("X", "", Status.OK, (), (), "", "")
    assert empty.is_resume_done(hash_source_urls(())) is False


def test_duplicate_titles_are_merged() -> None:
    tasks = [
        task(0, "Haier HSU-12HFPAA", ("https://a.example/p",)),
        task(1, "haier hsu-12hfpaa", ("https://a.example/p", "https://b.example/p")),
    ]
    merged, warnings = merge_duplicate_titles(tasks)
    assert len(merged) == 1
    assert merged[0].source_urls == ("https://a.example/p", "https://b.example/p")
    assert warnings and "duplicate title" in warnings[0]


def test_redo_forces_reprocessing(tmp_path: Path) -> None:
    config = live_config(tmp_path, redo_titles=("Haier HSU-12HFPAA Inverter AC",))
    deps, _, _, _ = build_deps(tmp_path, {HAIER_PAGE: load_fixture("product_ok")}, gallery_images())
    good = task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,))
    BatchRunner(config, deps, [good], {}).run()
    checkpoint = load_checkpoint(config.checkpoint_path)
    deps2, page_fetcher2, _, _ = build_deps(
        tmp_path, {HAIER_PAGE: load_fixture("product_ok")}, gallery_images()
    )
    BatchRunner(config, deps2, [good], checkpoint).run()
    assert HAIER_PAGE in page_fetcher2.requested


# --------------------------------------------------------------------------- #
# fatal host error
# --------------------------------------------------------------------------- #
def test_permanent_api_error_aborts_after_checkpoint_flush(tmp_path: Path) -> None:
    config = live_config(tmp_path, workers=1)
    host = FakeImageHost(permanent_after=0)                # first upload -> permanent
    deps, _, _, _ = build_deps(tmp_path, {HAIER_PAGE: load_fixture("product_ok")},
                               gallery_images(), host=host)
    with pytest.raises(PermanentAPIError):
        BatchRunner(config, deps, [task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,))], {}).run()
    # the outputs were flushed, and the failed product is retried next time
    assert config.report_csv.exists()
    assert load_checkpoint(config.checkpoint_path) == {}


def test_banner_cache_prevents_second_upload(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    deps, _, _, host = build_deps(tmp_path, {HAIER_PAGE: load_fixture("product_ok")}, gallery_images())
    first, _ = process_product(task(0, "Haier HSU-12HFPAA", (HAIER_PAGE,)), deps, config)
    uploads_after_first = len(host.uploads)
    second, _ = process_product(task(0, "Haier HSU-12HFPAA", (HAIER_PAGE,)), deps, config)
    assert first.digi_urls == second.digi_urls
    # only the product files are uploaded again; the banners come from the cache
    assert len(host.uploads) == uploads_after_first + 6


def test_safe_slug_is_filesystem_friendly() -> None:
    assert safe_slug("Haier 55 Inch 4K Smart QLED TV (55S80EUX)") == (
        "haier-55-inch-4k-smart-qled-tv-55s80eux"
    )


def test_failed_row_reasons_are_specific(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    deps, _, _, _ = build_deps(tmp_path, {}, {})
    title_only = task(0, "Premium LED TV 55 inch 2024", (HAIER_PAGE,))
    result, _ = process_product(title_only, deps, config)
    assert result.reason == "title has no model-like token"


# --------------------------------------------------------------------------- #
# fallback chain (plan.md section 11) - S15, budget, flags, output format
# --------------------------------------------------------------------------- #
def test_fallback_is_not_used_when_sheet_urls_solve_the_row(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    searcher = FakeSourceSearcher()
    deps, _, _, _ = build_deps(
        tmp_path, {HAIER_PAGE: load_fixture("product_ok")}, gallery_images()
    )
    deps.source_searcher = searcher
    result, _ = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    assert result.status is Status.OK
    assert searcher.calls == []                  # no search request was made at all
    assert "fallback" not in result.note


def test_fallback_candidate_is_verified_before_any_image_is_used(tmp_path: Path) -> None:
    """S15: a found page that shows another model yields nothing."""
    config = live_config(tmp_path)
    other_model = """<html><head><title>Haier HSU-18HFPAA Inverter AC</title></head>
    <body><div class="product-gallery">
    <img src="https://cdn.haier.com/p/hsu-18hfpaa-1.jpg"></div></body></html>"""
    searcher = FakeSourceSearcher(
        candidates=[
            PageCandidate(
                url="https://www.surmawala.pk/products/wrong-model",
                origin="json-search:www.surmawala.pk",
                method="json-search",
            )
        ],
        report=SourceSearchReport(tried=("www.surmawala.pk:shopify:1",)),
    )
    deps, _, image_fetcher, host = build_deps(
        tmp_path,
        {
            "https://www.haier.com/pk/dead.shtml": "<html><body>nothing here</body></html>",
            "https://www.surmawala.pk/products/wrong-model": other_model,
        },
        {"https://cdn.haier.com/p/hsu-18hfpaa-1.jpg": png_bytes(clean_product_image())},
    )
    deps.source_searcher = searcher
    result, _ = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", ("https://www.haier.com/pk/dead.shtml",)),
        deps,
        config,
    )
    assert result.status is Status.FAILED
    assert image_fetcher.requested == []         # not one image URL was even requested
    assert host.uploads == []
    decisions = [
        json.loads(line) for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    s15 = [line for line in decisions if line["rule_id"] == "S15"]
    assert s15 and "fallback candidate rejected" in s15[0]["detail"]


def test_fallback_recovers_a_row_and_is_labelled_in_the_output(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    found_page = """<html><head><title>Haier HSU-12HFPAA Inverter AC</title></head>
    <body><div class="product-gallery">
      <img src="https://cdn.shop.pk/p/hsu-12hfpaa-1.jpg">
      <img src="https://cdn.shop.pk/p/hsu-12hfpaa-2.jpg">
      <img src="https://cdn.shop.pk/p/hsu-12hfpaa-3.jpg">
    </div></body></html>"""
    searcher = FakeSourceSearcher(
        candidates=[
            PageCandidate(
                url="https://www.alfatah.com.pk/product/haier-hsu-12hfpaa/",
                origin="json-search:www.alfatah.com.pk",
                method="json-search",
                title_hint="Haier HSU-12HFPAA",
            )
        ],
        report=SourceSearchReport(
            tried=("www.alfatah.com.pk:woo:1",), used="json-search:www.alfatah.com.pk",
            note="fallback search: 1 candidate page(s) from json-search:www.alfatah.com.pk",
        ),
    )
    deps, _, _, host = build_deps(
        tmp_path,
        {
            "https://www.haier.com/pk/dead.shtml": "",
            "https://www.alfatah.com.pk/product/haier-hsu-12hfpaa/": found_page,
        },
        {
            "https://cdn.shop.pk/p/hsu-12hfpaa-1.jpg": png_bytes(clean_product_image(900, 900, "#2f5fa8")),
            "https://cdn.shop.pk/p/hsu-12hfpaa-2.jpg": png_bytes(second_angle_image()),
            "https://cdn.shop.pk/p/hsu-12hfpaa-3.jpg": png_bytes(third_angle_image()),
        },
    )
    deps.source_searcher = searcher
    result, images = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", ("https://www.haier.com/pk/dead.shtml",)),
        deps,
        config,
    )
    assert result.status is Status.OK
    assert len(images) == 3                       # target achieved from the found page
    assert "fallback used: yes" in result.note
    assert "alfatah" in result.note
    assert len(host.uploads) == 8                 # 3 images x 2 platforms + 2 banners
    decisions = [
        json.loads(line) for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    keeps = [line for line in decisions if line["decision"] == "keep"]
    assert keeps and all("source=fallback:json-search:www.alfatah.com.pk" in line["detail"] for line in keeps)


def test_fallback_disabled_restores_the_original_behaviour(tmp_path: Path) -> None:
    config = live_config(tmp_path, fallback_enabled=False)
    searcher = FakeSourceSearcher(
        candidates=[
            PageCandidate(
                url="https://www.alfatah.com.pk/product/x/",
                origin="json-search:www.alfatah.com.pk",
                method="json-search",
            )
        ]
    )
    deps, page_fetcher, _, _ = build_deps(
        tmp_path, {"https://www.haier.com/pk/dead.shtml": ""}, {}
    )
    deps.source_searcher = searcher
    result, _ = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", ("https://www.haier.com/pk/dead.shtml",)),
        deps,
        config,
    )
    assert result.status is Status.FAILED
    assert searcher.calls == []
    assert "alfatah" not in page_fetcher.requested


def test_sheet_url_wins_even_when_it_needs_the_second_url(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    searcher = FakeSourceSearcher(
        candidates=[
            PageCandidate(
                url="https://www.alfatah.com.pk/product/x/",
                origin="json-search:www.alfatah.com.pk",
                method="json-search",
            )
        ]
    )
    deps, page_fetcher, _, _ = build_deps(
        tmp_path,
        {"https://www.haier.com/pk/ok.shtml": load_fixture("product_ok")},
        gallery_images(),
    )
    deps.source_searcher = searcher
    result, _ = process_product(
        task(
            0,
            "Haier HSU-12HFPAA Inverter AC",
            ("https://www.haier.com/pk/dead.shtml", "https://www.haier.com/pk/ok.shtml"),
        ),
        deps,
        config,
    )
    assert result.status is Status.OK
    assert searcher.calls == []                   # the sheet had a working URL
    assert "alfatah" not in page_fetcher.requested


def test_sheet_page_with_zero_usable_images_triggers_the_fallback(tmp_path: Path) -> None:
    """Customer decision: matched pages that give no usable image are still 'unsolved'."""
    config = live_config(tmp_path)
    empty_gallery = """<html><head><title>Haier HSU-12HFPAA Inverter AC</title></head>
    <body><div class="product-gallery">
      <img src="https://cdn.haier.com/p/hsu-12hfpaa-1.jpg"></div></body></html>"""
    found_page = """<html><head><title>Haier HSU-12HFPAA Inverter AC</title></head>
    <body><div class="product-gallery">
      <img src="https://cdn.shop.pk/p/hsu-12hfpaa-9.jpg"></div></body></html>"""
    searcher = FakeSourceSearcher(
        candidates=[
            PageCandidate(
                url="https://www.alfatah.com.pk/product/haier-hsu-12hfpaa/",
                origin="json-search:www.alfatah.com.pk",
                method="json-search",
            )
        ]
    )
    deps, _, _, _ = build_deps(
        tmp_path,
        {
            HAIER_PAGE: empty_gallery,
            "https://www.alfatah.com.pk/product/haier-hsu-12hfpaa/": found_page,
        },
        {"https://cdn.shop.pk/p/hsu-12hfpaa-9.jpg": png_bytes(clean_product_image())},
    )
    deps.source_searcher = searcher
    result, images = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    assert searcher.calls != []                    # the sheet page matched but gave nothing
    assert len(images) == 1
    assert result.status is Status.PARTIAL


# --------------------------------------------------------------------------- #
# download economy: header probe and largest-size rewrite
# --------------------------------------------------------------------------- #
def test_header_probe_rejects_tiny_image_without_a_download(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    small_png_header = (
        b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
        + (60).to_bytes(4, "big") + (60).to_bytes(4, "big")
    )
    url = IMG1                                  # a gallery image of the fixture page
    deps, _, image_fetcher, _ = build_deps(
        tmp_path, {HAIER_PAGE: load_fixture("product_ok")}, {}
    )
    image_fetcher.probe_headers[url] = small_png_header
    result, images = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    assert images == []
    assert url not in image_fetcher.requested             # never fully downloaded
    assert url in image_fetcher.probes                    # ...but the probe happened
    decisions = [
        json.loads(line) for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    s8 = [line for line in decisions if line["rule_id"] == "S8"]
    assert s8 and "header probe" in s8[0]["detail"]


def test_largest_size_rewrite_is_tried_first_then_the_original(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    page = """<html><head><title>Haier HSU-12HFPAA Inverter AC</title></head>
    <body><div class="product-gallery">
      <img src="https://image.haier.com/p/hsu-12hfpaa-1_60.jpg"></div></body></html>"""
    big = "https://image.haier.com/p/hsu-12hfpaa-1_1000.jpg"
    deps, _, image_fetcher, _ = build_deps(
        tmp_path, {HAIER_PAGE: page}, {big: png_bytes(clean_product_image())}
    )
    result, images = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    assert len(images) == 1
    assert image_fetcher.requested[0] == big              # the rewrite came first
    assert images[0].source_url.endswith("_60.jpg")       # identity stays the page's URL


def test_download_budget_stops_runaway_rows(tmp_path: Path) -> None:
    config = live_config(
        tmp_path, image_full_download_max_per_page=1, image_full_download_max_per_row=1
    )
    page = """<html><head><title>Haier HSU-12HFPAA Inverter AC</title></head>
    <body><div class="product-gallery">
      <img src="https://cdn.haier.com/p/broken-1.jpg">
      <img src="https://cdn.haier.com/p/broken-2.jpg">
      <img src="https://cdn.haier.com/p/broken-3.jpg"></div></body></html>"""
    deps, _, image_fetcher, _ = build_deps(tmp_path, {HAIER_PAGE: page}, {})   # every URL 404s
    result, _ = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    assert result.status is Status.FAILED
    assert len(image_fetcher.requested) == 1              # budget enforced


def test_a_bad_page_does_not_starve_the_next_candidate(tmp_path: Path) -> None:
    """Live finding: Dawlance's dark renders spent the whole row budget before retail
    candidates were tried, so they were never even downloaded.  Each page now gets its
    own allowance and the next candidate page still gets a chance."""
    config = live_config(
        tmp_path, image_full_download_max_per_page=1, image_full_download_max_per_row=4
    )
    good_page = """<html><head><title>Haier HSU-12HFPAA Inverter AC</title></head>
    <body><div class="product-gallery">
      <img src="https://cdn.shop.pk/p/hsu-12hfpaa-9.jpg"></div></body></html>"""
    searcher = FakeSourceSearcher(
        candidates=[
            PageCandidate(
                url="https://www.alfatah.com.pk/product/haier-hsu-12hfpaa/",
                origin="json-search:www.alfatah.com.pk",
                method="json-search",
            )
        ]
    )
    deps, _, image_fetcher, _ = build_deps(
        tmp_path,
        {
            HAIER_PAGE: load_fixture("product_ok"),          # its URLs all 404 in this fake
            "https://www.alfatah.com.pk/product/haier-hsu-12hfpaa/": good_page,
        },
        {"https://cdn.shop.pk/p/hsu-12hfpaa-9.jpg": png_bytes(clean_product_image())},
    )
    deps.source_searcher = searcher
    result, images = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    assert len(images) == 1                              # the second page still delivered
    assert "https://cdn.shop.pk/p/hsu-12hfpaa-9.jpg" in image_fetcher.requested


def test_output_folder_explains_itself(tmp_path: Path) -> None:
    """`output/README.md` is generated per run, so the folder never needs tribal knowledge."""
    config = live_config(tmp_path)
    results = [
        ProductResult(
            task_index=0,
            title="Haier HSU-12HFPAA Inverter AC",
            status=Status.OK,
            brand="Haier",
            digi_urls=("https://i.example/1.jpg", "https://i.example/banner.jpg"),
            alfa_urls=("https://a.example/1.jpg", "https://a.example/banner.jpg"),
        ),
        ProductResult(task_index=1, title="Bad Row", status=Status.FAILED, reason="no page matched"),
    ]
    write_output_csvs(results, config)
    readme = (config.output_dir / "README.md").read_text(encoding="utf-8")
    assert "rows: 2 | ok: 1 | partial: 0 | failed: 1" in readme
    assert "pictures published: 1 per platform, plus the closing banner" in readme  # banner uncounted
    assert "digimall_product_images.csv" in readme
    assert "final_alfamall" in readme


# --------------------------------------------------------------------------- #
# output sheets: Title, Brand, Image_Urls (pipe separated, no spaces)
# --------------------------------------------------------------------------- #
def test_output_csv_has_title_brand_and_pipe_separated_urls(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    results = [
        ProductResult(
            task_index=0,
            title="Haier HSU-12HFPAA Inverter AC",
            status=Status.OK,
            brand="Haier",
            digi_urls=("https://i.example/1.jpg", "https://i.example/2.jpg"),
            alfa_urls=("https://a.example/1.jpg", "https://a.example/2.jpg"),
        )
    ]
    write_output_csvs(results, config)
    lines = config.digimall_csv.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "Title,Brand,Image_Urls"
    assert lines[1] == (
        "Haier HSU-12HFPAA Inverter AC,Haier,https://i.example/1.jpg|https://i.example/2.jpg"
    )
    assert " " not in lines[1].split(",")[2]
    alfa = config.alfamall_csv.read_text(encoding="utf-8").strip().splitlines()
    assert alfa[0] == "Title,Brand,Image_Urls"


def test_brand_column_is_filled_from_the_sheet_when_the_url_is_unknown(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    deps, _, _, _ = build_deps(
        tmp_path, {HAIER_PAGE: load_fixture("product_ok")}, gallery_images()
    )
    result, _ = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    assert result.brand == "Haier"


# --------------------------------------------------------------------------- #
# Live bug (2026-10-06): a --dry-run entry poisoned the real run's banner URL
# --------------------------------------------------------------------------- #
def test_banner_cache_is_scoped_to_the_run_mode(tmp_path: Path) -> None:
    """A dry run caches `file://` banner paths; a later real run reused them and the import
    CSV ended up with local paths instead of hosted URLs.  The cache key now carries the
    run mode, so the two runs can share `state/banner_urls.json` without mixing."""
    config = live_config(tmp_path)
    deps, _, _, _ = build_deps(tmp_path, pages={}, images={})
    cache_shared = deps.banner_cache
    assert cache_shared == {}

    dry_deps = replace(deps, image_host=DryRunHost(config.output_dir))
    dry_url = ensure_banner_url(replace(config, dry_run=True), dry_deps, "digimall")
    assert dry_url.startswith("file://")
    assert len(cache_shared) == 1

    live_url = ensure_banner_url(config, deps, "digimall")       # same cache, live mode
    assert live_url.startswith("https://"), "a dry-run banner URL leaked into a live run"
    assert live_url != dry_url
    assert len(cache_shared) == 2                               # both modes cached separately

    assert ensure_banner_url(config, deps, "digimall") == live_url   # live entry is reused


def test_a_live_run_refuses_to_publish_a_local_path(tmp_path: Path) -> None:
    """Defence in depth: if any stage ever yields a local path in a live run, the row fails
    loudly instead of shipping something the shop cannot download."""
    class LocalPathHost(FakeImageHost):
        """A misbehaving host that hands back local paths instead of hosted URLs."""

        def upload(self, data: bytes, filename: str) -> str:
            return f"file:///tmp/{filename}"

    config = live_config(tmp_path)
    deps, _, _, _ = build_deps(tmp_path, pages={}, images={}, host=LocalPathHost())
    image_file = tmp_path / "out" / "images" / "x" / "01.jpg"
    image_file.parent.mkdir(parents=True, exist_ok=True)
    image_file.write_bytes(png_bytes(clean_product_image(900, 900)))
    accepted = [
        AcceptedImage(
            source_url=IMG1,
            dedup_key="k",
            raw_path=image_file,
            square_path=image_file,
            digi_path=image_file,
            alfa_path=image_file,
        )
    ]
    result = ProductResult(task_index=0, title="Haier HSU-12HFPAA Inverter AC", status=Status.OK)
    final = finalize_and_upload(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), result, accepted, deps, config
    )
    assert final.status is Status.FAILED
    assert final.digi_urls == () and final.alfa_urls == ()
    assert "non-http" in final.reason


# --------------------------------------------------------------------------- #
# Live bug (2026-10-06): the Brand column came out empty for every resumed row
# --------------------------------------------------------------------------- #
def test_resume_keeps_the_brand_column(tmp_path: Path) -> None:
    """The user's question: why is the Brand column empty?  Because the checkpoint did not
    carry the brand, so every row restored from it wrote an empty brand.  Only the row that
    was processed in that run (a --redo) had one."""
    config = live_config(tmp_path)
    tasks = [task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,))]

    deps, pages, images, _ = build_deps(
        tmp_path, {HAIER_PAGE: load_fixture("product_ok")}, gallery_images()
    )
    results = BatchRunner(config, deps, tasks, {}).run()
    assert results[0].brand == "Haier"
    write_output_csvs(results, config)
    first_line = config.digimall_csv.read_text(encoding="utf-8").splitlines()[1]
    assert first_line.split(",")[1] == "Haier"

    # a second run against the same checkpoint: nothing is reprocessed
    checkpoint = load_checkpoint(config.checkpoint_path)
    assert checkpoint  # the first run wrote it
    resumed = BatchRunner(config, deps, tasks, checkpoint).run()
    assert resumed[0].note == "resumed from checkpoint"
    assert resumed[0].brand == "Haier", "the brand was lost on resume"
    write_output_csvs(resumed, config)
    again = config.digimall_csv.read_text(encoding="utf-8").splitlines()[1]
    assert again.split(",")[1] == "Haier", f"Brand column empty after resume: {again}"


def test_an_old_checkpoint_without_a_brand_still_loads(tmp_path: Path) -> None:
    """Checkpoints written before the brand field existed must not break resume."""
    config = live_config(tmp_path)
    entry = CheckpointEntry.from_dict(
        {
            "title": "Haier HSU-12HFPAA Inverter AC",
            "source_url_hash": "abc",
            "status": "ok",
            "digi_urls": ["https://i.example/1.jpg"],
            "alfa_urls": ["https://a.example/1.jpg"],
            "reason": "",
            "timestamp": "2026-10-06T00:00:00+00:00",
        }
    )
    assert entry.brand == ""
    tasks = [task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,))]
    deps, _, _, _ = build_deps(tmp_path, {}, {})
    results = BatchRunner(config, deps, tasks, {"haier hsu-12hfpaa inverter ac": entry}).run()
    assert results[0].brand == "Haier"          # recovered from the title


def test_one_download_slot_per_asset_not_per_attempt(tmp_path: Path) -> None:
    """Live bug (Dawlance DW-7500C): the URL-rewrite attempts (same photo, bigger size)
    each consumed a download slot, so the 4th official gallery image was never downloaded.
    A slot is now per asset; the retries inside it are free."""
    config = live_config(tmp_path, image_full_download_max_per_page=1, image_full_download_max_per_row=1)
    # only the URL exactly as published is served; the upscaled rewrite is not
    pages = {HAIER_PAGE: load_fixture("product_ok")}
    images_by_url = {IMG1: png_bytes(clean_product_image(900, 900))}
    deps, _, fetcher, _ = build_deps(tmp_path, pages, images_by_url)
    result, images = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    assert images, "the asset was never downloaded"
    assert len(fetcher.requested) >= 1
    assert result.images_accepted == 1


def test_official_page_gallery_is_accepted_when_the_product_fills_the_frame(tmp_path: Path) -> None:
    """End to end: an official brand page whose gallery shot fills the frame yields an image
    (strict S7 would have dropped it), while the same shot on a retail page does not."""
    config = live_config(tmp_path)
    fill = (50, 52, 56)
    frame_filling = Image.new("RGB", (900, 900), (255, 255, 255))
    ImageDraw.Draw(frame_filling).rectangle((200, 0, 700, 900), fill=fill)
    page = """<html><head><title>Haier HSU-12HFPAA Inverter AC</title></head>
    <body><div class="product-gallery"><img src="https://cdn.haier.com/p/hsu-12hfpaa-1.jpg"></div></body></html>"""

    official, _, _, _ = build_deps(tmp_path, {HAIER_PAGE: page}, {IMG1: png_bytes(frame_filling)})
    kept, images = process_product(task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), official, config)
    assert images, "official gallery shot was dropped even though the brand owns the page"
    assert kept.images_accepted == 1

    retail = "https://shop.example/p/hsu-12hfpaa"
    retail_deps, _, _, _ = build_deps(tmp_path, {retail: page.replace("cdn.haier.com", "shop.example")},
                                      {IMG1.replace("cdn.haier.com", "shop.example"): png_bytes(frame_filling)})
    _, retail_images = process_product(task(0, "Haier HSU-12HFPAA Inverter AC", (retail,)), retail_deps, config)
    assert not retail_images, "a retail page must not get the official-gallery acceptance"

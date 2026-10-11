"""Pipeline behaviour with fakes only: S14, ordering, resume and abort semantics."""

from __future__ import annotations

import io
import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import cast

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
    ImageCandidate,
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
    _dump_rejected,
    _layout_rescue_verdict,
    _page_download_allowance,
    ensure_banner_url,
    finalize_and_upload,
    process_product,
    write_output_csvs,
)
from sources import SourceChain

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


def test_a_different_colour_fails_the_row_instead_of_publishing_it(tmp_path: Path) -> None:
    """Customer decision (2026-10-07): the sheet's URL shows the BLACK variant of a
    GOLDEN row, so the page is rejected and nothing is published - not even with a
    flag, because a wrong colour costs the company a penalty.  (Supersedes the
    2026-10-05 "flag it and publish" rule.)"""
    config = live_config(tmp_path)
    html = """<html><head><title>Haier HRF-538 IFGA Refrigerator Black</title></head>
    <body><h1>Haier HRF-538 IFGA Black</h1>
    <div class="product-gallery"><img src="https://cdn.haier.com/p/hrf-538-ifga-1.jpg"></div></body></html>"""
    url = "https://www.haier.com/pk/hrf-538"
    images = {"https://cdn.haier.com/p/hrf-538-ifga-1.jpg": png_bytes(clean_product_image(900, 900))}
    deps, _, _, host = build_deps(tmp_path, {url: html}, images)
    result, accepted = process_product(
        task(0, "Haier Refrigerator HRF-538 IFGA Golden", (url,)), deps, config
    )
    assert result.status is Status.FAILED
    assert accepted == []
    assert host.uploads == []          # not one pixel of the black variant went out
    decisions = [
        json.loads(line) for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    s1 = [line for line in decisions if line["rule_id"] == "S1"]
    assert s1 and "another colourway" in s1[0]["detail"]


def test_switching_variant_separation_off_publishes_the_page_again(tmp_path: Path) -> None:
    """The single rollback field: with it off, colour is not looked at any more."""
    config = replace(live_config(tmp_path), variant_separation_enabled=False)
    html = """<html><head><title>Haier HRF-538 IFGA Refrigerator Black</title></head>
    <body><h1>Haier HRF-538 IFGA Black</h1>
    <div class="product-gallery"><img src="https://cdn.haier.com/p/hrf-538-ifga-1.jpg"></div></body></html>"""
    url = "https://www.haier.com/pk/hrf-538"
    images = {"https://cdn.haier.com/p/hrf-538-ifga-1.jpg": png_bytes(clean_product_image(900, 900))}
    deps, _, _, host = build_deps(tmp_path, {url: html}, images)
    result, accepted = process_product(
        task(0, "Haier Refrigerator HRF-538 IFGA Golden", (url,)), deps, config
    )
    assert accepted                      # the photo is published as before
    assert result.digi_urls and result.alfa_urls
    assert host.uploads


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


def test_header_probe_asks_for_the_largest_rewrite_not_the_tiny_variant(tmp_path: Path) -> None:
    """S8 must judge the asset, not the smallest variant the page happens to list.

    A Shopify page lists ``_288x.png``; the same asset exists as ``_1024x1024.png``.  Before
    the fix the probe hit the 288px file and rejected the product at S8 without ever asking
    for the large form.  Now the probe targets the largest rewrite, so the product passes and
    the large image is downloaded.
    """
    config = live_config(tmp_path)
    small = "https://cdn.shop.example/p/hsu-12hfpaa-1_288x.png"
    big = "https://cdn.shop.example/p/hsu-12hfpaa-1_1024x1024.png"
    page = f"""<html><head><title>Haier HSU-12HFPAA Inverter AC</title></head>
    <body><div class="product-gallery"><img src="{small}"></div></body></html>"""

    def header(side: int) -> bytes:
        return (
            b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
            + side.to_bytes(4, "big") + side.to_bytes(4, "big")
        )

    deps, _, image_fetcher, _ = build_deps(tmp_path, {HAIER_PAGE: page}, {big: png_bytes(clean_product_image())})
    image_fetcher.probe_headers[small] = header(288)
    image_fetcher.probe_headers[big] = header(1024)
    result, images = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    assert len(images) == 1
    assert big in image_fetcher.probes          # the probe went for the large form
    assert small not in image_fetcher.probes    # ...and never judged the thumbnail
    assert big in image_fetcher.requested


def test_largest_size_rewrite_is_tried_first_then_the_original(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    page = """<html><head><title>Haier HSU-12HFPAA Inverter AC</title></head>
    <body><div class="product-gallery">
      <img src="https://image.haier.com/p/hsu-12hfpaa-1_60.jpg"></div></body></html>"""
    big = "https://image.haier.com/p/hsu-12hfpaa-1.jpg"   # stripped original = full res
    deps, _, image_fetcher, _ = build_deps(
        tmp_path, {HAIER_PAGE: page}, {big: png_bytes(clean_product_image())}
    )
    result, images = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    assert len(images) == 1
    assert image_fetcher.requested[0] == big              # the crisp rung came first
    assert images[0].source_url.endswith("_60.jpg")       # identity stays the page's URL


def test_download_budget_stops_runaway_rows(tmp_path: Path) -> None:
    config = live_config(
        tmp_path, image_full_download_max_per_page=1, image_full_download_max_per_row=1
    )
    # The URLs name the row's own model so the wrong-product limbs stay silent and
    # the row reaches the download stage, where every one of them 404s.
    page = """<html><head><title>Haier HSU-12HFPAA Inverter AC</title></head>
    <body><div class="product-gallery">
      <img src="https://cdn.haier.com/p/hsu-12hfpaa-1.jpg">
      <img src="https://cdn.haier.com/p/hsu-12hfpaa-2.jpg">
      <img src="https://cdn.haier.com/p/hsu-12hfpaa-3.jpg"></div></body></html>"""
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
    # With the universal studio rescue (default) a clean 4-corner white shot is accepted on
    # retail too - that is the 2026-10-07 policy.  The *official-gallery* acceptance itself
    # stays host-scoped: with the universal rescue switched off, retail gets nothing.
    _, retail_images = process_product(task(0, "Haier HSU-12HFPAA Inverter AC", (retail,)), retail_deps, config)
    assert retail_images, "clean white studio shot should now be accepted on retail"

    strict = replace(config, studio_background_rescue=False)
    retail_deps2, _, _, _ = build_deps(tmp_path, {retail: page.replace("cdn.haier.com", "shop.example")},
                                       {IMG1.replace("cdn.haier.com", "shop.example"): png_bytes(frame_filling)})
    _, strict_images = process_product(task(0, "Haier HSU-12HFPAA Inverter AC", (retail,)), retail_deps2, strict)
    assert not strict_images, "a retail page must not get the official-gallery acceptance"


# --------------------------------------------------------------------------- #
# official gallery vs the per-page download allowance
# --------------------------------------------------------------------------- #
def _gallery_image(index: int, color: tuple[int, int, int] | None = None) -> Image.Image:
    """A distinct white-background catalog photo (position and shading vary by index)."""
    image = Image.new("RGB", (900, 900), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    left = 100 + (index % 4) * 110
    top = 80 + (index // 4) * 120
    w = 320 + (index % 3) * 30
    h = 380 + (index % 4) * 25
    fill = color or (190 - index * 8, 190 - index * 8, 192 - index * 8)
    draw.rounded_rectangle(
        (left, top, left + w, top + h),
        radius=20,
        fill=fill,
    )
    draw.ellipse((left + 30, top + 30, left + 80 + index * 5, top + 80 + index * 5), fill=(80, 80, 80))
    return image


def _gallery_page(host: str, count: int, model: str = "hsu-12hfpaa") -> str:
    images = "\n".join(
        f'<img src="https://cdn.{host}/p/{model}-{index}.jpg">' for index in range(1, count + 1)
    )
    return (
        f'<html><head><title>Haier HSU-12HFPAA Inverter AC</title></head><body>'
        f'<div class="product-gallery">{images}</div></body></html>'
    )


def test_page_download_allowance_reads_a_curated_gallery_in_full(tmp_path: Path) -> None:
    """The rule itself: a *curated gallery* page (brand or retailer) may be read in full,
    bounded by what a row may publish and by the row's own download budget; a scraped page
    whose images are only a whole-page fallback keeps the per-page cap."""
    config = live_config(
        tmp_path, image_full_download_max_per_page=6, image_full_download_max_per_row=18
    )
    curated = [
        ImageCandidate(url=f"https://cdn.haier.com/p/hsu-12hfpaa-{i}.jpg", page_url=HAIER_PAGE,
                       source="gallery_container", order=i, matches_model=True)
        for i in range(10)
    ]
    scraped = [
        ImageCandidate(url="https://cdn.haier.com/p/hsu-12hfpaa-spec.jpg", page_url=HAIER_PAGE,
                       source="whole_page", order=0, matches_model=True)
    ]
    audit = [
        ImageCandidate(url="https://cdn.haier.com/p/hsu-12hfpaa-spec.jpg", page_url=HAIER_PAGE,
                       source="dom_audit", order=0, matches_model=True)
    ]
    assert _page_download_allowance(curated, config) == 10          # brand's curated gallery
    assert _page_download_allowance(scraped, config) == 6           # scraped page: strict cap
    assert _page_download_allowance(audit, config) == 6             # S2/S3 audit nodes: no budget
    capped = live_config(
        tmp_path, image_full_download_max_per_page=6, max_images_per_product=4
    )
    assert _page_download_allowance(curated, capped) == 6           # never more than we publish
    tight = live_config(
        tmp_path, image_full_download_max_per_page=1, image_full_download_max_per_row=2
    )
    assert _page_download_allowance(curated, tight) == 2            # never more than the row


def test_official_gallery_is_read_in_full_not_truncated_by_the_page_allowance(
    tmp_path: Path,
) -> None:
    """Live bug: the DW 7500 TWIN TUB CLEAR LID (WHITE) page publishes 10 photos, but the
    6-download page allowance meant four of the manufacturer's own pictures were never even
    looked at.  An official curated gallery is no longer truncated."""
    # default target is 3 (owner speed decision 2026-10-08); this test pins a higher
    # target so it keeps measuring the *allowance*, not the target stop
    config = live_config(tmp_path, target_images_per_product=8)
    page = _gallery_page("haier.com", 8)
    images = {f"https://cdn.haier.com/p/hsu-12hfpaa-{i}.jpg": png_bytes(_gallery_image(i))
              for i in range(1, 9)}
    deps, _, image_fetcher, _ = build_deps(tmp_path, {HAIER_PAGE: page}, images)
    result, accepted = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    assert len(image_fetcher.requested) == 8, "the whole official gallery must be read"
    assert result.images_accepted == 8
    assert len(accepted) == 8
    decisions = [
        json.loads(line) for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    assert not [
        line for line in decisions
        if line["rule_id"] == "S13.fetch" and "allowance" in line["detail"]
    ], "an official gallery must not hit the page allowance"


def test_retail_curated_gallery_is_also_read_in_full(tmp_path: Path) -> None:
    """Owner request 2026-10-06: when the product is found on a retailer (priority 2),
    download its own gallery - not just the first six pictures."""
    config = live_config(tmp_path, target_images_per_product=10)
    retail = "https://www.surmawala.pk/products/dawlance-semi-automatic-dw-7500"
    page = _gallery_page("surmawala.pk", 10)
    images = {f"https://cdn.surmawala.pk/p/hsu-12hfpaa-{i}.jpg": png_bytes(_gallery_image(i))
              for i in range(1, 11)}
    deps, _, image_fetcher, _ = build_deps(tmp_path, {retail: page}, images)
    result, accepted = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (retail,)), deps, config
    )
    assert len(image_fetcher.requested) == 10
    assert result.images_accepted == 10
    assert len(accepted) == 10


def test_scraped_page_still_stops_at_the_per_page_allowance(tmp_path: Path) -> None:
    """The live Dawlance protection stays: a page whose images are only a model-bearing
    whole-page fallback (marketing renders) must not eat the row's allowance and starve
    the next candidate page."""
    # target 8 > allowance 6: the row is still hungry when the page allowance runs
    # out, which is exactly what produces the S13.fetch "allowance spent" record
    config = live_config(tmp_path, target_images_per_product=8)
    retail = "https://shop.example/p/hsu-12hfpaa"
    # no gallery container: every image URL carries the model, so source 5 serves them,
    # which is the whole-page fallback path (source = "whole_page").
    images_html = "\n".join(
        f'<img src="https://cdn.shop.example/p/hsu-12hfpaa-{i}.jpg">' for i in range(1, 9)
    )
    page = (
        "<html><head><title>Haier HSU-12HFPAA Inverter AC</title></head><body>"
        f"<div>{images_html}</div></body></html>"
    )
    images = {f"https://cdn.shop.example/p/hsu-12hfpaa-{i}.jpg": png_bytes(_gallery_image(i))
              for i in range(1, 9)}
    deps, _, image_fetcher, _ = build_deps(tmp_path, {retail: page}, images)
    result, _ = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (retail,)), deps, config
    )
    assert len(image_fetcher.requested) == 6             # the per-page cap still applies
    assert result.images_accepted == 6
    decisions = [
        json.loads(line) for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    assert any(
        line["rule_id"] == "S13.fetch" and "allowance" in line["detail"] for line in decisions
    )


def test_max_images_per_product_is_the_publication_ceiling(tmp_path: Path) -> None:
    """Owner request 2026-10-06: up to 10 of the product's own gallery photos may be
    published (the target of 3 still stops the search for *more pages*)."""
    config = live_config(tmp_path, max_images_per_product=10, target_images_per_product=10)
    page = _gallery_page("haier.com", 12)
    images = {f"https://cdn.haier.com/p/hsu-12hfpaa-{i}.jpg": png_bytes(_gallery_image(i))
              for i in range(1, 13)}
    deps, _, image_fetcher, _ = build_deps(tmp_path, {HAIER_PAGE: page}, images)
    result, accepted = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    assert result.images_accepted == 10
    assert len(accepted) == 10
    assert len(image_fetcher.requested) == 10            # stops at the ceiling, not the gallery


# --------------------------------------------------------------------------- #
# the priority chain: brand (1) -> retailer (2), with the real SourceChain
# --------------------------------------------------------------------------- #
class FakeJsonHttp:
    """Stub for the chain's HTTP boundary (``JsonHttp`` protocol)."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def get_text(self, url: str) -> str | None:
        self.requested.append(url)
        return self.pages.get(url)


def test_model_conflict_on_the_brand_page_moves_to_the_priority_2_retailer(
    tmp_path: Path,
) -> None:
    """Live scenario (2026-10-06): the sheet asks for the WHITE twin tub, the sheet's own
    URL serves the DW-7500**C** page (different model -> S1 rejects), and the row must then
    walk the priority chain: brand sources (1) first, then the retailers (2) in order -
    Surmawala being the first Shopify shop - and publish that shop's whole gallery."""
    # fallback_max_pages_per_row=6, per_source=3; target pinned to 10 so this keeps
    # measuring the retailer-fallback chain, not the new 3-image speed stop
    config = live_config(tmp_path, target_images_per_product=10)
    sheet_title = "Dawlance DW-7500 WHITE Twin Tub Washing Machine"

    # the sheet's own URL: the DW-7500C page -> the sheet's key (dw7500) must NOT match it
    official = "https://www.dawlance.com.pk/twin-tub-washing-machine/dw-7500c-semi-automatic-washing-machines"
    other_variant = (
        "<html><head><title>DW-7500C TWIN TUB (BLACK/GREY) | Dawlance</title></head>"
        "<body><h1>DW-7500C TWIN TUB</h1><div class='product-gallery'>"
        "<img src='https://www.dawlance.com.pk/media/resize/F01000000030_LO1.png/1000Wx1000H/image.webp'>"
        "</div></body></html>"
    )
    # the brand's sitemap: the whole DW-7500 family, in sitemap order
    sitemap = (
        "<urlset>"
        "<url><loc>https://www.dawlance.com.pk/twin-tub-washing-machine/"
        "dw-7500-twin-tub-glass-lid-black-grey-semi-automatic-washing-machines</loc></url>"
        "<url><loc>https://www.dawlance.com.pk/product/dw-7500-c/</loc></url>"
        "<url><loc>https://www.dawlance.com.pk/twin-tub-washing-machine/"
        "dw-7500-twin-tub-clear-lid-white-semi-automatic-washing-machines</loc></url>"
        "</urlset>"
    )
    # what Surmawala really answers for q=DW-7500 (fetched live): the white one first
    surmawala_json = json.dumps(
        {"resources": {"results": {"products": [
            {"url": "/products/dawlance-washing-machine-dw-6550g-white-price-in-pakistan",
             "title": "Dawlance Semi Automatic Washing Machine DW-7500 - 10Kg"},
            {"url": "/products/dawlance-semi-automatic-washing-machine-dw-7500c-10kg",
             "title": "Dawlance Semi Automatic Washing Machine DW-7500C - 10Kg"},
        ]}}}
    )
    http = FakeJsonHttp(
        {
            "https://www.dawlance.com.pk/robots.txt": "User-agent: *\nDisallow:\n",
            "https://www.dawlance.com.pk/sitemap.xml": sitemap,
            "https://www.surmawala.pk/robots.txt": "User-agent: *\nDisallow:\n",
            "https://www.surmawala.pk/search/suggest.json?q=DW-7500"
            "&resources[type]=product&resources[limit]=6": surmawala_json,
        }
    )
    surmawala_page = (
        "https://www.surmawala.pk/products/dawlance-washing-machine-dw-6550g-white-price-in-pakistan"
    )
    retail_html = (
        "<html><head><title>Dawlance Semi Automatic Washing Machine DW-7500 - 10Kg"
        "</title></head><body><h1>Dawlance Semi Automatic Washing Machine DW-7500 - 10Kg</h1>"
        "<div class='product-gallery'>"
        + "".join(
            f'<img src="https://cdn.surmawala.pk/p/dw7500-{i}.jpg">' for i in range(1, 11)
        )
        + "</div></body></html>"
    )
    pages = {
        official: other_variant,
        "https://www.dawlance.com.pk/twin-tub-washing-machine/"
        "dw-7500-twin-tub-glass-lid-black-grey-semi-automatic-washing-machines": other_variant,
        "https://www.dawlance.com.pk/product/dw-7500-c/": other_variant,
        "https://www.dawlance.com.pk/twin-tub-washing-machine/"
        "dw-7500-twin-tub-clear-lid-white-semi-automatic-washing-machines": other_variant,
        surmawala_page: retail_html,
    }
    images = {
        f"https://cdn.surmawala.pk/p/dw7500-{i}.jpg": png_bytes(_gallery_image(i))
        for i in range(1, 11)
    }
    deps, _, image_fetcher, _ = build_deps(tmp_path, pages, images)
    deps.source_searcher = SourceChain(config, http)

    result, accepted = process_product(
        task(0, sheet_title, (official,)), deps, config
    )
    # the retail tier was reached at all (the bug fixed here: it never was)
    assert any("surmawala" in url for url in http.requested)
    # and the shop's own gallery was published in full
    assert result.status is Status.OK
    assert result.images_accepted == 10
    assert len(accepted) == 10
    shop_downloads = [url for url in image_fetcher.requested if "surmawala" in url]
    assert len(shop_downloads) == 10, "every photo of the retailer's gallery is read"
    assert len(image_fetcher.requested) <= config.image_full_download_max_per_row
    assert "fallback used: yes" in result.note
    decisions = [
        json.loads(line) for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    # the DW-7500C pages were rejected for what they are: another model
    assert [line for line in decisions if line["rule_id"] == "S1"]


# --------------------------------------------------------------------------- #
# structured product JSON: 3+ images from a shop that only shows one on the page
# --------------------------------------------------------------------------- #
SHOPIFY_PAGE = "https://www.surmawala.pk/products/dawlance-semi-automatic-dw-7500c-10kg"
SHOPIFY_TITLE = "Dawlance DW-7500C Semi Automatic Washing Machine 10Kg"


def test_shopify_product_json_tops_up_a_one_image_page(tmp_path: Path) -> None:
    """Live complaint 2026-10-06: "shopify json gives only 1 image but we need at least 3".
    The HTML gallery of a Shopify product page can hold a single picture; the product's own
    JSON carries the whole set, and it is fetched when the gallery comes up short."""
    config = live_config(tmp_path, target_images_per_product=4)
    page = (
        "<html><head><title>Dawlance Semi Automatic Washing Machine DW-7500C - 10Kg</title>"
        "<script>var t='cdn.shopify.com'</script></head><body>"
        "<div class='product-gallery'>"
        "<img src='https://cdn.shopify.com/s/files/1/0829/files/7500-c.png?v=1'>"
        "</div></body></html>"
    )
    product_json = json.dumps({"product": {"images": [
        {"src": "https://cdn.shopify.com/s/files/1/0829/files/7500-c.png?v=1"},
        {"src": "https://cdn.shopify.com/s/files/1/0829/files/7500-c-side.png?v=2"},
        {"src": "https://cdn.shopify.com/s/files/1/0829/files/7500-c-top.png?v=3"},
        {"src": "https://cdn.shopify.com/s/files/1/0829/files/7500-c-lid.png?v=4"},
    ]}})
    # The pipeline canonicalises Shopify's ?v= cache-buster away, so the shop's file URL is
    # what gets requested - and served.
    files = "https://cdn.shopify.com/s/files/1/0829/files"
    images = {
        f"{files}/7500-c.png": png_bytes(_gallery_image(1)),
        f"{files}/7500-c-side.png": png_bytes(_gallery_image(2)),
        f"{files}/7500-c-top.png": png_bytes(_gallery_image(3)),
        f"{files}/7500-c-lid.png": png_bytes(_gallery_image(4)),
    }
    deps, pages, _, _ = build_deps(
        tmp_path, {SHOPIFY_PAGE: page, f"{SHOPIFY_PAGE}.json": product_json}, images
    )
    result, accepted = process_product(
        task(0, SHOPIFY_TITLE, (SHOPIFY_PAGE,)), deps, config
    )
    assert f"{SHOPIFY_PAGE}.json" in pages.requested, "the product JSON must be fetched"
    assert result.images_accepted == 4, "all four shop pictures, not just the visible one"
    assert len(accepted) == 4
    assert result.status is Status.OK


def test_woo_store_api_tops_up_a_one_image_page(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    page = (
        "<html><head><title>PEL PAWM-1100 Top Load Fully Automatic Washing Machine</title>"
        "<link rel='stylesheet' href='/wp-content/plugins/woocommerce/style.css'></head><body>"
        "<div class='woocommerce-product-gallery'>"
        "<img src='https://www.alfatah.com.pk/wp-content/uploads/pawm-1100-1.jpg'></div>"
        "</body></html>"
    )
    api_url = (
        "https://www.alfatah.com.pk/wp-json/wc/store/v1/products"
        "?slug=pel-11kg-top-load-automatic-washing-machine-pawm-1100i"
    )
    payload = json.dumps([{"id": 91, "images": [
        {"src": "https://www.alfatah.com.pk/wp-content/uploads/pawm-1100-1.jpg"},
        {"src": "https://www.alfatah.com.pk/wp-content/uploads/pawm-1100-2.jpg"},
        {"src": "https://www.alfatah.com.pk/wp-content/uploads/pawm-1100-3.jpg"},
    ]}])
    product_url = (
        "https://www.alfatah.com.pk/product/pel-11kg-top-load-automatic-washing-machine-pawm-1100i/"
    )
    images = {
        "https://www.alfatah.com.pk/wp-content/uploads/pawm-1100-1.jpg": png_bytes(_gallery_image(1)),
        "https://www.alfatah.com.pk/wp-content/uploads/pawm-1100-2.jpg": png_bytes(_gallery_image(2)),
        "https://www.alfatah.com.pk/wp-content/uploads/pawm-1100-3.jpg": png_bytes(_gallery_image(3)),
    }
    deps, pages, _, _ = build_deps(
        tmp_path, {product_url: page, api_url: payload}, images
    )
    result, accepted = process_product(
        task(0, "PEL PAWM-1100 Top Load Washing Machine", (product_url,)), deps, config
    )
    assert api_url in pages.requested
    assert result.images_accepted == 3
    assert len(accepted) == 3


# --------------------------------------------------------------------------- #
# relaxed fallback: brand + number publishes with a flag (owner decision)
# --------------------------------------------------------------------------- #
FALLBACK_PAGE = "https://www.surmawala.pk/products/dawlance-twin-tub-2500-special"
RELAXED_TITLE = "Dawlance DW 2500 C Twin Tub"


def _thin_fallback_page() -> str:
    return (
        "<html><head><title>Dawlance Twin Tub Washing Machine 2500 - Special Edition"
        "</title></head><body><h1>Dawlance Twin Tub Washing Machine 2500</h1>"
        "<div class='product-gallery'>"
        + "".join(
            f'<img src="https://cdn.surmawala.pk/p/dw2500-{i}.jpg">' for i in range(1, 4)
        )
        + "</div></body></html>"
    )


def test_relaxed_fallback_publishes_the_pictures_and_flags_the_row(tmp_path: Path) -> None:
    """Owner decision 2026-10-06: when nothing proves the exact model but the fallback page
    is this brand's product with this number, publish the pictures and flag the row for
    manual review ("I just need images")."""
    config = live_config(tmp_path)
    dead_sheet_url = "https://www.dawlance.com.pk/twin-tub-washing-machine/dw-2500c"
    unmatched_sheet_page = (
        "<html><head><title>DW-9999 TWIN TUB | Dawlance</title></head><body>"
        "<h1>DW-9999 TWIN TUB</h1></body></html>"
    )
    images = {
        f"https://cdn.surmawala.pk/p/dw2500-{i}.jpg": png_bytes(_gallery_image(i))
        for i in range(1, 4)
    }
    deps, _, _, _ = build_deps(
        tmp_path,
        {dead_sheet_url: unmatched_sheet_page, FALLBACK_PAGE: _thin_fallback_page()},
        images,
    )
    deps.source_searcher = FakeSourceSearcher(
        candidates=[
            PageCandidate(url=FALLBACK_PAGE, origin="json-search:www.surmawala.pk", method="json-search")
        ]
    )
    result, accepted = process_product(
        task(0, RELAXED_TITLE, (dead_sheet_url,)), deps, config
    )
    assert result.images_accepted == 3
    assert len(accepted) == 3
    assert result.status is Status.PARTIAL, "a flagged row is never silently ok"
    assert "flagged: relaxed fallback match" in result.note
    decisions = [
        json.loads(line) for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    assert any(line["rule_id"] == "S15.relaxed" for line in decisions)

    # the review queue is written for the human pass
    write_output_csvs([result], config, include_platform_csvs=False)
    review = (config.review_csv).read_text(encoding="utf-8")
    assert RELAXED_TITLE in review and "flagged: relaxed fallback match" in review


def test_the_sheets_own_url_is_never_relaxed(tmp_path: Path) -> None:
    """The relaxation exists for fallback pages only: a sheet URL must still prove the model
    (the sheet's URLs are chosen by hand - a mismatch there is a wrong row, not a rescue)."""
    config = live_config(tmp_path)
    images = {
        f"https://cdn.surmawala.pk/p/dw2500-{i}.jpg": png_bytes(_gallery_image(i))
        for i in range(1, 4)
    }
    deps, _, _, _ = build_deps(tmp_path, {FALLBACK_PAGE: _thin_fallback_page()}, images)
    result, accepted = process_product(
        task(0, RELAXED_TITLE, (FALLBACK_PAGE,)), deps, config
    )
    assert accepted == []
    assert result.status is Status.FAILED
    assert "flagged" not in result.note


def test_relaxed_matching_can_be_switched_off(tmp_path: Path) -> None:
    config = live_config(tmp_path, relaxed_fallback_matching=False)
    dead_sheet_url = "https://www.dawlance.com.pk/twin-tub-washing-machine/dw-2500c"
    unmatched_sheet_page = (
        "<html><head><title>DW-9999 TWIN TUB | Dawlance</title></head><body>"
        "<h1>DW-9999 TWIN TUB</h1></body></html>"
    )
    images = {
        f"https://cdn.surmawala.pk/p/dw2500-{i}.jpg": png_bytes(_gallery_image(i))
        for i in range(1, 4)
    }
    deps, _, _, _ = build_deps(
        tmp_path,
        {dead_sheet_url: unmatched_sheet_page, FALLBACK_PAGE: _thin_fallback_page()},
        images,
    )
    deps.source_searcher = FakeSourceSearcher(
        candidates=[PageCandidate(url=FALLBACK_PAGE, origin="json-search:www.surmawala.pk", method="json-search")]
    )
    result, accepted = process_product(
        task(0, RELAXED_TITLE, (dead_sheet_url,)), deps, config
    )
    assert accepted == []
    assert result.status is Status.FAILED


# --------------------------------------------------------------------------- #
# the wrong sheet link: the model in the title wins (live DW-7500C row, 2026-10-06)
# --------------------------------------------------------------------------- #
WRONG_LINK_TITLE = "Dawlance Semi Automatic Washing Machine 10kg (DW-7500C)"
WRONG_LINK = (
    "https://www.dawlance.com.pk/twin-tub-washing-machine/"
    "dw-7500-twin-tub-glass-lid-black-grey-semi-automatic-washing-machines"
)
#: the four photos the live page really serves (all `F01000000030_*`, 1000Wx1000H)
DAWLANCE_LO_DOWNLOADS = [
    "https://www.dawlance.com.pk/media/resize/"
    f"F01000000030_LO{n}_20220415_11{minute}.png/1000Wx1000H/image.webp"
    for n, minute in ((1, "3939"), (2, "3951"), (3, "4004"), (4, "4019"))
]
SURMAWALA_DW7500_PAGE = (
    "https://www.surmawala.pk/products/dawlance-washing-machine-dw-6550g-white-price-in-pakistan"
)
SURMAWALA_C_PAGE = (
    "https://www.surmawala.pk/products/dawlance-semi-automatic-washing-machine-dw-7500c-10kg"
)


def _dawlance_wrong_link_page() -> str:
    """Live shape: the page prints DW-7500 (no C) though the sheet row is a DW-7500C."""
    photos = "".join(
        f'<img src="{url}" alt="DW-7500 TWIN TUB GLASS LID BLACK/GREY (Photo {n}/4)">'
        for n, url in enumerate(DAWLANCE_LO_DOWNLOADS, 1)
    )
    return (
        "<html><head><title>DW-7500 TWIN TUB GLASS LID BLACK/GREY | Twin Tub Washing "
        "Machine | Washing Machines | Dawlance</title></head><body>"
        "<h1>DW-7500 TWIN TUB GLASS LID BLACK/GREY Twin Tub Washing Machine</h1>"
        f"<div class='product-gallery'>{photos}</div></body></html>"
    )


def _surmawala_suggest_dw7500c() -> str:
    """The live `suggest.json?q=DW-7500C` answer: the plain DW-7500 first, the C second."""
    return json.dumps({"resources": {"results": {"products": [
        {"url": "/products/dawlance-washing-machine-dw-6550g-white-price-in-pakistan",
         "title": "Dawlance Semi Automatic Washing Machine DW-7500 - 10Kg"},
        {"url": "/products/dawlance-semi-automatic-washing-machine-dw-7500c-10kg",
         "title": "Dawlance Semi Automatic Washing Machine DW-7500C - 10Kg"},
    ]}}})


def _shop_page(title: str, slug: str) -> str:
    photos = "".join(
        f'<img src="https://cdn.surmawala.pk/p/{slug}-{n}.png">' for n in range(1, 5)
    )
    return (
        f"<html><head><title>{title}</title><script>var s='cdn.shopify.com'</script>"
        f"</head><body><h1>{title}</h1><div class='product-gallery'>{photos}</div>"
        "</body></html>"
    )


def _wrong_link_deps(
    tmp_path: Path, *, with_chain: bool
) -> tuple[PipelineDeps, dict[str, bytes]]:
    pages = {
        WRONG_LINK: _dawlance_wrong_link_page(),
        SURMAWALA_DW7500_PAGE: _shop_page(
            "Dawlance Semi Automatic Washing Machine DW-7500 - 10Kg", "dw7500"
        ),
        SURMAWALA_C_PAGE: _shop_page(
            "Dawlance Semi Automatic Washing Machine DW-7500C - 10Kg", "dw7500c"
        ),
        f"{SURMAWALA_C_PAGE}.json": json.dumps({"product": {"images": [
            {"src": f"https://cdn.surmawala.pk/p/dw7500c-{n}.png?v={n}"} for n in range(1, 5)
        ]}}),
    }
    images = {
        **{url: png_bytes(_gallery_image(n)) for n, url in enumerate(DAWLANCE_LO_DOWNLOADS, 1)},
        **{
            f"https://cdn.surmawala.pk/p/{slug}-{n}.png": png_bytes(_gallery_image(n))
            for slug in ("dw7500", "dw7500c")
            for n in range(1, 5)
        },
    }
    deps, _, _, _ = build_deps(tmp_path, pages, images)
    if with_chain:
        http = FakeJsonHttp({
            "https://www.surmawala.pk/robots.txt": "User-agent: *\nDisallow:\n",
            "https://www.surmawala.pk/search/suggest.json?q=DW-7500C"
            "&resources[type]=product&resources[limit]=6": _surmawala_suggest_dw7500c(),
        })
        deps.source_searcher = SourceChain(live_config(tmp_path), http)
    return deps, images


def test_the_title_model_wins_over_a_wrong_sheet_link(tmp_path: Path) -> None:
    """The live row: title says DW-7500C, the link prints DW-7500 - so the row keeps looking.

    The link is a *tolerated spelling*, not proof.  The fallback chain then reaches
    Surmawala, whose `suggest.json?q=DW-7500C` answer carries the real DW-7500C listing, and
    those are the pictures the row publishes.
    """
    config = live_config(tmp_path, target_images_per_product=4)
    deps, _ = _wrong_link_deps(tmp_path, with_chain=True)
    result, accepted = process_product(task(0, WRONG_LINK_TITLE, (WRONG_LINK,)), deps, config)

    assert result.status is Status.OK
    assert len(accepted) == 4
    assert all("cdn.surmawala.pk/p/dw7500c-" in image.source_url for image in accepted)
    assert not any("dawlance.com.pk/media/resize" in image.source_url for image in accepted)
    # the plain DW-7500 shop page was offered first and skipped as another tolerated spelling
    assert not any("cdn.surmawala.pk/p/dw7500-" in image.source_url for image in accepted)
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert "deferred as last resort" in decisions
    assert "S15.tolerated" in decisions


def test_a_tolerated_sheet_page_is_published_with_a_flag_when_nothing_else_matches(
    tmp_path: Path,
) -> None:
    """No page prints the model: the wrong-link page is still published - flagged, not dropped."""
    config = live_config(tmp_path, target_images_per_product=4)
    deps, _ = _wrong_link_deps(tmp_path, with_chain=False)
    result, accepted = process_product(task(0, WRONG_LINK_TITLE, (WRONG_LINK,)), deps, config)

    assert len(accepted) == 4
    assert all("dawlance.com.pk/media/resize" in image.source_url for image in accepted)
    assert result.status is Status.PARTIAL              # published, but never silently "ok"
    assert "tolerated spelling" in result.note
    assert "page shows DW7500, sheet model is DW-7500C" in result.note
    write_output_csvs([result], config, include_platform_csvs=False)
    review = config.review_csv.read_text(encoding="utf-8")
    assert WRONG_LINK_TITLE in review
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert "published as last resort" in decisions


def test_a_page_printing_another_variant_suffix_is_a_last_resort_not_proof(
    tmp_path: Path,
) -> None:
    """Variant separation (2026-10-07): the sheet asks for IFGA, its own link prints IFPA.

    Those tails name different variants of one model, so the page is no longer exact
    proof: the row defers it, keeps looking for a page that prints its own model, and
    publishes it only as a flagged last resort.
    """
    config = live_config(tmp_path)
    sheet_url = "https://www.haier.com/pk/hrf-538"
    photo = "https://cdn.haier.com/p/hrf-538-ifpa-1.jpg"
    html = (
        "<html><head><title>Haier HRF-538 IFPA Refrigerator Golden</title></head>"
        "<body><h1>Haier HRF-538 IFPA</h1>"
        f"<div class='product-gallery'><img src=\"{photo}\"></div></body></html>"
    )
    deps, _, _, _ = build_deps(tmp_path, {sheet_url: html}, {photo: png_bytes(_gallery_image(1))})
    result, accepted = process_product(
        task(0, "Haier Refrigerator HRF-538 IFGA Golden", (sheet_url,)), deps, config
    )

    assert accepted                            # published as a last resort, never dropped
    assert result.status is Status.PARTIAL     # and never a silent "ok"
    assert "tolerated spelling" in result.note
    assert "IFPA" in result.note and "IFGA" in result.note
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert "deferred as last resort" in decisions


def test_a_tolerated_page_never_publishes_a_sibling_products_image(tmp_path: Path) -> None:
    """Orient run of 2026-10-10: the Pulse row's tolerated page also carries the Innova
    file.  Neither URL prints the ``19C`` key, so S4's two existing limbs are silent -
    the sheet itself is the proof: ``innova`` names *another row's* product, and a wrong
    product must never be downloaded, whatever the page's status is."""
    config = live_config(tmp_path)
    sheet_url = "https://newglacier.example/orient-pulse-19c-ultra-white"
    pulse_png = (
        "https://orient.com.pk/assets/products-images-2026/air-conditioners/"
        "15-ton-pulse-ultra-white-dc-inverter.png"
    )
    innova_png = (
        "https://orient.com.pk/assets/products-images-2026/air-conditioners/"
        "15-ton-innova-ultra-white-dc-inverter.png"
    )
    html = (
        "<html><head><title>Orient Pulse 19C</title></head>"
        "<body><h1>Orient Pulse 19C</h1>"
        f"<div class='product-gallery'><img src=\"{pulse_png}\">"
        f"<img src=\"{innova_png}\"></div></body></html>"
    )
    deps, _, _, _ = build_deps(
        tmp_path,
        {sheet_url: html},
        {pulse_png: png_bytes(_gallery_image(1)), innova_png: png_bytes(_gallery_image(2))},
    )
    row = ProductTask(
        index=0,
        title="Orient Inverter AC Pulse 19C Ultra White T3 1.5 Ton",
        source_urls=(sheet_url,),
        sibling_terms=frozenset({"innova"}),
    )
    result, accepted = process_product(row, deps, config)

    assert len(accepted) == 1                                  # the Pulse photo, nothing else
    assert "pulse" in accepted[0].source_url
    assert not any("innova" in image.source_url for image in accepted)
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert "names another product from this sheet" in decisions


def test_a_row_with_one_exact_image_is_never_topped_up_by_the_tolerated_page(
    tmp_path: Path,
) -> None:
    """Owner 2026-10-07: 1 correct image beats a mixed gallery.

    The sheet lists an exact-match page that yields a single clean photo, then the wrong-link
    (tolerated, different-variant) page.  The tolerated page must stay deferred - the row
    publishes its one correct picture as PARTIAL/flagged instead of mixing in four
    different-variant photos to hit the quota.
    """
    config = live_config(tmp_path)
    exact_one = "https://www.surmawala.pk/products/dawlance-dw-7500c-single"
    exact_url = "https://cdn.surmawala.pk/p/dw7500c-single-1.png"
    pages = {
        exact_one: (
            "<html><head><title>Dawlance Semi Automatic Washing Machine DW-7500C - 10Kg"
            "</title></head><body><h1>Dawlance DW-7500C</h1>"
            f"<div class='product-gallery'><img src=\"{exact_url}\"></div></body></html>"
        ),
        WRONG_LINK: _dawlance_wrong_link_page(),
    }
    images = {
        exact_url: png_bytes(_gallery_image(1)),
        **{url: png_bytes(_gallery_image(n)) for n, url in enumerate(DAWLANCE_LO_DOWNLOADS, 1)},
    }
    deps, _, _, _ = build_deps(tmp_path, pages, images)
    result, accepted = process_product(
        task(0, WRONG_LINK_TITLE, (exact_one, WRONG_LINK)), deps, config
    )

    assert len(accepted) == 1
    assert accepted[0].source_url == exact_url
    assert not any("dawlance.com.pk/media/resize" in i.source_url for i in accepted)
    assert result.status is Status.PARTIAL              # one image < min_required_images
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert "deferred as last resort" in decisions       # the wrong link was deferred...
    assert "published as last resort" not in decisions  # ...and never consumed to top up


def test_the_tolerated_spelling_can_be_switched_off(tmp_path: Path) -> None:
    """With the switch off the tolerated page is the answer, exactly as before this rule."""
    config = live_config(
        tmp_path, tolerated_spelling_as_last_resort=False, target_images_per_product=4
    )
    deps, _ = _wrong_link_deps(tmp_path, with_chain=True)
    result, accepted = process_product(task(0, WRONG_LINK_TITLE, (WRONG_LINK,)), deps, config)

    assert result.status is Status.OK
    assert len(accepted) == 4
    assert all("dawlance.com.pk/media/resize" in image.source_url for image in accepted)
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert "deferred as last resort" not in decisions


def test_non_official_sheet_url_prioritizes_official_brand_search(tmp_path: Path) -> None:
    """When sheet URL is non-official (e.g. Daraz), query official brand first."""
    config = live_config(tmp_path)
    daraz_url = "https://www.daraz.pk/products/homage-refrigerator-hrf-47662-gd.html"
    official_url = "https://homage.com.pk/products/hrf-47662-gd"
    official_img1 = "https://homage.com.pk/media/hrf-47662-gd-1.png"
    official_img2 = "https://homage.com.pk/media/hrf-47662-gd-2.png"
    daraz_img = "https://static-01.daraz.pk/p/daraz-blurry-1.jpg"

    official_html = (
        "<html><head><title>Homage Refrigerator HRF-47662 GD</title></head>"
        "<body><h1>Homage HRF-47662 GD</h1>"
        f"<div class='product-gallery'><img src=\"{official_img1}\"><img src=\"{official_img2}\"></div></body></html>"
    )
    daraz_html = (
        "<html><head><title>Homage Refrigerator HRF-47662 GD</title></head>"
        "<body><h1>Homage HRF-47662 GD</h1>"
        f"<div class='product-gallery'><img src=\"{daraz_img}\"></div></body></html>"
    )
    searcher = FakeSourceSearcher(
        candidates=[
            PageCandidate(
                url=official_url,
                origin="woo-search:homage.com.pk",
                method="woo-search",
                title_hint="Homage HRF-47662 GD",
            )
        ],
        report=SourceSearchReport(tried=("homage.com.pk:woo:1",), used="woo-search:homage.com.pk"),
    )
    deps, _, _, _ = build_deps(
        tmp_path,
        {daraz_url: daraz_html, official_url: official_html},
        {
            official_img1: png_bytes(clean_product_image(900, 900, "#2f5fa8")),
            official_img2: png_bytes(second_angle_image()),
            daraz_img: png_bytes(third_angle_image()),
        },
    )
    deps.source_searcher = searcher

    result, accepted = process_product(
        task(0, "Homage Refrigerator HRF-47662 GD", (daraz_url,)),
        deps,
        config,
    )

    assert result.status is Status.OK
    assert len(accepted) == 2
    # Official images were prioritized and used; Daraz was completely bypassed
    assert all("homage.com.pk/media" in img.source_url for img in accepted)
    assert not any("daraz.pk" in img.source_url for img in accepted)


def test_multi_image_continuation_does_not_stop_at_single_image(tmp_path: Path) -> None:
    """When a sheet URL gives only 1 image, continue fallback search to reach min_required_images."""
    config = live_config(tmp_path)
    sheet_url = "https://www.surmawala.pk/products/dawlance-dw-7500c"
    sheet_img = "https://cdn.surmawala.pk/p/dw7500c-1.png"
    fallback_url = "https://www.alfatah.com.pk/product/dawlance-dw-7500c"
    fallback_img = "https://www.alfatah.com.pk/wp-content/uploads/dw7500c-2.png"

    sheet_html = (
        "<html><head><title>Dawlance Washing Machine DW-7500C</title></head>"
        "<body><h1>Dawlance DW-7500C</h1>"
        f"<div class='product-gallery'><img src=\"{sheet_img}\"></div></body></html>"
    )
    fallback_html = (
        "<html><head><title>Dawlance Washing Machine DW-7500C</title></head>"
        "<body><h1>Dawlance DW-7500C</h1>"
        f"<div class='product-gallery'><img src=\"{fallback_img}\"></div></body></html>"
    )
    searcher = FakeSourceSearcher(
        candidates=[
            PageCandidate(
                url=fallback_url,
                origin="woo-search:www.alfatah.com.pk",
                method="woo-search",
                title_hint="Dawlance DW-7500C",
            )
        ],
        report=SourceSearchReport(
            tried=("www.alfatah.com.pk:woo:1",),
            used="woo-search:www.alfatah.com.pk",
        ),
    )
    deps, _, _, _ = build_deps(
        tmp_path,
        {sheet_url: sheet_html, fallback_url: fallback_html},
        {
            sheet_img: png_bytes(clean_product_image(900, 900, "#2f5fa8")),
            fallback_img: png_bytes(second_angle_image()),
        },
    )
    deps.source_searcher = searcher

    result, accepted = process_product(
        task(0, "Dawlance Washing Machine DW-7500C", (sheet_url,)),
        deps,
        config,
    )

    assert result.status is Status.OK
    # Did not stop at 1 image: fetched 2 images across sheet + fallback to fulfill min_required_images
    assert len(accepted) == 2
    assert accepted[0].source_url == sheet_img
    assert accepted[1].source_url == fallback_img


# --------------------------------------------------------------------------- #
# owner run 2026-10-08: dead sheet hosts, per-page download target, S16 default
# --------------------------------------------------------------------------- #
def test_excluded_host_sheet_url_is_never_fetched(tmp_path: Path) -> None:
    """A dead host pasted into the sheet must not burn retries x timeout."""
    config = live_config(tmp_path)
    deps, page_fetcher, _, _ = build_deps(tmp_path, {}, {})
    result, _ = process_product(
        task(
            0,
            "TCL 98 Inch SQD Mini LED TV (98C8L)",
            ("https://fahadelectronics.pk/products/tcl-98c8l-98-inch-4k-mini-led-google-tv",),
        ),
        deps,
        config,
    )
    assert page_fetcher.requested == []          # refused before any fetch
    assert result.status is Status.FAILED


def _six_image_gallery(
    tmp_path: Path, **overrides: object
) -> tuple[PipelineConfig, PipelineDeps, FakeImageFetcher]:
    """A Haier-style page whose own gallery carries six images, three of them repeats."""
    alt1 = "https://cdn.haier.com/p/alt/hsu-12hfpaa-1b.jpg"
    alt2 = "https://cdn.haier.com/p/alt/hsu-12hfpaa-2b.jpg"
    alt3 = "https://cdn.haier.com/p/alt/hsu-12hfpaa-3b.jpg"
    page = load_fixture("product_ok").replace(
        '"https://cdn.haier.com/p/hsu-12hfpaa-2.jpg"]',
        '"https://cdn.haier.com/p/hsu-12hfpaa-2.jpg",'
        '"https://cdn.haier.com/p/hsu-12hfpaa-3.jpg",'
        f'"{alt1}","{alt2}","{alt3}"]',
    )
    images = gallery_images()
    images[alt1], images[alt2], images[alt3] = images[IMG1], images[IMG2], images[IMG3]
    config = live_config(tmp_path, **overrides)
    deps, _, image_fetcher, _ = build_deps(tmp_path, {HAIER_PAGE: page}, images)
    return config, deps, image_fetcher


def test_a_curated_gallery_is_read_to_the_end(tmp_path: Path) -> None:
    """Owner decision, 2026-10-10: "download all product images, not 3 - remove this
    hardcoded concept".  Their Dawlance row is the proof it was needed: the brand's own
    gallery lists its slides as ``_1, _10, _11, _2, _3 …``, so stopping at the 3rd accept
    published two manuals plus an in-box sheet and never reached the seven product photos.
    A page carrying its own gallery is therefore scanned to the end - the published count
    is still bounded by ``max_images_per_product``, and repeats still fold at S10.
    """
    config, deps, image_fetcher = _six_image_gallery(tmp_path)
    result, accepted = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    assert result.status is Status.OK
    assert len(image_fetcher.requested) == 6              # every gallery image got a chance
    assert len(accepted) == config.target_images_per_product  # 3 repeats folded at S10
    assert config.gallery_publishes_all is True


def test_the_whole_gallery_rule_can_be_switched_off(tmp_path: Path) -> None:
    """The cost guarantee the old test protected is still a guarantee - it just belongs to
    the flag now: with ``gallery_publishes_all=False`` a six-image gallery costs the target
    (3 downloads), which is what the run budget was tuned on."""
    config, deps, image_fetcher = _six_image_gallery(tmp_path, gallery_publishes_all=False)
    result, accepted = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    assert result.status is Status.OK
    assert len(image_fetcher.requested) == config.target_images_per_product
    assert len(accepted) == config.target_images_per_product


def test_a_stray_image_page_keeps_the_row_target(tmp_path: Path) -> None:
    """Only pages offering a real gallery get the wide reading; three gallery images is
    below ``gallery_publishes_all_min`` and a retailer's ``<img>`` soup stays targeted."""
    from pipeline import _page_publish_goal

    config = live_config(tmp_path)
    goal = _page_publish_goal
    small = [
        ImageCandidate(
            url=f"u{i}", page_url=HAIER_PAGE, order=i, source="gallery_container",
            matches_model=True,
        )
        for i in range(3)
    ]
    big = [
        ImageCandidate(
            url=f"u{i}", page_url=HAIER_PAGE, order=i, source="gallery_container",
            matches_model=True,
        )
        for i in range(6)
    ]
    assert goal(small, config) == min(
        config.target_images_per_product, config.max_images_per_product
    )
    assert goal(big, config) == config.max_images_per_product


def test_color_gate_is_on_by_default(tmp_path: Path) -> None:
    """Owner decision 2026-10-08: a wrong colourway got published (white unit on a
    black row), so S16 now guards acceptance by default and is opt-*out*."""
    assert live_config(tmp_path).color_gate_enabled is True


# --------------------------------------------------------------------------- #
# stage 3c: last-resort salvage (owner decision 2026-10-08)
# --------------------------------------------------------------------------- #
def _jpeg_bytes(image: Image.Image, quality: int = 15) -> bytes:
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def test_below_floor_sharp_image_is_accepted_directly(tmp_path: Path) -> None:
    """Owner 2026-10-09 supersedes the 10-08 salvage detour: the compression floors
    judge *blur*, not bytes - "jahan blur hay wahin lagana hay".  A sharp payload
    below the byte/bpp floor (the Homage golden hero, the golden Haier AC) is simply
    accepted through the normal path instead of failing the row or waiting for 3c."""
    small_url = "https://cdn.haier.com/p/hsu-12hfpaa-hero-lowq.jpg"
    small = _jpeg_bytes(clean_product_image(700, 700, "#c9a227"))
    page = load_fixture("product_ok")
    for img in (IMG1, IMG2, IMG3):
        page = page.replace(img, small_url)
    config = live_config(tmp_path, min_lossy_image_bytes=len(small) + 2000)
    deps, _, _, _ = build_deps(tmp_path, {HAIER_PAGE: page}, {small_url: small})
    result, images = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    assert len(images) == 1                              # three URLs, one payload (dedup)
    assert result.status is Status.PARTIAL               # still below min_required=2
    log = [
        json.loads(line)
        for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    assert not any(entry["rule_id"] == "S13" for entry in log)      # no floor reject
    assert not any(entry["rule_id"] == "S13.salvage" for entry in log)


def test_salvage_takes_the_correct_colour_not_the_wrong_sibling(tmp_path: Path) -> None:
    """The same site serves maroon and golden below the floor: salvage must skip
    the wrong colour (S16.salvage) and publish only the golden one."""
    maroon_url = "https://www.hadielectronics.com.pk/wp-content/maroon.jpg"
    gold_url = "https://www.hadielectronics.com.pk/wp-content/golden.jpg"
    maroon = _jpeg_bytes(clean_product_image(700, 700, "#800000"))
    gold = _jpeg_bytes(clean_product_image(700, 700, "#d4af37"))
    page_url = "https://www.hadielectronics.com.pk/product/homage-hrf-47662-gd/"
    page = (
        "<!doctype html><html><head>"
        "<title>Homage HRF-47662-GD Freezer-on-Top Refrigerator 18 Cu Ft Golden</title>"
        '<script type="application/ld+json">{"@context":"https://schema.org",'
        '"@type":"Product","name":"Homage HRF-47662-GD Golden Refrigerator",'
        f'"sku":"HRF-47662-GD","image":["{maroon_url}","{gold_url}"]}}</script>'
        "</head><body></body></html>"
    )
    floor = max(len(maroon), len(gold)) + 2000
    config = live_config(tmp_path, min_lossy_image_bytes=floor)
    deps, _, _, _ = build_deps(
        tmp_path, {page_url: page}, {maroon_url: maroon, gold_url: gold}
    )
    result, images = process_product(
        task(
            0,
            "Homage Freezer-on-Top Refrigerator 18 Cu Ft Golden (HRF-47662-GD)",
            (page_url,),
        ),
        deps,
        config,
    )
    log = [
        json.loads(line)
        for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    # Since 2026-10-09 the sharp below-floor payloads are accepted directly, so the
    # colour gate (S16) - not the salvage colour check - keeps the wrong sibling out.
    rejected = [entry for entry in log if entry["rule_id"] == "S16" and entry["decision"] == "reject"]
    kept = [entry["url"] for entry in log if entry["decision"] == "keep"]
    assert any(entry["url"] == maroon_url for entry in rejected)  # wrong colour skipped
    assert gold_url in kept                                       # correct one published
    assert maroon_url not in kept
    assert len(images) == 1
    assert result.status is Status.PARTIAL


def test_salvage_can_be_disabled(tmp_path: Path) -> None:
    small_url = "https://cdn.haier.com/p/hsu-12hfpaa-hero-lowq.jpg"
    small = _jpeg_bytes(clean_product_image(700, 700, "#c9a227"))
    page = load_fixture("product_ok")
    for img in (IMG1, IMG2, IMG3):
        page = page.replace(img, small_url)
    config = live_config(
        tmp_path,
        min_lossy_image_bytes=len(small) + 2000,
        last_resort_salvage_enabled=False,
    )
    deps, _, _, _ = build_deps(tmp_path, {HAIER_PAGE: page}, {small_url: small})
    result, images = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    # The sharp below-floor picture is accepted directly since 2026-10-09, so even
    # with salvage switched off the row no longer fails - acceptance never needed
    # the salvage path.
    assert len(images) == 1
    assert result.status is Status.PARTIAL


def test_blurry_below_floor_image_is_never_salvaged(tmp_path: Path) -> None:
    """Owner 2026-10-08 (Haier H55K85FX): salvage had published two "puri blurred"
    pictures.  A below-floor image that is also blurred must stay rejected."""
    blur_url = "https://cdn.haier.com/p/hsu-12hfpaa-hero-blur.jpg"
    buf = io.BytesIO()
    clean_product_image(800, 800, "#c9a227").resize((90, 90)).resize((800, 800)).save(
        buf, "JPEG", quality=30
    )
    payload = buf.getvalue()
    page = load_fixture("product_ok")
    for img in (IMG1, IMG2, IMG3):
        page = page.replace(img, blur_url)
    config = live_config(tmp_path, min_lossy_image_bytes=len(payload) + 2000)
    deps, _, _, _ = build_deps(tmp_path, {HAIER_PAGE: page}, {blur_url: payload})
    result, images = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    assert images == []                                  # blurred stays rejected
    assert result.status is Status.FAILED
    log = [
        json.loads(line)
        for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    assert any(entry["rule_id"] == "S8.salvage" for entry in log)


def test_haier_thumb_upgrades_to_the_crisp_original_rung(tmp_path: Path) -> None:
    """Live page 2026-10-08 (h55k85guxfux.shtml): the gallery hands out _60/_480
    rungs while the crisp asset is _1680.  The probe and the download must both
    land on the crisp rung on an official page - the owner saw "puri blurred"
    pictures because only _480 was ever fetched."""
    soft = "https://image.haier.com/pk/tvs/W020260512624868079819_480.png"
    crisp = "https://image.haier.com/pk/tvs/W020260512624868079819.png"
    crisp_bytes = png_bytes(clean_product_image(1680, 945))
    page = (
        "<html><head><title>Haier H55K85FUX 55 Inch Google LED TV</title>"
        '<script type="application/ld+json">{"@context":"https://schema.org",'
        '"@type":"Product","name":"Haier H55K85FUX","sku":"H55K85FUX",'
        f'"image":["{soft}"]}}</script></head><body></body></html>'
    )
    config = live_config(tmp_path)
    deps, _, image_fetcher, _ = build_deps(tmp_path, {HAIER_PAGE: page}, {crisp: crisp_bytes})
    image_fetcher.probe_headers[crisp] = crisp_bytes[:64]
    result, accepted = process_product(
        task(0, "Haier 55 Inch Smart Google LED TV (H55K85FUX)", (HAIER_PAGE,)),
        deps,
        config,
    )
    assert image_fetcher.requested == [crisp], "the crisp rung, never the soft one"
    assert len(accepted) == 1
    # the published picture is built from the downloaded _1680 bytes; the candidate
    # URL (_480) stays as provenance in source_url
    assert accepted[0].source_url == soft
    assert result.status is Status.PARTIAL


# --------------------------------------------------------------------------- #
# owner 2026-10-08: branding file names and description-only galleries
# --------------------------------------------------------------------------- #
def test_decorated_logo_filename_is_rejected_at_s5() -> None:
    """``cropped-logooooooo.png`` - whole-token matching cannot see the logo."""
    from models import ModelKeys
    from pipeline import _decide_candidate

    keys = ModelKeys(cleaned_title="Orient Hyper 18X", tokens=("18X",), title_key="orient")
    candidate = ImageCandidate(
        url="https://bijlibazar.pk/wp-content/uploads/2024/10/cropped-logooooooo.png",
        page_url="https://bijlibazar.pk/product/orient-hyper-18x/",
        source="state_json",
        order=0,
        matches_model=True,
    )
    decision = _decide_candidate(candidate, keys, set())
    assert decision is not None
    assert decision[0] == "S5"
    assert "logo" in decision[1]


def test_description_only_page_relaxes_s2() -> None:
    from pipeline import relax_s2_when_description_only

    flagged = [
        ImageCandidate(url=f"https://surmawala.pk/uploads/es-24ar02w-{i}.jpg",
                       page_url="https://surmawala.pk/product/es-24ar02w/",
                       source="gallery_container", order=i, matches_model=True,
                       dom_flags=("S2",))
        for i in range(5)
    ]
    relaxed = relax_s2_when_description_only(flagged)
    assert all(c.dom_flags == () for c in relaxed)

    mixed = [
        ImageCandidate(url="https://x.pk/a.jpg", page_url="https://x.pk/p/",
                       source="gallery_container", order=0, matches_model=True,
                       dom_flags=("S2",)),
        ImageCandidate(url="https://x.pk/b.jpg", page_url="https://x.pk/p/",
                       source="gallery_container", order=1, matches_model=True),
    ]
    kept = relax_s2_when_description_only(mixed)
    assert kept[0].dom_flags == ("S2",)      # page has a real gallery: S2 stays strict


def test_official_below_floor_picture_publishes_and_retailer_search_never_runs(tmp_path: Path) -> None:
    """Owner 2026-10-09: "brand official sey mil gaye toh le lo jaisa hay".  The brand
    page only has a below-floor photo; since the compression floors now judge blur
    (not bytes), the sharp official picture is accepted on the official visit itself.
    Owner 2026-10-11 ("when found in brand official just download done"): once that
    official page has delivered, the retailer hunt never starts - the retail page is
    never fetched and the fallback chain never runs."""
    small_url = "https://cdn.haier.com/p/hsu-12hfpaa-hero-lowq.jpg"
    small = _jpeg_bytes(clean_product_image(700, 700, "#c9a227"))
    page = load_fixture("product_ok")
    for img in (IMG1, IMG2, IMG3):
        page = page.replace(img, small_url)
    retail = "https://www.retail.pk/products/hsu-12hfpaa"
    retail_page = (
        "<html><head><title>Haier HSU-12HFPAA Inverter AC</title></head>"
        "<body><div class=\"product-gallery\">"
        "<img src=\"https://cdn.retail.pk/hsu-12hfpaa-1.jpg\"></div></body></html>"
    )
    config = live_config(tmp_path, min_lossy_image_bytes=len(small) + 2000)
    deps, page_fetcher, _, _ = build_deps(
        tmp_path,
        {HAIER_PAGE: page, retail: retail_page},
        {
            small_url: small,
            "https://cdn.retail.pk/hsu-12hfpaa-1.jpg": png_bytes(clean_product_image()),
        },
    )
    deps.source_searcher = FakeSourceSearcher(
        candidates=[PageCandidate(url=retail, origin="json-search:www.retail.pk", method="json-search")]
    )
    result, images = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    log = [
        json.loads(line)
        for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    kept = [entry["url"] for entry in log if entry["decision"] == "keep"]
    assert small_url in kept                      # the official picture published
    assert HAIER_PAGE in page_fetcher.requested
    assert retail not in page_fetcher.requested   # official solved it: no retailer hunt
    assert not any("retail.pk" in entry["url"] for entry in log)


def test_an_ai_emblem_in_the_gallery_is_rejected_not_published(tmp_path: Path) -> None:
    """Live 2026-10-09: Haier's registry mixed the brand AI emblem into the product
    gallery and it was *kept*.  S14 must reject the badge and still publish the
    real product photos."""
    from PIL import ImageDraw

    emblem_url = "https://image.haier.com/pk/air-conditioners/ai-emblem.png"
    emblem = Image.new("RGB", (900, 900), (217, 217, 217))
    draw = ImageDraw.Draw(emblem)
    draw.ellipse((20, 20, 880, 880), outline=(85, 85, 85), width=22)
    draw.regular_polygon((430, 380, 190), 6, outline=(85, 85, 85), width=26)
    draw.ellipse((430, 430, 800, 800), outline=(85, 85, 85), width=26)
    page = load_fixture("product_ok").replace(IMG1, emblem_url)
    images = gallery_images()
    images[emblem_url] = png_bytes(emblem)
    config = live_config(tmp_path)
    deps, _, _, _ = build_deps(tmp_path, {HAIER_PAGE: page}, images)
    result, accepted = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    log = [
        json.loads(line)
        for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    s14 = [entry for entry in log if entry["rule_id"] == "S14"]
    assert [entry["url"] for entry in s14] == [emblem_url]
    assert all(entry["decision"] == "reject" for entry in s14)
    assert all(entry["url"] != emblem_url for entry in log if entry["decision"] == "keep")
    assert result.status is Status.OK


def test_an_official_grey_ground_shot_passes_without_a_curated_gallery(tmp_path: Path) -> None:
    """Owner 2026-10-09 (Kenwood eComfortPro.png): the brand's own studio shot has a
    light-grey ground that the retail corner bar (0.86) rejected.  On a verified
    official host the calmer bar applies to every gallery source - S1 already
    proved the model - so the picture publishes from a plain body img."""
    shot_url = "https://www.kenwoodpakistan.pk/cdn/shop/files/kec-1267s-front.png"
    from PIL import ImageDraw

    shot = Image.new("RGB", (500, 280), (217, 217, 217))
    draw = ImageDraw.Draw(shot)
    draw.rounded_rectangle((30, 40, 470, 200), radius=12, fill=(250, 250, 250))
    draw.rectangle((60, 210, 440, 235), fill=(90, 90, 90))
    draw.rectangle((215, 150, 285, 162), fill=(60, 60, 60))
    page_url = "https://www.kenwoodpakistan.pk/products/kec-1267s-comfort-pro"
    page = (
        "<html><head><title>Kenwood eComfort Pro KEC-1267S</title></head>"
        f"<body><p>Kenwood eComfort Pro KEC-1267S</p><img src=\"{shot_url}\"></body></html>"
    )
    config = live_config(tmp_path)
    deps, _, _, _ = build_deps(tmp_path, {page_url: page}, {shot_url: png_bytes(shot)})
    result, images = process_product(
        task(0, "Kenwood eComfort Pro KEC-1267S", (page_url,)), deps, config
    )
    log = [
        json.loads(line)
        for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    assert not [e for e in log if e["url"] == shot_url and e["decision"] == "reject"]
    assert len(images) == 1
    assert result.status is Status.PARTIAL


def test_a_row_without_any_model_code_gets_a_note_and_not_a_crash() -> None:
    """A keyword row (no model token, no glued identity) must still explain itself.

    Live 2026-10-09: ``Frost 18W Classic White | Orient Electronics Pakistan`` came back
    as ``failed: unexpected error: tuple index out of range``.  The note that explains a
    tolerated page indexed ``keys.identities[0]`` unguarded, so exactly the rows that own
    no code - the ones that need the explanation most - crashed instead of reporting.
    A failed row is a correct outcome; an exception in the reporting is not.
    """
    from model_matching import build_model_keys
    from pipeline import _tolerated_spelling_reason

    keys = build_model_keys("Frost 18W Classic White | Orient Electronics Pakistan")
    assert not keys.tokens and not keys.identities              # the shape that used to crash
    reason = _tolerated_spelling_reason("Orient Cooltron Frost 18W", keys)
    assert reason.startswith("page shows ") and "sheet model" in reason


# --------------------------------------------------------------------------- #
# official-gallery layout rescue (S20/S21) + --keep-rejected evidence dump
# --------------------------------------------------------------------------- #
def _product_wearing_a_marketing_panel(size: int = 1600) -> Image.Image:
    """A real product photo sitting on a marketing panel that bleeds off all four edges.

    Orient's hero is this shape: a tinted card painted edge to edge, the appliance in the
    middle.  S20 names the layout correctly - it *is* artwork - and the owner's rule of
    2026-10-10 says what to do about it: "cut the bottom labels, advertisement marketing
    labels, and add only product".  The body under the panel is a genuine accepted photo, so
    the crop the rescue publishes passes every gate on its own.
    """
    image = Image.new("RGB", (size, size), (226, 236, 248))
    unit = size // 2
    image.paste(clean_product_image(unit, unit, "#2f5fa8"), ((size - unit) // 2, (size - unit) // 2))
    return image


def _emblem_sheet(size: int = 1600) -> Image.Image:
    """Line art filling the frame - the S20 branch the rescue must never touch.

    Same construction as the emblem fixture in ``test_image_quality``: dense blocky marks
    reaching all four edges with studio-white corner boxes, so S7 and S14 stay quiet and only
    the layout sees it.  Its S20 reason is the *line art* branch, and that branch has nothing
    to crop - the artwork is the whole picture.
    """
    image = Image.new("RGB", (size, size), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    margin = size // 8
    shades = (35, 120, 255, 60, 255)
    cell = 40
    for y in range(0, size, cell):
        for x in range(0, size, cell):
            in_mark = (margin <= x <= size - margin - 1) or (margin <= y <= size - margin - 1)
            inside = not (x < margin or y < margin or x >= size - margin or y >= size - margin)
            if in_mark and inside:
                shade = shades[(x // cell + 3 * (y // cell)) % len(shades)]
                if shade != 255:
                    draw.rectangle((x, y, x + cell - 1, y + cell - 1), fill=(shade,) * 3)
    for edge in (0, size - 1):
        draw.point((edge, size // 2), fill=(35, 35, 35))
        draw.point((size // 2, edge), fill=(35, 35, 35))
    return image


def test_official_gallery_rescues_a_product_inside_marketing_artwork(tmp_path: Path) -> None:
    """Owner 2026-10-10: "marketing link easily scrap[es] but this actual[ly] not" - the page's
    own hero is marketing *artwork around* the product, so it must be cropped, not discarded."""
    from image_quality import run_quality_gates

    config = live_config(tmp_path)
    image = _product_wearing_a_marketing_panel()
    rule, detail = run_quality_gates(image, config)
    assert rule == "S20" and detail.startswith("painted artwork"), detail
    rescued, reason = _layout_rescue_verdict(image, config, rule, detail)
    assert rescued, reason
    assert "isolated product was judged instead" in reason


def test_official_gallery_never_rescues_an_emblem_or_a_caption_sheet(tmp_path: Path) -> None:
    """The rescue is limited to the painted-panel branch on purpose: line art has no product
    inside it, and the corpus proves the limit works - all 16 junk files of the owner's run
    (8 Haier emblem files, 2 in-box caption sheets, 6 ad renders) stay rejected."""
    from image_quality import run_quality_gates

    config = live_config(tmp_path)
    image = _emblem_sheet()
    rule, detail = run_quality_gates(image, config)
    assert rule == "S20" and detail.startswith("line art"), detail
    assert _layout_rescue_verdict(image, config, rule, detail) == (False, "")
    # a switch-off returns the row to the plain S20/S21 behaviour
    assert _layout_rescue_verdict(
        _product_wearing_a_marketing_panel(),
        replace(config, official_layout_rescue=False),
        "S20",
        "painted artwork covers 84% of the frame",
    ) == (False, "")


def test_keep_rejected_writes_the_pixels_that_died(tmp_path: Path) -> None:
    """The owner asked where a rejection can be found: the log has the sentence, and with
    ``--keep-rejected`` the bytes land next to the published folder, named by the rule."""
    config = live_config(tmp_path, keep_rejected_images=True)
    _dump_rejected(_emblem_sheet(), config, "Frost 18W Classic White", "https://x/a.png", "S20")
    folder = config.output_dir / "images" / safe_slug("Frost 18W Classic White") / "rejected"
    written = sorted(folder.glob("*.jpg"))
    assert len(written) == 1
    assert written[0].name.startswith("S20-")
    assert Image.open(written[0]).size == _emblem_sheet().size


def test_a_pasted_marketing_panel_is_cut_before_the_row_is_judged(tmp_path: Path) -> None:
    """The owner's Dawlance file of 2026-10-10: a studio shot of the unit with a flat black
    badge/warranty panel pasted underneath.  They asked for the panel to be cut and the
    product published - "cut the bottom labels, advertisement marketing labels, and add only
    product" - and this drives the whole path, including the rescue re-check that a unit test
    cannot reach.
    """
    import io

    from PIL import Image as PILImage
    from test_image_quality import _unit_with_badge_panel

    fixture = _unit_with_badge_panel(size=900)
    buf = io.BytesIO()
    fixture.save(buf, format="PNG")
    images = {IMG1: buf.getvalue()}
    config = live_config(tmp_path, min_short_side=120, min_long_side=200)
    deps, _, _, _ = build_deps(tmp_path, {HAIER_PAGE: load_fixture("product_ok")}, images)
    result, accepted = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    lines = [
        json.loads(line) for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    assert any(line["rule_id"] == "S7.panel" for line in lines), lines
    assert accepted, [line["detail"] for line in lines]
    saved = sorted((config.output_dir / "images" / safe_slug(result.title) / "raw").glob("*.jpg"))
    assert saved, result.title
    with PILImage.open(saved[0]) as published:
        assert published.size[1] < fixture.size[1]          # the panel is gone from the file too
    assert result.status in (Status.OK, Status.PARTIAL)


def test_a_tinted_studio_wash_cannot_cost_the_row_the_brands_own_hero(tmp_path: Path) -> None:
    """The owner's Orient row of 2026-10-10: the hero was found and downloaded, then S16 said
    "product area is a saturated colour (60% chromatic) - conflicts with target 'white'" and
    the row lost the only correct picture.  Their instruction was to make the safeguard
    correct rather than delete it: "don't force this in brand official, only in retailer".
    So the same image must publish from the brand's page and still be refused from a retailer.
    """
    from test_image_quality import _chroma_spread_share, _unit_on_a_tinted_plate

    strong = _unit_on_a_tinted_plate((150, 190, 240))  # tint the metric really calls a colour
    assert _chroma_spread_share(strong) >= 0.45  # the pre-fix measure condemns this file
    buf = io.BytesIO()
    strong.save(buf, format="PNG")
    images = {IMG1: buf.getvalue()}
    title = "Haier HSU-12HFPAA White Inverter AC"

    brand_deps, _, _, _ = build_deps(
        tmp_path, {HAIER_PAGE: load_fixture("product_ok")}, images
    )
    brand_result, brand_accepted = process_product(task(0, title, (HAIER_PAGE,)), brand_deps, live_config(tmp_path))
    assert brand_accepted, "the brand's own hero must not be lost to a studio wash"
    lines = [
        json.loads(line)
        for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    exemption = [line for line in lines if line["rule_id"] == "S16.official"]
    assert exemption, [line["rule_id"] for line in lines]
    assert "not enforced" in exemption[0]["detail"]
    assert brand_result.status in (Status.OK, Status.PARTIAL)

    # Same pixels, same model, a retailer's page: the colour gate keeps its teeth.
    retail_tmp = tmp_path / "retail"
    retail_tmp.mkdir()
    retail_deps, _, _, _ = build_deps(
        retail_tmp, {SHOPIFY_PAGE: load_fixture("product_ok")}, images
    )
    _, retail_accepted = process_product(
        task(0, title, (SHOPIFY_PAGE,)), retail_deps, live_config(retail_tmp)
    )
    assert not retail_accepted, "a wash-dominant picture from a retailer is still a colour conflict"
    retail_lines = [
        json.loads(line)
        for line in (retail_tmp / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    assert any(
        line["rule_id"] == "S16" and "saturated colour" in line["detail"]
        for line in retail_lines
    ), [line["rule_id"] for line in retail_lines]


def test_a_cut_out_brand_png_publishes_with_a_white_ground(tmp_path: Path) -> None:
    """The Orient rows kept dying on one thing: their PNGs are cut-outs, the plate colour is
    CSS (``#e5e5e5``), and the decoder handed the gates the transparency as **black** - so
    S16 called a Matt White unit "dark" and rejected the brand's own hero.  This drives the
    real path and then opens the file the customer would get.
    """
    from PIL import Image as PILImage
    from test_image_quality import _cut_out_unit_png

    images = {IMG1: _cut_out_unit_png()}
    config = live_config(tmp_path, min_short_side=120, min_long_side=200)
    deps, _, _, _ = build_deps(tmp_path, {HAIER_PAGE: load_fixture("product_ok")}, images)
    result, accepted = process_product(
        task(0, "Haier HSU-12HFPAA White Inverter AC", (HAIER_PAGE,)), deps, config
    )
    assert accepted, "a cut-out PNG must not be lost to its own transparency"
    lines = [
        json.loads(line) for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    assert not [line for line in lines if line["rule_id"].startswith("S16")], lines

    saved = sorted((config.output_dir / "images" / safe_slug(result.title) / "raw").glob("*.jpg"))
    assert saved, result.title
    with PILImage.open(saved[0]) as published:
        spots = (
            (3, 3),
            (published.size[0] - 4, 3),
            (3, published.size[1] - 4),
            (published.size[0] - 4, published.size[1] - 4),
        )
        corners = [cast("tuple[int, int, int]", published.convert("RGB").getpixel(xy)) for xy in spots]
    assert all(sum(px) > 3 * 240 for px in corners), corners
    assert result.status in (Status.OK, Status.PARTIAL)


def test_the_sibling_limb_never_rejects_a_file_anchored_to_its_own_row(tmp_path: Path) -> None:
    """Live 2026-10-11: the Hyper 18X row's own file ``…Hyper-18X-T3-Super-White-Turbo-Pro.webp``
    was rejected because ``turbo`` names another row of the sheet.  A file that carries the
    row's own key (or its own series words) is anchored to the row - the sibling word beside
    it is decoration, not identity.  An unanchored file naming the sibling still dies."""
    config = live_config(tmp_path)
    sheet_url = "https://www.bijlibazar.com/orient-hyper-18x"
    own_png = (
        "https://www.bijlibazar.com/wp-content/uploads/2025/08/"
        "Orient-Inverter-AC-1.5-Ton-Hyper-18X-T3-Super-White-Turbo-Pro.webp"
    )
    foreign_png = (
        "https://www.bijlibazar.com/wp-content/uploads/2025/09/Gree-18PITH-Turbo-Pro.webp"
    )
    html = (
        "<html><head><title>Orient Split AC Inverter Hyper 18X</title></head>"
        "<body><h1>Orient Split AC Inverter Hyper 18X</h1>"
        f"<div class='product-gallery'><img src=\"{own_png}\">"
        f"<img src=\"{foreign_png}\"></div></body></html>"
    )
    deps, _, _, _ = build_deps(
        tmp_path,
        {sheet_url: html},
        {own_png: png_bytes(_gallery_image(1)), foreign_png: png_bytes(_gallery_image(2))},
    )
    row = ProductTask(
        index=0,
        title="Orient Split AC Hyper 18X",
        source_urls=(sheet_url,),
        sibling_terms=frozenset({"turbo"}),
        own_terms=frozenset({"hyper"}),
    )
    result, accepted = process_product(row, deps, config)

    assert any("hyper-18x" in image.source_url.lower() for image in accepted), "the anchored file stays"
    assert not any("gree-18pith" in image.source_url.lower() for image in accepted)
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert "names another product from this sheet" in decisions


# --------------------------------------------------------------------------- #
# Owner run 2026-10-11: the Pulse row downloaded Innovia
# --------------------------------------------------------------------------- #
def test_the_row_stops_searching_once_the_brand_official_page_delivered(tmp_path: Path) -> None:
    """The owner's "when found in brand official just download done": the row's official
    page gave one correct picture, then the quota hunt visited a retailer page and the
    fallback chain - the source of every wrong image in that run.  Once an official page
    has delivered, the row publishes what the brand gave it and searches nothing else."""
    config = live_config(tmp_path)
    official_url = "https://www.orient.com.pk/products/pulse-19c-ultra-white-t3.html"
    official_png = (
        "https://www.orient.com.pk/assets/products-images-2026/air-conditioners/"
        "15-ton-pulse-ultra-white-dc-inverter.png"
    )
    retailer_url = "https://www.bijlibazar.com/orient-pulse-19c-ultra"
    retailer_png = "https://www.bijlibazar.com/media/pulse-19c-hero.webp"
    fallback_url = "https://www.coolingshop.pk/products/orient-pulse-19c"
    fallback_png = "https://www.coolingshop.pk/cdn/pulse-19c.jpg"
    official_html = (
        "<html><head><title>Pulse 19C Ultra White T3 | Orient Electronics Pakistan</title></head>"
        "<body><h1>Pulse 19C Ultra White T3</h1>"
        f"<div class='product-gallery'><img src=\"{official_png}\"></div></body></html>"
    )
    retailer_html = (
        "<html><head><title>Orient Pulse 19C Ultra White T3 price</title></head>"
        "<body><h1>Orient Pulse 19C Ultra White T3</h1>"
        f"<div class='product-gallery'><img src=\"{retailer_png}\"></div></body></html>"
    )
    fallback_html = (
        "<html><head><title>Orient Pulse 19C Ultra White T3</title></head>"
        "<body><h1>Orient Pulse 19C Ultra White T3</h1>"
        f"<div class='product-gallery'><img src=\"{fallback_png}\"></div></body></html>"
    )
    searcher = FakeSourceSearcher(
        candidates=[
            PageCandidate(
                url=fallback_url, origin="woo:www.coolingshop.pk", method="woo",
                title_hint="Orient Pulse 19C",
            )
        ],
        report=SourceSearchReport(tried=("www.coolingshop.pk:woo:1",)),
    )
    deps, page_fetcher, image_fetcher, _ = build_deps(
        tmp_path,
        {official_url: official_html, retailer_url: retailer_html, fallback_url: fallback_html},
        {
            official_png: png_bytes(clean_product_image()),
            retailer_png: png_bytes(second_angle_image()),
            fallback_png: png_bytes(third_angle_image()),
        },
    )
    deps.source_searcher = searcher
    result, images = process_product(
        task(0, "Pulse 19C Ultra White T3 | Orient Electronics Pakistan", (official_url, retailer_url)),
        deps,
        config,
    )
    assert len(images) == 1
    assert "pulse-ultra-white" in images[0].source_url
    assert result.status is Status.PARTIAL       # one image, flagged for review - never a wrong one
    assert retailer_url not in page_fetcher.requested
    assert fallback_url not in page_fetcher.requested
    assert searcher.calls == []                  # the fallback chain never even ran
    assert retailer_png not in image_fetcher.requested
    assert fallback_png not in image_fetcher.requested


def test_an_official_page_of_another_line_publishes_nothing(tmp_path: Path) -> None:
    """The exact wrong download of the owner run 2026-10-11: the brand's own sitemap
    offered ``innova-19s-ultra-white-t3.html`` for the Pulse row, and the official-host
    fuzzy promotion accepted it.  The page names another line (no ``pulse`` anywhere),
    so it is strength 0 and its gallery is never looked at."""
    config = live_config(tmp_path)
    sheet_url = "https://www.orient.com.pk/air-conditioners.html"
    sheet_html = (
        "<html><head><title>Air Conditioners | Orient Electronics Pakistan</title></head>"
        "<body><h1>Orient Air Conditioners</h1><p>Explore the full range.</p></body></html>"
    )
    innova_url = "https://www.orient.com.pk/products/innova-19s-ultra-white-t3.html"
    innova_png = (
        "https://www.orient.com.pk/assets/products-images-2026/air-conditioners/"
        "15-ton-innova-ultra-white-dc-inverter-4742e7e9.png"
    )
    innova_html = (
        "<html><head><title>Innova 19S Ultra White T3 | Orient Electronics Pakistan</title></head>"
        "<body><h1>Innova 19S Ultra White T3</h1>"
        f"<div class='product-gallery'><img src=\"{innova_png}\"></div></body></html>"
    )
    searcher = FakeSourceSearcher(
        candidates=[
            PageCandidate(
                url=innova_url, origin="sitemap:www.orient.com.pk",
                method="sitemap", title_hint="innova-19s-ultra-white-t3.html",
            )
        ],
        report=SourceSearchReport(tried=("www.orient.com.pk:sitemap:1",)),
    )
    deps, _, image_fetcher, _ = build_deps(
        tmp_path,
        {sheet_url: sheet_html, innova_url: innova_html},
        {innova_png: png_bytes(clean_product_image())},
    )
    deps.source_searcher = searcher
    result, images = process_product(
        task(0, "Pulse 19C Ultra White T3 | Orient Electronics Pakistan", (sheet_url,)),
        deps,
        config,
    )
    assert images == []
    assert result.status is Status.FAILED
    assert innova_png not in image_fetcher.requested


def test_an_image_naming_an_unknown_line_dies_without_sheet_knowledge(tmp_path: Path) -> None:
    """The sheet-knowledge limb is silent when the other product has no row of its own
    (this sheet has no Innova row), yet the wrong file still names itself.  A strongly
    matched page may carry both galleries; the file whose name says ``innova`` is refused
    for a Pulse row by its filename alone, and the row's own file stays."""
    config = live_config(tmp_path)
    page_url = "https://www.bijlibazar.com/orient-pulse-19c-ultra-white"
    own_png = (
        "https://www.orient.com.pk/assets/products-images-2026/air-conditioners/"
        "15-ton-pulse-ultra-white-dc-inverter.png"
    )
    foreign_png = (
        "https://www.orient.com.pk/assets/products-images-2026/air-conditioners/"
        "15-ton-innova-ultra-white-dc-inverter-4742e7e9.png"
    )
    html = (
        "<html><head><title>Orient Pulse 19C Ultra White T3 price in Pakistan</title></head>"
        "<body><h1>Orient Pulse 19C Ultra White T3</h1>"
        f"<div class='product-gallery'><img src=\"{own_png}\"><img src=\"{foreign_png}\"></div>"
        "</body></html>"
    )
    deps, _, _, _ = build_deps(
        tmp_path,
        {page_url: html},
        {
            own_png: png_bytes(clean_product_image(900, 900, "#f4f4f4")),
            foreign_png: png_bytes(second_angle_image()),
        },
    )
    result, images = process_product(
        task(0, "Pulse 19C Ultra White T3 | Orient Electronics Pakistan", (page_url,)),
        deps,
        config,
    )
    assert any("pulse-ultra-white" in image.source_url for image in images)
    assert not any("innova" in image.source_url for image in images)
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert "names another product line" in decisions


def test_a_panasonic_page_is_never_published_for_an_orient_row(tmp_path: Path) -> None:
    """The exact wrong download of the owner run 2026-10-12: the Megatron 18X row
    published Panasonic ``18XKF9`` renders found by a bare ``18X`` shop search.  The
    retailer page names another maker in its own title, so it is strength 0 outright -
    not tolerated, not rescued - and its gallery (the nameless ``UntitledProject``
    renders) is never looked at."""
    config = live_config(tmp_path)
    page_url = (
        "https://www.surmawala.pk/products/panasonic-18xkf-9-1-5-ton-inverter-"
        "air-conditioner-2024"
    )
    panasonic_jpg = "https://cdn.shopify.com/s/files/1/x/files/panasonic.jpg"
    render_webp = (
        "https://cdn.shopify.com/s/files/1/x/files/"
        "UntitledProject-2024-04-29T104202.450.webp"
    )
    html = (
        "<html><head><title>Panasonic 18XKF-9 1.5 Ton Inverter Air Conditioner 2024"
        " – Surmawala</title></head>"
        "<body><h1>Panasonic 18XKF-9 1.5 Ton Inverter Air Conditioner 2024</h1>"
        f"<div class='product-gallery'><img src=\"{panasonic_jpg}\">"
        f"<img src=\"{render_webp}\"></div></body></html>"
    )
    deps, _, image_fetcher, _ = build_deps(
        tmp_path,
        {page_url: html},
        {
            panasonic_jpg: png_bytes(clean_product_image()),
            render_webp: png_bytes(clean_product_image()),
        },
    )
    result, images = process_product(
        task(
            0,
            "Orient Megatron Infinity 18X eComfort DC Inverter AC - 1.5 Ton",
            (page_url,),
        ),
        deps,
        config,
    )
    assert images == []
    assert result.status is Status.FAILED
    assert panasonic_jpg not in image_fetcher.requested
    assert render_webp not in image_fetcher.requested
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert "panasonic product" in decisions


def test_an_image_named_after_another_maker_dies_on_a_brandless_page(tmp_path: Path) -> None:
    """The page gate judges the page's visible title - but a degraded fetch may print no
    maker at all, and then only the file's own name is left.  A ``panasonic.jpg`` on an
    Orient row is refused by the maker limb before download, while the row's own image
    (whose name carries no maker) still publishes."""
    config = live_config(tmp_path)
    page_url = "https://www.retailpk.net/air-conditioners/megatron-infinity-18x-ecomfort"
    foreign_jpg = "https://cdn.shopify.com/s/files/1/x/files/panasonic.jpg"
    own_jpg = "https://www.retailpk.net/cdn/megatron-infinity-18x-ecomfort-white-front.jpg"
    html = (
        "<html><head><title>Megatron Infinity 18X eComfort DC Inverter Air Conditioner"
        " 1.5 Ton</title></head>"
        "<body><h1>Megatron Infinity 18X eComfort DC Inverter AC 1.5 Ton</h1>"
        "<p>Orient 18X eComfort series, DC inverter, energy efficient.</p>"
        f"<div class='product-gallery'><img src=\"{foreign_jpg}\">"
        f"<img src=\"{own_jpg}\"></div></body></html>"
    )
    deps, _, image_fetcher, _ = build_deps(
        tmp_path,
        {page_url: html},
        {
            foreign_jpg: png_bytes(clean_product_image()),
            own_jpg: png_bytes(clean_product_image(900, 900, "#f4f4f4")),
        },
    )
    result, images = process_product(
        task(
            0,
            "Orient Megatron Infinity 18X eComfort DC Inverter AC - 1.5 Ton",
            (page_url,),
        ),
        deps,
        config,
    )
    assert len(images) == 1
    assert "megatron-infinity-18x-ecomfort" in images[0].source_url
    assert foreign_jpg not in image_fetcher.requested
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert "names another maker" in decisions

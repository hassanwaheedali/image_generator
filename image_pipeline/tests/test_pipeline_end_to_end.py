"""Pipeline behaviour with fakes only: S14, ordering, resume and abort semantics."""

from __future__ import annotations

import csv
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
    synthetic_platforms,
    third_angle_image,
)
from PIL import Image, ImageDraw

from config import ConfigError, PipelineConfig
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
    CHECKPOINT_LAYOUT,
    AcceptedImage,
    CheckpointEntry,
    ImageCandidate,
    ModelKeys,
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
    _coherence_note,
    _dump_rejected,
    _layout_rescue_verdict,
    _page_download_allowance,
    ensure_banner_url,
    finalize_and_upload,
    process_product,
    write_output_csvs,
)
from platforms import PlatformPlan
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
        platforms=synthetic_platforms(tmp_path / "assets"),
    )
    return deps, page_fetcher, image_fetcher, image_host


def _plan(tmp_path: Path) -> PlatformPlan:
    """The plan the fake asset tree resolves to: Alfamall and DigiMall, no Website artwork."""
    return synthetic_platforms(tmp_path / "assets")


def task(
    index: int,
    title: str,
    urls: tuple[str, ...],
    *,
    tail_owner: int = 0,
    siblings: frozenset[str] = frozenset(),
    own: frozenset[str] = frozenset(),
) -> ProductTask:
    return ProductTask(
        index=index,
        title=title,
        source_urls=urls,
        tail_owner_count=tail_owner,
        sibling_terms=siblings,
        own_terms=own,
    )


def _urls(digi: str, alfa: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """One URL per platform, as ``ProductResult.platform_urls`` stores them (plan order)."""
    return (("alfa", (alfa,)), ("digi", (digi,)))


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
    digi = result.urls_for("digi")
    alfa = result.urls_for("alfa")
    assert len(digi) == 4                                  # 3 images + closing banner
    assert digi[-1].endswith("digimall-closing-banner.jpg")
    assert len(alfa) == len(digi)
    assert alfa[-1].endswith("alfamall-closing-banner.jpg")
    # banners are uploaded last, in the plan's platform order (alfa before digi)
    assert host.uploads[-2].startswith("alfamall-closing-banner")
    assert host.uploads[-1].startswith("digimall-closing-banner")
    for accepted in images:
        assert len(accepted.final_paths) == 2
        assert all(path.exists() for _, path in accepted.final_paths)
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
    assert len(result.urls_for("digi")) == 2 and len(result.urls_for("alfa")) == 2
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
    assert result.platform_urls == ()
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
    assert result.urls_for("digi") and result.urls_for("alfa")
    assert host.uploads


# --------------------------------------------------------------------------- #
# ordering + CSV writers
# --------------------------------------------------------------------------- #
def test_output_rows_preserve_input_order(tmp_path: Path) -> None:
    config = live_config(tmp_path)
    results = [
        ProductResult(task_index=2, title="C", status=Status.FAILED, reason="no page matched this model"),
        ProductResult(task_index=0, title="A", status=Status.OK, platform_urls=_urls("u1", "a1")),
        ProductResult(task_index=1, title="B", status=Status.PARTIAL, platform_urls=_urls("u2", "a2")),
    ]
    write_output_csvs(results, config, _plan(tmp_path))
    lines = (config.output_dir / "digi" / "digimall_product_images.csv").read_text(
        encoding="utf-8"
    ).strip().splitlines()
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
    assert by_index[0].platform_urls                 # published URLs survive the resume
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
        platform_urls=(("digi", ("https://img.example/a.jpg",)),
                       ("alfa", ("https://img.example/a.jpg",))),
        reason="",
        timestamp="2026-01-01T00:00:00Z",
        layout=CHECKPOINT_LAYOUT,
    )
    # same URLs -> done
    assert entry.is_resume_done(hash_source_urls(("https://old.example/p",)), ("alfa", "digi")) is True
    # changed Source URL -> retry
    assert entry.is_resume_done(hash_source_urls(("https://new.example/p",)), ("alfa", "digi")) is False
    # empty URL list with an ok status -> retry
    empty = CheckpointEntry("X", "", Status.OK, (), "", "")
    assert empty.is_resume_done(hash_source_urls(()), ("alfa", "digi")) is False


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
    assert first.platform_urls == second.platform_urls
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


def test_a_page_that_did_not_prove_the_model_vouches_for_no_picture(tmp_path: Path) -> None:
    """Live bug the owner caught on the refrigerator run 2026-10-19: the `HRF-418IDGA` row
    published `haier-276ipra.png` and `316-ipra.png` - two *other* fridges - because a
    `S15.relaxed` page (brand + the number 418, no exact proof) was allowed to hand over
    whatever its gallery held.  A page that never proved this model cannot vouch for its
    files, so on such a page every picture must carry this row's own code.  Measured on his
    run: 12 of 37 published pictures were unproven-page + unproven-file."""
    config = live_config(tmp_path)
    page = "https://www.surmawala.pk/products/haier-refrigerator-418-smart-inverter-double-door"
    html = (
        "<html><head><title>Haier Refrigerator 418 Smart Inverter Double Door</title></head>"
        "<body><div class='product-gallery'>"
        '<img src="https://www.surmawala.pk/cdn/shop/files/haier-276ipra.png">'
        '<img src="https://www.surmawala.pk/cdn/shop/files/haier-418idga-1.png">'
        "</div></body></html>"
    )
    other = "https://www.surmawala.pk/cdn/shop/files/haier-276ipra.png"
    own = "https://www.surmawala.pk/cdn/shop/files/haier-418idga-1.png"
    deps, _, _, _ = build_deps(
        tmp_path,
        {
            "https://www.haier.com/pk/none.shtml": "<html><body>nothing</body></html>",
            page: html,
        },
        {
            other: png_bytes(clean_product_image(900, 500, "#c0392b")),
            own: png_bytes(clean_product_image(700, 700, "#2e6b4f")),
        },
    )
    deps.source_searcher = FakeSourceSearcher(
        candidates=[
            PageCandidate(url=page, origin="json-search:www.surmawala.pk", method="json-search")
        ]
    )
    result, accepted = process_product(
        task(
            0,
            "Haier HRF-418IDGA 11 Cu Ft Refrigerator",
            ("https://www.haier.com/pk/none.shtml",),
        ),
        deps,
        config,
    )

    assert [image.source_url for image in accepted] == [own]   # only the file that proves the row
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert '"rule_id": "S15.image"' in decisions
    assert "names no code of this row" in decisions
    assert result.status is not Status.OK


def test_the_unverified_marketplace_last_resort_publishes_when_switched_on(
    tmp_path: Path,
) -> None:
    """Owner 2026-10-19, stage 3d, shipped OFF: a row that found nothing anywhere may still
    take the shop's picture when the shop writes the code in a shape S1 cannot read - live,
    `801217 WB` and `120826 JT` pages print `hwm-80-1217`, split by a hyphen.  Here the page's
    title names no model at all, but its file does.  The row publishes, and it is capped and
    named: `partial`, an UNVERIFIED note, and a `review_products.csv` line, because a human
    must look at the picture before the shop uploads it."""
    config = replace(live_config(tmp_path), last_resort_marketplace_fallback=True)
    page = """<html><head><title>Haier Inverter Split Air Conditioner</title></head>
    <body><div class="product-gallery">
    <img src="https://cdn.surmawala.pk/items/hsu-12hfpaa-1.jpg"></div></body></html>"""
    deps, _, _, host = build_deps(
        tmp_path,
        {"https://www.surmawala.pk/products/haier-inverter-split-ac": page},
        {"https://cdn.surmawala.pk/items/hsu-12hfpaa-1.jpg": png_bytes(clean_product_image())},
    )
    deps.source_searcher = FakeSourceSearcher(
        candidates=[
            PageCandidate(
                url="https://www.surmawala.pk/products/haier-inverter-split-ac",
                origin="json-search:www.surmawala.pk",
                method="json-search",
            )
        ]
    )
    result, accepted = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", ("https://www.haier.com/pk/none.shtml",)),
        deps,
        config,
    )

    assert len(accepted) == 1
    assert result.status is Status.PARTIAL                # never ok: nobody proved the page
    assert "UNVERIFIED" in result.note
    assert host.uploads                                   # and it did publish
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert '"rule_id": "S15.unverified"' in decisions


def test_the_unverified_marketplace_last_resort_is_off_by_default(tmp_path: Path) -> None:
    """Switched off - today's behaviour, unchanged: the same unprovable page yields nothing."""
    config = live_config(tmp_path)
    page = """<html><head><title>Haier Inverter Split Air Conditioner</title></head>
    <body><div class="product-gallery">
    <img src="https://cdn.surmawala.pk/items/hsu-12hfpaa-1.jpg"></div></body></html>"""
    deps, _, image_fetcher, host = build_deps(
        tmp_path,
        {"https://www.surmawala.pk/products/haier-inverter-split-ac": page},
        {"https://cdn.surmawala.pk/items/hsu-12hfpaa-1.jpg": png_bytes(clean_product_image())},
    )
    deps.source_searcher = FakeSourceSearcher(
        candidates=[
            PageCandidate(
                url="https://www.surmawala.pk/products/haier-inverter-split-ac",
                origin="json-search:www.surmawala.pk",
                method="json-search",
            )
        ]
    )
    result, accepted = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", ("https://www.haier.com/pk/none.shtml",)),
        deps,
        config,
    )

    assert accepted == []
    assert result.status is Status.FAILED
    assert image_fetcher.requested == []
    assert host.uploads == []


def test_stage_3d_visits_only_a_named_shop_and_keeps_the_sheet_tier(tmp_path: Path) -> None:
    """The page list *is* stage 3d's safety story: a shop the owner named, nothing else, and a
    sheet URL stays a sheet URL - so relaxed matching (which is fallback-only) can never reach
    it.  A dead host and a collection listing are dropped by the same two guards the canonical
    path uses, silently: stage 2/3 already logged that refusal for this URL."""
    from pipeline import _unverified_marketplace_pages

    config = live_config(
        tmp_path,
        last_resort_marketplace_fallback=True,
        last_resort_marketplace_hosts=("www.surmawala.pk",),
        excluded_hosts=("telex.pk",),
    )
    row = task(
        0,
        "Haier HWM 80-1217 WB",
        (
            "https://www.surmawala.pk/products/haier-washing-machine-80-1217",
            "https://telex.pk/products/haier-80-1217",
            "https://friendshome.pk/products/haier-80-1217",
        ),
    )
    searched = [
        PageCandidate(
            url="https://www.surmawala.pk/collections/washing-machines",
            origin="json-search:www.surmawala.pk",
            method="json-search",
        ),
        PageCandidate(
            url="https://www.surmawala.pk/products/haier-hwm-80-1217-wb",
            origin="json-search:www.surmawala.pk",
            method="json-search",
        ),
    ]

    assert _unverified_marketplace_pages(task=row, searched=searched, config=config) == [
        ("https://www.surmawala.pk/products/haier-washing-machine-80-1217", False),
        ("https://www.surmawala.pk/products/haier-hwm-80-1217-wb", True),
    ]


def test_the_unverified_rescue_lifts_only_a_page_that_is_silent(tmp_path: Path) -> None:
    """S15.unverified rescues a page that says nothing about the model.  A page refused for
    another colourway or another maker's product is a different product and stays refused -
    the rescue is about a code we cannot read, never about a verdict we disagree with."""
    from models import PageMatchResult
    from pipeline import _unverified_rescue

    deps, _, _, _ = build_deps(tmp_path, {}, {})
    silent = PageMatchResult(
        "https://www.surmawala.pk/products/haier-washing-machine",
        0,
        (),
        "no model key found on the page",
        "none",
        (),
    )
    rescued = _unverified_rescue(
        silent, deps=deps, title="Haier HWM 80-1217 WB", page_url=silent.url
    )
    assert rescued.match_rule == "unverified"
    assert rescued.strength == 1

    for rule in ("colour_conflict", "other_brand_product"):
        refused = replace(silent, match_rule=rule, detail=f"page shows {rule}")
        assert (
            _unverified_rescue(
                refused, deps=deps, title="Haier HWM 80-1217 WB", page_url=refused.url
            ).match_rule
            == rule
        )


def test_a_title_only_row_publishes_from_the_search_chain(tmp_path: Path) -> None:
    """A row whose URL column Step-1 never filled still gets its pictures (owner 2026-10-20:
    50 refrigerator rows written from the shops, titles only).  The title alone drives the
    search, and every page rule still runs on what the search returns."""
    config = live_config(tmp_path)
    page = """<html><head><title>Haier HSU-12HFPAA Inverter AC</title></head>
    <body><div class="product-gallery">
    <img src="https://cdn.haier.com/p/hsu-12hfpaa-1.jpg"></div></body></html>"""
    searcher = FakeSourceSearcher(
        candidates=[
            PageCandidate(
                url="https://www.surmawala.pk/products/haier-hsu-12hfpaa-inverter-ac",
                origin="json-search:www.surmawala.pk",
                method="json-search",
            )
        ],
        report=SourceSearchReport(tried=("www.surmawala.pk:shopify:1",)),
    )
    deps, _, _, host = build_deps(
        tmp_path,
        {"https://www.surmawala.pk/products/haier-hsu-12hfpaa-inverter-ac": page},
        {"https://cdn.haier.com/p/hsu-12hfpaa-1.jpg": png_bytes(clean_product_image())},
    )
    deps.source_searcher = searcher
    result, accepted = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", ()), deps, config
    )

    assert accepted                                    # it found the picture
    assert searcher.calls != []                        # and it searched from the title alone
    assert result.status in (Status.OK, Status.PARTIAL)
    assert host.uploads


def test_a_row_with_nothing_published_asks_the_rest_of_the_catalogue(tmp_path: Path) -> None:
    """Live run refrigerators_50_titles 2026-10-20 (K4): thirteen rows failed with
    ``sources tried: www.dawlance.com.pk:sitemap:0, www.surmawala.pk:shopify:3,
    modernelectronics.pk:shopify:3`` while ``japanelectronics.com.pk/products/
    dawlance-ref-9191-acce-coral-red`` sold the product - two answering shops ate the
    six-candidate budget, the chain broke, and the shop that had it was never queried.

    The widened hunt is the row's second chance: same rules per page, bigger budget."""
    config = live_config(tmp_path, fallback_max_pages_per_row_when_nothing_found=24)
    starved_url = "https://www.surmawala.pk/products/dawlance-ref-9191-acce-refrigerator"
    japan_url = "https://japanelectronics.com.pk/products/dawlance-ref-9191-acce-refrigerator"
    japan_image = "https://japanelectronics.com.pk/cdn/dawlance-ref-9191-1.jpg"
    starved = """<html><head><title>Dawlance REF-9191 ACCE Refrigerator</title></head>
    <body><p>price on request</p></body></html>"""
    japan = f"""<html><head><title>Dawlance REF-9191 ACCE Refrigerator</title></head>
    <body><div class="product-gallery"><img src="{japan_image}"></div></body></html>"""
    searcher = FakeSourceSearcher(
        candidates=[
            PageCandidate(
                url=starved_url, origin="json-search:www.surmawala.pk", method="json-search"
            )
        ],
        wide_candidates=[
            PageCandidate(
                url=starved_url, origin="json-search:www.surmawala.pk", method="json-search"
            ),
            PageCandidate(
                url=japan_url,
                origin="json-search:japanelectronics.com.pk",
                method="json-search",
            ),
        ],
        report=SourceSearchReport(tried=("www.surmawala.pk:shopify:1",)),
    )
    deps, page_fetcher, _, _ = build_deps(
        tmp_path,
        {starved_url: starved, japan_url: japan},
        {japan_image: png_bytes(clean_product_image())},
    )
    deps.source_searcher = searcher
    result, accepted = process_product(
        task(0, "Dawlance REF-9191 ACCE Refrigerator", ()), deps, config
    )

    assert any(call[2] for call in searcher.calls), "an empty row must get the widened hunt"
    assert japan_url in page_fetcher.requested
    assert accepted
    assert result.status in (Status.OK, Status.PARTIAL)


def test_a_row_the_wide_hunt_also_came_back_empty_says_it_ran_out_of_budget(
    tmp_path: Path,
) -> None:
    """F6: "not found" must mean *not available*, never "we stopped looking".  The owner reads
    this note to decide whether to paste a link, so it says what was asked and what was refused."""
    config = live_config(tmp_path, fallback_max_pages_per_row_when_nothing_found=24)
    dead_url = "https://www.surmawala.pk/products/dawlance-ref-9193-acce-refrigerator"
    page = """<html><head><title>Dawlance REF-9193 ACCE Refrigerator</title></head>
    <body><p>no photos</p></body></html>"""
    searcher = FakeSourceSearcher(
        candidates=[
            PageCandidate(url=dead_url, origin="json-search:www.surmawala.pk", method="json-search")
        ],
        report=SourceSearchReport(
            tried=("www.surmawala.pk:shopify:1", "modernelectronics.pk:shopify:0")
        ),
    )
    deps, _, _, _ = build_deps(tmp_path, {dead_url: page}, {})
    deps.source_searcher = searcher
    result, accepted = process_product(
        task(0, "Dawlance REF-9193 ACCE Refrigerator", ()), deps, config
    )

    assert not accepted
    assert "search budget ran out" in result.note
    assert "www.surmawala.pk:shopify:1" in result.note


def test_a_row_that_already_published_never_pays_for_the_wide_hunt(tmp_path: Path) -> None:
    """The widened budget is for empty rows only: a row that got its pictures from the first
    hunt costs the same requests it costs today."""
    config = live_config(tmp_path)
    page = """<html><head><title>Dawlance REF-9194 ACCE Refrigerator</title></head>
    <body><div class="product-gallery">
    <img src="https://cdn.surmawala.pk/dawlance-ref-9194-1.jpg"></div></body></html>"""
    url = "https://www.surmawala.pk/products/dawlance-ref-9194-acce-refrigerator"
    searcher = FakeSourceSearcher(
        candidates=[
            PageCandidate(url=url, origin="json-search:www.surmawala.pk", method="json-search")
        ],
        report=SourceSearchReport(tried=("www.surmawala.pk:shopify:1",)),
    )
    deps, _, _, _ = build_deps(
        tmp_path,
        {url: page},
        {"https://cdn.surmawala.pk/dawlance-ref-9194-1.jpg": png_bytes(clean_product_image())},
    )
    deps.source_searcher = searcher
    _, accepted = process_product(task(0, "Dawlance REF-9194 ACCE Refrigerator", ()), deps, config)

    assert accepted
    assert not any(call[2] for call in searcher.calls), "a row with pictures does not re-search"


def test_the_shipped_wide_budget_is_smaller_than_the_row_budget_so_no_row_pays_for_it(
    tmp_path: Path,
) -> None:
    """Owner 2026-10-20, and this is the shipped default: on refrigerators_60_titles all 14
    wide hunts crawled the 18-source catalogue and published **zero** images, so he set
    ``fallback_max_pages_per_row_when_nothing_found`` below ``fallback_max_pages_per_row``.
    A small value is a setting, never a ConfigError, and it means a failing row searches once
    - which is what a run that refuses to start, or crawls for nothing, costs him otherwise."""
    shipped = live_config(tmp_path)          # no override: the class defaults as they ship
    assert (
        shipped.fallback_max_pages_per_row_when_nothing_found
        <= shipped.fallback_max_pages_per_row
    ), "the extra hunt is off by the owner's own setting"
    dead_url = "https://www.surmawala.pk/products/dawlance-ref-9195-acce-refrigerator"
    page = """<html><head><title>Dawlance REF-9195 ACCE Refrigerator</title></head>
    <body><p>no photos</p></body></html>"""
    searcher = FakeSourceSearcher(
        candidates=[
            PageCandidate(url=dead_url, origin="json-search:www.surmawala.pk", method="json-search")
        ],
        report=SourceSearchReport(tried=("www.surmawala.pk:shopify:1",)),
    )
    deps, _, _, _ = build_deps(tmp_path, {dead_url: page}, {})
    deps.source_searcher = searcher
    result, accepted = process_product(
        task(0, "Dawlance REF-9195 ACCE Refrigerator", ()), deps, shipped
    )

    assert not accepted
    assert searcher.calls == [("Dawlance REF-9195 ACCE Refrigerator", "dawlance", False)]
    assert "search budget ran out" not in result.note


def test_a_variant_spelling_page_cannot_lend_a_file_naming_another_number(tmp_path: Path) -> None:
    """Kenwood run 2026-10-20: the `KRF-24457` rows published `22257-5.jpg`, `22257-4.jpg` and
    `23357-int.jpg` from a page that matched on a tolerated spelling - a different model number
    each time, and S4 could not see them because those numbers belong to no row of that sheet.
    A file that writes a number at all must write THIS row's number."""
    config = live_config(tmp_path)
    page = """<html><head><title>Homage 15 CFT Top Mount Inverter Refrigerator HR-47452 TECH R-310</title></head>
    <body><div class="product-gallery">
    <img src="https://cdn.shopify.com/s/files/22257-5.jpg">
    <img src="https://cdn.shopify.com/s/files/hr-47452-bkg-front.jpg"></div></body></html>"""
    deps, _, image_fetcher, host = build_deps(
        tmp_path,
        {"https://www.alfatah.com.pk/product/homage-hr-47452-tech-r-310/": page},
        {
            "https://cdn.shopify.com/s/files/22257-5.jpg": png_bytes(
                clean_product_image(1000, 1000, "#1b1b1b")
            ),
            "https://cdn.shopify.com/s/files/hr-47452-bkg-front.jpg": png_bytes(
                clean_product_image(900, 900, "#1b1b1b")
            ),
        },
    )
    result, accepted = process_product(
        task(
            0,
            "Homage HR-47452 BKG Tech Inverter 15 Cu Ft Glass Door Direct Cool Inverter "
            "Refrigerator - Black",
            ("https://www.alfatah.com.pk/product/homage-hr-47452-tech-r-310/",),
        ),
        deps,
        config,
    )

    rules = [
        json.loads(line)["rule_id"]
        for line in (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "S15.file_code" in rules
    assert all("22257" not in a.source_url for a in accepted)
    assert not any("22257" in u for u in image_fetcher.requested)   # refused before any download
    assert any(a.source_url.endswith("hr-47452-bkg-front.jpg") for a in accepted)
    assert result.status in (Status.OK, Status.PARTIAL)
    assert host.uploads


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
            platform_urls=(
                ("digi", ("https://i.example/1.jpg", "https://i.example/banner.jpg")),
                ("alfa", ("https://a.example/1.jpg", "https://a.example/banner.jpg")),
            ),
        ),
        ProductResult(task_index=1, title="Bad Row", status=Status.FAILED, reason="no page matched"),
    ]
    write_output_csvs(results, config, _plan(tmp_path))
    readme = (config.output_dir / "README.md").read_text(encoding="utf-8")
    assert "rows: 2 | ok: 1 | partial: 0 | failed: 1" in readme
    assert "pictures published: Alfamall 1, Digimall 1" in readme  # banner uncounted
    assert "digi/digimall_product_images.csv" in readme
    assert "images/alfa/<product>/" in readme


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
            platform_urls=(
                ("digi", ("https://i.example/1.jpg", "https://i.example/2.jpg")),
                ("alfa", ("https://a.example/1.jpg", "https://a.example/2.jpg")),
            ),
            name_prefix="HSU-12HFPAA",
        )
    ]
    write_output_csvs(results, config, _plan(tmp_path))
    lines = (config.output_dir / "digi" / "digimall_product_images.csv").read_text(
        encoding="utf-8"
    ).strip().splitlines()
    assert lines[0] == "Title,Brand,Image_Urls,images"
    assert lines[1] == (
        'Haier HSU-12HFPAA Inverter AC,Haier,https://i.example/1.jpg|https://i.example/2.jpg,'
        '"HSU-12HFPAA_1.jpg, HSU-12HFPAA_2.jpg"'
    )
    assert " " not in lines[1].split(",")[2]
    alfa = (config.output_dir / "alfa" / "alfamall_product_images.csv").read_text(
        encoding="utf-8"
    ).strip().splitlines()
    assert alfa[0] == "Title,Brand,Image_Urls,images"
    assert alfa[1].endswith('"HSU-12HFPAA_1.jpg, HSU-12HFPAA_2.jpg"')


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
    digi = deps.platforms.by_key("digi")
    assert digi is not None and digi.banner_path is not None
    banner = digi.banner_path

    dry_deps = replace(deps, image_host=DryRunHost(config.output_dir))
    dry_url = ensure_banner_url(replace(config, dry_run=True), dry_deps, "digimall", banner)
    assert dry_url.startswith("file://")
    assert len(cache_shared) == 1

    live_url = ensure_banner_url(config, deps, "digimall", banner)   # same cache, live mode
    assert live_url.startswith("https://"), "a dry-run banner URL leaked into a live run"
    assert live_url != dry_url
    assert len(cache_shared) == 2                               # both modes cached separately

    assert ensure_banner_url(config, deps, "digimall", banner) == live_url  # live entry is reused


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
            final_paths=(("digi", image_file), ("alfa", image_file)),
        )
    ]
    result = ProductResult(task_index=0, title="Haier HSU-12HFPAA Inverter AC", status=Status.OK)
    keys = ModelKeys(
        cleaned_title="Haier HSU-12HFPAA Inverter AC",
        tokens=("HSU-12HFPAA",),
        title_key="haier",
    )
    final = finalize_and_upload(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)),
        result,
        accepted,
        deps,
        config,
        keys,
    )
    assert final.status is Status.FAILED
    assert final.platform_urls == ()
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
    write_output_csvs(results, config, deps.platforms)
    digi_sheet = config.output_dir / "digi" / "digimall_product_images.csv"
    first_line = digi_sheet.read_text(encoding="utf-8").splitlines()[1]
    assert first_line.split(",")[1] == "Haier"

    # a second run against the same checkpoint: nothing is reprocessed
    checkpoint = load_checkpoint(config.checkpoint_path)
    assert checkpoint  # the first run wrote it
    resumed = BatchRunner(config, deps, tasks, checkpoint).run()
    assert resumed[0].note == "resumed from checkpoint"
    assert resumed[0].brand == "Haier", "the brand was lost on resume"
    write_output_csvs(resumed, config, deps.platforms)
    again = digi_sheet.read_text(encoding="utf-8").splitlines()[1]
    assert again.split(",")[1] == "Haier", f"Brand column empty after resume: {again}"


def test_an_old_checkpoint_without_a_brand_still_loads(tmp_path: Path) -> None:
    """Checkpoints written before the brand field existed must not break resume.

    The entry below is layout 2 with no ``brand`` key - exactly what a run before that
    field wrote - so it must still resume, and the brand must come back from the title.
    """
    config = live_config(tmp_path)
    entry = CheckpointEntry.from_dict(
        {
            "layout": CHECKPOINT_LAYOUT,
            "title": "Haier HSU-12HFPAA Inverter AC",
            "source_url_hash": hash_source_urls((HAIER_PAGE,)),
            "status": "ok",
            "platform_urls": [["digi", ["https://i.example/1.jpg"]],
                              ["alfa", ["https://a.example/1.jpg"]]],
            "reason": "",
            "timestamp": "2026-10-06T00:00:00+00:00",
        }
    )
    assert entry.brand == ""
    tasks = [task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,))]
    deps, _, _, _ = build_deps(tmp_path, {}, {})
    results = BatchRunner(config, deps, tasks, {"haier hsu-12hfpaa inverter ac": entry}).run()
    assert results[0].note == "resumed from checkpoint"
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


def test_shopify_product_json_tops_up_when_html_has_junk_images(tmp_path: Path) -> None:
    """When HTML contains 1 product image plus junk (logo, social icon), candidate count
    is 3. Without pre-filtering, target=3 would falsely skip JSON fetch.
    With pre-filtering, junk is excluded from usable count, so JSON is fetched."""
    config = live_config(tmp_path, target_images_per_product=3)
    page = (
        "<html><head><title>Dawlance Semi Automatic Washing Machine DW-7500C - 10Kg</title>"
        "<script>var t='cdn.shopify.com'</script></head><body>"
        "<div class='product-gallery'>"
        "<img src='https://cdn.shopify.com/s/files/1/0829/files/7500-c.png?v=1'>"
        "</div>"
        "<img src='https://cdn.shopify.com/s/files/1/0829/files/SurmawalaLogo-504w.png'>"
        "<a href='https://tiktok.com/@surmawala'>"
        "<img src='https://cdn.shopify.com/s/files/1/0829/files/tiktokicon.png'>"
        "</a>"
        "</body></html>"
    )
    product_json = json.dumps({"product": {"images": [
        {"src": "https://cdn.shopify.com/s/files/1/0829/files/7500-c.png?v=1"},
        {"src": "https://cdn.shopify.com/s/files/1/0829/files/7500-c-side.png?v=2"},
        {"src": "https://cdn.shopify.com/s/files/1/0829/files/7500-c-top.png?v=3"},
    ]}})
    files = "https://cdn.shopify.com/s/files/1/0829/files"
    images = {
        f"{files}/7500-c.png": png_bytes(_gallery_image(1)),
        f"{files}/7500-c-side.png": png_bytes(_gallery_image(2)),
        f"{files}/7500-c-top.png": png_bytes(_gallery_image(3)),
    }
    deps, pages, _, _ = build_deps(
        tmp_path, {SHOPIFY_PAGE: page, f"{SHOPIFY_PAGE}.json": product_json}, images
    )
    result, accepted = process_product(
        task(0, SHOPIFY_TITLE, (SHOPIFY_PAGE,)), deps, config
    )
    assert f"{SHOPIFY_PAGE}.json" in pages.requested, "the product JSON must be fetched despite junk HTML candidates"
    assert result.images_accepted == 3
    assert len(accepted) == 3
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
    write_output_csvs([result], config, _plan(tmp_path))
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
# tail-chunk last resort (live run website_ac_clean 2026-10-15: mabdullah sells the
# MSEF1C-18HRFN1-QC6 row as "18HRFN1 QC6 … GO EASY 1.5 Ton" and every page died)
# --------------------------------------------------------------------------- #
TAIL_TITLE = "Midea 1.5 Ton DC Inverter Air Conditioner MSEF1C-18HRFN1-QC6 Go Easy"
TAIL_SHEET_URL = "https://www.mabdullah.pk/products/midea-18hrfn1-qc6-1-5-ton-go-easy-t3-inverter-ac"


def _tail_page() -> str:
    return (
        "<html><head><title>Midea 18HRFN1-QC6 1.5 Ton GO EASY T3 Inverter Split AC"
        "</title></head><body><h1>Midea 18HRFN1 QC6 1.5 Ton GO EASY</h1>"
        "<div class='product-gallery'>"
        + "".join(f'<img src="https://cdn.mabdullah.pk/p/goeasy-{i}.jpg">' for i in range(1, 4))
        + "</div></body></html>"
    )


def _tail_images() -> dict[str, bytes]:
    return {
        f"https://cdn.mabdullah.pk/p/goeasy-{i}.jpg": png_bytes(_gallery_image(i))
        for i in range(1, 4)
    }


def test_a_tail_printing_sheet_page_publishes_flagged_as_last_resort(tmp_path: Path) -> None:
    """The complete code tail + the sheet's own series word + the row's tonnage is
    enough to publish - but never silently: the row is PARTIAL, the decision line
    names the rule, and the review CSV carries it for the human pass."""
    config = live_config(tmp_path)
    deps, _, _, _ = build_deps(tmp_path, {TAIL_SHEET_URL: _tail_page()}, _tail_images())
    result, accepted = process_product(
        task(0, TAIL_TITLE, (TAIL_SHEET_URL,), tail_owner=1), deps, config
    )
    assert result.images_accepted == 3 and len(accepted) == 3
    assert result.status is Status.PARTIAL, "a tail-chosen page is never silently ok"
    assert "flagged: tail-chunk match" in result.note
    decisions = [
        json.loads(line) for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    assert any(line["rule_id"] == "S1.tail" for line in decisions)
    write_output_csvs([result], config, _plan(tmp_path))
    review = (config.review_csv).read_text(encoding="utf-8")
    assert TAIL_TITLE in review and "tail-chunk match" in review


def test_the_sheet_knows_another_rows_series_and_the_tail_page_dies(tmp_path: Path) -> None:
    """Wiring, not arithmetic: the limb must be handed the sheet's own verdict about the
    other rows.  A page printing our tail and our series word but *named* as another
    row's product in its ``<h1>`` (live run 2026-10-15: the same ``18HRFN1 QC6`` tail sits
    on Midea's Xtreme and Titan lines) is a different product's picture, so the row stays
    dead instead of publishing a flagged wrong series."""
    config = live_config(tmp_path)
    xtreme_page = (
        "<html><head><title>Midea 18HRFN1-QC6 1.5 Ton GO EASY T3 Inverter Split AC"
        "</title></head><body><h1>Midea 18HRFN1 QC6 1.5 Ton Xtreme</h1>"
        "<div class='product-gallery'>"
        + "".join(f'<img src="https://cdn.mabdullah.pk/p/goeasy-{i}.jpg">' for i in range(1, 4))
        + "</div></body></html>"
    )
    deps, _, _, _ = build_deps(tmp_path, {TAIL_SHEET_URL: xtreme_page}, _tail_images())
    result, accepted = process_product(
        task(0, TAIL_TITLE, (TAIL_SHEET_URL,), tail_owner=1, siblings=frozenset({"xtreme"})),
        deps,
        config,
    )
    assert accepted == [] and result.status is Status.FAILED


def _shade_page(shade: str) -> str:
    """A product page whose own name sells the ``shade`` Red, with three gallery shots."""
    return (
        f"<html><head><title>Dawlance 9140-WB-GD Avante {shade} Red Refrigerator</title></head>"
        f"<body><h1>Dawlance Avante {shade} Red</h1><div class='product-gallery'>"
        + "".join(
            f'<img src="https://cdn.shop.pk/p/9140-wb-gd-{shade.lower()}-{i}.jpg">'
            for i in range(1, 4)
        )
        + "</div></body></html>"
    )


def test_a_row_naming_no_shade_publishes_one_shade_not_a_mix(tmp_path: Path) -> None:
    """9140-WB-GD is sold as Pearl Red and Ruby Red: a row that says only "Red" gets one
    shade's pictures, never a gallery that mixes the two (owner 2026-10-16)."""
    from model_matching import set_colourway_words

    row = "Dawlance Avante Freezer-On-Top Refrigerator 9 Cu Ft Red (9140-WB-GD)"
    pearl = "https://www.surmawala.pk/dawlance-9140-wb-gd-avante-refrigerator"
    ruby = "https://www.friendshome.pk/dawlance-9140-wb-gd-avante-refrigerator"
    images = {
        f"https://cdn.shop.pk/p/9140-wb-gd-{shade}-{i}.jpg": png_bytes(_gallery_image(i))
        for shade in ("pearl", "ruby")
        for i in range(1, 4)
    }
    config = live_config(tmp_path, target_images_per_product=4)
    set_colourway_words({"ruby"})
    try:
        deps, _, _, _ = build_deps(
            tmp_path, {pearl: _shade_page("Pearl"), ruby: _shade_page("Ruby")}, images
        )
        result, accepted = process_product(task(0, row, (pearl, ruby)), deps, config)
    finally:
        set_colourway_words(())
    assert result.images_accepted == 3 and len(accepted) == 3, "one shade's gallery only"
    decisions = [
        json.loads(line) for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    blocked = [line for line in decisions if line["rule_id"] == "S20.mixed-shade"]
    assert [line["url"] for line in blocked] == [ruby]


def test_a_page_that_names_no_shade_never_locks_the_gallery(tmp_path: Path) -> None:
    """The fail-open half: a terse page is not refused for want of a shade, and it cannot
    decide the gallery's shade either - omission stays no opinion."""
    from model_matching import set_colourway_words

    row = "Dawlance Avante Freezer-On-Top Refrigerator 9 Cu Ft Red (9140-WB-GD)"
    shaded = "https://www.surmawala.pk/dawlance-9140-wb-gd-avante-refrigerator"
    terse = "https://www.friendshome.pk/dawlance-9140-wb-gd-avante-refrigerator"
    terse_page = (
        "<html><head><title>Dawlance 9140-WB-GD Avante Refrigerator</title></head>"
        "<body><h1>Dawlance Avante Refrigerator</h1><div class='product-gallery'>"
        "<img src='https://cdn.shop.pk/p/9140-wb-gd-1.jpg'></div></body></html>"
    )
    config = live_config(tmp_path)
    set_colourway_words({"ruby"})
    try:
        deps, _, _, _ = build_deps(
            tmp_path,
            {shaded: _shade_page("Ruby"), terse: terse_page},
            {
                **{
                    f"https://cdn.shop.pk/p/9140-wb-gd-ruby-{i}.jpg": png_bytes(_gallery_image(i))
                    for i in range(1, 4)
                },
                "https://cdn.shop.pk/p/9140-wb-gd-1.jpg": png_bytes(_gallery_image(1)),
            },
        )
        result, _ = process_product(task(0, row, (shaded, terse)), deps, config)
    finally:
        set_colourway_words(())
    decisions = [
        json.loads(line) for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    assert not any(
        line["url"] == terse and line["rule_id"] == "S20.mixed-shade" for line in decisions
    ), "the page that named no shade was refused for that"
    assert result.images_accepted >= 3


def test_a_tail_page_never_preempts_a_page_that_prints_the_model(tmp_path: Path) -> None:
    """The limb is a dead-end rescue only: a page printing the exact code still wins,
    and the tail page never enters the report."""
    config = live_config(tmp_path)
    exact_url = "https://www.dawlance-official.example/msef1c-18hrfn1-qc6"
    exact_page = (
        "<html><head><title>Midea MSEF1C-18HRFN1-QC6 Go Easy 1.5 Ton</title></head><body>"
        "<h1>Midea MSEF1C-18HRFN1-QC6 Go Easy</h1><div class='product-gallery'>"
        + "".join(f'<img src="https://cdn.mabdullah.pk/p/goeasy-{i}.jpg">' for i in range(1, 4))
        + "</div></body></html>"
    )
    deps, pages, _, _ = build_deps(
        tmp_path, {exact_url: exact_page, TAIL_SHEET_URL: _tail_page()}, _tail_images()
    )
    result, accepted = process_product(
        task(0, TAIL_TITLE, (exact_url, TAIL_SHEET_URL), tail_owner=1), deps, config
    )
    assert len(accepted) == 3
    assert "tail-chunk" not in result.note
    assert TAIL_SHEET_URL not in " ".join(pages.requested) or result.status is Status.OK


def test_the_tail_limb_is_inert_without_one_unique_tail_on_the_sheet(tmp_path: Path) -> None:
    """``tail_owner_count`` is the sheet's verdict: zero (not computed) or several
    (ambiguous) rows and the limb never fires - the row stays dead and loud."""
    config = live_config(tmp_path)
    deps, _, _, _ = build_deps(tmp_path, {TAIL_SHEET_URL: _tail_page()}, _tail_images())
    result, accepted = process_product(
        task(0, TAIL_TITLE, (TAIL_SHEET_URL,)), deps, config
    )
    assert accepted == []
    assert result.status is Status.FAILED


def test_the_tail_limb_can_be_switched_off(tmp_path: Path) -> None:
    config = live_config(tmp_path, tail_chunk_match_enabled=False)
    deps, _, _, _ = build_deps(tmp_path, {TAIL_SHEET_URL: _tail_page()}, _tail_images())
    result, accepted = process_product(
        task(0, TAIL_TITLE, (TAIL_SHEET_URL,), tail_owner=1), deps, config
    )
    assert accepted == []
    assert result.status is Status.FAILED


def test_a_tail_page_on_the_fallback_tier_is_flagged_s15(tmp_path: Path) -> None:
    """The same last resort reached through the retailer *search* tier is logged as
    S15.tail, not S1.tail: the tier a page came from decides the rule id, so the
    review report shows where the guess happened."""
    config = live_config(tmp_path)
    dead = "https://www.mideapakistan.store/products/msef1c-18hrfn1-qc6"
    searcher = FakeSourceSearcher(
        candidates=[
            PageCandidate(
                url=TAIL_SHEET_URL,
                origin="json-search:www.mabdullah.pk",
                method="json-search",
            )
        ],
        report=SourceSearchReport(tried=("www.mabdullah.pk:shopify:1",)),
    )
    deps, _, _, _ = build_deps(
        tmp_path, {dead: "", TAIL_SHEET_URL: _tail_page()}, _tail_images()
    )
    deps.source_searcher = searcher
    result, accepted = process_product(
        task(0, TAIL_TITLE, (dead,), tail_owner=1), deps, config
    )
    assert result.status is Status.PARTIAL
    assert "flagged: tail-chunk match" in result.note
    decisions = [
        json.loads(line)
        for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    assert any(line["rule_id"] == "S15.tail" for line in decisions)


def test_a_page_naming_another_brand_is_never_rescued_by_the_tail(tmp_path: Path) -> None:
    """A retailer page carrying our exact tail, series word and tonnage but naming
    ANOTHER maker is refused on the brand (``match_rule`` ``other_brand_product``).
    The tail limb only fills a *missing* model proof - it must not overturn that verdict
    and publish a wrong-brand picture (owner rule: the brand must match, by any means)."""
    config = live_config(tmp_path)
    other_brand_page = (
        "<html><head><title>Haier 18HRFN1-QC6 1.5 Ton GO EASY T3 Inverter Split AC"
        "</title></head><body><h1>Haier 18HRFN1 QC6 1.5 Ton GO EASY</h1>"
        "<div class='product-gallery'>"
        + "".join(f'<img src="https://cdn.mabdullah.pk/p/goeasy-{i}.jpg">' for i in range(1, 4))
        + "</div></body></html>"
    )
    deps, _, _, _ = build_deps(tmp_path, {TAIL_SHEET_URL: other_brand_page}, _tail_images())
    result, accepted = process_product(
        task(0, TAIL_TITLE, (TAIL_SHEET_URL,), tail_owner=1), deps, config
    )
    assert accepted == []
    assert result.status is Status.FAILED
    decisions = [
        json.loads(line)
        for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    assert not any(line["rule_id"] in ("S1.tail", "S15.tail") for line in decisions)


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
    write_output_csvs([result], config, _plan(tmp_path))
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


def _catalog_artwork(*, offset: float = 0.0, scale: float = 0.5, open_lid: bool = False) -> Image.Image:
    """One product photographed for a catalog: body, tub, control strip - served with the
    shop's own padding (``offset`` moves it off-centre, the way two shops crop one file)."""
    image = Image.new("RGB", (900, 900), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    width = int(900 * scale)
    left = int((900 - width) * 0.5 + 900 * offset)
    top = int((900 - width) * 0.5)
    draw.rounded_rectangle((left, top, left + width, top + width), radius=22, fill="#2f5fa8")
    draw.ellipse((left + width * 0.18, top + width * 0.18, left + width * 0.82, top + width * 0.55),
                 fill="#dff0ff")
    draw.rectangle((left + width * 0.1, top + width * 0.72, left + width * 0.9, top + width * 0.82),
                   fill="#123a6b")
    if open_lid:
        draw.rectangle((left + width * 0.15, top - width * 0.16, left + width * 0.85, top + width * 0.05),
                       fill="#e8eef7")
    else:
        draw.rectangle((left + width * 0.15, top + width * 0.02, left + width * 0.85, top + width * 0.10),
                       fill="#123a6b")
    return image


def _twin_tub_page(image_urls: tuple[str, ...]) -> str:
    """One shop's product page: the model in its title and heading, gallery images as given."""
    images = "".join(f'<img src="{url}">' for url in image_urls)
    return (
        "<html><head><title>Haier HTW-100-196S</title></head>"
        "<body><h1>Haier HTW-100-196S</h1>"
        f"<div class='product-gallery'>{images}</div></body></html>"
    )


def test_a_rehosted_copy_is_folded_on_the_picture_we_publish(tmp_path: Path) -> None:
    """Owner, 2026-10-17: the washing-machine run published the same machine twice, because
    two shops serve the same artwork with different padding.  S10 already catches that pair -
    measured on the live files, dHash 3 and correlation 0.9972 - but it ran on the **raw**
    download, where the same pair sits out of reach at dHash 9 / 0.9922.  The picture that
    ships is the isolated square, so that is the picture the dedup must judge."""
    config = live_config(tmp_path)
    wide = "https://friendshome.example/cdn/shop/files/HTW100-196S.jpg"
    tight = "https://surmawala.example/cdn/shop/files/haier-100-196s.jpg"
    page_a = "https://friendshome.example/products/haier-htw-100-196s"
    page_b = "https://surmawala.example/products/haier-htw-100-196s"

    deps, _, _, _ = build_deps(
        tmp_path,
        {page_a: _twin_tub_page((wide,)), page_b: _twin_tub_page((tight,))},
        {
            wide: png_bytes(_catalog_artwork()),
            tight: png_bytes(_catalog_artwork(offset=0.18)),
        },
    )
    result, accepted = process_product(
        task(0, "Haier HTW-100-196S Twin Tub Washer", (page_a, page_b)), deps, config
    )

    assert len(accepted) == 1, "the same picture shipped twice - S10 judged the raw bytes"
    assert accepted[0].source_url == wide
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert '"rule_id": "S10"' in decisions                       # folded, and it is on the record
    assert "re-served copy of accepted image #1" in decisions    # the hard branch, like the live pair
    assert result.status is not Status.OK                        # one picture is not a full album


def test_two_genuine_angles_survive_the_second_look(tmp_path: Path) -> None:
    """The danger this rule cannot be allowed to create: a lid open and a lid shut are two
    pictures a shop pays for, and they are the same product.  ``dedup.duplicate_reason``
    documents a protected pair that is *closer* on raw measurements than some real
    duplicates, so the second look is only safe while isolation keeps them apart - this is
    that guarantee, pinned."""
    config = live_config(tmp_path)
    shut = "https://cdn.shop.example/p/htw-100-196s-1.jpg"
    open_url = "https://cdn.shop.example/p/htw-100-196s-2.jpg"
    page = "https://shop.example/products/haier-htw-100-196s"

    deps, _, _, _ = build_deps(
        tmp_path,
        {page: _twin_tub_page((shut, open_url))},
        {
            shut: png_bytes(_catalog_artwork()),
            open_url: png_bytes(_catalog_artwork(open_lid=True)),
        },
    )
    _, accepted = process_product(
        task(0, "Haier HTW-100-196S Twin Tub Washer", (page,)), deps, config
    )

    assert len(accepted) == 2, "the second angle was folded - the album lost a real picture"


def _front_load_page_for(model: str, image_urls: tuple[str, ...]) -> str:
    """A shop page whose title and heading prove one model, carrying the gallery given."""
    images = "".join(f'<img src="{url}">' for url in image_urls)
    return (
        f"<html><head><title>{model}</title></head>"
        f"<body><h1>{model}</h1>"
        f"<div class='product-gallery'>{images}</div></body></html>"
    )


def _front_load_page(image_urls: tuple[str, ...]) -> str:
    """A shop page proven to be the 13 kg row, carrying the gallery the owner found."""
    return _front_load_page_for("Haier 1301217E", image_urls)


def test_the_sibling_capacity_of_the_row_is_refused_as_s4_code(tmp_path: Path) -> None:
    """Owner, 2026-10-17: the 13 kg "1301217E" row published the 8 kg "hwm80-1217" twice.
    The digit limb must stop it at the URL, before a byte is fetched, and say which rule
    refused it on the record."""
    config = live_config(tmp_path)
    own = "https://shop.example/cdn/files/haier-130-1217E.jpg"
    sibling = "https://shop.example/cdn/files/haier-hwm80-1217-washing-machine.jpg"
    page = "https://shop.example/products/haier-1301217e"
    deps, _, images_fetched, _ = build_deps(
        tmp_path,
        {page: _front_load_page((own, sibling))},
        {
            own: png_bytes(_catalog_artwork()),
            sibling: png_bytes(_catalog_artwork(offset=-0.3, scale=0.42)),
        },
    )
    _, accepted = process_product(
        task(0, "Haier 1301217E Front Load Washer", (page,)), deps, config
    )

    assert [image.source_url for image in accepted] == [own]
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert '"rule_id": "S4.code"' in decisions
    assert "another capacity of this number family" in decisions
    assert sibling not in images_fetched.requested


def test_the_sibling_refusal_runs_only_while_the_switch_is_on(tmp_path: Path) -> None:
    """One switch, one behaviour: with ``model_code_shape_gate=False`` the row reads exactly
    as it did before this rule existed."""
    config = replace(live_config(tmp_path), model_code_shape_gate=False)
    own = "https://shop.example/cdn/files/haier-130-1217E.jpg"
    sibling = "https://shop.example/cdn/files/haier-hwm80-1217-washing-machine.jpg"
    page = "https://shop.example/products/haier-1301217e"
    deps, _, _, _ = build_deps(
        tmp_path,
        {page: _front_load_page((own, sibling))},
        {
            own: png_bytes(_catalog_artwork()),
            sibling: png_bytes(_catalog_artwork(offset=-0.3, scale=0.42)),
        },
    )
    process_product(task(0, "Haier 1301217E Front Load Washer", (page,)), deps, config)

    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert '"rule_id": "S4.code"' not in decisions


def _album_shot(body: tuple[int, int, int], dial_x: int) -> Image.Image:
    """One catalog photo: an appliance body on studio white, with the dial moved so the
    album's own dedup keeps the shots apart."""
    image = Image.new("RGB", (900, 900), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((200, 130, 700, 780), radius=24, fill=body)
    draw.ellipse((dial_x, 170, dial_x + 90, 260), fill=(70, 70, 78))
    strip = tuple(max(0, channel - 25) for channel in body)
    draw.rectangle((250, 640, 650, 720), fill=strip)
    return image


_WHITE_BLACK = (150, 152, 158)     # one spinner: a grey body with a dark tub band
_BEIGE_BLUE = (222, 205, 170)      # the other variant the same gallery served


def test_an_album_holding_two_bodies_is_flagged_in_the_note(tmp_path: Path) -> None:
    """Owner, 2026-10-17 on `haier-6050`: "look pictures 1 to 3 then 4-5 entirely different,
    what logic is this".  The pipeline cannot delete the minority without deleting correct
    pictures elsewhere (measured: a single detail shot spans the same distance), so the row
    says it out loud in the report and leaves the call to the owner."""
    config = replace(
        live_config(tmp_path), target_images_per_product=5, max_images_per_product=5
    )
    urls = [f"https://shop.example/cdn/files/haier-6050-{index}.jpg" for index in range(1, 6)]
    page = "https://shop.example/products/haier-6050"
    shots = (
        (_WHITE_BLACK, 260), (_WHITE_BLACK, 330), (_WHITE_BLACK, 400),
        (_BEIGE_BLUE, 260), (_BEIGE_BLUE, 380),
    )
    deps, _, _, _ = build_deps(
        tmp_path,
        {page: _front_load_page_for("Haier 6050", tuple(urls))},
        {url: png_bytes(_album_shot(*shot)) for url, shot in zip(urls, shots, strict=True)},
    )
    result, accepted = process_product(
        task(0, "Haier 6050 Top Load Washer", (page,)), deps, config
    )

    assert len(accepted) >= 4, [image.source_url for image in accepted]
    assert "two different-looking bodies" in result.note
    assert result.status is Status.OK                       # flagged, never failed


def test_the_coherence_note_can_be_switched_off(tmp_path: Path) -> None:
    config = replace(
        live_config(tmp_path),
        target_images_per_product=5,
        max_images_per_product=5,
        album_coherence_note_enabled=False,
    )
    urls = [f"https://shop.example/cdn/files/haier-6050-{index}.jpg" for index in range(1, 6)]
    page = "https://shop.example/products/haier-6050"
    shots = (
        (_WHITE_BLACK, 260), (_WHITE_BLACK, 330), (_WHITE_BLACK, 400),
        (_BEIGE_BLUE, 260), (_BEIGE_BLUE, 380),
    )
    deps, _, _, _ = build_deps(
        tmp_path,
        {page: _front_load_page_for("Haier 6050", tuple(urls))},
        {url: png_bytes(_album_shot(*shot)) for url, shot in zip(urls, shots, strict=True)},
    )
    result, accepted = process_product(
        task(0, "Haier 6050 Top Load Washer", (page,)), deps, config
    )

    assert len(accepted) >= 4
    assert "two different-looking bodies" not in result.note


def test_a_second_shop_s_gallery_joins_only_file_by_file(tmp_path: Path) -> None:
    """The row's first picture settles the home even when that page carries one stray file.
    A second shop is then a stranger like any other: each of its files is admitted by its
    **own** name (`haier-130-1217E-1.jpg` proves the row; a nameless file from the same page
    would not) and only while the row is still short of `min_required_images`.  Nothing here
    needs the page's size, which is why `album_lock_min_curated` no longer exists."""
    config = live_config(tmp_path)
    stray = "https://cdn.friendshome.example/files/haier-130-1217E-hero.jpg"
    gallery = (
        "https://cdn.surmawala.example/files/haier-130-1217E-1.jpg",
        "https://cdn.surmawala.example/files/haier-130-1217E-2.jpg",
    )
    page_a = "https://friendshome.example/products/haier-1301217e"
    page_b = "https://surmawala.example/products/haier-1301217e"
    deps, _, _, _ = build_deps(
        tmp_path,
        {
            page_a: _front_load_page_for("Haier 1301217E", (stray,)),
            page_b: _front_load_page_for("Haier 1301217E", gallery),
        },
        {
            stray: png_bytes(_catalog_artwork()),
            gallery[0]: png_bytes(_catalog_artwork(open_lid=True)),
            gallery[1]: png_bytes(_catalog_artwork(offset=-0.3, scale=0.42)),
        },
    )
    _, accepted = process_product(
        task(0, "Haier 1301217E Front Load Washer", (page_a, page_b)), deps, config
    )

    assert [image.source_url for image in accepted] == [stray, gallery[0]]
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    # The row reached min_required_images with one proven stranger, so the second one is
    # refused: the top-up door fills a short row, it does not import a whole gallery.
    assert '"rule_id": "S22.source_lock"' in decisions
    assert "album belongs to friendshome.example" in decisions


def test_the_album_home_is_the_page_host_not_the_image_cdn(tmp_path: Path) -> None:
    """Two shops, one CDN.  `cdn.shopify.com` serves thousands of stores, so keying the home
    on the *image* host would let any Shopify shop into a locked gallery - the page is the
    source, exactly as the owner framed it ("one source, not one here and one from other")."""
    config = live_config(tmp_path)
    shared_cdn = "https://cdn.shopify.com/files"
    home_one = f"{shared_cdn}/haier-130-1217E-1.jpg"
    home_two = f"{shared_cdn}/haier-130-1217E-2.jpg"
    other = f"{shared_cdn}/haier-130-1217E-3.jpg"
    page_a = "https://friendshome.example/products/haier-1301217e"
    page_b = "https://traders.example/products/haier-1301217e"
    deps, _, _, _ = build_deps(
        tmp_path,
        {
            page_a: _front_load_page_for("Haier 1301217E", (home_one, home_two)),
            page_b: _front_load_page_for("Haier 1301217E", (other,)),
        },
        {
            home_one: png_bytes(_catalog_artwork()),
            home_two: png_bytes(_catalog_artwork(open_lid=True)),
            other: png_bytes(_catalog_artwork(offset=-0.3, scale=0.42)),
        },
    )
    _, accepted = process_product(
        task(0, "Haier 1301217E Front Load Washer", (page_a, page_b)), deps, config
    )

    assert [image.source_url for image in accepted] == [home_one, home_two]
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert '"rule_id": "S22.source_lock"' in decisions
    assert "album belongs to friendshome.example" in decisions
    assert other in decisions                   # the same CDN host is no ticket in


def test_the_last_resort_never_breaks_the_album(tmp_path: Path) -> None:
    """Stage 3c publishes below-floor pictures when a row is short - and it is still an
    album rule.  The trap is the *order*: shop A is visited while no home exists yet, so its
    below-floor photo becomes an offer; shop B's gallery then becomes the home; and the last
    resort must not slip A's picture in afterwards, because a second shop's reject is
    exactly the mixed gallery S22 exists to prevent (owner 2026-10-07: one correct picture
    beats a bigger mixed one)."""
    small = _jpeg_bytes(clean_product_image(700, 700, "#d4af37"))
    # min_lossy_image_bytes puts this payload below the byte floor; salvage_min_sharpness=0.0
    # then closes the "a sharp below-floor file is good enough" door (that branch needs a
    # positive bar), so it really becomes a last-resort offer and stage 3c runs.  With
    # min_required_images=3 two published pictures still leave the row short.
    config = live_config(
        tmp_path,
        min_lossy_image_bytes=len(small) + 2000,
        salvage_min_sharpness=0.0,
        min_required_images=3,
    )
    offer = "https://cdn.traders.example/files/product-photo-9.jpg"     # names no code
    home = (
        "https://cdn.friendshome.example/files/haier-130-1217E-1.jpg",
        "https://cdn.friendshome.example/files/haier-130-1217E-2.jpg",
    )
    page_a = "https://traders.example/products/haier-1301217e"
    page_b = "https://friendshome.example/products/haier-1301217e"
    deps, _, _, _ = build_deps(
        tmp_path,
        {
            page_a: _front_load_page_for("Haier 1301217E", (offer,)),
            page_b: _front_load_page_for("Haier 1301217E", home),
        },
        {
            offer: small,
            home[0]: png_bytes(_catalog_artwork()),
            home[1]: png_bytes(_catalog_artwork(open_lid=True)),
        },
    )
    result, accepted = process_product(
        task(0, "Haier 1301217E Front Load Washer", (page_a, page_b)), deps, config
    )

    assert [image.source_url for image in accepted] == list(home)
    assert all(image.page_url == page_b for image in accepted)   # every picture names its home
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert '"rule_id": "S22.source_lock"' in decisions
    assert "last-resort picture offered by another source" in decisions
    assert result.status is Status.PARTIAL       # 2 of the 3 this config now asks for


def test_a_note_that_cannot_tighten_its_groups_is_refused_as_a_config_error(tmp_path: Path) -> None:
    """One config field per threshold, and the pair has to be able to agree: with a group
    allowed to be as wide as the gap that separates two groups, no album can ever qualify and
    S22.coherence is silently dead.  ``__post_init__`` says so instead."""
    with pytest.raises(ConfigError, match="below album_coherence_value_gap"):
        live_config(
            tmp_path,
            album_coherence_group_spread_max=0.30,
            album_coherence_value_gap=0.18,
        )


def test_a_missing_square_stops_the_note_not_the_row(tmp_path: Path) -> None:
    """S22.coherence re-reads the published squares, so a file the platform could not produce
    would otherwise raise inside the summary stage and turn a row that published real pictures
    into ``Status.FAILED`` - a report-only rule with the teeth to fail a product (fresh-context
    review 2026-10-19).  The note gives up instead."""
    config = live_config(tmp_path)
    urls = [f"https://shop.example/cdn/files/haier-6050-{index}.jpg" for index in range(1, 6)]
    page = "https://shop.example/products/haier-6050"
    shots = (
        (_WHITE_BLACK, 260), (_WHITE_BLACK, 330), (_WHITE_BLACK, 400),
        (_BEIGE_BLUE, 260), (_BEIGE_BLUE, 380),
    )
    deps, _, _, _ = build_deps(
        tmp_path,
        {page: _front_load_page_for("Haier 6050", tuple(urls))},
        {url: png_bytes(_album_shot(*shot)) for url, shot in zip(urls, shots, strict=True)},
    )
    _, accepted = process_product(
        task(0, "Haier 6050 Top Load Washer", (page,)),
        deps,
        replace(config, target_images_per_product=5, max_images_per_product=5),
    )
    broken = replace(accepted[0], square_path=tmp_path / "gone.jpg")

    assert _coherence_note([*accepted[1:], broken], config) == ""


def test_the_last_resort_never_mixes_two_shops(tmp_path: Path) -> None:
    """The other half of the same rule: a row whose pages never looked like a gallery has no
    home, so the first last-resort picture must **claim** one.  Without that claim the offers
    simply publish in list order and the album is shop A's photo beside shop B's - the mix S22
    exists to prevent, arriving through the door S22 was never asked about (fresh-context
    review 2026-10-19)."""
    first_small = _jpeg_bytes(_album_shot(_WHITE_BLACK, 260))
    second_small = _jpeg_bytes(_album_shot(_WHITE_BLACK, 400))
    config = live_config(
        tmp_path,
        min_lossy_image_bytes=max(len(first_small), len(second_small)) + 2000,
        salvage_min_sharpness=0.0,
    )
    shot_a = "https://cdn.traders.example/files/product-photo-8.jpg"
    shot_b = "https://cdn.friendshome.example/files/product-photo-9.jpg"
    page_a = "https://traders.example/products/haier-1301217e"
    page_b = "https://friendshome.example/products/haier-1301217e"
    deps, _, _, _ = build_deps(
        tmp_path,
        {
            # one picture per page: neither page is a gallery, so neither can lock the album
            page_a: _front_load_page_for("Haier 1301217E", (shot_a,)),
            page_b: _front_load_page_for("Haier 1301217E", (shot_b,)),
        },
        {shot_a: first_small, shot_b: second_small},
    )
    result, accepted = process_product(
        task(0, "Haier 1301217E Front Load Washer", (page_a, page_b)), deps, config
    )

    assert len(accepted) == 1                       # the first last resort wins the album
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert '"rule_id": "S22.source_lock"' in decisions
    assert "last-resort picture offered by another source" in decisions
    assert result.status is Status.PARTIAL          # and it says so instead of mixing


def test_the_last_resort_publishes_two_pictures_from_one_shop(tmp_path: Path) -> None:
    """The claim must not starve the row it just made: two below-floor pictures from the same
    shop are one album, so both publish - 1 image is not the price of the rule above."""
    first_small = _jpeg_bytes(_album_shot(_WHITE_BLACK, 260))
    second_small = _jpeg_bytes(_album_shot(_WHITE_BLACK, 400))
    config = live_config(
        tmp_path,
        min_lossy_image_bytes=max(len(first_small), len(second_small)) + 2000,
        salvage_min_sharpness=0.0,
    )
    shot_a = "https://cdn.traders.example/files/product-photo-8.jpg"
    shot_b = "https://cdn.traders.example/files/product-photo-9.jpg"
    page = "https://traders.example/products/haier-1301217e"
    deps, _, _, _ = build_deps(
        tmp_path,
        {page: _front_load_page_for("Haier 1301217E", (shot_a, shot_b))},
        {shot_a: first_small, shot_b: second_small},
    )
    _, accepted = process_product(
        task(0, "Haier 1301217E Front Load Washer", (page,)), deps, config
    )

    assert len(accepted) == 2
    assert all(image.page_url == page for image in accepted)


def test_a_short_row_with_no_gallery_takes_another_shop_with_proof(tmp_path: Path) -> None:
    """The claim is a veto, not a wall: the same proof that admits a second source in the
    normal path admits one here, because the file carries this row's own code."""
    first_small = _jpeg_bytes(_album_shot(_WHITE_BLACK, 260), quality=90)
    second_small = _jpeg_bytes(_album_shot(_WHITE_BLACK, 400), quality=15)
    config = live_config(
        tmp_path,
        min_lossy_image_bytes=max(len(first_small), len(second_small)) + 2000,
        salvage_min_sharpness=0.0,
    )
    shot_a = "https://cdn.traders.example/files/product-photo-8.jpg"
    shot_b = "https://cdn.friendshome.example/files/haier-130-1217E-2.jpg"
    page_a = "https://traders.example/products/haier-1301217e"
    page_b = "https://friendshome.example/products/haier-1301217e"
    deps, _, _, _ = build_deps(
        tmp_path,
        {
            page_a: _front_load_page_for("Haier 1301217E", (shot_a,)),
            page_b: _front_load_page_for("Haier 1301217E", (shot_b,)),
        },
        {shot_a: first_small, shot_b: second_small},
    )
    _, accepted = process_product(
        task(0, "Haier 1301217E Front Load Washer", (page_a, page_b)), deps, config
    )

    assert len(accepted) == 2                       # proven, so the album grows
    assert {image.page_url for image in accepted} == {page_a, page_b}


def test_two_single_image_shops_never_make_one_album(tmp_path: Path) -> None:
    """The live shape the lock used to miss (final washing-machine dry-run, 2026-10-19):
    `haier-1301217GB` published one picture from friendshome.pk and one from aysonline.pk
    because **neither** page looked like a gallery, so no home was ever claimed and the
    "one source" rule had nothing to enforce.  The page that published first owns the album
    either way; a shop that carries a real gallery still takes it over, and a stranger still
    gets in with proof - so this refuses the mix without starving the row."""
    config = live_config(tmp_path)
    stray_a = "https://cdn.friendshome.example/files/haier-130-1217GB-1.jpg"
    stray_b = "https://cdn.aysonline.example/files/image002.jpg"   # names no code, no line
    page_a = "https://friendshome.example/products/haier-1301217gb"
    page_b = "https://aysonline.example/products/haier-1301217gb"
    deps, _, _, _ = build_deps(
        tmp_path,
        {
            page_a: _front_load_page_for("Haier 1301217GB", (stray_a,)),
            page_b: _front_load_page_for("Haier 1301217GB", (stray_b,)),
        },
        {
            stray_a: png_bytes(_catalog_artwork()),
            stray_b: png_bytes(_catalog_artwork(open_lid=True)),
        },
    )
    _, accepted = process_product(
        task(0, "Haier 1301217GB Front Load Washer", (page_a, page_b)), deps, config
    )

    assert [image.source_url for image in accepted] == [stray_a]
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert '"rule_id": "S22.source_lock"' in decisions
    assert "album belongs to friendshome.example" in decisions


def test_a_bigger_gallery_is_still_not_a_ticket(tmp_path: Path) -> None:
    """The live leak this closes (washing-machine dry-run, 2026-10-19): the `851708` row
    published ``hwm-85-1709.png`` - another model - from a second shop, and a Midea row
    published a nameless ``UntitledProject_71``, because any page offering two gallery images
    used to **take the album over** from the shop that published first.  A page's size says
    nothing about whether its files are this product; only the file's own name does.  So the
    home never moves, and this gallery is judged like any other stranger: prove the row or
    stay out."""
    config = live_config(tmp_path)
    own = "https://cdn.pak-electronics.example/files/85-1708-1.png"
    page_own = "https://pak-electronics.example/products/haier-851708"
    sibling_gallery = (
        "https://cdn.lahorelectronics.example/files/hwm-85-1709.png",   # another model
        "https://cdn.lahorelectronics.example/files/promo-strip.png",
    )
    page_gallery = "https://lahorelectronics.example/products/haier-851708"
    deps, _, _, _ = build_deps(
        tmp_path,
        {
            page_own: _front_load_page_for("Haier 851708", (own,)),
            page_gallery: _front_load_page_for("Haier 851708", sibling_gallery),
        },
        {
            own: png_bytes(_catalog_artwork()),
            sibling_gallery[0]: png_bytes(_catalog_artwork(open_lid=True)),
            sibling_gallery[1]: png_bytes(_catalog_artwork(offset=-0.3, scale=0.42)),
        },
    )
    _, accepted = process_product(
        task(0, "Haier 851708 Top Load Washer", (page_own, page_gallery)), deps, config
    )

    assert [image.source_url for image in accepted] == [own]
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert '"rule_id": "S22.source_lock"' in decisions
    assert "album belongs to pak-electronics.example" in decisions


def test_a_second_source_never_joins_a_gallery_that_already_has_enough(tmp_path: Path) -> None:
    """Owner, 2026-10-17: "it must download image from one source not too many ... we choose
    the best one from one, not one here and one from other source".  Two shops' sets of the
    same model differ in crop, in light and - in the washing-machine run - in which variant
    stands in the frame, so the page that owns the album is the album."""
    config = live_config(tmp_path)
    first = "https://friendshome.example/cdn/files/haier-130-1217E-1.jpg"
    second = "https://friendshome.example/cdn/files/haier-130-1217E-2.jpg"
    other = "https://surmawala.example/cdn/files/haier-130-1217E-3.jpg"
    page_a = "https://friendshome.example/products/haier-1301217e"
    page_b = "https://surmawala.example/products/haier-1301217e"
    deps, _, _, _ = build_deps(
        tmp_path,
        {page_a: _front_load_page((first, second)), page_b: _front_load_page((other,))},
        {
            first: png_bytes(_catalog_artwork()),
            second: png_bytes(_catalog_artwork(open_lid=True)),
            other: png_bytes(_catalog_artwork(offset=-0.3, scale=0.42)),
        },
    )
    _, accepted = process_product(
        task(0, "Haier 1301217E Front Load Washer", (page_a, page_b)), deps, config
    )

    assert [image.source_url for image in accepted] == [first, second]
    assert all(image.page_url == page_a for image in accepted)   # every picture names its home
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert '"rule_id": "S22.source_lock"' in decisions
    assert "album belongs to friendshome.example" in decisions


def test_a_short_row_takes_another_source_only_when_the_file_proves_the_row(
    tmp_path: Path,
) -> None:
    """The top-up door, and what it requires: the row is below ``min_required_images``, so a
    second shop may join - but only with proof.  ``haier-1301217e-front.png`` carries the
    row's own key; ``IMG_9931.png`` carries nothing, and an unprovable picture from a second
    source is exactly the wrong-product risk the lock exists to stop."""
    config = live_config(tmp_path)
    hero = "https://friendshome.example/cdn/files/haier-130-1217E-1.jpg"
    junk = "https://friendshome.example/cdn/files/promo-banner-1200x400.jpg"
    proven = "https://surmawala.example/cdn/files/haier-1301217e-front.png"
    unproven = "https://traders.example/cdn/files/IMG_9931.png"
    page_a = "https://friendshome.example/products/haier-1301217e"
    page_b = "https://surmawala.example/products/haier-1301217e"
    page_c = "https://traders.example/products/haier-1301217e"
    deps, _, _, _ = build_deps(
        tmp_path,
        {
            page_a: _front_load_page((hero, junk)),
            page_b: _front_load_page((proven,)),
            page_c: _front_load_page((unproven,)),
        },
        {
            hero: png_bytes(_catalog_artwork()),
            junk: png_bytes(_catalog_artwork(scale=0.98)),
            proven: png_bytes(_catalog_artwork(open_lid=True)),
            unproven: png_bytes(_catalog_artwork(offset=-0.3, scale=0.42)),
        },
    )
    _, accepted = process_product(
        task(0, "Haier 1301217E Front Load Washer", (page_a, page_b, page_c)), deps, config
    )

    assert [image.source_url for image in accepted] == [hero, proven]
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert '"rule_id": "S22.source_lock"' in decisions
    assert "traders.example" in decisions                        # the unproven file is the refusal


def test_the_source_lock_runs_only_while_the_switch_is_on(tmp_path: Path) -> None:
    """One switch, one behaviour: with ``album_source_lock_enabled=False`` the row reads
    exactly as it did before this rule existed - all three sources may fill it."""
    config = replace(live_config(tmp_path), album_source_lock_enabled=False)
    first = "https://friendshome.example/cdn/files/haier-130-1217E-1.jpg"
    second = "https://friendshome.example/cdn/files/haier-130-1217E-2.jpg"
    other = "https://surmawala.example/cdn/files/haier-130-1217E-3.jpg"
    page_a = "https://friendshome.example/products/haier-1301217e"
    page_b = "https://surmawala.example/products/haier-1301217e"
    deps, _, _, _ = build_deps(
        tmp_path,
        {page_a: _front_load_page((first, second)), page_b: _front_load_page((other,))},
        {
            first: png_bytes(_catalog_artwork()),
            second: png_bytes(_catalog_artwork(open_lid=True)),
            other: png_bytes(_catalog_artwork(offset=-0.3, scale=0.42)),
        },
    )
    _, accepted = process_product(
        task(0, "Haier 1301217E Front Load Washer", (page_a, page_b)), deps, config
    )

    assert len(accepted) == 3, [image.source_url for image in accepted]
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert "S22.source_lock" not in decisions


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


def test_a_mixed_gallery_is_reported_not_pruned_when_the_row_states_none(tmp_path: Path) -> None:
    """S17 wired for real (owner 2026-10-16). The rule was written, tested and advertised - and
    never called, so a row stating no colour word had nothing comparing its pictures: S16 needs
    `keys.colour_word` and says nothing without one.  Two blue shots settle the album's colour,
    and the red picture after them is another product's photo.

    RETIRED 2026-10-21 (owner): the same hue signal that catches that red photo also kills a
    side or interior view of the RIGHT product (the Floral Brown CW1's blue-grey cabinet side
    measures 100 % blue and is correct), and "I don't want them to reject correct images at
    all".  Colour is now judged strictly on the row's first picture; S17 keeps recording the
    album's families for the run report but deletes nothing."""
    blue_a = "https://www.hadielectronics.com.pk/wp-content/blue-1.jpg"
    blue_b = "https://www.hadielectronics.com.pk/wp-content/blue-2.jpg"
    red_url = "https://www.hadielectronics.com.pk/wp-content/red.jpg"
    page_url = "https://www.hadielectronics.com.pk/product/homage-hrf-47662/"
    gallery = "".join(f'<img src="{u}">' for u in (blue_a, blue_b, red_url))
    page = (
        "<!doctype html><html><head>"
        "<title>Homage HRF-47662 Freezer-on-Top Refrigerator 18 Cu Ft</title></head><body>"
        f'<div class="product-gallery">{gallery}</div></body></html>'
    )
    config = live_config(tmp_path, target_images_per_product=3)
    deps, _, _, _ = build_deps(
        tmp_path,
        {page_url: page},
        {
            blue_a: _jpeg_bytes(clean_product_image(700, 700, "#2f5fa8")),
            blue_b: _jpeg_bytes(clean_product_image(760, 640, "#1e4f96")),
            red_url: _jpeg_bytes(clean_product_image(640, 760, "#c0392b")),
        },
    )
    result, images = process_product(
        task(0, "Homage Freezer-on-Top Refrigerator 18 Cu Ft (HRF-47662)", (page_url,)),
        deps,
        config,
    )
    log = [
        json.loads(line)
        for line in (tmp_path / "out" / "decisions.jsonl").read_text().splitlines()
    ]
    assert [i.source_url for i in images] == [blue_a, blue_b, red_url], "detail shots are not colour-judged"
    assert not any(
        e["rule_id"] == "S17" and e["decision"] == "reject" for e in log
    ), log
    assert "colour not stated" in result.note, "a mixed album is reported to the owner, not guessed at"


def test_an_unresolved_colour_mix_is_asked_not_guessed(tmp_path: Path) -> None:
    """One blue and one red picture agree with nothing: no family has the two shots S17 needs
    to call a gallery settled, so the run publishes what it has and asks the owner which colour
    he bought - his rule, never guess a colourway for him (2026-10-16)."""
    blue_url = "https://www.hadielectronics.com.pk/wp-content/blue-only.jpg"
    red_url = "https://www.hadielectronics.com.pk/wp-content/red-only.jpg"
    page_url = "https://www.hadielectronics.com.pk/product/homage-hrf-47662/"
    gallery = "".join(f'<img src="{u}">' for u in (blue_url, red_url))
    page = (
        "<!doctype html><html><head>"
        "<title>Homage HRF-47662 Freezer-on-Top Refrigerator 18 Cu Ft</title></head><body>"
        f'<div class="product-gallery">{gallery}</div></body></html>'
    )
    config = live_config(tmp_path, target_images_per_product=4)
    deps, _, _, _ = build_deps(
        tmp_path,
        {page_url: page},
        {
            blue_url: _jpeg_bytes(clean_product_image(700, 700, "#2f5fa8")),
            red_url: _jpeg_bytes(clean_product_image(760, 640, "#c0392b")),
        },
    )
    result, images = process_product(
        task(0, "Homage Freezer-on-Top Refrigerator 18 Cu Ft (HRF-47662)", (page_url,)),
        deps,
        config,
    )
    assert len(images) == 2, "nothing has settled, so nothing is refused"
    assert "colour not stated:" in result.note, result.note
    assert "blue x1" in result.note and "red x1" in result.note


def test_a_row_that_states_its_colour_is_never_asked(tmp_path: Path) -> None:
    """The question is for rows that name no colour at all.  This row is a **display** product,
    so S16 is exempt and cannot be the reason anything is missing - the only thing left to
    prove is that a stated colour word keeps the row out of the notice."""
    blue_url = "https://www.hadielectronics.com.pk/wp-content/screen-blue.jpg"
    gold_url = "https://www.hadielectronics.com.pk/wp-content/screen-gold.jpg"
    page_url = "https://www.hadielectronics.com.pk/product/homage-55q2/"
    gallery = "".join(f'<img src="{u}">' for u in (blue_url, gold_url))
    page = (
        "<!doctype html><html><head>"
        "<title>Homage 55Q2 Blue 55 Inch QLED Smart TV</title></head>"
        f'<body><div class="product-gallery">{gallery}</div></body></html>'
    )
    config = live_config(tmp_path, target_images_per_product=4)
    deps, _, _, _ = build_deps(
        tmp_path,
        {page_url: page},
        {
            blue_url: _jpeg_bytes(clean_product_image(700, 700, "#2f5fa8")),
            gold_url: _jpeg_bytes(clean_product_image(760, 640, "#d4af37")),
        },
    )
    result, _ = process_product(
        task(0, "Homage 55 Inch QLED Smart TV Blue (55Q2)", (page_url,)),
        deps,
        config,
    )
    assert "colour not stated" not in result.note, result.note


def test_a_mixed_album_is_never_published_as_plain_ok(tmp_path: Path) -> None:
    """The case a fresh-context review found (2026-10-16): one blue shot arrives, then two red
    ones.  Nothing was refused - the lock only forms when the second red lands - so the row
    published `ok` with a red-and-blue album and an empty note.  A lock is a veto, not a
    verdict: an album holding two families is the owner's question and a review row, never a
    silent success."""
    blue_url = "https://www.hadielectronics.com.pk/wp-content/one-blue.jpg"
    red_a = "https://www.hadielectronics.com.pk/wp-content/red-a.jpg"
    red_b = "https://www.hadielectronics.com.pk/wp-content/red-b.jpg"
    page_url = "https://www.hadielectronics.com.pk/product/homage-hrf-47662/"
    gallery = "".join(f'<img src="{u}">' for u in (blue_url, red_a, red_b))
    page = (
        "<!doctype html><html><head>"
        "<title>Homage HRF-47662 Freezer-on-Top Refrigerator 18 Cu Ft</title></head><body>"
        f'<div class="product-gallery">{gallery}</div></body></html>'
    )
    config = live_config(tmp_path, target_images_per_product=3)
    deps, _, _, _ = build_deps(
        tmp_path,
        {page_url: page},
        {
            blue_url: _jpeg_bytes(clean_product_image(700, 700, "#2f5fa8")),
            red_a: _jpeg_bytes(clean_product_image(760, 640, "#c0392b")),
            red_b: _jpeg_bytes(clean_product_image(640, 760, "#b02028")),
        },
    )
    result, images = process_product(
        task(0, "Homage Freezer-on-Top Refrigerator 18 Cu Ft (HRF-47662)", (page_url,)),
        deps,
        config,
    )
    assert len(images) == 3, "nothing to refuse: each was accepted before any lock existed"
    assert result.status is Status.PARTIAL, result.status
    assert "colour not stated:" in result.note, result.note


def test_a_neutral_picture_never_sets_the_gallery_colour(tmp_path: Path) -> None:
    """S17 errs on the keep side, like every colour rule here: a white or grey shot is no
    opinion about the family, so a studio gallery of neutrals loses nothing - and the first
    chromatic picture after them still sets it."""
    urls = [f"https://www.hadielectronics.com.pk/wp-content/n{i}.jpg" for i in (1, 2, 3)]
    page_url = "https://www.hadielectronics.com.pk/product/homage-hrf-47662/"
    gallery = "".join(f'<img src="{u}">' for u in urls)
    page = (
        "<!doctype html><html><head>"
        "<title>Homage HRF-47662 Freezer-on-Top Refrigerator 18 Cu Ft</title></head><body>"
        f'<div class="product-gallery">{gallery}</div></body></html>'
    )
    images_bytes = {
        urls[0]: _jpeg_bytes(clean_product_image(700, 700, "#f2f2f2")),
        urls[1]: _jpeg_bytes(clean_product_image(720, 720, "#d9d9d9")),
        urls[2]: _jpeg_bytes(clean_product_image(680, 680, "#c0392b")),
    }
    config = live_config(tmp_path, target_images_per_product=3)
    deps, _, _, _ = build_deps(tmp_path, {page_url: page}, images_bytes)
    _, images = process_product(
        task(0, "Homage Freezer-on-Top Refrigerator 18 Cu Ft (HRF-47662)", (page_url,)),
        deps,
        config,
    )
    assert [i.source_url for i in images] == urls


def test_the_gallery_colour_gate_can_be_switched_off(tmp_path: Path) -> None:
    """One config field per safeguard: `color_gate_enabled` already governs S16 and now
    governs S17 too, so a run can be compared with and without the album rule.  The album is
    already **locked** here (two blue shots), so the guard is live with the switch on and the
    third, red picture survives only with it off - a test that passed with the rule running
    would prove nothing."""
    blue_a = "https://www.hadielectronics.com.pk/wp-content/blue-1.jpg"
    blue_b = "https://www.hadielectronics.com.pk/wp-content/blue-2.jpg"
    red_url = "https://www.hadielectronics.com.pk/wp-content/red.jpg"
    page_url = "https://www.hadielectronics.com.pk/product/homage-hrf-47662/"
    gallery = "".join(f'<img src="{u}">' for u in (blue_a, blue_b, red_url))
    page = (
        "<!doctype html><html><head>"
        "<title>Homage HRF-47662 Freezer-on-Top Refrigerator 18 Cu Ft</title></head><body>"
        f'<div class="product-gallery">{gallery}</div></body></html>'
    )
    images_bytes = {
        blue_a: _jpeg_bytes(clean_product_image(700, 700, "#2f5fa8")),
        blue_b: _jpeg_bytes(clean_product_image(760, 640, "#1e4f96")),
        red_url: _jpeg_bytes(clean_product_image(640, 760, "#c0392b")),
    }
    config = live_config(tmp_path, target_images_per_product=3, color_gate_enabled=False)
    deps, _, _, _ = build_deps(tmp_path, {page_url: page}, images_bytes)
    _, images = process_product(
        task(0, "Homage Freezer-on-Top Refrigerator 18 Cu Ft (HRF-47662)", (page_url,)),
        deps,
        config,
    )
    assert len(images) == 3, "with the guard off the locked album keeps its red sibling too"


def test_a_wrong_colour_sibling_after_the_hero_publishes_as_a_detail_shot(tmp_path: Path) -> None:
    """The same site serves maroon and golden for a Golden row.  Until 2026-10-20 the main
    colour gate refused the maroon one; the owner retired that (with the first-image rule):
    the hue signal cannot tell a wrong sibling from a correct side or interior view, and a
    safeguard that costs correct pictures is worse than none.  The golden hero is judged
    and kept; the maroon picture follows it as an unjudged detail shot."""
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
    assert rejected == []                    # colour deletes nothing after the hero now
    assert gold_url in kept                  # the hero carries the row's colour
    assert maroon_url in kept                # the sibling joins as an unjudged detail shot
    assert len(images) == 2
    assert result.status is Status.OK


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


def test_finals_are_per_platform_and_share_one_stem(tmp_path: Path) -> None:
    """The publishable tree: ``images/<platform>/<product>/<code>_N.jpg``, evidence in ``audit/``.

    One stem on every platform is the point - the owner compares the same photograph across
    the shops by filename alone - and the audit files must not sit inside a platform folder,
    because a platform folder is zipped and handed over as it stands.
    """
    config = live_config(tmp_path)
    deps, _, _, _ = build_deps(
        tmp_path, {HAIER_PAGE: load_fixture("product_ok")}, gallery_images()
    )
    title = "Haier HSU-12HFPAA Inverter AC"
    result, accepted = process_product(task(0, title, (HAIER_PAGE,)), deps, config)
    assert accepted, result.reason
    slug = safe_slug(title)

    assert [key for key, _ in accepted[0].final_paths] == ["alfa", "digi"]
    names = {path.name for _, path in accepted[0].final_paths}
    assert len(names) == 1, names
    for platform_key, path in accepted[0].final_paths:
        assert path.parent == config.images_dir / platform_key / slug
        assert path.name.endswith("_1.jpg")
        assert path.exists()

    assert accepted[0].raw_path.parent == config.audit_dir / slug / "raw"
    assert accepted[0].square_path.parent == config.audit_dir / slug / "isolated"


def _sheet_rows(path: Path) -> list[list[str]]:
    """A written import sheet, read back the way a shop would read it."""
    return list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))


def test_the_import_sheet_names_the_files_it_published(tmp_path: Path) -> None:
    """One sheet serves both import modes (owner decision D3): the hosted URLs and the
    bare filenames, and the two must describe the same pictures - in the same order."""
    config = live_config(tmp_path)
    deps, _, _, _ = build_deps(
        tmp_path, {HAIER_PAGE: load_fixture("product_ok")}, gallery_images()
    )
    title = "Haier HSU-12HFPAA Inverter AC"
    result, accepted = process_product(task(0, title, (HAIER_PAGE,)), deps, config)
    assert result.status is Status.OK
    write_output_csvs([result], config, deps.platforms)

    for key in ("alfa", "digi"):
        header, row = _sheet_rows(config.output_dir / key / f"{key}mall_product_images.csv")
        assert header == ["Title", "Brand", "Image_Urls", "images"]
        assert row[0] == title and row[1] == "Haier"
        urls = row[2].split("|")
        names = row[3].split(", ")
        assert len(urls) == len(names) == len(accepted) + 1  # + closing banner
        assert names[-1].endswith(f"_{len(accepted) + 1}.jpg")
        for name in names:
            assert (config.images_dir / key / safe_slug(title) / name).is_file(), name


def test_a_dry_run_sheet_lists_files_but_no_urls(tmp_path: Path) -> None:
    """Owner decision 2026-10-13: a dry run still writes the sheets, with ``images`` filled
    and ``Image_Urls`` empty - and the README says so, so nobody imports a preview."""
    config = live_config(tmp_path, dry_run=True)
    deps, _, _, _ = build_deps(
        tmp_path, {HAIER_PAGE: load_fixture("product_ok")}, gallery_images()
    )
    result, accepted = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    assert result.status is Status.OK
    write_output_csvs([result], config, deps.platforms)

    _, row = _sheet_rows(config.output_dir / "digi" / "digimall_product_images.csv")
    assert row[2] == ""
    assert len(row[3].split(", ")) == len(accepted) + 1
    readme = (config.output_dir / "README.md").read_text(encoding="utf-8")
    assert "--dry-run" in readme and "empty `Image_Urls` column" in readme


def test_keep_rejected_writes_the_pixels_that_died(tmp_path: Path) -> None:
    """The owner asked where a rejection can be found: the log has the sentence, and with
    ``--keep-rejected`` the bytes land next to the published folder, named by the rule."""
    config = live_config(tmp_path, keep_rejected_images=True)
    _dump_rejected(_emblem_sheet(), config, "Frost 18W Classic White", "https://x/a.png", "S20")
    folder = config.audit_dir / safe_slug("Frost 18W Classic White") / "rejected"
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
    saved = sorted((config.audit_dir / safe_slug(result.title) / "raw").glob("*.jpg"))
    assert saved, result.title
    with PILImage.open(saved[0]) as served:
        assert served.size == fixture.size                     # the shop's own picture, intact
    with PILImage.open(config.audit_dir / safe_slug(result.title) / "clean" / "01-clean.jpg") as cut:
        assert cut.size[1] < fixture.size[1]                   # the cut is on the record beside it
    assert result.status in (Status.OK, Status.PARTIAL)


def _decisions(tmp_path: Path) -> list[dict[str, object]]:
    """Every line the run wrote to ``decisions.jsonl``, for a failure message."""
    path = tmp_path / "out" / "decisions.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_the_raw_folder_keeps_the_picture_the_shop_served(tmp_path: Path) -> None:
    """``raw/`` is what the shop served; the cleanup gates' cut belongs in ``clean/``.

    Both files used to be the same post-crop picture, so a row that published a sliced product
    left no trace of what was cut or where.  Live case: the owner's Dawlance Dual Pro T3 of
    2026-10-15, where a 520px "layout seam" trim took the bottom of the unit and the audit
    folder showed only the result.
    """
    import io

    from PIL import Image as PILImage
    from test_image_quality import _unit_with_badge_panel

    fixture = _unit_with_badge_panel(size=900)
    buffer = io.BytesIO()
    fixture.save(buffer, format="PNG")
    config = live_config(tmp_path, min_short_side=120, min_long_side=200)
    deps, _, _, _ = build_deps(
        tmp_path, {HAIER_PAGE: load_fixture("product_ok")}, {IMG1: buffer.getvalue()}
    )
    result, accepted = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    assert accepted, [line["detail"] for line in _decisions(tmp_path)]
    audit = config.audit_dir / safe_slug(result.title)
    with PILImage.open(audit / "raw" / "01-raw.jpg") as served:
        assert served.size == fixture.size, "raw is not the picture the shop served"
    with PILImage.open(audit / "clean" / "01-clean.jpg") as cut:
        assert cut.size[1] < fixture.size[1], "the cut the gates made was not recorded"


def test_no_clean_folder_when_nothing_was_cut(tmp_path: Path) -> None:
    """A cut is rare, so the folder is rare too - not a byte-for-byte twin of every raw."""
    from PIL import Image as PILImage

    config = live_config(tmp_path)
    deps, _, _, _ = build_deps(
        tmp_path, {HAIER_PAGE: load_fixture("product_ok")}, gallery_images()
    )
    result, accepted = process_product(
        task(0, "Haier HSU-12HFPAA Inverter AC", (HAIER_PAGE,)), deps, config
    )
    assert accepted, [line["detail"] for line in _decisions(tmp_path)]
    audit = config.audit_dir / safe_slug(result.title)
    with PILImage.open(audit / "raw" / "01-raw.jpg") as served:
        assert served.size == (900, 900)
    assert not (audit / "clean").exists()


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

    saved = sorted((config.audit_dir / safe_slug(result.title) / "raw").glob("*.jpg"))
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


def test_a_camel_case_file_of_the_rows_own_line_is_published(tmp_path: Path) -> None:
    """Owner run 2026-10-12, the false positive: the ``E-Comfort Pro KEC-1267S`` row's
    own hero on its own brand site is ``eComfortPro.png`` - the line name glued into one
    camel hump.  The wrong-product limb read one unknown word and refused the row's own
    picture.  Camel humps are separators, the file spells the row's own ``comfort``, and
    the image publishes."""
    config = live_config(tmp_path)
    page_url = "https://www.kenwoodpakistan.pk/products/e-comfort-pro-kec-1267s"
    own_png = "https://www.kenwoodpakistan.pk/cdn/shop/files/eComfortPro.png"
    html = (
        "<html><head><title>E-Comfort Pro KEC-1267S | Kenwood Pakistan</title></head>"
        "<body><h1>Kenwood E-Comfort Pro KEC-1267S</h1>"
        "<p>1.5 ton DC inverter air conditioner.</p>"
        f"<div class='product-gallery'><img src=\"{own_png}\"></div></body></html>"
    )
    deps, _, image_fetcher, _ = build_deps(
        tmp_path,
        {page_url: html},
        {own_png: png_bytes(clean_product_image(900, 900, "#f4f4f4"))},
    )
    result, images = process_product(
        task(0, "E-Comfort Pro KEC-1267S", (page_url,)), deps, config
    )
    assert len(images) == 1
    assert own_png in image_fetcher.requested
    decisions = (tmp_path / "out" / "decisions.jsonl").read_text(encoding="utf-8")
    assert "names another product line" not in decisions

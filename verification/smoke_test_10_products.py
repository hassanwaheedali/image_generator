"""Deep smoke test: the ten real sheet rows, end to end, offline (no network).

    cd image_pipeline && .venv/bin/python ../verification/smoke_test_10_products.py

Why offline: this sandbox has no egress (every host answers SSL_ERROR_SYSCALL from bash),
so the live fetch + the live freeimage.host upload must run on the owner's machine.  What
*is* exercised here is the real pipeline code - `read_input` -> `BatchRunner` (3 workers,
same as a live run) -> `process_product` -> `finalize_and_upload` -> `write_output_csvs` -
over the real ten titles from ``uploads/sample test 10 products images.csv``, with the page
and image doubles the test-suite uses.  The Dawlance row's page shape is the live-measured
one; the other nine are synthetic fixtures built to walk every branch:

    row 1  wrong link (prints DW-7500 for a DW-7500C row) -> chain finds the real model
    row 2  sheet page prints the model exactly, gallery of 4
    row 3  sheet page shows nothing -> retail fallback finds the model
    row 4  sheet page matches, only 2 usable pictures (thin gallery)
    row 5  sheet page shows a *different* variant (PAWM-1100I) -> refused -> fallback
    row 6  en-dash in the sheet title (PMO-23) matches a hyphen on the page
    row 7  sheet URL is dead (fetch failure) -> chain finds the model
    row 8  sheet page matches but its only image fails the gates -> chain finds the model
    row 9  no page prints the model; a shop page carries brand + number -> flagged (relaxed)
    row 10 nothing matches anywhere -> failed row, no images, reason recorded

The banner rule under test (owner's "last image becomes one URL for every product"):

    * the closing banner is uploaded **once per template** for the whole run,
    * the very same URL is appended **last** to every published row, both templates,
    * a second run reuses the cached banner URL and uploads nothing.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PIPELINE = REPO / "image_pipeline"
sys.path.insert(0, str(PIPELINE))
sys.path.insert(0, str(PIPELINE / "tests"))

from config import PipelineConfig                                  # noqa: E402
from conftest import (                                              # noqa: E402
    FakeImageFetcher,
    FakeImageHost,
    FakePageFetcher,
    make_assets,
    png_bytes,
)
from csv_io import load_banner_cache, load_checkpoint, merge_duplicate_titles, read_input  # noqa: E402
from decisions import DecisionLog                                  # noqa: E402
from models import PageCandidate, Status                           # noqa: E402
from pipeline import BatchRunner, PipelineDeps, ensure_banner_url   # noqa: E402
from sources import SourceChain                                    # noqa: E402
import test_pipeline_end_to_end as T                               # noqa: E402

SHEET = REPO / "uploads" / "sample test 10 products images.csv"
DW_TITLE = "Dawlance Semi Automatic Washing Machine 10kg (DW-7500C)"
DW_LINK = (
    "https://www.dawlance.com.pk/twin-tub-washing-machine/"
    "dw-7500-twin-tub-glass-lid-black-grey-semi-automatic-washing-machines"
)
SHOP = "https://www.surmawala.pk"
DW_PHOTOS = [
    "https://www.dawlance.com.pk/media/resize/"
    f"F01000000030_LO{n}_20220415_11{minute}.png/1000Wx1000H/image.webp"
    for n, minute in ((1, "3939"), (2, "3951"), (3, "4004"), (4, "4019"))
]
C_PAGE = f"{SHOP}/products/dawlance-semi-automatic-washing-machine-dw-7500c-10kg"

KNOWN_RULES = {
    "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S7.band", "S7.seam", "S7.official",
    "S8", "S9", "S10", "S11", "S11.fetch", "S12", "S13", "S13.fetch", "S14",
    "S15", "S15.fetch", "S15.relaxed", "S15.tolerated", "4.1", "accept", "source",
}


class FakeHttp:
    """The chain's HTTP boundary (``JsonHttp`` protocol), serving prepared answers."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def get_text(self, url: str) -> str | None:
        self.requested.append(url)
        return self.pages.get(url)


class _AnswerAll(FakeHttp):
    """Like the real shops: an unknown model gets a *valid empty answer*, not a failure."""

    def get_text(self, url: str) -> str | None:
        answer = super().get_text(url)
        if answer is not None:
            return answer
        if "robots.txt" in url:
            return "User-agent: *\nDisallow:\n"
        if "/search/suggest.json" in url or "/wp-json/wc/store" in url or "sitemap" in url:
            return _empty_suggest() if "sitemap" not in url else "<urlset></urlset>"
        return None


def _png(index: int) -> bytes:
    return png_bytes(T._gallery_image(index))


def _page(title: str, images: list[str], extra: str = "") -> str:
    photos = "".join(f'<img src="{url}" alt="{title} (Photo {n}/{len(images)})">'
                     for n, url in enumerate(images, 1))
    return (
        f"<html><head><title>{title} | Product Page</title>{extra}</head><body>"
        f"<h1>{title}</h1><div class='product-gallery'>{photos}</div></body></html>"
    )


def _empty_suggest() -> str:
    """What a real suggest/store endpoint answers for an unknown model: valid, empty JSON."""
    return json.dumps({"resources": {"results": {"products": []}}})


def _suggest(base: str, term: str) -> str:
    from urllib.parse import quote
    return (
        f"{base}/search/suggest.json?q={quote(term)}"
        "&resources[type]=product&resources[limit]=6"
    )


def _shop_signature() -> str:
    return "<script>window.Shopify = {shop: 'x'};</script>"


def _brand_url(slug: str) -> str:
    return f"https://brand.example/{slug}"


def build_world() -> tuple[dict[str, str], dict[str, bytes], dict[str, str]]:
    """(pages, images, shop-http answers) for the ten real rows."""
    pages: dict[str, str] = {}
    images: dict[str, bytes] = {}
    http: dict[str, str] = {}

    def add_page(url: str, html: str) -> None:
        pages[url] = html

    def add_images(urls: list[str]) -> None:
        for n, url in enumerate(urls, 1):
            images[url] = _png(n)

    # row 1 - Dawlance DW-7500C: the link prints DW-7500 (live shape) -> Surmawala's C page
    add_images(DW_PHOTOS)
    add_page(DW_LINK, _page("DW-7500 TWIN TUB GLASS LID BLACK/GREY", DW_PHOTOS))
    c_images = [f"https://cdn.surmawala.pk/p/dw7500c-{n}.png" for n in range(1, 5)]
    add_images(c_images)
    add_page(C_PAGE, _page("Dawlance Semi Automatic Washing Machine DW-7500C - 10Kg",
                           c_images, _shop_signature()))
    pages[f"{C_PAGE}.json"] = json.dumps({"product": {"images": [
        {"src": url} for url in c_images]}})
    http[f"{SHOP}/robots.txt"] = "User-agent: *\nDisallow:\n"
    # the live `suggest.json?q=DW-7500C` answer: the plain DW-7500 first, the C second
    http[_suggest(SHOP, "DW-7500C")] = json.dumps({"resources": {"results": {"products": [
        {"url": "/products/dawlance-washing-machine-dw-6550g-white-price-in-pakistan",
         "title": "Dawlance Semi Automatic Washing Machine DW-7500 - 10Kg"},
        {"url": "/products/dawlance-semi-automatic-washing-machine-dw-7500c-10kg",
         "title": "Dawlance Semi Automatic Washing Machine DW-7500C - 10Kg"}]}}})

    # row 2 - Haier H50S80EUX: the sheet page prints it exactly
    url2 = "https://www.haier.com/pk/tvs/h50s80eux.shtml"
    img2 = [f"https://cdn.haier.com/p/h50s80eux-{n}.jpg" for n in range(1, 5)]
    add_images(img2)
    add_page(url2, _page("Haier 50 Inch QLED Google LED TV (H50S80EUX)", img2))

    # row 3 - Haier 55S80EUX: the sheet page (priceoye) shows nothing -> retail fallback
    url3 = "https://priceoye.pk/led-tv/haier/haier-55-inch-qled-google-tv-h55s80eux"
    add_page(url3, _page("Haier 55 Inch QLED Google TV", img2))
    shop3 = f"{SHOP}/products/haier-55-inch-qled-google-tv-55s80eux"
    img3 = [f"https://cdn.surmawala.pk/p/55s80eux-{n}.png" for n in range(1, 5)]
    add_images(img3)
    add_page(shop3, _page("Haier 55S80EUX 55 Inch 4K Smart QLED TV", img3, _shop_signature()))
    http[_suggest(SHOP, "55S80EUX")] = json.dumps({"resources": {"results": {"products": [
        {"url": shop3.replace(SHOP, ""), "title": "Haier 55S80EUX 55 Inch QLED TV"}]}}})

    # row 4 - Haier H55K85FX: the sheet page matches but only two usable pictures
    url4 = "https://digimall.faysalbank.com/product/haier-55-smart-google-led-tv-h55k85fx"
    img4 = [f"https://cdn.digimall.pk/p/h55k85fx-{n}.jpg" for n in range(1, 3)]
    add_images(img4)
    add_page(url4, _page("Haier 55 Inch Smart Google LED TV (H55K85FX)", img4))

    # row 5 - PEL PAWM-1100: Alfatah shows the *sibling* PAWM-1100I -> refused -> fallback
    url5 = ("https://www.alfatah.com.pk/product/"
            "pel-11kg-top-load-automatic-washing-machine-pawm-1100i/")
    add_page(url5, _page("PEL 11kg Top Load Automatic Washing Machine PAWM-1100I", img4))
    shop5 = f"{SHOP}/products/pel-11kg-top-load-pawm-1100"
    img5 = [f"https://cdn.surmawala.pk/p/pawm1100-{n}.png" for n in range(1, 5)]
    add_images(img5)
    add_page(shop5, _page("PEL Top Load Fully Automatic Washing Machine PAWM-1100", img5,
                          _shop_signature()))
    http[_suggest(SHOP, "PAWM-1100")] = json.dumps({"resources": {"results": {"products": [
        {"url": shop5.replace(SHOP, ""),
         "title": "PEL Top Load Fully Automatic Washing Machine PAWM-1100"}]}}})

    # row 6 - PEL PMO-23 (en dash in the sheet title, hyphen on the page)
    url6 = "https://eshop.pel.com.pk/products/pel-kitchen-pro-23-ltr-microwave-oven"
    img6 = [f"https://cdn.pel.com.pk/p/pmo23-{n}.jpg" for n in range(1, 5)]
    add_images(img6)
    add_page(url6, _page("PEL Kitchen Pro Grill Microwave Oven PMO-23 KITCHEN PRO GR", img6))

    # row 7 - TCL 85C8L: the sheet URL is dead -> the chain recovers
    url7 = "https://www.tcl.com/au/en/tvs/85c8l"
    add_page(url7, "")                       # empty body = fetch trouble
    shop7 = f"{SHOP}/products/tcl-85c8l-mini-led-tv"
    img7 = [f"https://cdn.surmawala.pk/p/85c8l-{n}.png" for n in range(1, 5)]
    add_images(img7)
    add_page(shop7, _page("TCL 85 Inch SQD Mini LED TV 85C8L", img7, _shop_signature()))
    http[_suggest(SHOP, "85C8L")] = json.dumps({"resources": {"results": {"products": [
        {"url": shop7.replace(SHOP, ""), "title": "TCL 85C8L 85 Inch Mini LED TV"}]}}})

    # row 8 - TCL 98C8L: the sheet page matches but its only picture is a promo banner
    url8 = "https://fahadelectronics.pk/products/tcl-98c8l-98-inch-4k-mini-led-google-tv"
    promo = "https://fahadelectronics.pk/banner/sale-50-off-promo.jpg"
    images[promo] = png_bytes(T._gallery_image(1))
    add_page(url8, _page("TCL 98 Inch SQD Mini LED TV (98C8L)", [promo]))
    page8 = f"{SHOP}/products/tcl-98c8l-mini-led"
    img8 = [f"https://cdn.surmawala.pk/p/98c8l-{n}.png" for n in range(1, 5)]
    add_images(img8)
    add_page(page8, _page("TCL 98C8L 98 Inch SQD Mini LED Google TV", img8, _shop_signature()))
    http[_suggest(SHOP, "98C8L")] = json.dumps({"resources": {"results": {"products": [
        {"url": page8.replace(SHOP, ""), "title": "TCL 98C8L 98 Inch Mini LED Google TV"}]}}})

    # row 9 - PEL PWM-1250: nothing prints the model; a shop page carries brand + 1250
    url9 = ("https://alfamall.com/pel-%7C-125-kg-%7C-pwm-1250-%7C-top-load-%7C-semi-auto-%7C-"
            "white-on-installment-upto-36-months-electroease-3")
    add_page(url9, _page("PEL Top Load Semi Automatic Washing Machine 12.5 Kg", []))
    weak9 = f"{SHOP}/products/pel-top-load-washing-machine-1250"
    img9 = [f"https://cdn.surmawala.pk/p/1250-{n}.png" for n in range(1, 4)]
    add_images(img9)
    add_page(weak9, _page("PEL Top Load Washing Machine 1250 White", img9, _shop_signature()))
    http[_suggest(SHOP, "PWM-1250")] = json.dumps({"resources": {"results": {"products": [
        {"url": weak9.replace(SHOP, ""), "title": "PEL Top Load Washing Machine 1250"}]}}})

    # row 10 - Homage HRF-47662-GD: nothing matches anywhere -> failed, nothing published
    url10 = ("https://www.daraz.pk/products/homage-freezer-on-top-refrigerator-18-cu-ft-"
             "golden-hrf-47662-gd-i934349040.html")
    add_page(url10, "")                      # blocked by the marketplace

    # every other term a row may search for answers "no results" instead of failing
    for path in (f"{SHOP}/search/suggest.json", "https://www.alfatah.com.pk/wp-json/wc/store/products",
                 "https://www.alfamall.com/wp-json/wc/store/products",
                 "https://www.telex.pk/search/suggest.json"):
        http.setdefault(path, _empty_suggest())
    return pages, images, http


def make_config(root: Path, *, dry_run: bool = False) -> PipelineConfig:
    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    make_assets(assets)
    return PipelineConfig(
        assets_dir=assets,
        output_dir=root / "out",
        state_dir=root / "state",
        dry_run=dry_run,
        workers=3,                           # same parallelism as a live run
        domain_min_interval_s=0.0,
        upload_min_interval_s=0.0,
        api_key="smoke-test-key",
    )


def make_deps(tmp_path: Path, config: PipelineConfig, pages, images, http) -> PipelineDeps:
    deps = PipelineDeps(
        page_fetcher=FakePageFetcher(pages=pages),
        image_fetcher=FakeImageFetcher(images=images),
        image_host=FakeImageHost(),
        decisions=DecisionLog(config.decisions_path),
        banner_cache=load_banner_cache(config.banner_cache_path),
        banner_cache_lock=threading.Lock(),
    )
    deps.source_searcher = SourceChain(config, _AnswerAll(http))
    return deps


def run_batch(config: PipelineConfig, deps: PipelineDeps) -> list:
    tasks, warnings = merge_duplicate_titles(read_input(SHEET))
    assert not warnings, warnings
    assert len(tasks) == 10, len(tasks)
    checkpoint = {} if not config.resume else load_checkpoint(config.checkpoint_path)
    return BatchRunner(config, deps, tasks, checkpoint).run()


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"    {'PASS' if ok else 'FAIL'}  {label}{('  - ' + detail) if detail else ''}")
    return ok


def main() -> int:
    failures: list[bool] = []
    print("=" * 78)
    print("DEEP SMOKE TEST - 10 real sheet rows, offline, workers=3")
    print("=" * 78)

    root = Path(tempfile.mkdtemp(prefix="smoke10-"))
    config = make_config(root)
    pages, images, http = build_world()
    deps = make_deps(root, config, pages, images, http)
    results = run_batch(config, deps)

    published = [r for r in results if r.digi_urls]
    print(f"\n1. the ten rows")
    print(f"    {'title':<62} {'status':<8} {'images':>6}  flagged")
    for result in results:
        flagged = "yes" if (config.review_csv.exists()
                            and result.title in config.review_csv.read_text(encoding="utf-8")) else ""
        print(f"    {result.title[:60]:<62} {result.status.value:<8} "
              f"{max(len(result.digi_urls) - 1, 0):>6}  {flagged}")
    failures.append(check("all 10 rows came back", len(results) == 10))
    failures.append(check("at least 7 rows published pictures", len(published) >= 7,
                          f"{len(published)} published"))

    # --- the owner's banner rule: one upload, same URL last in every row -----------
    print("\n2. the closing banner (one URL, shared by every product, always last)")
    banner_digi = ensure_banner_url(config, deps, "digimall")
    banner_alfa = ensure_banner_url(config, deps, "alfamall")
    digi_last = {r.digi_urls[-1] for r in published}
    alfa_last = {r.alfa_urls[-1] for r in published}
    uploads = Counter(deps.image_host.uploads)                     # type: ignore[attr-defined]
    failures.append(check("every published row ends with the digimall banner",
                          digi_last == {banner_digi}, str(sorted(digi_last))))
    failures.append(check("every published row ends with the alfamall banner",
                          alfa_last == {banner_alfa}, str(sorted(alfa_last))))
    failures.append(check("both banners share one URL across all rows",
                          len(digi_last) == 1 and len(alfa_last) == 1))
    failures.append(check("the banner was uploaded exactly once per template",
                          uploads["digimall-closing-banner.jpg"] == 1
                          and uploads["alfamall-closing-banner.jpg"] == 1,
                          f"digimall={uploads['digimall-closing-banner.jpg']} "
                          f"alfamall={uploads['alfamall-closing-banner.jpg']}"))
    failures.append(check("no other upload was repeated",
                          all(count == 1 for name, count in uploads.items()), str(dict(uploads))))

    # --- the import CSV the shop reads --------------------------------------------
    print("\n3. the import CSV shape (Title,Brand,Image_Urls | pipe-separated, no spaces)")
    lines = config.digimall_csv.read_text(encoding="utf-8").strip().splitlines()
    failures.append(check("header is exactly Title,Brand,Image_Urls",
                          lines[0] == "Title,Brand,Image_Urls", lines[0]))
    failures.append(check("one data line per published row",
                          len(lines) - 1 == len(published), f"{len(lines) - 1} lines"))
    shape_ok = all(
        len(line.split(",")) == 3 and " " not in line.split(",")[2]
        and line.split(",")[2].split("|")[-1] == banner_digi
        for line in lines[1:]
    )
    failures.append(check("every line: 3 columns, no spaces in URLs, banner last", shape_ok))
    counts = [len(line.split(",")[2].split("|")) for line in lines[1:]]
    failures.append(check("each row carries its pictures + the one banner",
                          all(count >= 2 for count in counts), str(counts)))

    # --- flags, failures, traceability --------------------------------------------
    print("\n4. flags, failures and the audit trail")
    review = config.review_csv.read_text(encoding="utf-8").strip().splitlines()
    flagged_titles = {line.split(",")[0].strip('"') for line in review[1:]}
    failures.append(check("review CSV lists the relaxed row (row 9)",
                          any("PWM-1250" in title for title in flagged_titles),
                          str(sorted(flagged_titles))))
    failures.append(check("review CSV does not list the clean rows",
                          not any("H50S80EUX" in title for title in flagged_titles)))
    failed_rows = config.failed_csv.read_text(encoding="utf-8").strip().splitlines()
    failures.append(check("the unmatched row 10 is in failed.csv with a reason",
                          any("HRF-47662-GD" in line for line in failed_rows[1:]),
                          failed_rows[1][:70] if len(failed_rows) > 1 else "(none)"))
    rules = Counter(json.loads(line)["rule_id"] for line in
                    config.decisions_path.read_text(encoding="utf-8").splitlines())
    unknown = sorted(set(rules) - KNOWN_RULES)
    failures.append(check("every decision carries a known rule id", not unknown, str(unknown)))
    failures.append(check("the wrong-link deferral is traced (S15.tolerated)",
                          rules["S15.tolerated"] >= 1, str(rules["S15.tolerated"])))
    failures.append(check("the relaxed rescue is traced (S15.relaxed)",
                          rules["S15.relaxed"] >= 1, str(rules["S15.relaxed"])))
    # the fake host names its uploads after the row title, so trace the *source* the
    # pipeline downloaded from in the decision log instead
    kept = [json.loads(line) for line in
            config.decisions_path.read_text(encoding="utf-8").splitlines()]
    kept = [entry for entry in kept if entry["decision"] == "keep"]
    failures.append(check("row 1 published Surmawala's DW-7500C pictures",
                          sum(1 for e in kept if "cdn.surmawala.pk/p/dw7500c-" in e["url"]) >= 3,
                          f"{sum(1 for e in kept if 'cdn.surmawala.pk/p/dw7500c-' in e['url'])} "
                          "shop pictures kept"))
    failures.append(check("row 1 did not publish the wrong link's pictures",
                          not any("dawlance.com.pk/media/resize" in e["url"] for e in kept)))

    # --- no duplicates, only the banner repeats across rows ------------------------
    print("\n5. dedup and URL hygiene")
    dupes = [r.title for r in published if len(set(r.digi_urls)) != len(r.digi_urls)]
    failures.append(check("no row repeats one of its own URLs", not dupes, str(dupes)))
    non_banner = [url for r in published for url in r.digi_urls[:-1]]
    failures.append(check("no product picture is shared between rows",
                          len(set(non_banner)) == len(non_banner),
                          f"{len(non_banner)} pictures"))
    failures.append(check("both platforms carry the same pictures",
                          all(len(r.digi_urls) == len(r.alfa_urls) for r in published)))

    # --- second run: resume + the banner cache ------------------------------------
    print("\n6. second run (same state dir): resume + no re-upload of the banner")
    deps2 = make_deps(root, config, pages, images, http)
    results2 = run_batch(config, deps2)
    uploads2 = Counter(deps2.image_host.uploads)                   # type: ignore[attr-defined]
    resumed = [r for r in results2 if "resumed from checkpoint" in r.note]
    failures.append(check("every published row came back from the checkpoint",
                          len(resumed) == len(published),
                          f"{len(resumed)} resumed of {len(published)} published"))
    failures.append(check("the failed row was retried, not skipped",
                          any(r.title.startswith("Homage") and "resumed" not in r.note
                              for r in results2)))
    failures.append(check("the second run uploaded no banner at all",
                          uploads2["digimall-closing-banner.jpg"] == 0
                          and uploads2["alfamall-closing-banner.jpg"] == 0))

    # --- dry-run never leaks its URLs into a real run ------------------------------
    print("\n7. dry-run keeps its own banner cache")
    dry_root = Path(tempfile.mkdtemp(prefix="smoke10-dry-"))
    dry_config = make_config(dry_root, dry_run=True)
    dry_deps = make_deps(dry_root, dry_config, pages, images, http)
    dry_url = ensure_banner_url(dry_config, dry_deps, "digimall")
    real_url = ensure_banner_url(config, deps, "digimall")
    failures.append(check("a dry-run banner URL is never reused for a real run",
                          dry_url != real_url or dry_config.dry_run, dry_url))

    passed = sum(1 for ok in failures if ok)
    print("\n" + "=" * 78)
    print(f"RESULT: {passed}/{len(failures)} checks passed"
          f"{' - ALL GREEN' if passed == len(failures) else ' - SEE FAILURES ABOVE'}")
    print(f"artifacts: {config.output_dir}")
    print("=" * 78)
    return 0 if passed == len(failures) else 1


if __name__ == "__main__":
    raise SystemExit(main())

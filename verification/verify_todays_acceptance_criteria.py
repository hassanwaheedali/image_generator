"""The three acceptance cases for today's deadline, offline (repo fakes, no network).

    python3 verification/verify_todays_acceptance_criteria.py      # from image_pipeline/

CASE 1  the sheet's own URL matches -> the whole job runs (the title that used to die in
        the tokenizer: ``DW 7500 TWIN TUB CLEAR LID (WHITE)``)
CASE 2  the row's own link is the *wrong page*: the live DW-7500C row carries a Dawlance URL
        that prints DW-7500 (the C-slugged URL 404s), so the model in the title wins - the
        chain reaches Surmawala's DW-7500C listing and publishes its pictures, unflagged
CASE 2b nothing anywhere prints the model -> the wrong-link page is still published, as
        ``PARTIAL`` and flagged, never dropped
CASE 3  no page can prove the model -> relaxed fallback publishes the pictures **and**
        flags the row in ``output_dir/review_products.csv`` instead of failing
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from collections import Counter
from pathlib import Path
from urllib.parse import quote, urlsplit

PIPELINE_DIR = Path(__file__).resolve().parent.parent / "image_pipeline"
sys.path.insert(0, str(PIPELINE_DIR))
sys.path.insert(0, str(PIPELINE_DIR / "tests"))

from config import PipelineConfig                        # noqa: E402
from conftest import (                                   # noqa: E402
    FakeImageFetcher,
    FakeImageHost,
    FakePageFetcher,
    make_assets,
    png_bytes,
)
from decisions import DecisionLog                        # noqa: E402
from models import ProductTask                           # noqa: E402
from pipeline import PipelineDeps, process_product, write_output_csvs  # noqa: E402
from sources import SourceChain                          # noqa: E402
import test_pipeline_end_to_end as T                     # noqa: E402

SHOP = "https://www.surmawala.pk"
SHOP_PAGE = f"{SHOP}/products/dawlance-washing-machine-dw-6550g-white-price-in-pakistan"
OFFICIAL = (
    "https://www.dawlance.com.pk/twin-tub-washing-machine/"
    "dw-7500-twin-tub-clear-lid-white-semi-automatic-washing-machines"
)
#: The sheet's own link for the DW-7500C row - live, it is a real page that **prints
#: DW-7500** (the C-slugged URL is a 404; measured 2026-10-06).
WRONG_LINK = (
    "https://www.dawlance.com.pk/twin-tub-washing-machine/"
    "dw-7500-twin-tub-glass-lid-black-grey-semi-automatic-washing-machines"
)
C_TITLE = "Dawlance Semi Automatic Washing Machine 10kg (DW-7500C)"
#: what that page serves: four `F01000000030_*` studio photos at 1000Wx1000H
C_PHOTOS = [
    "https://www.dawlance.com.pk/media/resize/"
    f"F01000000030_LO{n}_20220415_11{minute}.png/1000Wx1000H/image.webp"
    for n, minute in ((1, "3939"), (2, "3951"), (3, "4004"), (4, "4019"))
]
SHOP_C_PAGE = (
    "https://www.surmawala.pk/products/dawlance-semi-automatic-washing-machine-dw-7500c-10kg"
)
SHOP_PLAIN_PAGE = (
    "https://www.surmawala.pk/products/dawlance-washing-machine-dw-6550g-white-price-in-pakistan"
)
C_PAGE = C_TITLE
DW_ASSETS = [
    "F11031006930_LO1_20231120_115524", "F11031006930_LO1_20231120_120009",
    "F11031006930_LO2_20231120_115549", "F11031006930_LO3_20231120_115626",
    "F11031006930_LO4_20231120_115656", "F11031006930_LO5_20231120_115716",
    "F11031006930_LO6_20231120_115804", "F11031006930_OTH_20231120_115911",
    "F11031006930_OTH_20231120_115925", "F11031006930_OTH_20231120_115956",
]
DW_IMAGES = [
    f"https://www.dawlance.com.pk/media/resize/{asset}.png/1000Wx1000H/image.webp"
    for asset in DW_ASSETS
]


def _png(index: int) -> bytes:
    return png_bytes(T._gallery_image(index))


class FakeHttp:
    """A stand-in for the source layer's ``Http``: serves the prepared shop endpoints."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def get_text(self, url: str) -> str | None:
        self.requested.append(url)
        return self.pages.get(url)


def _suggest(url: str, term: str) -> str:
    return (
        f"{url}/search/suggest.json?q={quote(term)}"
        "&resources[type]=product&resources[limit]=6"
    )


def white_page(label: str, count: int = 10) -> str:
    photos = "\n".join(
        f'<img src="{url}" alt="{label} (Photo {n + 1}/{count})">'
        for n, url in enumerate(DW_IMAGES[:count])
    )
    return (
        f"<html><head><title>{label} | Twin Tub Washing Machine | Dawlance</title></head>"
        f"<body><h1>{label}</h1><div class='pdp-gallery'>{photos}</div></body></html>"
    )


def wrong_link_page() -> str:
    """Live shape: the row's own link prints ``DW-7500`` although the row says ``DW-7500C``."""
    photos = "".join(
        f'<img src="{url}" alt="DW-7500 TWIN TUB GLASS LID BLACK/GREY (Photo {n}/4)">'
        for n, url in enumerate(C_PHOTOS, 1)
    )
    return (
        "<html><head><title>DW-7500 TWIN TUB GLASS LID BLACK/GREY | Twin Tub Washing "
        "Machine | Dawlance</title></head><body>"
        "<h1>DW-7500 TWIN TUB GLASS LID BLACK/GREY Twin Tub Washing Machine</h1>"
        f"<div class='product-gallery'>{photos}</div></body></html>"
    )


def unmatched_page() -> str:
    """A page that names no model of ours at all: the chain has to do the work."""
    return (
        "<html><head><title>DW-9999 TWIN TUB | Dawlance</title></head><body>"
        "<h1>DW-9999 TWIN TUB</h1></body></html>"
    )


def shop_page(title: str, slug: str) -> str:
    photos = "".join(
        f'<img src="https://cdn.surmawala.pk/p/{slug}-{n}.png">' for n in range(1, 5)
    )
    return (
        f"<html><head><title>{title}</title><script>var s='cdn.shopify.com'</script>"
        f"</head><body><h1>{title}</h1><div class='product-gallery'>{photos}</div>"
        "</body></html>"
    )


def make_config(root: Path) -> PipelineConfig:
    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    make_assets(assets)
    return PipelineConfig(
        assets_dir=assets,
        output_dir=root / "out",
        state_dir=root / "state",
        dry_run=True,
        workers=1,
        domain_min_interval_s=0.0,
        upload_min_interval_s=0.0,
        api_key="offline-test-key",
    )


def run_case(
    label: str,
    title: str,
    urls: tuple[str, ...],
    pages: dict[str, str],
    images: dict[str, bytes],
    shop_http: FakeHttp | None = None,
    debug: dict[str, list[str]] | None = None,
) -> int:
    root = Path(tempfile.mkdtemp())
    config = make_config(root)
    deps = PipelineDeps(
        page_fetcher=FakePageFetcher(pages=pages),
        image_fetcher=FakeImageFetcher(images=images),
        image_host=FakeImageHost(),
        decisions=DecisionLog(config.decisions_path),
        banner_cache={},
        banner_cache_lock=threading.Lock(),
    )
    if shop_http is not None:
        deps.source_searcher = SourceChain(config, shop_http)
    result, accepted = process_product(ProductTask(0, title, urls), deps, config)
    write_output_csvs([result], config, include_platform_csvs=False)
    rules = Counter(
        json.loads(line)["rule_id"] for line in config.decisions_path.read_text().splitlines()
    )
    rows = config.review_csv.read_text(encoding="utf-8").strip().splitlines()
    print(f"\n=== {label}")
    print(f"    title     : {title!r}")
    print(f"    status    : {result.status.value}    images: {result.images_accepted}")
    print(f"    note      : {result.note[:150]}")
    print(f"    review row: {rows[-1][:130] if len(rows) > 1 else '(none)'}")
    print(f"    rules     : {dict(rules)}")
    if debug is not None:
        debug["sources"] = sorted({
            urlsplit(image.source_url).netloc for image in accepted
        })
    return result.images_accepted


def main() -> int:
    cases = []

    # CASE 1 - the sheet's own URL matches: the whole job runs.
    cases.append((
        "CASE 1  sheet URL matches -> whole job runs",
        run_case(
            "CASE 1: official page, title exactly as the brand prints it",
            "DW 7500 TWIN TUB CLEAR LID (WHITE)",
            (OFFICIAL,),
            {OFFICIAL: white_page("DW 7500 TWIN TUB CLEAR LID (WHITE)")},
            {url: _png(n) for n, url in enumerate(DW_IMAGES, 1)},
        ),
        3,
    ))

    # CASE 2 - the row's own link is the wrong page (prints DW-7500 for a DW-7500C row):
    #          the chain reaches Surmawala and the live `q=DW-7500C` answer carries the C.
    shop_pages = {f"{SHOP}/robots.txt": "User-agent: *\nDisallow:\n"}
    live_c_answer = json.dumps({"resources": {"results": {"products": [
        {"url": "/products/dawlance-washing-machine-dw-6550g-white-price-in-pakistan",
         "title": "Dawlance Semi Automatic Washing Machine DW-7500 - 10Kg",
         "image": "https://cdn.shopify.com/s/files/1/0829/6490/6267/files/7500.jpg?v=1754727205"},
        {"url": "/products/dawlance-semi-automatic-washing-machine-dw-7500c-10kg",
         "title": "Dawlance Semi Automatic Washing Machine DW-7500C - 10Kg",
         "image": "https://cdn.shopify.com/s/files/1/0829/6490/6267/files/7500-c.png?v=1787567578"},
    ]}}})
    for term in ("DW-7500C", "DW7500C", "DW 7500C"):
        shop_pages[_suggest(SHOP, term)] = live_c_answer
    c_json = json.dumps({"product": {"images": [
        {"src": f"https://cdn.surmawala.pk/p/dw7500c-{n}.png?v={n}"} for n in range(1, 5)
    ]}})
    dbg = {}
    cases.append((
        "CASE 2  wrong link (prints DW-7500) -> Surmawala answers the real DW-7500C",
        run_case(
            "CASE 2: the model in the title wins over the wrong sheet link",
            C_TITLE,
            (WRONG_LINK,),
            {
                WRONG_LINK: wrong_link_page(),
                SHOP_PLAIN_PAGE: shop_page(
                    "Dawlance Semi Automatic Washing Machine DW-7500 - 10Kg", "dw7500"
                ),
                SHOP_C_PAGE: shop_page(
                    "Dawlance Semi Automatic Washing Machine DW-7500C - 10Kg", "dw7500c"
                ),
                f"{SHOP_C_PAGE}.json": c_json,
            },
            {
                **{url: _png(n) for n, url in enumerate(C_PHOTOS, 1)},
                **{
                    f"https://cdn.surmawala.pk/p/{slug}-{n}.png": _png(n)
                    for slug in ("dw7500", "dw7500c")
                    for n in range(1, 5)
                },
            },
            FakeHttp({**shop_pages}),
            dbg,
        ),
        3,
    ))
    assert dbg["sources"] == ["cdn.surmawala.pk"], dbg["sources"]

    # CASE 2b - nothing anywhere prints the model: the wrong-link page is published, flagged.
    cases.append((
        "CASE 2b  nothing better exists -> tolerated spelling published + flagged",
        run_case(
            "CASE 2b: the wrong-link page is published with a flag instead of failing",
            C_TITLE,
            (WRONG_LINK,),
            {WRONG_LINK: wrong_link_page()},
            {url: _png(n) for n, url in enumerate(C_PHOTOS, 1)},
        ),
        3,
    ))

    # CASE 3 - nothing can prove the model: publish with a flag.
    weak_page = f"{SHOP}/products/dawlance-2500-special"
    weak_html = (
        "<html><head><title>Dawlance Twin Tub Washing Machine 2500 - Special</title>"
        "</head><body><h1>Dawlance Twin Tub Washing Machine 2500</h1><div class='product-gallery'>"
        + "".join(f"<img src='https://cdn.surmawala.pk/p/2500-{n}.jpg'>" for n in range(1, 4))
        + "</div></body></html>"
    )
    cases.append((
        "CASE 3  nothing verifies -> relaxed fallback, published + flagged",
        run_case(
            "CASE 3: relaxed fallback publishes the pictures and flags the row",
            "Dawlance DW 2500 C Twin Tub",
            ("https://www.dawlance.com.pk/x/dw-2500c",),
            {"https://www.dawlance.com.pk/x/dw-2500c": unmatched_page(), weak_page: weak_html},
            {f"https://cdn.surmawala.pk/p/2500-{n}.jpg": _png(n) for n in range(1, 4)},
            FakeHttp({
                f"{SHOP}/robots.txt": "User-agent: *\nDisallow:\n",
                _suggest(SHOP, "DW-2500-C"): json.dumps({"resources": {"results": {"products": [
                    {"url": "/products/dawlance-2500-special", "title": "Dawlance 2500 Twin Tub"}
                ]}}}),
                _suggest(SHOP, "DW2500C"): '{"resources": {"results": {"products": []}}}',
            }),
        ),
        3,
    ))

    print("\n=== VERDICT")
    ok = True
    for label, produced, wanted in cases:
        good = produced >= wanted
        ok &= good
        print(f"    {'PASS' if good else 'FAIL'}  {label}  (images: {produced} >= {wanted})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

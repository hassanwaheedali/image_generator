"""Read-only probe: what does the pipeline actually see on ONE official product page?

Why this file exists
--------------------
`image_pipeline/tests/` is offline by design (fakes only), and a datacenter IP cannot
reach dawlance.com.pk at all (Akamai answers a non-browser client with a TLS reset /
403).  So "how many of the 10 official photos does the pipeline get?" can only be
answered on a machine that can reach the site.  This probe answers it, and it changes
NOTHING: no pipeline code is touched, no upload happens, no API key is needed
(``dry_run=True``), and every artefact goes to a throw-away temp folder.

What it prints
--------------
1. the model keys the sheet title produces (this is where rows usually die),
2. the page verification verdict (strength / match rule),
3. every gallery candidate: extraction source, order, DOM flags, dedup key, and the
   ``tag.class`` ancestor chain that produced it (this reveals the real container class),
4. a faithful run of the pipeline's own ``_consume_candidates`` over those candidates,
   so every S-rule verdict is the real one,
5. the tally: candidates found, downloads used, images accepted, and the caps that
   apply (target / max / per-page download allowance).

Usage
-----
    # on a machine that can reach the site (no key needed, nothing is uploaded):
    python3 verification/probe_official_gallery.py \
        "https://www.dawlance.com.pk/twin-tub-washing-machine/dw-7500-twin-tub-clear-lid-white-semi-automatic-washing-machines" \
        "DW 7500 TWIN TUB CLEAR LID (WHITE)"

    # offline re-run from a saved page (image step is then a smoke test only):
    python3 verification/probe_official_gallery.py --html page.html --fake-images "TITLE"

    # every spelling of a title in one go (shows which ones carry a model key):
    python3 verification/probe_official_gallery.py --keys-only \
        "DW 7500 TWIN TUB CLEAR LID (WHITE)" "DW-7500 TWIN TUB CLEAR LID (WHITE)"
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import threading
from collections import Counter
from pathlib import Path

PIPELINE = Path(__file__).resolve().parent.parent / "image_pipeline"
sys.path.insert(0, str(PIPELINE))

from bs4 import BeautifulSoup
from config import PipelineConfig
from decisions import DecisionLog
from dedup import ProductDedup
from fetch import (
    CurlCffiImageFetcher,
    CurlCffiPageFetcher,
    DomainRateLimiter,
)
from gallery_extractors import ExtractionContext, extract_candidates
from model_matching import build_model_keys
from models import ProductTask
from page_verification import (
    MatchSettings,
    strip_unwanted_regions,
    verify_page,
)
from PIL import Image, ImageDraw
from pipeline import (
    PipelineDeps,
    _consume_candidates,
    _DownloadBudget,
    _page_download_allowance,
    csv_escape,
)
from protocols import BinaryResponse
from url_filters import detect_brand, host_is_official


class RecordingImageFetcher:
    """Wraps the real image fetcher and counts what a page really costs in downloads."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.downloads: list[str] = []
        self.probes: list[str] = []

    def fetch_image(self, url: str, referer: str) -> BinaryResponse:
        self.downloads.append(url)
        return self._inner.fetch_image(url, referer)          # type: ignore[attr-defined]

    def probe_image(self, url: str, referer: str, max_bytes: int) -> BinaryResponse | None:
        self.probes.append(url)
        prober = getattr(self._inner, "probe_image", None)
        return prober(url, referer=referer, max_bytes=max_bytes) if prober else None


class FakeImageFetcher:
    """Offline smoke test: a clean white-background picture for every URL.

    The S6/S7/S8/S9 verdicts this produces are NOT evidence about the real photo;
    they only prove the probe wiring runs.  Never quote them as a finding.
    """

    def __init__(self) -> None:
        self.downloads: list[str] = []
        self.probes: list[str] = []

    @staticmethod
    def _bytes(width: int = 1000, height: int = 1000) -> bytes:
        image = Image.new("RGB", (width, height), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((250, 180, 750, 820), radius=30, fill=(47, 95, 168))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def fetch_image(self, url: str, referer: str) -> BinaryResponse:
        self.downloads.append(url)
        return BinaryResponse(url=url, status_code=200, content=self._bytes(), content_type="image/png")

    def probe_image(self, url: str, referer: str, max_bytes: int) -> BinaryResponse | None:
        self.probes.append(url)
        return BinaryResponse(
            url=url, status_code=206, content=self._bytes(1000, 1000)[:max_bytes], content_type="image/png"
        )


def ancestor_chain(node: object, limit: int = 4) -> str:
    """``tag.class`` chain above a node - reveals which container the extractor matched."""
    parts: list[str] = []
    current = getattr(node, "parent", None)
    while current is not None and len(parts) < limit:
        name = getattr(current, "name", None)
        if name in (None, "[document]"):
            break
        classes = current.get("class") or []
        parts.append(f"{name}.{'.'.join(classes)}" if classes else f"{name}")
        current = current.parent
    return " < ".join(parts) if parts else "(no ancestors)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", nargs="?", help="official product page URL")
    parser.add_argument("titles", nargs="*", help="sheet title for that page (1st one; extra ones are ignored)")
    parser.add_argument("-t", "--title", action="append", default=[], help="sheet title (repeatable, used by --keys-only)")
    parser.add_argument("--html", type=Path, help="use a saved page instead of fetching (offline mode)")
    parser.add_argument("--fake-images", action="store_true", help="offline smoke test: synthesise the images")
    parser.add_argument("--keys-only", action="store_true", help="just show the model keys per title, then stop")
    parser.add_argument("--dump-html", type=Path, help="save the fetched page here (for later --html runs)")
    args = parser.parse_args(argv)

    all_titles = [value for value in ([args.url, *args.titles] if args.html is None else []) if value]
    all_titles.extend(args.title)
    if args.keys_only:
        titles = all_titles or list(args.title)
        print("title -> model keys (empty keys = the row dies at rule 4.1, 0 pages, 0 images)")
        for title in titles:
            keys = build_model_keys(title)
            mark = "OK " if keys.all_keys else "DEAD"
            print(f"  {mark} {title!r:<52} tokens={list(keys.tokens)} identities={list(keys.identities)}")
        return 0

    title = args.title[-1] if args.title else (args.titles[0] if args.titles else None)
    if not args.html and (not args.url or not title):
        parser.error("url and a sheet title are required (or use --keys-only)")
    if args.html and not title and args.title:
        title = args.title[-1]
    url = args.url or ""
    if not title:
        parser.error("a sheet title is required")
    if args.html and not url:
        url = "https://example.invalid/page-with-no-url"   # only used as a relative-link base
    keys = build_model_keys(title)
    brand = detect_brand(url, title)
    print("=" * 100)
    print(f"page          : {url}")
    print(f"sheet title   : {title!r}")
    print(f"model keys    : tokens={list(keys.tokens)} identities={list(keys.identities)} "
          f"colours={list(keys.colours)} variants={keys.variant_dict()}")
    print(f"brand         : {brand}  (official host: {host_is_official(url, brand)})")
    if not keys.all_keys:
        print("ROW VERDICT   : FAILED at rule 4.1 'title has no model-like token' - "
              "no page is fetched, 0 images, whatever the site shows.")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # Compositing needs the four assets.  The repo's own (placeholder) templates are used
        # when present; otherwise small synthetic ones are written to the temp folder.
        assets = PIPELINE / "assets"
        if not all((assets / name).exists() for name in
                   ("DigiMall.png", "AlfaMall.png", "digi_last.png", "alfa_last.png")):
            assets = tmp_path / "assets"
            assets.mkdir(parents=True, exist_ok=True)
            template = Image.new("RGBA", (1200, 1200), (255, 255, 255, 255))
            ImageDraw.Draw(template).rectangle((0, 0, 1200, 150), fill=(12, 78, 160, 255))
            template.save(assets / "DigiMall.png")
            template.save(assets / "AlfaMall.png")
            banner = Image.new("RGB", (1200, 420), (250, 250, 250))
            banner.save(assets / "digi_last.png")
            banner.save(assets / "alfa_last.png")
        config = PipelineConfig(
            assets_dir=assets,
            output_dir=tmp_path / "out",
            state_dir=tmp_path / "state",
            dry_run=True,                     # no key, no upload, ever
            workers=1,
            domain_min_interval_s=0.0,
            upload_min_interval_s=0.0,
        )
        limiter = DomainRateLimiter(0.0)
        if args.html is not None:
            html = args.html.read_text(encoding="utf-8")
            resolved = url
            print(f"source        : saved file {args.html} ({len(html):,} bytes)")
        else:
            response = CurlCffiPageFetcher(config, limiter).fetch_page(url)
            html, resolved = response.text, response.url or url
            print(f"fetch         : HTTP {response.status_code}, {len(html):,} bytes")
            if args.dump_html:
                args.dump_html.write_text(html, encoding="utf-8")
                print(f"                saved to {args.dump_html}")

        soup = strip_unwanted_regions(BeautifulSoup(html, "lxml"), resolved)
        match = verify_page(resolved, html, keys, brand=brand, settings=MatchSettings.from_config(config))
        print(f"page verdict  : strength={match.strength} rule={match.match_rule} detail={match.detail!r}")
        print(f"                page name={match.page_name!r} notes={list(match.variant_notes)}")
        if not match.accepted:
            print("ROW VERDICT   : FAILED at S1 - page does not prove this model, 0 images.")
            return 0

        context = ExtractionContext(
            page_url=resolved, soup=soup, keys=keys, strength=match.strength, brand=brand,
            raw_html=html, match_rule=match.match_rule,
            brand_match_min_length=config.brand_match_min_length,
            brand_match_min_digits=config.brand_match_min_digits,
        )
        candidates = extract_candidates(context)
        print(f"\ncandidates    : {len(candidates)}")
        by_source: Counter[str] = Counter()
        for index, candidate in enumerate(candidates, start=1):
            by_source[candidate.source] += 1
            node_chain = ""
            if index == 1:
                node = next(
                    (img for img in soup.find_all(["img", "source", "a"])
                     if img.get("src") == candidate.url or img.get("href") == candidate.url), None
                )
                node_chain = ancestor_chain(node) if node is not None else "(not found in cleaned DOM)"
            print(f"  {index:>3}. [{candidate.source:<16}] flags={list(candidate.dom_flags)} "
                  f"model_in_url={candidate.matches_model} {csv_escape(candidate.url)}")
            if node_chain:
                print(f"       DOM container chain: {node_chain}")
        print(f"  by source  : {dict(by_source)}")
        if not candidates:
            print("ROW VERDICT   : FAILED at S12 - the page yielded no gallery candidate, 0 images.")
            return 0

        decisions = DecisionLog(config.decisions_path)
        image_fetcher: object = (
            FakeImageFetcher() if args.fake_images else RecordingImageFetcher(CurlCffiImageFetcher(config, limiter))
        )
        deps = PipelineDeps(
            page_fetcher=CurlCffiPageFetcher(config, limiter),
            image_fetcher=image_fetcher,               # type: ignore[arg-type]
            image_host=None,                           # type: ignore[arg-type]  (never used: dry run, no upload)
            decisions=decisions,
            banner_cache={},
            banner_cache_lock=threading.Lock(),
        )
        task = ProductTask(index=0, title=title, source_urls=(url,))
        accepted: list = []
        rejections: Counter[str] = Counter()
        _consume_candidates(
            task=task, page_url=resolved, candidates=candidates, keys=keys, config=config, deps=deps,
            dedup=ProductDedup(
                hard_threshold=config.dedup_hamming_threshold,
                soft_threshold=config.dedup_hamming_soft_threshold,
                corr_threshold=config.dedup_corr_threshold,
            ),
            seen_keys=set(), accepted=accepted, rejections=rejections,
            source_label="sheet", budget=_DownloadBudget(
                config.image_full_download_max_per_page, config.image_full_download_max_per_row
            ),
            official_page=host_is_official(resolved, brand),
        )

        print(f"\ndecisions.jsonl ({config.decisions_path.name}):")
        for line in config.decisions_path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            print(f"  {record['rule_id']:<9} {record['decision']:<6} {record['detail']}")
        print(f"\naccepted      : {len(accepted)} image(s) from this page")
        print(f"downloads     : {len(getattr(image_fetcher, 'downloads', []))} "
              f"(image probes: {len(getattr(image_fetcher, 'probes', []))})")
        print(f"rejections    : {dict(rejections)}")
        print("\ncaps that apply after this page")
        print(f"  target_images_per_product      = {config.target_images_per_product}")
        print(f"  max_images_per_product         = {config.max_images_per_product}")
        print(f"  per-page download allowance    = {_page_download_allowance(candidates, config)} "
              f"(default {config.image_full_download_max_per_page}; a curated gallery is read "
              f"in full - {len(candidates)} candidates here)")
        print(f"  image_full_download_max_per_row  = {config.image_full_download_max_per_row}")
        if args.fake_images:
            print("\nWARNING: --fake-images was used, so the S6/S7/S8/S9 verdicts above are a wiring")
            print("         smoke test, not evidence about the real photographs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

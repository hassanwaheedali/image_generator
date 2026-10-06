# Findings — Dawlance **DW 7500 TWIN TUB CLEAR LID (WHITE)** (official page, 10 photos)

> **Fixes applied later the same day** (see `CHANGELOG.md` → *Unreleased* and the tests named below):
> * **F4 (the 4-of-10 ceiling)** — a page offering a *curated* gallery is now read in full, up to
>   `max_images_per_product` (10) and never past the row budget; scraped pages keep the 6-download cap.
>   Tests: `test_official_gallery_is_read_in_full_not_truncated_by_the_page_allowance`,
>   `test_retail_curated_gallery_is_also_read_in_full`, `test_scraped_page_still_stops_at_the_per_page_allowance`.
> * **F3 (zoom link bought a second download slot)** — `dedup_key` now folds `…/FILE.png` and
>   `…/FILE.png/1000Wx1000H/image.webp` onto one asset.
>   Test: `test_zoom_link_and_served_render_are_one_asset`.
> * **F1 (the tokenizer gap: `DW 7500` -> no model keys) is NOT fixed yet** — the sheet workaround below
>   still applies, and the priority-2 retailer path (Surmawala, whose listing for this product *does*
>   carry `DW-7500` in its title) now works, so a row with a key-bearing title is rescued there.
> * The **fallback** was fixed independently of this product: the brand tier could fill the whole
>   candidate budget, so the retailers were never searched.
>   Test: `test_model_conflict_on_the_brand_page_moves_to_the_priority_2_retailer`.

**Date:** 2026-10-06 · **Scope:** investigation only — *no pipeline code was changed*.
**Question asked:** how many images does the pipeline get from the official product URL, and is the checking correct?
**Method:** the live page was read (via a browser-like fetch — a datacenter IP cannot reach dawlance.com.pk),
and every claim below was then replayed **offline through the pipeline's own functions** (`build_model_keys`,
`verify_page`, `extract_candidates`, `url_filters`, `dedup_key`, and a full `process_product` run with the test
doubles), using the **real URLs and real page strings** from that live fetch.

---

## Verdict in one line

> **0 images** — and the page is never even fetched: the title exactly as Dawlance prints it
> (`DW 7500 …`, space, no hyphen) produces **no model keys**, so the row dies at rule **4.1**
> (`title has no model-like token`) before any network work.
> With a key-bearing spelling the page verifies fine (strength 2) and the 10 photos become reachable —
> but only through the **generic gallery-container** path, and the run then publishes **3 (target) and at
> most 5 (max)**, never all 10 (per-page download allowance is 6).

---

## The page (measured live, 2026-10-06)

`https://www.dawlance.com.pk/twin-tub-washing-machine/dw-7500-twin-tub-clear-lid-white-semi-automatic-washing-machines`

* Title / H1: `DW 7500 TWIN TUB CLEAR LID (WHITE)` — price Rs 47,300 — breadcrumb `Washing Machines › Semi Automatic › Twin Tub`.
* **Exactly 10 gallery photos**, `(Photo 1/10)` … `(Photo 10/10)`, every one served as
  `https://www.dawlance.com.pk/media/resize/<ASSET>.png/1000Wx1000H/image.webp` with:

| # | asset id | | # | asset id |
|---|---|---|---|---|
| 1 | `F11031006930_LO1_20231120_115524` | | 6 | `F11031006930_LO5_20231120_115716` |
| 2 | `F11031006930_LO1_20231120_120009` | | 7 | `F11031006930_LO6_20231120_115804` |
| 3 | `F11031006930_LO2_20231120_115549` | | 8 | `F11031006930_OTH_20231120_115911` |
| 4 | `F11031006930_LO3_20231120_115626` | | 9 | `F11031006930_OTH_20231120_115925` |
| 5 | `F11031006930_LO4_20231120_115656` | | 10 | `F11031006930_OTH_20231120_115956` |

* The same `LO1` asset is repeated inside the **Product Specification** section (photo 1 of the gallery).
* Other images on the page are not product photos: feature graphics (`/medias/Robust-pulsator-washer-1280.png?context=…`),
  the "We have compared for you" widget (`F01000047500_MDM2_LOW_1.png`, `F01000000030_LO1_20220415_113939.png` at `530Wx530H`),
  icons (`sutanki.svg`, `Warranty Icon.png`, `bnpl32-v1.svg`).

---

## F1 — The row dies at recognition: `DW 7500` has no model key (critical)

`build_model_keys()` (measured):

| sheet title | tokens | identities | outcome |
|---|---|---|---|
| `DW 7500 TWIN TUB CLEAR LID (WHITE)` | `[]` | `[]` | **DEAD — rule 4.1, 0 pages, 0 images** |
| `Dawlance DW 7500 TWIN TUB CLEAR LID (WHITE)` | `[]` | `[]` | **DEAD** |
| `Dawlance Twin Tub Washing Machine 10kg DW 7500 White` | `[]` | `[]` | **DEAD** |
| `DW 7500 TWIN TUB CLEAR LID` | `[]` | `[]` | **DEAD** |
| `DW 7500` | `[]` | `[]` | **DEAD** |
| `DW-7500 TWIN TUB CLEAR LID (WHITE)` | `['DW-7500']` | `['dw7500']` | OK |
| `DW7500 TWIN TUB CLEAR LID (WHITE)` | `['DW7500']` | `['dw7500']` | OK |
| `DW 7500C TWIN TUB CLEAR LID (WHITE)` | `['7500C']` | `['7500c']` | OK (brand+code path) |
| `Dawlance Semi Automatic Washing Machine 10kg (DW-7500C)` | `['DW-7500C']` | `['dw7500c']` | OK (this is the sample-sheet row) |
| `Dawlance Twin Tub Washing Machine 10kg White (DW-7500)` | `['DW-7500']` | `['dw7500']` | OK |

**Why** (`model_matching.py`): `model_like_token` needs letters **and** digits in *one* token —
`model_like_token("DW") = False` (2 chars, no digit), `model_like_token("7500") = False` (no letter) —
and `_token_and_identity` only glues *following alphabetic* words onto a model token; it never joins a
letter word with a following numeric word. The bracketed fallback only rescues text inside `(...)`,
and here the brackets hold `(WHITE)`.

**End-to-end proof** (real `pipeline.process_product`, test doubles, real page HTML):

```
title          : 'DW 7500 TWIN TUB CLEAR LID (WHITE)'
page requests  : 0      image requests: 0
status         : failed  reason='title has no model-like token'
decisions.jsonl: {"rule_id": "4.1", "detail": "title has no model-like token", ...}
```

So whatever the site shows — 10 photos or 100 — this row produces **nothing**.

## F2 — Even with keys, the branded Dawlance extractor finds 0 of the 10 photos

`gallery_extractors/dawlance.py` accepts a media URL only when it contains **this page's MDM code**
(`page_mdm_code(page)` = `dw7500`), else a model key. Today's CDN serves opaque SFCC asset ids, so
measured on the 10 real URLs:

| check (real URL) | result |
|---|---|
| `has_bad_keyword` (S5) | ok — passes, 10/10 |
| `url_belongs_to_other_model` (S4) | ok — passes, 10/10 (asset prefix `f` ≠ our prefix `dw`) |
| `find_matching_keys(url, …, include_title_key=False)` | **False, 10/10** |
| `url_matches_this_product(...)` | **False, 10/10** |
| `mdm in url` (`dawlance.extract`) | **False, 10/10** |
| `dedup_key` | 10 **distinct** keys — no collapse (good) |
| `upscale_candidates` | original asset `…LO1….png` first, then the `1000Wx1000H` webp (good) |

Consequence: the `registry:dawlance` source contributes **nothing** on this page, and the strict
whole-page fallback (S12: image URL must carry a model key) can never accept these URLs either.
The 10 photos are reachable only via the **generic gallery-container** path (or JSON-LD / embedded JSON).

**Which path is real on the live DOM is still unknown**, and it decides everything:

| hypothesis (simulated with the real code + real URLs) | candidates found | accepted |
|---|---|---|
| **H1** — gallery sits in a container class from the 22-entry `GALLERY_CONTAINER_SELECTORS` (e.g. `.pdp-gallery`, `.swiper`, `.sd_imgs`) | **10** (`gallery_container`, no flags) | S7/S9 decide |
| **H2** — container class not in that list and no JSON-LD/embedded JSON | 1 (the spec-section copy, flagged **S2**) | **0** (S2 rejects it) |

Indirect evidence that H1 is true today: the sibling row `DW-7500C` (glass lid, black/grey) really did
publish 4 photos from `dawlance.com.pk` in the live run (`verification/VERIFICATION.md`,
`verification/sizing.txt`, contact sheet `verification/dawlance_gallery.jpg`). Its media URLs
(`F01000000030_*`) cannot satisfy the MDM/model-in-URL rule either, so those 4 must have come from the
generic container / JSON-LD path — i.e. some DOM path on this site does match. **The probe below confirms
it for the white variant and prints the actual container class.**

## F3 — One zoom-link wart: the same asset can take two download slots

If a gallery image is wrapped in a link to the same asset, e.g.
`<a href="…/F11031006930_LO1_….png"><img src="…/F11031006930_LO1_….png/1000Wx1000H/image.webp"></a>`,
`collect_img_urls` prefers the `href` (good — bigger file) but `dedup_key` gives the two forms different
keys (measured):

```
media/resize/f11031006930_lo1_20231120_115524            <- the .png
media/resize/f11031006930_lo1_20231120_115524.png/image  <- the 1000Wx1000H webp
```

A simulated zoom-link page yielded **11 candidates for 10 photos**. S10 catches the visual duplicate
after download, but the extra download has already been charged to the page budget.

## F4 — Why it can never give you all 10 (by design, but maybe not the design you want)

| cap | value | effect on this page |
|---|---|---|
| `target_images_per_product` | 3 | checked only **between pages**, not inside one page |
| `max_images_per_product` | 5 | within a page, collection stops after 5 accepted |
| `image_full_download_max_per_page` | 6 | **only the first 6 of 10 photos are ever downloaded** |
| `image_full_download_max_per_row` | 18 | not binding here |

Measured in the offline end-to-end run (key-bearing title, H1 page, 10 candidates): **3 accepted**, 6 downloads,
then `S13.fetch "page download allowance spent"`. That last line is bookkeeping, not a failure — it was
emitted *after* the row already had enough images — but it looks like an error in `decisions.jsonl`.
So the realistic yield for this product is **3–5 photos**, and **4–7 of the site's 10 photos are never looked at**.

## F5 — The image-quality verdicts for the real photos are still untested

This sandbox cannot reach dawlance.com.pk (Akamai resets a non-browser client; only PyPI/GitHub egress is
allowed here — a public CORS proxy got HTTP 403 from the same edge). So S6/S7/S8/S9/S10 were **not** run on
the real pixels. Expectation from the sibling variant (`verification/dawlance_gallery.jpg`: clean studio shots
on white, including a top-down open-lid shot): most `LO*` photos will fail the strict S7 border ring and
need the scoped **`S7.official`** rescue (curated gallery source + official host + ≥2 neutral corners) —
exactly the case that rescue was built for; the three `OTH_*` photos (likely lifestyle/detail) are the
likely genuine S7 rejects.

---

## How to finish the investigation (one command, on a machine that can reach the site)

```bash
python3 verification/probe_official_gallery.py \
  "https://www.dawlance.com.pk/twin-tub-washing-machine/dw-7500-twin-tub-clear-lid-white-semi-automatic-washing-machines" \
  "DW-7500 TWIN TUB CLEAR LID (WHITE)" --dump-html /tmp/dw7500.html
```

`verification/probe_official_gallery.py` (added with this note) is **read-only**: `dry_run=True`, no API key,
nothing uploaded, all artefacts in a temp folder. It prints

1. the model keys for the title (and with `--keys-only -t "…"` for any list of spellings),
2. the `verify_page` verdict (strength / match rule / page name),
3. every candidate with its source, DOM flags, dedup key **and its `tag.class` ancestor chain** (this
   settles H1 vs H2 and names the real gallery container class),
4. a faithful run of the pipeline's own `_consume_candidates` — so each `decisions.jsonl` line is the real
   rule verdict — plus downloads used and the accepted count.

It was smoke-tested in this session in offline mode (`--html` + `--fake-images`) against a page built from
the real strings and real URLs: it reported 10 `gallery_container` candidates and the 6-of-10 budget ceiling.

---

## Recommended fixes (NOT applied — findings only)

1. **Tokenizer (the real bug).** In `model_matching` (`scan` / `_token_and_identity`), glue an alphabetic
   1–4-letter prefix word with a following 3–5-digit numeric word (`DW` + `7500` → `dw7500`), skipping
   unit/quantity words (`10 kg`, `2 sets`) and keeping the current guards (never extend a key; a bare
   `7500` alone must stay unmatchable; `HSU-12` ≠ `HSU-12HFPAA`). Cover the five spellings in F1 as a
   table-driven test. *This alone turns the row from "0 images, no fetch" into "page verified".*
2. **Dawlance extractor is stale for the current site.** Either accept gallery media URLs from the page's
   own gallery container regardless of the asset id, or re-scope the MDM rule (keep the model-in-URL
   requirement for the whole-page fallback only). Confirm the live container class is in
   `GALLERY_CONTAINER_SELECTORS`; if it is not, add it (worth doing anyway — it is the path that saved this
   brand twice).
3. **`dedup_key`:** fold `<asset>.<ext>` and `<asset>.<ext>/<transform>/image.webp` into one asset so a
   zoom link cannot buy two download slots.
4. **Decide the intended yield for a 10-photo official gallery.** If 3 is wanted, stop *inside* a page at
   the target and stop writing `S13.fetch "page download allowance spent"` as a rejection line; if more is
   wanted (5, or all 10), raise `image_full_download_max_per_page` (6 today) accordingly. Right now the
   pipeline silently ignores 4–7 official photos of this product.
5. **Sheet workaround available today** (no code change): write the model so it survives the tokenizer —
   `Dawlance Twin Tub Washing Machine 10kg White (DW-7500)` or `DW-7500 TWIN TUB CLEAR LID (WHITE)`.
   `DW 7500 …` exactly as the brand prints it, and `DW 7500C …` (works, but via the looser brand+code
   path), are the two cases to know about.

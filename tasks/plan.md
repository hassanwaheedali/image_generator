# Implementation Plan: Verified Product-Image Pipeline (A → Z)

**Feature name:** sheet-driven product-image pipeline → DigiMall + Alfa Mall import sheets
**Plan author session:** 2026-10-06 · **Skill used:** `planning-and-task-breakdown`
**Status legend:** ✅ done, with the evidence that proves it · ⬜ remaining
**Note on ordering:** the first implementation pass already happened in this session (the user asked
for working code before this planning pass). This document is therefore the **complete A → Z plan**
for the feature, with per-task status recorded honestly. Everything still open is in **Phase 3
(Task 11b)** and **Phase 9–10**.

---

## Overview

One command reads a sheet (`Title` + source URL per row) and, per product: fetches the page, **proves
the page shows that exact product**, extracts only that product's own gallery photos, rejects
everything that is not a clean white-background product shot, de-duplicates within the product,
isolates the product, normalises it to a **1:1 canvas with the product perfectly centred**, composites
it onto the **DigiMall** and **Alfa Mall** templates **without ever painting over the template's logo
band**, appends the platform's closing banner **last**, exports JPEG, uploads to **freeimage.host**,
and writes two import sheets — `Title,Brand,Image_Urls` with `|`-separated URLs and no spaces.
When the sheet's own URL cannot solve a row, a **verified fallback chain** searches official brand
sources and then retail Shopify/WooCommerce shops (JSON search first). Every decision, keep or reject,
is one auditable line in `decisions.jsonl`.

## Architecture Decisions

1. **The sheet's URL always wins.** Fallback sources are contacted **only** when the row is unsolved
   (no page matched, or a matched page gave zero usable images). Rationale: the user curates those
   URLs; search must never override a working source.
2. **JSON method before HTML crawling.** Every source declares its platform in
   `config.source_platforms` (measured: Shopify `suggest.json`, Woo `wc/store/products`, brand
   sitemaps). One request per source per row; no JS rendering, no breadth-first crawling.
3. **Official brand sources before retail** (HaierMall → haier.com.pk → Dawlance → TCL → PEL →
   Homage, then retail). Brand sitemap indexes are cached in `state/sitemaps/<host>.jsonl` so the
   first row of a brand pays one crawl and later rows are local lookups.
4. **A found page is never trusted (S15).** Search results are *candidate pages*; each is re-verified
   with the **sheet's** model before any image URL is read. The search term is always the sheet's
   model — never a code read from a page — so a stale URL can never "fix itself" into a successor
   model.
5. **Recognition is two plain rules** (model key; brand + code), **not** a score. Colour and variant
   differences are **flags** (`run_report.csv` Note), never failures — deliberately, because the
   source URLs are human-chosen. Image-side gates stay strict.
6. **All rules are config fields** (`config.PipelineConfig`), including every threshold; data tables
   (`UPSCALE_REWRITE_RULES`, `BAD_URL_KEYWORDS`, `KNOWN_BRANDS`, source lists) live next to the code
   that uses them. No magic numbers in stage functions.
7. **One stage = one function**, and each row logs its path:
   `1 recognition → 2 sheet pages → 3 fallback search → 4 images → 5 compose → 6 upload → 7 status`.
8. **Safety beats recall, except where the user chose otherwise.** S1–S14 stay intact; S15 was added
   for the fallback; colour/variant flags and "body-only page = normal page" are explicit user
   decisions from 2026-10-05.
9. **Atomic writes everywhere** (temp + `os.replace`), append-only checkpoint for resume, banner URLs
   cached by content hash so banners upload once.
10. **No LLM and no content-understanding APIs** — only deterministic regex/DOM/pixel logic a human
    can audit.

## Dependency Graph

```
config.py (rules)  models.py (types)  protocols.py (boundaries)
        │                   │                   │
        └────────┬──────────┴─────────┬─────────┘
                 │                    │
        fetch.py (transport)     url_filters.py (S5/S11, brands, rewrite table)
                 │                    │
                 │            model_matching.py (recognition rules)
                 │                    │
                 │          page_verification.py (S1/S2/S3 + flags)
                 │                    │
                 │    gallery_extractors/* (registry, dawlance, haier, magento, json_ld,
                 │                        embedded_json, generic_gallery)
                 │                    │
                 │            image_quality.py (S6/S7/S8/S9/S13 + header probe)
                 │                    │
                 │               dedup.py (S10 per product)
                 │                    │
                 │            compositing.py (isolate → 1:1 centre → template-safe paste)
                 │                    │
        image_host.py (freeimage)     │
                 └──────────┬─────────┘
                     pipeline.py  (stages, S14 upload gate, BatchRunner, CSV writers)
                            │
        sources.py (S15 fallback chain) ──┘
                            │
                         main.py (CLI → PipelineConfig → BatchRunner)
                            │
              outputs: digimall CSV · alfamall CSV · failed CSV · run_report · decisions.jsonl
```

Implementation order = bottom-up along this graph (foundations → recognition → gallery → gates →
compose → hosting → batch → fallback → efficiency → docs → acceptance).

## Task List

### Phase 0 — Foundation (types, rules surface, writers)

- [x] **Task 1: Rule surface and paths** — `config.py`
- [x] **Task 2: Domain models + network protocols** — `models.py`, `protocols.py`
- [x] **Task 3: Sheet reader, atomic writers, checkpoint, decisions log** — `csv_io.py`, `decisions.py`

### Checkpoint: Foundation
- [x] `pytest tests/test_csv_io_resume.py tests/test_json_helpers.py` green
- [x] `mypy --strict` clean on these modules
- [x] Config validation fails fast on impossible numbers

### Phase 1 — Recognition slice (prove a page is the right product)

- [x] **Task 4: Model keys from the sheet title** — `model_matching.py`
- [x] **Task 5: The two recognition rules + flags** — `model_matching.match_product()`, `page_verification.verify_page()`
- [x] **Task 6: Page-side zero-tolerance guards (S2 description, S3 other-page links)** — `page_verification.py`

### Checkpoint: Recognition
- [x] `pytest tests/test_model_matching.py tests/test_page_verification.py` green (24 + 23 tests)
- [x] Verified live: stale Haier URL (`H50S80EUX` page serving `H50S80GUX`) is **rejected** with the shown model named in the Note

### Phase 2 — Gallery slice (only this product's photos)

- [x] **Task 7: Extractor registry + brand/JSON-LD/embedded-JSON/gallery sources** — `gallery_extractors/*` (8 files)
- [x] **Task 8: Image gates S5/S6/S7/S8/S9/S13** — `image_quality.py`, `url_filters.py`
- [x] **Task 9: Per-product de-duplication (S10)** — `dedup.py`

### Checkpoint: Gallery
- [x] `pytest tests/test_gallery_extractors.py tests/test_image_quality.py tests/test_dedup.py` green
- [x] Zero-tolerance guarantees proven: no description images, no other-page-link images, no cross-product leakage

### Phase 3 — Compose slice (1:1, centred, template-safe, JPEG)

- [x] **Task 10: Isolation + 1:1 centre canvas + light unsharp** — `compositing.py`
- [x] **Task 11: Template compositing with header-safe margin; banner appended last** — `compositing.apply_template()`, `pipeline.finalize_and_upload()`
- [x] **Task 11b: Prove the template's logo band is byte-identical after compositing** — **done**
  **Description:** the customer rule "the product must not mess with the template logo" had no direct
  test; it now has four, and they are mutation-verified (a deliberately broken `apply_template` fails
  the suite).
  **Acceptance criteria:**
  - [x] Logo band pixels are byte-identical before/after `apply_template` — tested with a
        **semi-transparent** band, so a product pasted underneath it would bleed through and fail
  - [x] The product is never pasted above `header_safe_margin()` (`template_content_box` invariant)
  - [x] Template artwork **inside** the content area survives (the product canvas never paints over it)
  - [x] The check also runs against the real `assets/DigiMall.png` and `assets/AlfaMall.png`
  **Verification:** `python3 -m pytest tests/test_image_quality.py -q` (17 tests); mutation A
  (`top = 0`) fails 2 tests, mutation B (composited order swapped) fails the decoration test.
  **Dependencies:** Task 11 · **Files:** `tests/test_image_quality.py` · **Scope:** XS
- [x] **Task 12: Local artefact layout** (`raw/`, `isolated/`, `final_digimall/`, `final_alfamall/`)

### Checkpoint: Compose
- [x] `test_apply_template_keeps_template_size`, `test_square_canvas_and_sharpen`, `test_isolation_keeps_product_and_never_returns_empty`
- [x] **Task 11b closed** — logo-band and decoration guarantees are tested and mutation-verified

### Phase 4 — Hosting and output slice

- [x] **Task 13: freeimage.host client, banner cache, permanent-error abort** — `image_host.py`, `pipeline.py`
- [x] **Task 14: Output sheets `Title,Brand,Image_Urls` (pipes, no spaces), failed CSV, run report** — `pipeline.write_output_csvs()`, `config.OUTPUT_HEADER`

### Checkpoint: Outputs
- [x] `test_output_csv_has_title_brand_and_pipe_separated_urls` asserts the exact header and row format
- [x] `test_failed_product_uploads_nothing`, `test_permanent_api_error_aborts_after_checkpoint_flush`

### Phase 5 — Batch runtime slice

- [x] **Task 15: BatchRunner — workers, input order, resume, `--dry-run`, `--redo`, `--limit`, abort** — `pipeline.py`, `main.py`

### Checkpoint: Batch
- [x] `pytest tests/test_pipeline_end_to_end.py` green (29 tests); resume/skip/retry semantics covered
- [x] `--dry-run` writes no platform CSVs and never calls the image host

### Phase 6 — Fallback chain slice (user-approved change)

- [x] **Task 16: Source registry, platform map, chain ordering (brand → retail)** — `config.py`, `sources.py`
- [x] **Task 17: Shopify + WooCommerce JSON search parsers** — `sources.py`
- [x] **Task 18: Sitemap index crawl + `state/` cache + robots courtesy check** — `sources.py`
- [x] **Task 19: S15 verification of candidates, trigger rule, source labelling** — `pipeline.py`

### Checkpoint: Fallback
- [x] `pytest tests/test_sources.py` green (14 tests) + 12 new end-to-end tests in `test_pipeline_end_to_end.py`
- [x] Live dry-run on the real 10-row sheet: **ok 3 / partial 5 / failed 2** (was 1 / 4 / 5), **12 images kept, 5 from fallback**
- [x] Rescue examples confirmed in `decisions.jsonl`: `source=fallback:json-search:www.alfatah.com.pk`, `source=fallback:json-search:hadielectronics.com.pk`, `source=fallback:sitemap:www.haier.com`
- [x] Verified that a search hit pointing at another model produces **zero** image requests (S15 test)

### Phase 7 — Efficiency slice

- [x] **Task 20: Per-host curl_cffi sessions + retry classification (403 = no retry)** — `fetch.py`
- [x] **Task 21: 64 KB header probe (JPEG/PNG/WebP/GIF dimensions before download)** — `fetch.probe_image()`, `image_quality.dimensions_from_header()`
- [x] **Task 22: Largest-size URL rewrite + download budget + target of 3 images** — `url_filters.upscale_candidates()`, `config.py`, `pipeline.py`
- [x] **Task 22b: Two-level (per-page + per-row) download budget** — found live in this session:
      the Dawlance row spent its entire allowance on the brand page's dark renders, so the retail
      candidates were never downloaded. Fix: `image_full_download_max_per_page` (6) **and**
      `image_full_download_max_per_row` (18); a page that exhausts its own allowance stops, the next
      candidate page still gets a chance. Evidence: Dawlance moved **failed → ok (2 images)**,
      suite has `test_a_bad_page_does_not_starve_the_next_candidate`.

### Checkpoint: Efficiency
- [x] `test_header_probe_rejects_tiny_image_without_a_download` (60×60 rejected with zero full downloads)
- [x] `test_largest_size_rewrite_is_tried_first_then_the_original` (Haier `_60.jpg` → `_1000.jpg`)
- [x] `test_download_budget_stops_runaway_rows`

### Phase 8 — Documentation and traceability

- [x] **Task 23: README (install, run, Colab cell, layout, add-extractor, tuning, S1–S15 table, "where to edit what" map) + plan.md §11**

### Checkpoint: Docs
- [x] README renders and matches the code (headers, flags, sources, budgets)
- [x] `plan.md` §11 marked IMPLEMENTED with measured numbers

### Phase 9 — Acceptance on real data ⬜ REMAINING

- [ ] **Task 24: Real upload run (needs `FREEIMAGE_API_KEY`)**
  **Description:** Export the key and run the pipeline without `--dry-run` so images are actually
  hosted and the two import sheets are written.
  **Acceptance criteria:**
  - [ ] `output/digimall_product_images.csv` header is exactly `Title,Brand,Image_Urls`
  - [ ] Every URL in both sheets opens (HTTP 200) and is a JPEG on freeimage.host
  - [ ] The **last** URL in each row is that platform's closing banner
  - [ ] Rows with `ok`/`partial` appear in **both** sheets; `failed_products.csv` lists only failures
  **Verification:** `python3 main.py --input sample_input.csv --workers 4` then spot-open 3 URLs from each sheet; compare `run_report.csv` against the dry-run baseline.
  **Dependencies:** Task 13, Task 14, Task 23
  **Files likely touched:** none (config/output only)
  **Estimated scope:** Small (run + verify)

- [ ] **Task 25: First-run review and tuning of fallback rows**
  **Description:** Inspect the 5 fallback-sourced images (`output/images/<product>/final_digimall/`)
  and confirm each shows the correct product/colour; adjust gates via `--dry-run` if a shop's photos
  are consistently rejected or wrongly accepted.
  **Acceptance criteria:**
  - [ ] Every fallback image checked against its sheet title (model + colour)
  - [ ] Any tuning recorded as a config change **plus** a new/updated test
  - [ ] `run_report.csv` shows no unexplained `failed` row
  **Verification:** `--dry-run --redo "<title>"` for each reviewed row; diff the report.
  **Dependencies:** Task 24
  **Files likely touched:** `config.py`, possibly one test module
  **Estimated scope:** Small

### Checkpoint: Acceptance
- [ ] Both import sheets open cleanly in Excel/Google Sheets with correct columns
- [ ] Human reviewed the fallback-sourced rows
- [ ] All acceptance criteria in the original brief re-read and ticked

### Phase 9b — Secrets handling ✅

- [x] **Task 28: `.env` support for `FREEIMAGE_API_KEY`** — `config.load_env_file()` + `parse_env_text()`
  (pure, tested), called once from `main()` before the config is built.
  **Acceptance criteria:**
  - [x] **`secrets.env`** (visible, at the top level *and* inside the project) plus `.env` / `env.txt` as alternatives and `secrets.example.env` as the committed template; `.gitignore` blocks all three names and the run artefacts
  - [x] Precedence: `--env-file` → `./.env` → `./secrets.env` → `./env.txt` → same names next to `main.py` → same names in the folder **above** the project; a real environment variable always wins, and an unfilled template never shadows a filled file
  - [x] Values are never logged; startup prints `secrets: loaded <path>` and `freeimage key: set|not needed (dry-run)|MISSING`
  - [x] Missing/blank key + no `--dry-run` still fails fast with a message that names the `.env` route
  **Verification:** `pytest tests/test_env_file.py -q` (5 tests); live: dry-run prints `freeimage key: not needed (dry-run)`, a filled `.env` prints `set`, an empty one fails fast.
  **Dependencies:** Task 1 · **Files:** `config.py`, `main.py`, `.env`, `.env.example`, `.gitignore`, `tests/test_env_file.py` · **Scope:** S

### Phase 9c — First live run: two defects found and fixed ✅

The first real upload run (10 products, real key) exposed two failures that no fake-based test
could see. Both are fixed, tested and mutation-verified.

- [x] **Task 29: the product was hidden behind the template**
  `DigiMall.png` / `AlfaMall.png` are RGBA with an **all-opaque** alpha channel, so the white
  knockout was skipped and the opaque template painted over the product — every final image was
  the bare template (14 identical outputs, cross-product identity).
  Fix: `compositing.template_has_transparency()` decides from *pixels* (`alpha.min() < min_alpha`),
  new config field `template_transparency_min_alpha=250`; the knockout is limited to the product
  area (`make_white_transparent(..., box=...)`) so the logo band stays byte-identical.
  **Verification:** `test_an_opaque_alpha_channel_does_not_hide_the_product`,
  `test_shipped_templates_count_as_opaque_not_transparent`,
  `test_shipped_templates_really_show_two_different_products`; mutation (old `"A" not in bands`
  check restored) fails them. Live: 14 distinct finals, one per accepted photo.
- [x] **Task 30: a dry run poisoned the live banner URLs**
  `--dry-run` cached `file://` banner paths in `state/banner_urls.json`; the live run reused them,
  so the import CSVs contained local paths instead of hosted URLs.
  Fix: `pipeline._banner_cache_key()` scopes every entry to `dry:` / `live:`; plus a hard guard in
  `finalize_and_upload` — a live row that yields any non-`http(s)` URL fails instead of publishing.
  **Verification:** `test_banner_cache_is_scoped_to_the_run_mode`,
  `test_a_live_run_refuses_to_publish_a_local_path`; mutations of both fail them.
- [x] **Task 24 acceptance re-checked after the fixes** — see `/home/user/verification/VERIFICATION.md`:
  11/11 checks pass (header, no spaces, no local paths, banner strictly last once per row, no duplicate
  image inside a row, 30/30 URLs HTTP 200 + image content type, uploads pixel-identical to the local
  finals, per-product mapping, no shared finals, key absent from every artefact).
  **Live result: ok 4 / partial 5 / failed 1** (`PEL PWM-1250`, photo-truth: no white-background gallery).

### Phase 9d — The false negative that mattered (PWM-1250) ✅

The user asked why `PWM-1250` failed instead of being rescued by the fallback. Answer: the fallback
*had* run (the sheet URL is an alfamall.com listing page, so it fell through to the chain) and it had
found two correct, clean studio photos - but both were rejected for shop furniture that is not the
product. That is exactly the false-negative failure mode the user said to fear.

- [x] **Task 31: `S7.band` - cut a shop promo strip before the S7 ring test**
  `image_quality.strip_promo_band()` + `_scan_promo_band()`; config `promo_band_strip_enabled`,
  `promo_band_max_ratio=0.30`, `promo_band_min_height_px=24`, `promo_band_min_width_ratio=0.80`,
  `promo_band_dark_max_mean=200`, `promo_band_background_gap_px=6`, `promo_band_gap_min_mean=200`,
  `promo_band_edge_margin_max_px=80`, `promo_band_white_level=245`.
  **Acceptance:** strips are cut, dark product bodies running off the edge are not, a strip without a
  bright inner gap is not, the switch works, and the cut is logged as `S7.band`.
  **Verification:** `test_promo_band_is_cut_so_the_photo_passes_S7`,
  `test_promo_band_is_never_cut_without_a_clean_gap`,
  `test_promo_band_detection_can_be_switched_off`; mutation "never cut" is caught.
- [x] **Task 32: `S7.seam` - trim a thin two-edge layout line before the S7 ring test**
  `image_quality.trim_edge_seams()` + `_scan_seam()`; config `seam_trim_enabled`,
  `seam_min_span_ratio=0.95`, `seam_max_width_px=12`, `seam_offcut_max_content_ratio=0.03`,
  `seam_keep_min_ratio=0.5`, `seam_max_passes=3`.
  **Acceptance:** a 2 px top line and a 5 px side seam are trimmed; product content beside a seam is
  never trimmed; at most half the picture may be lost; each trim logged as `S7.seam`.
  **Verification:** `test_edge_seam_is_trimmed_so_the_photo_passes_S7`,
  `test_a_seam_beside_real_content_is_left_alone`; 2 mutations caught.
- [x] **Stage 4b wired into `_consume_candidates`** - both cuts happen *before* `run_quality_gates`,
  each recorded with its own rule id, and every S1-S14 gate still runs afterwards on the cleaned image.
- [x] **Live re-run of the row (`--redo`)**: `PEL PWM-1250` **failed -> ok**, 2 photos + banner.
  Totals now **ok 5 / partial 5 / failed 0**; both import CSVs have 10 rows; 34/34 URLs HTTP 200;
  acceptance script: 0 structural problems.

### Phase 9e — The user's own templates, real sizing, and the Brand column ✅

Three orders in one round: swap in the supplied brand templates, make the product **big and
centred** without touching the logo, and fix the empty Brand column.

- [x] **Task 33: the supplied templates are in place, with the product sized from its own content box**
  `assets/DigiMall.png`, `assets/AlfaMall.png` replaced by the user's files (originals kept in
  `assets/previous_placeholder/`).  `compositing.product_content_bbox()` + `fit_product_size()` size the
  product by **its own** bounding box instead of the padded square it travels in, so a tall product
  fills the height and a wide one fills the width.  `optical_center_bias` is now **0.0** (perfectly
  centred) and only the product's content is pasted - the white padding that used to creep over the
  logo band is cropped away first.
  **Acceptance criteria:**
  - [x] Tall product fills >= 70% of the canvas height on both real templates (live: 90% Dawlance, 90% Homage)
  - [x] Wide product fills >= 75% of the width (live: 87.4%)
  - [x] Horizontally centred within 3 px; never glued to the bottom (>= 6% gap)
  - [x] Product never crosses the logo band (`header_safe_margin`); the band stays byte-identical in the composite
  - [x] Every threshold is a config field (`product_fill_ratio`, `product_content_white_level`, `product_content_min_ratio`, `optical_center_bias`)
  **Verification:** `test_product_fills_the_real_template`, `test_wide_product_fills_the_real_template`,
  `test_product_is_centred_and_never_glued_to_the_bottom`, plus the existing logo-band tests.
  **Mutations caught 3/3** (fill 0.95->0.5, centring removed, padding pasted again).
- [x] **Task 34: the Brand column survives a resume**
  `CheckpointEntry.brand` added, written by `ProductResult.to_checkpoint_dict()` and restored by
  `BatchRunner.run()`; an old checkpoint without the field still loads and the brand is recovered
  from the title.
  **Verification:** `test_resume_keeps_the_brand_column`, `test_an_old_checkpoint_without_a_brand_still_loads`;
  mutation (brand forced to "") is caught.
- [x] **Task 35: production tooling - `ruff` wired in through `pyproject.toml`**
  Rule set `E,F,W,I,B,UP,C4,SIM,PIE,PERF,RUF,FURB,ISC,S,BLE` (includes bandit `S` and flake8-blind-except),
  with two documented exemptions: `BLE001`/`S110` (the batch runner catches `Exception` on purpose so one
  bad product cannot abort the other nine - it logs with a traceback) and `RUF001` (box-drawing characters
  in log messages).  81 findings fixed for real: atomic writes now use `mkstemp` + `contextlib.suppress`,
  imports sorted, casts removed, loops replaced by `all()`/`any()`/`extend()`.  mypy strict stays clean via
  an override for the three third-party libraries that ship no stubs (`scipy`, `requests`, `openpyxl`).
  **Verification:** `python3 -m ruff check .` -> *All checks passed*; `mypy --strict` -> clean, 37 files.

### Phase 10 — Optional follow-ups ⬜

- [ ] **Task 26: Brand extractors for the two stubborn rows** (Dawlance DWF gallery, Homage/Daraz)
  **Description:** Add `gallery_extractors/dawlance*.py` refinements (or a new one) so those brands'
  own pages can supply images when retail does not.
  **Acceptance criteria:** [ ] the row moves from `failed` to `ok`/`partial` in a dry-run, [ ] a fixture + test is added, [ ] no other row regresses.
  **Verification:** `--dry-run --redo "<title>" --limit 1`; `pytest tests/test_gallery_extractors.py`.
  **Dependencies:** Task 24 · **Files:** `gallery_extractors/*`, `tests/fixtures/*`, `tests/test_gallery_extractors.py` · **Scope:** Medium
- [ ] **Task 27: Source-list extension** (more PK shops, corrected Subhan/Zeesol domains)
  **Acceptance criteria:** [ ] each new source is platform-verified with a one-line probe, [ ] added to `config.source_platforms` only after it returns hits.
  **Files:** `config.py` (+ optionally `tests/test_sources.py`) · **Scope:** XS

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Fallback images are a third-party shop's choice of photo | Med | S15 re-verification, colour/variant flags, `source=fallback:…` per image, `fallback used: yes` in the report, human review in Task 25 |
| A stale sheet URL silently resolves to a successor model | High | S1 rejects; search term is always the sheet's model; redirects logged |
| Marketplace JSON endpoints change or start blocking | Med | one request per source per row; 1.2 s/host; 403 = skip without retry; sources are one config line to remove |
| Brand pages publish dark marketing renders (Dawlance, TCL as measured) | Med | S7 rejects; fallback to retail; documented as expected, not a bug |
| Secrets leaking into a commit or a log | High | `.env` is git-ignored; values are never logged; startup prints only `set` / `MISSING` |
| Upload host quota / bad key | High | `FREEIMAGE_API_KEY` fail-fast; `PermanentAPIError` flushes the checkpoint and aborts the run; nothing half-published |
| Wrong product published due to a loose match | High | two-rule recognition + guards (code distinctiveness, prefix conflict, other-manufacturer domain) + S15; `brand_match_enabled=False` as the strict switch |
| One page starving the rest of a row's downloads | Med | fixed in Task 22b: per-page budget + `test_a_bad_page_does_not_starve_the_next_candidate` |
| Threshold drift as new brands are added | Low | every threshold is a config field; calibration loop documented in README §5 |

## Open Questions

1. **`FREEIMAGE_API_KEY`** — needed to execute Task 24 (the only blocker for the real run).
2. Should Dawlance's black-background renders ever be accepted (relax S7 per brand), or is the current
   "reject → use retail" behaviour the desired end state?
3. Which additional shops to add in Task 27 (Subhan works on `www.subhanelectronics.pk`; Zeesol needed
   a corrected domain).
4. Is 3 the right per-product target for every category, or should large appliances aim for 4–5?

## Parallelization Opportunities

- **Safe to parallelize:** Task 11b (compose test), Task 26 (brand extractor + fixture), Task 27
  (source list), README/doc updates.
- **Must be sequential:** Task 24 → Task 25 (real run before review); Task 13/14 before Task 24.
- **Needs coordination:** any change to `config.py` (shared rule surface) — land config edits one at a
  time and re-run `pytest` + `mypy` after each.

## Red Flags Check (self-audit against the skill)

- [x] No task is XL; largest are Medium (Task 26, Task 7-class work already done)
- [x] Every task has acceptance criteria and a verification step
- [x] Dependency order respected (foundations → slices → acceptance)
- [x] Checkpoints after every 2–3 tasks
- [x] High-risk items surfaced early (recognition and gates came before hosting)
- [x] No task touches more than ~5 files (Task 7 was split into one-file extractors)

## Verification before starting Phase 9

- [x] `python3 -m pytest tests` → **206 passed**
- [x] `python3 -m mypy --strict .` → **Success: no issues found in 36 source files**
- [x] `python3 main.py --input sample_input.csv --dry-run --workers 4` → **ok 3 / partial 5 / failed 2**
- [ ] Human provides `FREEIMAGE_API_KEY`
- [ ] Human approves the fallback rows policy (Open Question 2)

---

# Phase 11 — Ship pass (2026-10-06, user order: fix bug → clean → re-test → review → skill → docs)

**Status: complete.** Every task below was executed, verified live and is reproducible.

| Task | What was done | Evidence |
|---|---|---|
| **T36 — Dawlance download bug** | Root cause: strict S7 border-ring rejects a brand's *own* studio shots when the product fills the frame (measured within-tolerance 0.964 / 0.983 / 0.725 / 0.130 against the 0.92 bar). Two further bugs starved the 4th photo: the same asset served as `.png` + `.webp` made two dedup keys, and the download allowance was charged per HTTP attempt instead of per picture. Fixes: scoped `S7.official` rescue (`official_gallery_pass`, curated gallery source + `host_is_official` + ≥2 neutral corners), `dedup_key` strips the trailing extension, budget charged once per asset. | Live run: **4 accepted / 1 rejected**, `4 image(s)`, status `ok`; new tests in `test_image_quality.py` + `test_pipeline_end_to_end.py` |
| **T37 — Purge** | 20 unused defs (`product_paste_size`, `status_from_checkpoint`, `white_background`, `_gray_array`, `QualityGate`, `cli`, `identity_pattern`, `text_matches_model`, `url_matches_model`, `score_candidate_order`, `url_hash_payload`, `best_urls`, `extract_meta_title`, `build_assets_check`, `load_banner_cache_for`, `rgb_copy`, 2× test `keys_for`, `brand_domains`, `WEAK_PAGE`); dead config fields `input_path`, `prefer_colour_images`, `wide/tall_aspect_threshold` (+ their validation); 5 unused imports; `tools/`, `assets/previous_placeholder/`, `.env`, `.env.example`, `requirements.txt`, `output/`, `state/`, all caches. `pyproject.toml` is now the only dependency list. | `vulture` audit + AST-verified deletions (`ast.parse` per file), grep re-checks, workspace **30 MB → 692 KB** |
| **T38 — Re-test after the purge** | `ruff check .` → All checks passed! · `mypy --strict .` → 36 files clean · `pytest tests -q` → **213 passed** | tool output (kept in the session log) |
| **T39 — Live end-to-end run** | `python3 main.py --input sample_input.csv --workers 4` on the cleaned tree, real uploads | **ok 5 / partial 5 / failed 0**, 19 + 19 pictures, **40/40 URLs HTTP 200**, Brand on 10/10, banner last, Dawlance **4** pictures — `verification/VERIFICATION.md` |
| **T40 — Production review** | `respect_robots` was documented but dead → wired into `SourceChain`; removed `mark_host_dead`/`_dead_hosts` and the unreachable `parse_shopify_products`; every run now writes a self-explaining `output/README.md`; a full output disk now exits 3 with a plain message instead of a traceback; upload verification switched from a brittle fixed MAE to nearest-neighbour identification (38/38) | code + tests + `verification/VERIFICATION.md` |
| **T41 — Context-engineering skill** | `AGENTS.md` (rules: stack, commands, conventions, boundaries, patterns, error-output discipline), `docs/PROJECT_MAP.md` (data flow, module map, S-rule registry → function → test, task index), `CLAUDE.md` pointer; README links both | files in repo |

## Decisions worth keeping on the record

- **The sheet URL still wins.** Fallback search only runs when the row is unsolved, and every found page
  is re-verified against the *sheet's* model (S15). `H50S80EUX` refused its own sheet URL (it serves
  `H50S80GUX`) and was published only from pages that really show `H50S80EUX` — exactly the intended
  behaviour, no tolerance for one-character substitutions.
- **S1/S3/S7/`verify_page` were not loosened.** The only relaxations are the three scoped ones that were
  approved: the fallback chain, `S7.band`/`S7.seam` (shop furniture on genuine shots) and `S7.official`
  (a brand's own gallery shot that fills the frame, official host only).
- **`output/` is now self-documenting** (generated `README.md`), and every picture in it can be traced
  back to the decision line that admitted it.

---

# Phase 12 — S16 Visual Color Gate & Intra-Gallery Consistency

**Feature name:** Safeguard S16 (Visual Color Gate), Safeguard S17 (Intra-Gallery Consistency), and S4 Model Discrepancy Tokenizer Fix  
**Skill used:** `planning-and-task-breakdown`  
**Problem statement:** E-commerce stores (such as `hadielectronics.com.pk`) frequently bundle all colorway variants (e.g. Golden, Purple, Red) into a single product gallery. When processing a product whose title specifies a distinct color (e.g., `Homage Freezer-on-Top Refrigerator 18 Cu Ft Golden (HRF-47662-GD)`), the pipeline previously accepted Purple and Red refrigerator photos alongside the Golden one.  
**Objective:** Deterministic, high-performance (<2ms/image) foreground color extraction and validation to ensure that candidate images never depict conflicting colorways, and a single product's gallery never mixes contradictory colors.

## Architecture Decisions

1. **Deterministic Vectorized HSV Analysis (No External APIs / No ML Models):**
   - High-speed thumbnail downsampling (`96x96` bilinear PIL resize, ~0.5ms).
   - High-brightness background segmentation (`V > 0.90, S < 0.08` studio white mask) combined with central 75% product bounding mask.
   - Vectorized NumPy HSV conversion and chromatic clustering.
   - Execution time budget strictly bounded under **2.5 ms** per image (zero pipeline throughput impact).
2. **Explicit Hue / Chroma Family Mapping:**
   - Grounded directly in `variant_terms.COLOUR_FAMILIES`.
   - Distinct chromatic cones:
     - `purple`: Hue $260^\circ$ to $330^\circ$ (Magenta-Violet)
     - `red` / `maroon`: Hue $335^\circ$ to $360^\circ \cup 0^\circ$ to $20^\circ$
     - `gold` / `champagne`: Warm low-chroma / yellow-gold hue $25^\circ$ to $65^\circ$, warm tone ($R \ge G > B$)
     - `blue`: Hue $180^\circ$ to $255^\circ$
     - `green`: Hue $75^\circ$ to $160^\circ$
     - `black`: Value $< 0.35$, Chroma $< 0.15$
     - `white`: Value $> 0.75$, Chroma $< 0.12$
     - `grey` / `silver`: Value $0.35$ to $0.75$, Chroma $< 0.15$
3. **Fail-Open Policy for Unambiguous Cases:**
   - If `keys.colour_word` is blank (no color specified in title), S16 is inactive.
   - If the product foreground has negligible chromaticity (achromatic/metallic) and the target color is neutral, accept.
   - Hard rejection triggers **only** on positive chromatic conflict (e.g. image foreground exhibits strong Purple or Red cluster when target is Gold/Silver/White/etc.).
4. **Intra-Gallery Consistency (S17):**
   - Multi-image gallery coherence: if the first accepted image is predominantly of one chromatic family (or neutral), subsequent images in that product gallery must not belong to a contradicting chromatic family.
5. **S4 Model Discrepancy URL Tokenizer Fix:**
   - Improve `url_belongs_to_other_model` so hyphenated filenames like `Homage-HRF-47552-GD-15-Cuft-Refrigerator3` correctly isolate `47552` and reject it when the sheet model is `47662` (and isolate capacity `15-cuft` vs `18-cuft`).

## Task Breakdown

### Task 42: Foreground Colorway Extractor & Classifier (`image_quality.py`)
- **Description:** Implement `extract_foreground_color_profile` and `classify_color_conflict` in `image_quality.py`. Takes a PIL Image, downsamples to 96x96, masks studio background, extracts foreground chromaticity (chromatic ratio, median hue, mean saturation, mean brightness), and compares with the target `colour_word`.
- **Acceptance criteria:**
  - Correctly identifies Image 01 as Purple (hue ~301°) and Image 02 as Maroon/Red (hue ~345°).
  - Flags both as conflicting with target color `golden` (and family `gold`).
  - Benchmarked execution time is $< 2.5$ ms per image.
- **Verification:** Unit test with synthetic patches (pure red, purple, gold, black, white, silver) and actual product fixtures.
- **Dependencies:** None.
- **Files touched:** `image_pipeline/image_quality.py`, `image_pipeline/tests/test_image_quality.py`.
- **Estimated scope:** Small (1-2 files).

### Task 43: S16 Quality Gate Integration & Config (`config.py`, `pipeline.py`)
- **Description:** Add `color_gate_enabled`, `color_chromatic_threshold`, `color_hue_tolerance` to `PipelineConfig`. Wire `verify_image_color` into `pipeline._consume_candidates` right after `run_quality_gates`. Log rejections under rule ID `S16`.
- **Acceptance criteria:**
  - Rejections written to `decisions.jsonl` with rule ID `S16` and detail stating detected color vs expected color.
  - Can be toggled on/off via `PipelineConfig.color_gate_enabled`.
- **Verification:** `pytest tests/test_image_quality.py`, `pytest tests/test_pipeline_end_to_end.py`.
- **Dependencies:** Task 42.
- **Files touched:** `image_pipeline/config.py`, `image_pipeline/pipeline.py`.
- **Estimated scope:** Small (2 files).

### Task 44: S17 Intra-Gallery Color Consistency Tracker (`dedup.py`, `pipeline.py`)
- **Description:** Enhance `ProductDedup` (or create `ProductColorTracker`) to record the chromatic profile of accepted images for the current product. Reject candidate images whose dominant chromatic profile contradicts previously accepted images.
- **Acceptance criteria:**
  - Prevents a product gallery from containing a mix of different colored variants even if the title didn't specify a color.
  - Logs rejection with rule ID `S17` in `decisions.jsonl`.
- **Verification:** Unit test asserting rejection when adding a purple image after a red image.
- **Dependencies:** Task 42, Task 43.
- **Files touched:** `image_pipeline/dedup.py`, `image_pipeline/pipeline.py`, `image_pipeline/tests/test_dedup.py`.
- **Estimated scope:** Small (2 files).

### Task 45: S4 Image URL Model & Capacity Discrepancy Fix (`model_matching.py`)
- **Description:** Refactor `url_belongs_to_other_model` to tokenize hyphenated and concatenated URLs properly, extracting numeric model codes (e.g. `47552` vs `47662`) and capacity terms (`15cuft` vs `18cuft`).
- **Acceptance criteria:**
  - `url_belongs_to_other_model(".../Homage-HRF-47552-GD-15-Cuft-Refrigerator3.jpg", keys_for("HRF-47662-GD 18 Cu Ft"))` returns `True`.
  - Legitimate own-model URLs remain unrejected.
- **Verification:** Unit tests in `test_model_matching.py`.
- **Dependencies:** None.
- **Files touched:** `image_pipeline/model_matching.py`, `image_pipeline/tests/test_model_matching.py`.
- **Estimated scope:** Small (2 files).

### Task 46: Regression Test Suite & Verification (`tests/*`)
- **Description:** Comprehensive unit and integration test suite covering S16, S17, S4 fixes, asserting zero regressions across the existing 336+ tests.
- **Acceptance criteria:**
  - `pytest tests -q` passes 100%.
  - `ruff check .` and `mypy --strict .` pass with zero warnings or errors.
- **Verification:** Full automated CI checks.
- **Dependencies:** Tasks 42–45.
- **Files touched:** `image_pipeline/tests/*`.
- **Estimated scope:** Small.

### Task 47: Live Pipeline Dry-Run Verification on Homage Row
- **Description:** Run `main.py --input sample_input.csv --dry-run` and inspect `Homage Freezer-on-Top Refrigerator 18 Cu Ft Golden (HRF-47662-GD)`.
- **Acceptance criteria:**
  - Purple Image 01 (`Refrigerator3.jpg`) and Red Image 02 (`Refrigerator2.jpg`) are rejected with `S16` (or `S4`).
  - Only Golden Image 03 (`Refrigerator4.jpg`) is accepted.
  - Zero wrong-colored pictures in final output folder.
- **Verification:** Inspect `output/images/homage-freezer-on-top-refrigerator-18-cu-ft-golden-hrf-47662-gd/` and `decisions.jsonl`.
- **Dependencies:** Task 46.
- **Files touched:** Runtime execution.
- **Estimated scope:** Small.

## Risk & Mitigation Matrix

| Risk | Impact | Mitigation |
|---|---|---|
| False rejection of metallic/champagne finishes | High | Champagne/Gold is identified by warm color balance ($R \ge G > B$) and low/medium saturation; only reject when foreground has strong opposing chromatic hue ($H \in [260^\circ, 330^\circ]$ purple or $[340^\circ, 20^\circ]$ red). |
| Performance degradation on large images | High | Always downsample to $96 \times 96$ thumbnail before any array operations. Vectorized NumPy operations take $< 2$ ms. |
| Mixed appliance body (e.g. black door with gold trim) | Medium | Measure both dominant hue and chromatic fraction; multi-panel appliances still pass unless the primary body panel strongly contradicts the target family. |
| Non-standard color terminology | Low | Grounded in existing `variant_terms.COLOUR_FAMILIES`. Unrecognized colors fail open safely. |


---

# Option 2 Implementation Plan — Direct Image URLs (no scraping) · 2026-10-10

## Overview
Option 2 replaces the scraper front half with direct input: each sheet row supplies the
product's own image URLs. The flow per row is: download → validate → branded-template
guard (new rule S19) → white-background gates → colour rule (S16) → dedup (S10) →
isolate/square/sharpen → compose on both templates → upload (S14, banner last) →
import CSVs with hosted links. Everything from the download onward **reuses the Option 1
functions**; no forks. (Gate G10 of `docs/riper/option1-fix-plan.md`, owner-approved
2026-10-10. Input confirmed: `Title,Images` sheet, pipe-separated direct image URLs.)

## Architecture decisions
- New module `direct_pipeline.py` with `process_direct_product()` carrying the same
  contract as `process_product()`; `BatchRunner` gains an optional `processor` param
  (default = Option 1, zero behaviour change).
- New safeguard **S19 "branded template guard"**: background-only test. Store theme
  templates (iShopping yellow, promo frames, logo bands) betray themselves through
  *saturated corner patches*; white/light-grey studio corners never fire it. The check
  never judges the product body, so a product that fills the frame is safe (that case is
  already owned by S7 studio rescue). One function, three config fields, lenient defaults.
- Skipped in direct mode (by design): page fetch/verification S1–S3, gallery extractors,
  fallback chain S15, URL heuristics S4/S5/S12, salvage. Kept: S13 bytes, S8/S6 size,
  S9, S14 emblem, S7 (+studio rescue, +S18 crop closure inside isolation), S16 colour,
  S10 dedup, S14 upload contract.
- Titles without a model code are fine in direct mode (no "4.1" rejection): there is no
  page to prove; `build_model_keys` is still called to extract the colour word for S16.

## Task list (TDD, one commit per task, gates green before every commit)

### Phase 1 — foundation
- [x] Task O2-1: config — `direct_mode` + S19 fields + `__post_init__` validation (`config.py`)
- [x] Task O2-2: S19 gate `branded_template_reason()` (`image_quality.py`), RED tests first

### Checkpoint — Option 1 regression: full suite green (seam not touched yet)

### Phase 2 — seam + processor
- [x] Task O2-3: `BatchRunner` optional `processor` seam (`pipeline.py`, ≤5 lines)
- [x] Task O2-4: `direct_pipeline.py` processor, RED e2e tests first (fakes)

### Checkpoint — direct flow publishes a clean white product with links; blocks a yellow template

### Phase 3 — CLI + real pixels
- [x] Task O2-5: `--direct` CLI wiring (`main.py`), no SourceChain in direct mode
- [x] Task O2-6: real-image fixtures (AC / refrigerator / other / branded template) via
      web tooling; fixture tests + loopback-HTTP `--dry-run` end-to-end

### Phase 4 — docs + review
- [x] Task O2-7: docs sync (AGENTS.md registry, README rule table, PROJECT_MAP, CHANGELOG)

### Checkpoint — complete
- [ ] ruff + mypy --strict + pytest green; per-category results table recorded

## Risks & mitigations
| Risk | Impact | Mitigation |
|---|---|---|
| S19 over-fires on warm-white/neutral studio shots | High (owner's red line) | corner-only evidence, saturation bar 0.30, warm-grey has ~0.03; direct accept tests included |
| S19 misses templates with white corners | Medium | those still hit the S7 white-background gate; S19 is additive, not sole defence |
| Direct URL with no image extension rejected | High (stupid filter) | S5 URL heuristics deliberately NOT applied in direct mode; bytes decide |
| Product filling frame rejected | High | S7 studio rescue + S18 crop closure reused unchanged |
| Option 1 regression via the seam | High | processor defaults to `process_product`; full 330+ suite re-run at every checkpoint |


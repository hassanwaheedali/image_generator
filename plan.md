# PLAN.md — Product Image Pipeline (full build plan)

> This file is the **contract** for the code. Nothing gets written that is not listed here.
> Status legend: ✅ = already written in the workspace · ⬜ = planned, not written yet.

---

## 0. What this program does (one paragraph)

You give it a sheet of products (`Title`, source page URL). For every product it opens the page,
**proves the page is really about that exact model**, pulls out **only that product's own gallery
photos**, throws away junk (ads, logos, banners, QR codes, tiny thumbnails, description pictures,
"related products" pictures, other models' pictures, duplicate angles), cuts the product out of its
white background, makes it square, sharpens it lightly, pastes it on the **DigiMall** template and on
the **Alfa Mall** template, uploads both versions to an image host, and writes **two ready-to-import
CSVs** (one per platform) plus a failed-products CSV, a run report, and a full audit trail on disk.

The single most important rule of the whole program:

> **It is always better to publish nothing for a product than to publish the wrong picture for it.**

A product that ends with zero good images is a **correct result** — it goes to `failed_products.csv`
for a human to look at. The program never "fills the gap" with a fallback image.

---

## 1. Big picture (flow)

```
input sheet (CSV/XLSX)
        │
        ▼
 ┌──────────────┐   1 row = 1 product (Title + 1..N source URLs)
 │ read + merge │   duplicate titles merged, input order remembered
 └──────┬───────┘
        ▼        (4 workers in parallel; every row independent)
 ┌──────────────────────────────────────────────────────────────────┐
 │ per product                                                      │
 │  1. build model keys from the Title      ── no key? → FAIL fast  │
 │  2. pick URLs (official brand domain first)                      │
 │  3. for each URL:                                                │
 │       fetch page → verify page strength (0/1/2)  ── 0 → reject   │
 │       extract gallery candidates (4 allowed sources only)        │
 │       cheap URL filters (S4,S5,S10) → download → quality gates   │
 │       (S13,S8,S6,S9,S7) → isolate → square → sharpen → dedup S10 │
 │  4. enough images? → composite 2 templates → upload both-or-none  │
 │  5. append closing banner (only if ≥1 real image)                │
 └──────┬───────────────────────────────────────────────────────────┘
        ▼
 digimall_product_images.csv | alfamall_product_images.csv | failed_products.csv
 run_report.csv | decisions.jsonl | pipeline_run.log | images/<slug>/... | state/checkpoint.jsonl
```

---

## 2. Input and output

### 2.1 Input
* CSV (read with `utf-8-sig`) or Excel `.xlsx` (via `openpyxl`).
* Header names are matched **case-insensitively and ignoring spaces/underscores**.
* Title column aliases: `Title`, `Product Title`, `Name`.
* URL column aliases: `Source URL`, `SourceURL`, `Images`, `Image URLs`, `URL`, `Source URLs`,
  `Image URL`.
  *(The real sample sheet uses **`Images`**, so this alias is not optional.)*
* URL cell may hold several URLs separated by `|` → they are **ordered candidates**.
* Blank title → row fails with `empty title`. Blank URL → row fails with `no source url`.
* **Duplicate titles are merged**: URLs concatenated in order, de-duplicated, warning logged.
* ⚠️ **Sample-sheet finding (10/10 rows):** the model number sits **inside brackets** in every row
  (`(DW-7500C)`, `(H50S80EUX)`, `(55S80EUX)`, `(H55K85FX)`, `(PAWM-1100)`, `(PMO–23 Kitchen Pro GR`,
  `(85C8L)`, `(98C8L)`, `(PWM-1250)`, `(HRF-47662-GD)`) and row 6 has an **unclosed** bracket.
  Applying the literal rule "delete bracket content, then look for keys" would make **all 10 rows fail**
  with `title has no model-like token`. Safe fix (rescue-only, loosens nothing else) in §3.5.
* Output rows always follow **input order**, even though 4 workers finish in random order.

### 2.2 Brand
`KNOWN_BRANDS: dict[brand, list[domain]]` → reverse map `DOMAIN_TO_BRAND`.
Lookup order: **URL domain → brand**; only if the domain is unknown, the **first alphabetic word of
the title** is tried. Brand is used for (a) a brand-specific gallery extractor and (b) preferring
official domains when a product has several URLs. `KNOWN_BRANDS` holds **manufacturers only** (Haier, Dawlance, PEL, TCL, Homage, Orient, Gree,
Samsung, LG…); retail marketplaces live in a separate constant `RETAIL_MARKETPLACE_DOMAINS`
(Daraz, PriceOye, Alfatah, Alfamall, DigiMall, Symbios…), because a shop is not a brand.
`host_is_other_manufacturer(host, brand)` is the guard that stops a Dawlance row from being
satisfied by a `samsung.com` page; marketplaces never trigger it.

### 2.3 Assets (fixed files, loaded once, checked **before** any work)
`assets/DigiMall.png`, `assets/AlfaMall.png` (templates), `assets/digi_last.png`,
`assets/alfa_last.png` (closing banners). If any is missing → **fail fast** and print every missing
path it looked for. Names are configurable.

### 2.4 Output tree
```
output/
├── digimall_product_images.csv   Title,Urls   (| separated, DigiMall hosted URLs, banner LAST)
├── alfamall_product_images.csv   Title,Urls   (| separated, Alfa hosted URLs, banner LAST)
├── failed_products.csv           Title,Reason   (ONLY failed rows)
├── run_report.csv                Title,Status,Pages_Tried,Pages_Rejected,Images_Accepted,Note
├── images/<title-slug>/  raw/  isolated/  final_digimall/  final_alfamall/
├── decisions.jsonl               one JSON line per image/page decision (title,url,decision,rule_id,detail)
└── pipeline_run.log
state/
├── checkpoint.jsonl              append-only per-product result (drives resume)
└── banner_urls.json              uploaded banner URLs keyed by file content hash
```
* **Status:** `ok` (≥ `min_required_images`), `partial` (1 … min-1), `failed` (0).
* `ok` + `partial` go to both platform CSVs. **Only `failed` goes to `failed_products.csv`.**
* Closing banner is appended **only when the product has ≥ 1 real image** — banner-only rows are
  never produced.
* Every CSV / JSON write is **atomic** (temp file in the same folder + `os.replace`).
* `--dry-run`: everything except the real upload and the real output CSVs (still writes
  `decisions.jsonl` + `run_report.csv` + local final images) — used to calibrate thresholds.

---

## 3. Matching rules (the heart)

### 3.1 Model keys from the Title (`model_matching.build_model_keys`)
1. Remove everything inside `( )` and `[ ]`.
2. Normalise whitespace.
3. A token is **model-like** if, after stripping surrounding punctuation:
   * ≥ 3 alphanumeric characters, **and**
   * contains **at least one letter and at least one digit**, **and**
   * is **not** a unit/quantity token
     (`^\d+([.,]\d+)?(kg|kgs|g|ltr|ltrs|l|liter|litre|ton|tons|btu|inch|in|w|kw|hz|mm|cm|m|gb|tb|mah|v|cft|cuft)$`).
     So `12000BTU`, `1.5Ton`, `9KG`, `55inch` are **not** keys.
4. Pure numbers (`2024`, `1.5`, `12`) are **never** keys.
5. Keys = model-like tokens (deduplicated, in order) **+ the whole cleaned title** as one extra key
   (the whole-title key never counts as a token).
6. **Zero model-like tokens ⇒ the row fails immediately**: `title has no model-like token`,
   no page is fetched at all. (`allow_title_only_verification` default `False` can relax this.)

Example: `Haier HSU-12HFPAA 1.5 Ton Inverter (White)` → keys `["HSU-12HFPAA"]` + title key.
`1.5`, `Ton`, `Inverter`, `White` are not keys.

### 3.2 Flexible pattern (`model_matching.key_pattern`, cached)
Characters of the key (alphanumerics only, lowercased) joined by `[\s\-_/.]*`, wrapped in
`(?<![a-z0-9])…(?![a-z0-9])`. So `HSU-12` matches `hsu12`, `HSU 12`, `hsu_12` but **not** the
`HSU-12` inside `HSU-12HFPAA` and **not** `AHSU-12`.

### 3.3 Page strength (`page_verification.verify_page`)
| Strength | Meaning | Consequence |
|---|---|---|
| **2 strong** | key found in URL path, `<title>`, `og:title`, any `<h1>`, or a JSON-LD `Product/IndividualProduct` node (`name`/`sku`/`mpn`/`model`/`productID`/`url`) | normal extraction |
| **1 weak** | key found **only** in body text after header/footer/nav/related/share regions are stripped | **normal extraction** (customer decision 2026-10-05): once the number is on the page, the gallery is taken. The whole-page fallback still needs strength 2. |
| **0 none** | not found anywhere | **reject the whole page, extract nothing (S1)** |

### 3.4 "This image belongs to another model" (S4 operational definition)
For a candidate image URL take the filename + parent folder and pull out **code-like tokens**
(matched against keys **and** identity patterns, §3.5.4, so glued filenames are not mis-flagged)
(letters+digits, ≥ 3 alphanumerics; ignoring `800x800`, long hex hashes, `IMG_1234`/`DSC0001` camera
names and dates):
* some token matches this product's key → **keep**
* else a token shares the **same alphabetic prefix** as one of this product's keys
  (`HSU-12HFPAA` vs image `HSU-18HFPAA-front.png`, both `HSU…`) → **reject as `other_model`**
* else (no token, or an unrelated code like `img123`) → **neutral**, other gates decide.

---

## 3.5 Model-key edge cases (settled rules, with the reasoning)

### 3.5.1 "Sheet says `HRF-538 IFGA`, target says `538 IFGA`" → reject alone, accept with the brand

Keys from the title = `HRF-538` (letters+digits, ≥3 chars) + the whole-title key; `IFGA` is **not** a
key (no digit) and `538` is **not** a key (pure number, rule 4.1.4). So a bare tail proves nothing.
**Customer rule (§3.5.3): brand + tail = same product.** With `Haier` present in the same strong
field, `538 IFGA` **is** a match; with no brand anywhere it is still rejected.

| Target shows | Match? | Note |
|---|---|---|
| `HRF-538 IFGA` | ✅ exact | separators are free: `hrf 538 ifga`, `HRF_538`, `hrf-538` |
| `HRF538IFGA` (glued, no separator) | ✅ | glued spelling of the whole identity `HRF-538 IFGA` - see §3.5.4 |
| `HSU-12HFPAA` for sheet `HSU-12` (nothing after the token in the title) | ❌ | key extended by characters the title never contained → different variant |
| `Haier Refrigerator 538 IFGA INV` | ✅ **brand + code** | brand token present **and** the code `538ifga` appears with no letters in front of it |
| `Haier RF-538 IFGA` | ✅ **brand + code** | the leading letters are the sheet's own family (`rf` of `hrf538ifga`) |
| `Haier 55 Inch QLED TV H55S80EUX` for sheet model `55S80EUX` | ✅ **brand + code** | the `H` is the brand initial |
| `Refrigerator 538 IFGA INV` (no brand anywhere) | ❌ | a code alone never certifies a page |
| `Haier HSF-538 IFGA` (same digits, different letters) | ❌ | the letters in front of the code (`hsf` → `sf`) are not the sheet's own (`rf`) |
| image `HRF-538-IFGA-1.jpg`, strong page | ✅ | S4 matches the key ⇒ keep |
| image `538-ifga-1.jpg`, **strong** page | ✅ | S4 neutral ⇒ the other gates decide |
| image `538-ifga-1.jpg`, **weak (strength 1)** page | ❌ | weak pages are URL-confirmed-only |

**Rule of thumb: the *page* proves the model, not the image filename.**

### 3.5.2 Bracketed model numbers (fixes all 10 sample rows)

Rule 4.1.1 (drop bracket content) stays as written, but with two **rescue-only** config flags,
both ON by default. They can only turn a *hard-failing row* into a *checkable row* — they never
loosen a page, an image or any safeguard:

1. `use_bracketed_keys_as_fallback = True` — brackets are still ignored for normal parsing; **only if**
   that yields zero model-like tokens, the bracketed text is re-scanned for keys
   (`(HRF-47662-GD)` → key `HRF-47662-GD`). The `(White)` / `(1.5 Ton)` noise case is unaffected
   (`1.5` and `Ton` are never keys anyway).
2. `unbalanced_bracket_keeps_text = True` — an unclosed `(` (sample row 6) keeps its tail instead of
   deleting it, so `PMO–23` becomes a key. (En dash is normalised away.)
3. Rescued rows still face the complete S1–S14 gauntlet. No fallback image source is introduced (S12).

### 3.5.3 Brand + code match (the one relaxed rule, ON by default)

Customer rule: `HRF-` is Haier's own family prefix, so when the page names the **brand** and shows the
**code** (`538 IFGA`), it is the same product even though the prefix is missing. This is the *only*
relaxation in product recognition; everything else stays strict.

A page matches with `match_rule="brand_code"` **only when all of these hold**:

1. `brand_match_enabled=True` and a brand was detected (`detect_brand`: manufacturer domain → brand,
   else the first alphabetic word of the title). No brand ⇒ the rule is off for that row.
2. The brand name appears as a **whole word** in the text being tested (title/H1/URL, or body text).
3. The code is **distinctive**: ≥ `brand_match_min_length=5` alphanumerics, ≥ `brand_match_min_digits=3`
   digits and at least one letter (`538ifga` ✅ · `12` ❌ · `7500` ❌). These two numbers are config
   fields, not constants.
4. Separators are free (`538 IFGA` = `538-IFGA` = `538ifga`), and the code must sit on a whole-token
   boundary.
5. **Letters in front of the code are checked.** The code is accepted at a position only when the
   letters immediately before it (`HSF-538` → `hsf`) are, after dropping one brand initial,
   a **prefix of the sheet's own family letters**. Spelled out:
   nothing (`538 IFGA`) ✅ · brand initial only (`H55S80EUX` for `55S80EUX`) ✅ ·
   sheet's family (`RF-538 IFGA`, `HRF-538 IFGA`) ✅ · anything else (`HSF`, `HQ`, `SF`) ❌.
   A word or a slash between letters and code means "no prefix" (`Haier 538 IFGA`,
   `/p/538-ifga.jpg`) - those letters can never be mistaken for a family.
6. The page domain is not another **manufacturer** domain (`host_is_other_manufacturer`), so a Dawlance
   row can never be satisfied by `samsung.com`. Retail marketplaces (`daraz.pk`, `priceoye.pk`,
   `alfatah.com.pk`, `alfamall.pk`, `digimall.pk`, ...) are a separate constant and never trigger it.

The accepted spellings of a code are produced by `brand_match_cores()`:
`hrf538ifga` → `("hrf538ifga", "rf538ifga", "538ifga")`, each also tried with a dropped trailing letter
(§3.5.2). `55s80eux` has no leading letters, so it yields exactly one spelling.

**S12 anchor:** the whole-page fallback accepts an image URL when it is anchored to this product -
the plain model key **or** one of those brand+code spellings (`url_matches_this_product`). A URL with
no code at all is still never taken, which is what keeps S12's "no fallback image sources" true.

**Extraction:** a `brand_code` page behaves exactly like any other verified page - sources 1-5, all
gates S2-S14 unchanged (customer decision: the sheet's URLs are chosen by hand).

**Audit trail:** `PageMatchResult.match_rule ∈ {model_key, brand_code, body_model_key, body_brand_code,
none, other_brand_domain}`; `decisions.jsonl` records `S1` (rejected) or the acceptance rule; the Note
shows `brand+code match: haier + 538ifga`.

**Residual risk (accepted by customer, 2026-10-05):** if a site prints neither the prefix nor the
brand, two families sharing the same code are indistinguishable (`HRF-538 IFGA` vs `HSF-538 IFGA`).
Guards 3-6 shrink this; they cannot remove it. `brand_match_enabled=False` restores strict
model-key-only behaviour.

### 3.5.4 Glued (separator-free) spellings - identity keys### 3.5.4 Glued (separator-free) spellings - identity keys

A key may be extended **only by the title's own tail**, never by characters the title does not contain.

* **Identity pattern** = key-core + tail-core joined into one separator-tolerant regex. `Haier HRF-538 IFGA`
  yields an identity pattern that matches `HRF538IFGA`, `HRF-538-IFGA`, `hrf 538 ifga`, `HRF_538_IFGA` -
  all the same product, separators being optional everywhere.
* **No tail in the title** (e.g. `Haier HSU-12 1.5 Ton`) ⇒ no identity pattern ⇒ the page token
  `HSU12HFPAA` is still rejected. That is the original `HSU-12` != `HSU-12HFPAA` protection, intact.
* A page token extending the key with *other* letters (`HRF538XYZ`) matches neither the identity pattern
  nor the boundary-anchored bare key ⇒ rejected.
* The same identity keys are used by S4, so an image named `HRF538IFGA-1.jpg` is **kept**; under the
  strict rule it would have been mis-flagged `other_model` (its alphabetic prefix `hrf` equals the key's)
  - a false rejection.
* Config: `allow_glued_identity_match = True` (default). Off = only separator-optional matching of the
  bare key (the stricter behaviour).

### 3.6 Colour + variant check (customer decisions, 2026-10-05)

Principle: **loose about recognising the product, strict about publishing a wrong image.**
The sheet's source URLs are human-checked, so page verification is deliberately simple.

| Check | Rule | Outcome |
|---|---|---|
| Model number | number must appear on the page (separators free, glued spellings OK, or brand + number-tail) | no match -> page rejected (S1) |
| **Colour** | title names a colour **and** the page's own product name names a *different* colour | **row is kept and flagged**: Note `colour mismatch (flagged): title says gold, page says black` (customer decision 2026-10-05: flag, do not fail) |
| Colour (silence) | title names a colour, page does not mention one | accepted - not strict |
| **Variant** | title says `10 kg` / `Top Load` / `Double Door` and the page says a different value on the same axis | **publish + flag** in `run_report.csv` Note (`variant mismatch: capacity_kg 10 vs 12`) |
| Variant (silence) | page does not mention the axis | accepted |
| Photos | colour is used **only for ordering** (matching-colour photos first); a photo is never rejected for its colour | ordering only (customer choice) |

Colour families (synonyms collapse): white(off white, ivory-white) - black - grey(silver, graphite,
stainless steel, chrome) - gold(golden, champagne, bronze, copper) - beige(cream, ivory) - brown -
red(maroon, burgundy) - blue(navy) - green - purple - pink - orange - yellow.
Variant axes compared: capacity (kg/g/l/ton/btu/cuft), size (inch/cm/mm/ft), power (w/kw), plus type
words top/front load, semi/fully automatic, twin tub, single/double/side-by-side door, chest/vertical
freezer. `display` technology (LED vs QLED) is deliberately **not** an axis (too noisy on real pages).

Config: `check_colour_match=True`, `flag_variant_mismatch=True` (both are flags only - they annotate
`run_report.csv` and never reject a photo).
There is no "fail the row on colour" switch any more - the customer decision is to flag and publish.
Both flags land in the same free-text Note column, so one row can carry several flags.

A weak page (model only in the body text) is treated as a **normal** page (`match_rule` =
`body_model_key` / `body_brand_code`): its gallery is used exactly like a strong page's, still passing
S2-S14. No separate "weak page" mode exists any more.

### 3.7 Live calibration (real sample sheet, 2026-10-05)

Three passes over `sample_input.csv` (10 rows, `--dry-run`) against the real sites; every change below
is covered by a test:

| Finding | Fix |
|---|---|
| Sheet `DW-7500C`, Dawlance page prints `DW-7500` ⇒ rejected | trailing-letter tolerance (`truncation_variants`, `allow_key_truncation_match=True`, test `test_key_truncation_matches_sheet_suffix`) |
| Row 2 URL serves `H50S80GUX` while the sheet says `H50S80EUX` (wrong URL in the sheet) | kept as a correct `reject:S1`; the Note now names the code the page shows: `no model key found on the page (page shows h50s80gux)` |
| Dawlance/TCL galleries are CDN renders on **black** backgrounds | correct `reject:S7`; the Note says `(gallery is not white-background)` so a human sees why in one line |
| Every Dawlance photo shared one `dedup_key` (`.../1000Wx1000H/image.webp`) | resize segments are ignored when computing the key, while the **downloadable URL stays untouched** (the transformed path is the only one that exists): `test_cdn_transform_segments_are_kept_for_fetching`, `test_dedup_key_sees_through_cdn_transforms` |
| S3 rejections on Alfatah/Daraz were that site's promo banner (`Leader-Board.webp` → `/grand-sales-gala-2026/`), app-store badges and a *different* product | S3 was right; those rows fail for lack of a working gallery extractor for the site - the documented extension path (README "add a brand extractor") |
| One-line reasons were useless for debugging | `run_report.csv` Note now carries the rejection histogram (`images rejected: S7 x5, S3 x1`) plus the page-level reason |

Final dry-run state of the sample sheet: **ok 1 · partial 4 · failed 5** (was ok 1 · partial 3 ·
failed 6 before the simplifications). The 5 failures are: 1 wrong source URL in the sheet, 2 galleries
that are black-background renders, 2 sites that need a gallery extractor.

---

## 4. Safeguards S1–S14 (each one a named function + a direct test)

| ID | Safeguard | Function | Test |
|---|---|---|---|
| S1 | No page ⇒ no images (strength 0) | `page_verification.verify_page()` | `test_strength0_page_yields_no_images` |
| S2 | Never take images from description/article content (ancestor check) | `page_verification.is_in_description_region()` | `test_description_images_never_extracted` |
| S3 | No related/recommended/upsell images (link to another page) | `page_verification.is_linked_to_other_page()` | `test_related_carousel_excluded` |
| S4 | No image whose own URL belongs to another model | `model_matching.url_belongs_to_other_model()` | `test_other_model_url_rejected` |
| S5 | No ads / banners / badges / marketing graphics (token keywords) | `url_filters.has_bad_keyword()` | `test_cartridge_not_rejected_cart_rejected` |
| S6 | No banner-shaped images (`max/min > 3.5`) | `image_quality.has_extreme_aspect()` | `test_wide_strip_rejected` |
| S7 | Clean background only (whole border ring, ≥2 neutral "bright" corners) | `image_quality.border_is_clean()` | `test_white_corners_busy_middle_rejected` |
| S8 | Minimum real size (short ≥ 180 px, long ≥ 350 px) | `image_quality.is_large_enough()` | `test_tiny_thumbnail_rejected` |
| S9 | No QR / barcodes (filename token **and** real decode) | `image_quality.is_qr_or_barcode()` | `test_qr_rejected` |
| S10 | De-dup **per product only** (state created inside `process_product`) | `dedup.ProductDedup` | `test_dedup_state_not_shared_across_products` |
| S11 | Excluded domains / non-product paths checked **before** any fetch | `url_filters.is_fetchable_product_url()` | `test_listing_and_blog_urls_not_fetched` |
| S12 | **Zero fallbacks** — no description images, no CSV image column, no "primary image" | architectural + scoped whole-page fallback | `test_pipeline_has_no_fallback_source` |
| S13 | Bytes must really be an image (Content-Type, no SVG, no animated GIF, ≤15 MB, Pillow decode, `MAX_IMAGE_PIXELS` guard, EXIF rotation) | `image_quality.validate_image_bytes()` | `test_non_image_and_oversize_rejected` |
| S14 | Nothing uploaded until the product's final selection is fixed; **both platforms or neither** | `pipeline.finalize_and_upload()` | `test_failed_product_uploads_nothing` |

**Three zero-tolerance guarantees** (each has its own test):
1. Zero images from description/article content → S2 + `test_zero_images_from_description_regions`.
2. Zero images from links pointing at another page → S3 + `test_zero_images_from_other_page_links`.
3. Zero images carried between products (no shared mutable state) → S10 + `test_no_cross_product_image_leakage`.

**Allowed image sources — and nothing else:** (1) a registered brand/platform extractor,
(2) JSON-LD `Product` node whose own name/sku/mpn matches the keys, (3) dedicated gallery
containers, (4) strictly-scoped whole-page fallback (strength 2 + no container + every URL carries a
model key). Every accepted image is traceable to one of these four in `decisions.jsonl`.

---

## 5. PHASE 2 decisions (the four risky choices)

| Risky decision | Option A | Option B | Picked | Why |
|---|---|---|---|---|
| Description images inside a tab/article wrapper | plain CSS ancestor match | strip pass **marks** stripped subtrees (`data-pipeline-stripped="1"`), ancestor walk stops at the nearest marker | **B** | a gallery container that happens to sit inside a tab wrapper survives, while a real `.description` block still rejects everything inside it |
| Extractor registry signature | `dict[str, Callable[[ExtractionContext], list[str]]]` (bare URLs) | same dict, but returns `ImageCandidate` with `source` + `order` | **B** | traceability to the four allowed sources is mandatory; provenance must exist before filtering |
| Thread pool / checkpoint / atomic writes | each worker writes outputs | worker returns `ProductResult`; **one writer** under a single lock appends checkpoint and regenerates the CSVs; atomic `os.replace` | **B** | deterministic ordering, no interleaved writes, Ctrl-C flush is easy, `PermanentAPIError` → cancel flag + flush |
| White product on white background | lower the mask threshold globally | keep thresholds, add a scoped fallback: mask < 3 % ⇒ bbox of pixels differing from pure white by ≤ 10 (shadows included) | **B** | strictness preserved for the normal case; the fallback only ever produces a **crop**, never a blank/synthetic image |

---

## 6. PHASE 3 — file-by-file plan

> Rule: network + filesystem only in `fetch.py`, `image_host.py`, `csv_io.py`, `pipeline.py`.
> `model_matching`, `url_filters`, `image_quality`, `dedup`, `compositing`, `page_verification`,
> `gallery_extractors` are pure/testable logic. No module-level mutable per-product state anywhere.

### 6.1 `config.py` ✅
* `class ConfigError(ValueError)`
* `@dataclass(frozen=True) class PipelineConfig` + `__post_init__()` validation (quotas, workers,
  `min ≤ max`, ratios in (0,1], backoff order, aspect thresholds, colours…).
* `_read_api_key() -> str` — reads `FREEIMAGE_API_KEY` **only**; there is no default in code.
* Constants: `OUTPUT_HEADER = ("Title","Urls")`, `FAILED_HEADER`, `REPORT_HEADER`,
  the four asset-name defaults.
* Properties: `asset_paths`, `images_dir`, `decisions_path`, `log_path`, `digimall_csv`,
  `alfamall_csv`, `failed_csv`, `report_csv`, `checkpoint_path`, `banner_cache_path`.
* **Every threshold in this document is a field here** (no magic numbers in algorithms).

**`PipelineConfig` fields (name = default)**

| Group | Fields |
|---|---|
| paths | `input_path="input.csv"`, `assets_dir="assets"`, `output_dir="output"`, `state_dir="state"` |
| assets | `digimall_template_name="DigiMall.png"`, `alfamall_template_name="AlfaMall.png"`, `digimall_banner_name="digi_last.png"`, `alfamall_banner_name="alfa_last.png"` |
| run | `dry_run=False`, `workers=4`, `limit=None`, `redo_titles=()`, `resume=True` |
| quotas | `min_required_images=2`, `max_images_per_product=5`, `allow_title_only_verification=False`, `unbalanced_bracket_keeps_text=True`, `use_bracketed_keys_as_fallback=True` |
| recognition (two rules) | `allow_key_truncation_match=True`, `brand_match_enabled=True`, `brand_match_min_length=5`, `brand_match_min_digits=3`, `brand_match_max_extra_tokens=2`, `brand_match_reject_other_brand_domain=True`, `allow_title_only_verification=False` |
| flags (never reject) | `check_colour_match=True`, `flag_variant_mismatch=True` |
| fetching | `request_timeout_s=25.0`, `max_retries=3`, `backoff_base_s=0.8`, `backoff_max_s=8.0`, `domain_min_interval_s=1.2`, `impersonate="chrome120"`, `user_agent=…`, `accept_language="en-US,en;q=0.9"` |
| url filter | `block_collections_without_products=True` |
| S13 | `max_image_bytes=15_728_640`, `max_image_pixels=40_000_000` |
| S6/S8 | `min_short_side=180`, `min_long_side=350`, `max_aspect_ratio=3.5` |
| S7 | `border_ring_ratio=0.06`, `border_ring_min_px=3`, `border_clean_min_fraction=0.92`, `border_bg_tolerance=28.0`, `border_std_max=22.0`, `neutral_bright_min_value=0.86`, `neutral_bright_max_saturation=0.14`, `min_neutral_corners=2`, `corner_patch_ratio=0.12` |
| dedup | `dedup_hamming_threshold=6`, `dedup_hamming_soft_threshold=14`, `dedup_corr_threshold=0.995` |
| compositing | `canonical_side=1200`, `padding_px=6`, `isolation_bg_distance=32.0`, `isolation_min_foreground_fraction=0.03`, `isolation_min_component_ratio=0.02`, `isolation_neighbor_distance_px=35.0`, `degenerate_crop_ratio=0.03`, `white_fallback_tolerance=10`, `unsharp_radius=0.75`, `unsharp_percent=30`, `unsharp_threshold=4`, `product_fill_ratio=0.92`, `template_side_margin_ratio=0.04`, `template_bottom_margin_ratio=0.03`, `template_top_scan_ratio=0.35`, `template_header_gap_px=8`, `template_default_top_margin_px=90`, `template_white_cutoff=250`, `template_make_white_transparent=True`, `wide_aspect_threshold=1.25`, `tall_aspect_threshold=0.80`, `optical_center_bias=-0.02`, `jpeg_quality=95` |
| host | `api_key=<env>`, `upload_min_interval_s=0.6` |

### 6.2 `models.py` ✅
All frozen dataclasses: `ProductTask(index,title,source_urls)` (+`url_hash_payload()`),
`ModelKeys(cleaned_title,tokens,title_key)` (+`all_keys`,`__bool__`),
`PageMatchResult(url,strength,matched_keys,detail,match_rule,variant_notes,page_name)` (+`accepted`; `match_rule ∈ {model_key, brand_code, body_model_key, body_brand_code, none, other_brand_domain}`; `variant_notes` carries the colour and variant **flags**),
`ImageCandidate(url,page_url,source,order,matches_model)`,
`ImageDecision(title,url,decision,rule_id,detail)`,
`AcceptedImage(source_url,dedup_key,raw_path,square_path,digi_path,alfa_path)`,
`ProductResult(task_index,title,status,digi_urls,alfa_urls,reason,pages_tried,pages_rejected,
images_accepted,note,source_url_hash)` (+`best_urls`,`to_checkpoint_dict()`),
`CheckpointEntry(title,source_url_hash,status,digi_urls,alfa_urls,reason,timestamp)`
(+`from_dict()`,`is_resume_done(hash)`).
Enums: `Status(ok|partial|failed)`, `Decision(keep|reject)`,
`ImageRole(raw|isolated|final_digimall|final_alfamall)`.

### 6.3 `protocols.py` ✅
`PageResponse(url,status_code,text)`, `BinaryResponse(url,status_code,content,content_type)`;
`PageFetcher.fetch_page(url)`, `ImageFetcher.fetch_image(url,referer)`,
`ImageHost.upload(data,filename) -> str`. All `Protocol` + `runtime_checkable`.
This is what lets the whole pipeline run in tests with fakes and **no network**.

### 6.4 `fetch.py` ✅
`FetchError(RuntimeError)`, `PermanentAPIError(FetchError)`,
`classify_http_status(status) -> "ok"|"permanent"|"transient"`,
`DomainRateLimiter(min_interval_s).wait(url)` (per-host, thread-safe),
`retry_with_backoff(operation, *, max_retries, base_delay_s, max_delay_s, description,
sleeper=time.sleep, random_fn=random.random, attempts_holder=None)` — **pure, injectable**,
`CurlCffiPageFetcher`, `CurlCffiImageFetcher` (thread-local `curl_cffi` sessions,
`impersonate="chrome120"`, realistic UA, `Referer` = page URL; 404/410/403 = permanent, no retries).

### 6.5 `model_matching.py` ✅
`normalize_text`, `normalize_key`, `key_pattern` (lru_cache), `model_like_token`,
`strip_bracketed`, `build_model_keys` (lru_cache), `find_matching_keys` (lru_cache),
`text_matches_model`, `url_matches_model`, `code_like_tokens`, `url_belongs_to_other_model` (S4),
`score_candidate_order`, `tail_key`, `title_tails`, `find_matching_tails`, `brand_token_present`, `conflicting_prefix_token`, `identity_keys`, `identity_pattern`.

### 6.6 `url_filters.py` ✅
Constants: `BAD_URL_KEYWORDS` (logo, icon, badge, payment, cart, review, banner, flag, footer,
whatsapp, facebook, instagram, twitter, linkedin, pinterest, share, sharer, avatar, placeholder,
pixel, spacer, qr, qrcode, barcode, warranty, guarantee, seal, certified, energy-guide, star-rating,
special-price, topimg, newpro), `MULTI_WORD_BAD_KEYWORDS` (energy-guide, star-rating, special-price,
water-saving, cold-plasma), `EXCLUDED_DOMAINS`, `NON_PRODUCT_PATH_PATTERNS` (`/category/`,
`/collections/`, `/blog/`, `/news/`, `/about`, `/contact`, `/support/`, `/service/`, `/warranty/`,
`/press/`, `/media-center/`, `/campaign/`, `/promotions/`, `/search`, `?s=` …), `KNOWN_BRANDS`,
`DOMAIN_TO_BRAND`, `RETAIL_MARKETPLACE_DOMAINS`, `IMAGE_EXTENSIONS`.
`url_path_tokens`, `has_bad_keyword(url, extra_keywords=())` (S5 — whole-token only, so
`cartridge` ≠ `cart`, `silicon` ≠ `icon`; `banner2` blocked by `banner`),
`detect_brand`, `official_domains`, `prioritize_source_urls`,
`is_fetchable_product_url(url, *, block_collections_without_products=True)` (S11),
`canonicalize_url(url, page_url)`, `dedup_key(canonical_url)`, `host_is_other_manufacturer(host, brand)`.

Canonicalisation rules (section 6.2 of the spec, implemented here):
resolve relative + `//cdn…`; strip `_800x800`, `-500x500`, `_medium`, `_large`, `_grande`, `_thumb`,
`_compact`, `_scaled`; drop tracking params; **drop the whole query only when the path ends in a real
image extension** (so `image.php?id=123` keeps `?id=123`); skip `data:` URIs and 1×1 pixels.

### 6.7 `image_quality.py` ⬜
`ImageValidationError(Exception)` (`rule_id`, `detail`),
`validate_image_bytes(content, content_type, *, max_bytes, max_pixels) -> Image` (S13),
`is_large_enough(img, *, min_short_side, min_long_side) -> bool` (S8),
`has_extreme_aspect(img, *, max_aspect_ratio) -> bool` (S6),
`border_is_clean(img, *, ring_ratio, ring_min_px, tolerance, std_max) -> bool` (S7),
`corner_patch_stats(img, ratio) -> list[(value, saturation)]`,
`neutral_corner_count(img, ratio, min_value, max_saturation) -> int`,
`is_qr_or_barcode(img, url, *, extra_keywords=()) -> bool` (S9),
`IMAGE_GATE_TABLE` (ordered `S8 → S6 → S9 → S7`),
`to_rgb`, `white_background`, `thumbnail_for_hash`.

### 6.8 `dedup.py` ⬜ (S10)
`ImageFingerprint(dhash:int, gray2d:np.ndarray)`, `compute_fingerprint(img)`,
`hamming_distance(a,b)`, `is_visual_duplicate(fp, existing, *, hard_threshold, soft_threshold,
corr_threshold)`, `class ProductDedup(*, hard_threshold, soft_threshold, corr_threshold)` with
`.is_duplicate(img)`, `.add(img,url) -> bool`, `.__len__()`. **No module-level state.**

### 6.9 `compositing.py` ⬜
`detect_background_color(img, *, patch_ratio)`,
`isolate_main_product(img, config) -> (Image, degraded:bool)`,
`white_fallback_bbox(img, tolerance) -> (x0,y0,x1,y1)|None`,
`to_square_canvas(img, side, background)`, `sharpen(img, *, radius, percent, threshold)`,
`header_safe_margin(template, config) -> int` (scans the top ~35 % for non-transparent/non-white
pixels — no hard-coded logo height), `template_content_box(template, config)`,
`product_paste_size(product, box_w, box_h, *, fill_ratio, wide, tall)`,
`apply_template(product_square, template, config)`,
`make_white_transparent(template, cutoff)`,
`to_jpeg_bytes(img, *, quality, background=(255,255,255))`.
Isolation = corner colour → distance mask → connected components (scipy) → largest + neighbours
(< 35 px away and ≥ 2 % of the largest) → crop with 6 px padding; guards: crop < 3 % of the source ⇒
use the uncropped image; mask < 3 % ⇒ white-product fallback bbox; never an empty/failed crop.

### 6.10 `page_verification.py` ⬜
* `DESCRIPTION_REGION_SELECTORS` — **all 15 required**: `#description`, `.description`,
  `.product-description`, `.product__description`, `#tab-description`, `.woocommerce-Tabs-panel`,
  `.tab-content`, `.tab-pane`, `.product-tabs`, `.long-description`, `.rte`, `.entry-content`,
  `article`, `[itemprop="description"]`, `[id*="description" i]`, `[class*="description" i]`,
  `[class*="specification" i]`, `[class*="overview" i]`.
* `UNWANTED_REGION_SELECTORS` (header, footer, nav, related/upsell/cross-sell/recently-viewed/
  recommendation blocks, share bars, cookie bars, modals).
* `STRIPPED_ATTR = "data-pipeline-stripped"`.
* `strip_unwanted_regions(soup, page_url) -> BeautifulSoup` — removes unwanted regions and **marks**
  them (this mark is what lets a gallery inside a tab wrapper survive S2).
* `is_in_description_region(tag, *, boundary) -> bool` (S2) — ancestor-only check; stops at the
  nearest `STRIPPED_ATTR` marker; a marked subtree is never a "description region".
* `is_linked_to_other_page(tag, page_url) -> bool` (S3) — walks up to the closest `<a>`; different
  host+path ⇒ excluded, **unless the href is the image file itself** (lightbox).
* `page_body_text(soup)`, `extract_meta_title(soup)` (title / og:title / twitter:title / h1),
  `verify_page(page_url, html, keys, *, brand: str | None, settings: MatchSettings | None) -> PageMatchResult` (S1) — delegates the whole decision to `model_matching.match_product()` (model key **or** brand+code, §3.5.3), rejects another manufacturer's domain, and records colour/variant flags without rejecting.

### 6.11 `gallery_extractors/` ⬜
* `__init__.py` — `ExtractionContext` dataclass (`page_url`, `soup`, `keys`, `strength`, `config`,
  `raw_html`), `ExtractorFn` type, `REGISTRY: dict[str, ExtractorFn]` keyed by **domain substring**,
  `register_extractor(*domains)` decorator (adding a brand = one file + one decorator line,
  **no if/elif ladder anywhere**), `extract_candidates(ctx)` = sources 1→4 then the scoped 5,
  `iter_gallery_nodes`, `candidates_from_tags`, `collect_img_urls` (srcset → largest width,
  `data-large_image`, `data-zoom-image`, `data-highres`, `data-full`, `data-original`, `data-src`,
  `data-lazy-src`, `src`; canonicalised, `ImageCandidate`s with provenance).
* `jsonutil.py` — `safe_json_loads`, `iter_json_objects` (tolerant raw-JSON scanning for
  `mage/gallery/gallery` blobs; **no LLM, no guessing**).
* `json_ld.py` — only `Product`/`IndividualProduct` nodes whose **own** `name/sku/mpn/model/
  productID/url` matches the keys; never descends into `ItemList`/related nodes.
* `embedded_json.py` — generic Shopify-style `images` arrays: **strength 2 only** and only when the
  object's own `title`/`handle`/`sku` matches.
* `generic_gallery.py` — `GALLERY_CONTAINER_SELECTORS` (`product-gallery`,
  `woocommerce-product-gallery`, `product-images`, `product__photos`, `gallery-thumbs`, `thumbnails`,
  `product-thumbnails`, `slick-slider`, `flexslider`, `product-media-gallery`, `gallery-placeholder`,
  `fotorama`, `swiper`), `has_gallery_container`, `extract_container_images`,
  `extract_whole_page_fallback` (only when 1–4 empty **and** no container **and** strength 2; every
  URL must carry a model key).
* `magento.py` (`mage/gallery/gallery` JSON), `haier.py` (`sd_imgs`, `js_prodetail_imglist`,
  `smallList`), `dawlance.py` (only media URLs containing **this page's own** MDM code from the URL;
  if the page URL has no MDM code, require a model-key match in the media URL).

### 6.12 `image_host.py` ⬜
`classify_upload_response(payload) -> ("ok"|"permanent"|"transient", message)` — permanent on
invalid key / quota / forbidden; `class FreeImageHost(*, api_key, min_interval_s, timeout_s,
max_retries, base_delay_s, max_delay_s).upload(data, filename) -> str` — multipart POST, JPEG bytes,
429 + 5xx retryable with backoff, `PermanentAPIError` on bad key ⇒ the run aborts **after** the
checkpoint is flushed.

### 6.13 `csv_io.py` ⬜
`normalize_header`, `find_column`, `read_input(path) -> list[ProductTask]` (CSV `utf-8-sig` / XLSX),
`merge_duplicate_titles(tasks) -> (rows, warnings)`, `hash_source_urls(urls)`, `safe_slug(title)`,
`atomic_write_text`, `atomic_write_csv`, `append_jsonl`, `load_checkpoint(path) ->
dict[str, CheckpointEntry]`, `append_checkpoint(path, result, *, timestamp)`, `load_banner_cache`,
`save_banner_cache`, `sha256_bytes`, `save_image_atomic(img, path, *, jpeg_quality=None)`.

### 6.14 `decisions.py` ⬜
`DecisionLog(path, lock=None)` with `.record(...)`, `.keep(...)`, `.reject(...)` — thread-safe JSONL
append + flush, so `decisions.jsonl` is complete even after a crash.

### 6.15 `pipeline.py` ⬜
* `PipelineDeps(page_fetcher, image_fetcher, image_host, decisions, banner_cache,
  banner_cache_lock)`.
* `process_product(task, deps, config, cancel_event=None) -> (ProductResult, list[AcceptedImage])`.
* `finalize_and_upload(task, result, images, deps, config) -> ProductResult` (S14).
* `ensure_banner_url(config, deps, role) -> str` (content-hash cached in `state/banner_urls.json`).
* `write_output_csvs(results, config, *, include_platform_csvs=True)` (atomic, input order,
  failed-only failed CSV, banner last).
* `class BatchRunner(config, deps, tasks, checkpoint)` `.run() -> list[ProductResult]` —
  bounded `ThreadPoolExecutor(config.workers)`, **one lock for all shared writes**, resume skip-set
  (`--redo` forces), `limit`, KeyboardInterrupt ⇒ stop submitting + flush + clean exit,
  `PermanentAPIError` ⇒ flush checkpoint, write reports, re-raise.
* `_DryRunHost` — returns local `file://` URLs; only used when `config.dry_run` is set.

**`process_product` — numbered steps (this is the code skeleton):**
1. `build_model_keys(title)`; no keys ⇒ `failed` `"title has no model-like token"`, **no fetch**.
2. `detect_brand` + `prioritize_source_urls` (official domain first, else input order).
3. For each URL: `is_fetchable_product_url` (S11) → else decision line + next URL.
4. `fetch_page` (retry/backoff). Page-level `PermanentAPIError` (404/410) ⇒ decision + next URL —
   a dead page never aborts the run or the product early.
5. `verify_page` (S1). `strength == 0` ⇒ **zero extraction**, `pages_rejected += 1`, next URL.
6. `strip_unwanted_regions` → `ExtractionContext` → `extract_candidates` (sources 1-4, then 5). Every verified page is treated the same way: source 5 is used only when sources 1-4 produced nothing, the page has no gallery container, and each URL is anchored to this product's code (S12).
7. Per candidate, ordered model-first (stable sort): duplicate `dedup_key` within this product →
   `S10`; `has_bad_keyword` → `S5`; `url_belongs_to_other_model` → `S4`; description ancestor → `S2`;
   link to another page → `S3`; strength-1 page and URL lacks a model-only key → `S1.weak-page`.
8. `fetch_image(url, referer=page_url)` → `validate_image_bytes` → `S13` on failure.
9. Gates in fixed order `S8 → S6 → S9 → S7` + `neutral_corner_count ≥ min_neutral_corners`:
   first failure logs its rule ID and stops.
10. `isolate_main_product` (guards) → `to_square_canvas` → `sharpen`; save `raw/` + `isolated/`;
    `ProductDedup.add(...)` — **the dedup instance is created here and never shared (S10)**.
11. Accept: `[accept]` log + `decisions.jsonl` line; stop at `max_images_per_product`; stop trying
    further URLs once `accepted ≥ min_required_images`.
12. Zero accepted ⇒ `failed` with the exact reason (`all source URLs rejected as non-product pages` /
    `no page matched this model` / `pages matched but no image passed quality gates`) and **no
    uploads at all**.
13. Otherwise `finalize_and_upload`: composite both templates from the same square, save
    `final_digimall/` + `final_alfamall/`, then upload each image to **both** platforms (any failure ⇒
    that product fails, nothing half-published); append the 2 closing banners **last**.
14. Status `ok` if `accepted ≥ min_required_images` else `partial`; return the `ProductResult`.

### 6.16 `main.py` ⬜
`argparse`: `--input`, `--assets-dir`, `--output-dir`, `--dry-run`, `--redo TITLE [TITLE …]`,
`--limit N`, `--workers N`, plus `--no-resume`, `--min-images`, `--max-images`, `--log-level`.
Job: fail-fast asset check → read sheet → merge duplicates → load checkpoint → build real fetchers /
host (API key from env, **missing key = hard error** unless `--dry-run`) → `BatchRunner.run()` →
write CSVs + report → print the same summary as `run_report.csv` to stdout.

### 6.17 `tests/` ⬜
`conftest.py` (synthetic image factories: clean product, busy middle, wide strip, tiny, QR, white-on-
white, two angles; fake fetchers/host; HTML fixture loader), `fixtures/` (`product_ok.html`,
`listing_page.html`, `related_carousel.html`, `description_images.html`, `bundle_two_models.html`,
`magento_gallery.html`, `jsonld_two_products.html`, `no_gallery.html`, `weak_page.html`), and the
test files `test_model_matching`, `test_url_filters`, `test_json_helpers`, `test_page_verification`,
`test_gallery_extractors`, `test_image_quality`, `test_dedup`, `test_csv_io_resume`,
`test_pipeline_end_to_end`.

Required test cases (all from the spec, plus adversarial extras):
* model: `Haier HSU-12HFPAA 1.5 Ton Inverter (White)` keeps `HSU-12HFPAA`, not `1.5`/`Ton`/`White`;
  `Premium LED TV 55 inch 2024` ⇒ zero keys ⇒ `"title has no model-like token"`; `HSU-12` must not
  match inside `HSU-12HFPAA`/`AHSU-12` but must match `hsu12`/`HSU 12`/`hsu_12`.
* model edge cases (§3.5): all **10 sample titles** produce ≥1 key (`test_bracketed_model_keys_rescued`);
  unclosed bracket keeps the tail → `PMO–23` (`test_unbalanced_bracket_keeps_tail`);
  **glued identity** (§3.5.4): `HRF538IFGA` / `HRF-538-IFGA` / `hrf 538 ifga` all match `HRF-538 IFGA`
  (`test_glued_identity_matches`); `HSU12HFPAA` still rejected for sheet `HSU-12`
  (`test_glued_prefix_extension_still_rejected`); `HRF538XYZ` rejected (`test_glued_unknown_extension_rejected`);
  image `HRF538IFGA-1.jpg` kept, not flagged `other_model` (`test_glued_image_url_not_other_model`);
  **brand + code** (§3.5.3): `Haier HRF-538 IFGA` vs `Haier Refrigerator 538 IFGA INV` ⇒ strength 2,
  `match_rule=brand_code` (`test_brand_code_match_haier_538_ifga`); same target without the brand ⇒
  strength 0 (`test_brand_code_without_brand_rejected`); `Haier HSF-538 IFGA` ⇒ rejected
  (`test_conflicting_prefix_rejected`); sheet `55S80EUX` vs page `H55S80EUX` ⇒ matched (brand initial);
  code too short (`HSU-12` → `12`) ⇒ never relaxed (`test_short_tail_not_relaxed`); another
  manufacturer's domain ⇒ rejected (`test_other_manufacturer_domain_rejected`); retail domain (daraz)
  allowed (`test_retail_domain_brand_code_allowed`); `brand_match_enabled=False` restores strict
  behaviour (`test_brand_code_off_switch`); dropped trailing letter `DW-7500C` vs page `DW-7500`
  (`test_dropped_trailing_letter_page_matches`, `test_key_truncation_matches_sheet_suffix`);
  a different Haier model `HSU-18HFPAA` still rejected (`test_different_haier_model_still_rejected`);
  the S12 anchor accepts a brand+code image URL
  (`test_whole_page_fallback_works_for_a_brand_code_page`).
* colour/variant (§3.6): title `Golden` vs page `Black` ⇒ **row published and flagged**
  (`test_colour_conflict_flags_the_row_but_keeps_it`, `test_colour_conflict_is_a_flag_not_a_failure`);
  title colour + page silent ⇒ accepted (`test_colour_silence_ok`);
  `10 kg` vs `12 kg` ⇒ published but noted (`test_variant_mismatch_flags_not_fails`);
  colour-preferring image order (`test_colour_images_ranked_first`); body-text-only page takes the
  gallery (`test_weak_page_extracts_gallery`).
* urls: `cartridge-filter.png` kept, `cart.png` rejected; `silicon-pad.png` kept, `icon-menu.png`
  rejected; `banner2.jpg` rejected; `image.php?id=123` ≠ `?id=456`; `product_800x800.jpg?v=3` →
  canonical without query; `/a/front.png` ≠ `/b/front.png`; S4 examples (own / other / neutral).
* pages: listing page ⇒ strength 0 + zero images; body-text-only ⇒ strength 1 + only URL-matching
  images; related carousel excluded; description images excluded even 3 levels deep; two JSON-LD
  products ⇒ only the matching node's images; valid match + no gallery ⇒ `failed`.
* images: clean white product accepted; white corners + busy centre rejected; 1000×150 rejected;
  100×100 rejected; non-image/SVG/oversize rejected; QR rejected (`skipif` if OpenCV cannot encode);
  white-on-white isolation does not crash and does not return an empty crop.
* dedup: same image resized = duplicate; different angle ≠ duplicate; two products (and two threads)
  each keep their own copy (S10).
* csv/resume/pipeline: order preserved with out-of-order workers; duplicate titles merged;
  interrupt → resume skips done, retries failed/empty/changed-URL; failed product ⇒ **zero** upload
  calls; partial ⇒ exactly one banner per platform, appended last; `ok` ⇒ model-URL images first,
  banner last; `PermanentAPIError` ⇒ run aborts **after** the checkpoint flush.

### 6.18 Root files
`requirements.txt` ✅ · `pyproject.toml` ✅ (mypy `--strict`, `ignore_missing_imports` only for
`cv2`, `imagehash`, `curl_cffi`, `scipy`, `requests`, `openpyxl`) · `README.md` ⬜ (install + run +
Colab cell, folder layout, how to add a brand extractor, how to tune with `--dry-run` +
`decisions.jsonl`, Safeguard Traceability Table).

---

## 7. Resume / checkpointing

1. After each product: append one line to `state/checkpoint.jsonl` (`title`, `source_url_hash`,
   `status`, `digi_urls`, `alfa_urls`, `reason`, `timestamp`) and atomically rebuild the CSVs.
2. **Done** = status `ok`/`partial` **and** non-empty URL list **and** the same source-URL hash.
   `failed`, empty, or changed-URL rows are always retried.
3. `--redo "Title A" "Title B"` forces exact titles to be reprocessed.
4. Closing banners are cached in `state/banner_urls.json` by file content hash ⇒ no re-upload.
5. Test: interrupt after N products, rerun, prove the N finished ones are skipped and the failed ones
   are retried.

---

## 8. Assumptions (decided, safe defaults)

1. Output header is `Title, Urls` — one constant (`config.OUTPUT_HEADER`).
2. Input header aliases include **`Images`** (the sample sheet uses it) besides `Source URL`.
3. Unbalanced `(` with no `)` ⇒ with `unbalanced_bracket_keeps_text=True` the tail is **kept** (so
   sample row 6 yields `PMO–23`); set it to `False` for the strict "drop it" behaviour.
3b. Brackets are ignored for normal key parsing, but if that leaves **zero** keys the bracketed text
   is re-scanned (rescue-only), because all 10 sample rows carry their model in brackets.
3b2. **Glued spellings** of the full model identity are accepted (`HRF538IFGA` = `HRF-538 IFGA`), but a
   key is never matched when the page extends it with characters absent from the title
   (`HSU-12` vs `HSU-12HFPAA`). `allow_glued_identity_match=False` disables even the glued case.
3b3. **Colour and variant differences only flag a row** (customer decisions of 2026-10-05): the row is
   still published and the Note records `colour mismatch (flagged)...` / `variant mismatch (flagged)...`.
   Photos are never rejected for colour, only ordered by it (§3.6).
3b4. **Body-text-only pages are treated as normal pages** now that the source URLs are human-checked.
3c. **Brand + code match is ON by default** (customer decision): a page naming the same brand and
   showing the same distinctive code (`538 IFGA` for `HRF-538 IFGA`) matches without the `HRF-`
   prefix, guarded by the brand-named, code-distinctiveness, prefix-conflict and
   other-manufacturer-domain checks (§3.5.3). Set `brand_match_enabled=False` for strict mode.
4. `known brands` map is rebuilt from the sample sheet + common PK brands (original map not attached).
5. Strength-1 (weak) pages: only image URLs containing a **model-like token** (not the whole-title
   key) are accepted.
6. Dawlance extractor applies only when the page URL carries an MDM code, or the media URL carries a
   model key.
7. Local final images and uploads are **JPEG q95 on white** (transparency flattened).
8. `min_required_images = 2`, `max_images_per_product = 5`.
9. `--dry-run` never touches the platform CSVs and never calls the image host.
10. Ties in candidate ordering keep gallery order (stable sort, model-bearing URLs first).

## 9. Known limitations

* Pages whose gallery is built by JavaScript at runtime will **fail safely** (no images) rather than
  guess.
* Very light-grey product on white may be skipped (deliberate: reject when unsure).
* Thresholds (`border_is_clean`, isolation) need one calibration pass on a real sample via
  `--dry-run`.
* Logos/packaging shots that sit in the genuine gallery are kept if they pass all gates; the
  keyword list is the only defence there.
* **Brand + code matching** (§3.5.3) trades a little strictness for recall: when a site prints neither
  prefix nor brand in the code, two families sharing the same code are indistinguishable
  (`HRF-538 IFGA` vs `HSF-538 IFGA`). Every acceptance is recorded as
  `brand+code match: <brand> + <code>` in the Note and in `decisions.jsonl`, so those rows can be
  filtered for review. `brand_match_enabled=False` disables it.
* **Colour/variant differences no longer stop a row** (customer decision), so a page showing the same
  model in another colour is published and merely flagged; re-uploading is the human's call.
* Sites whose gallery is built by JavaScript **and** whose images are in no JSON payload still produce
  no images until a brand extractor is added for them (the documented extension path).

---

## 10. Build status and verification

| File | Status |
|---|---|
| `requirements.txt`, `pyproject.toml`, `sample_input.csv` | ✅ written |
| `config.py`, `models.py`, `protocols.py` | ✅ written |
| `fetch.py`, `model_matching.py`, `url_filters.py` | ✅ written |
| `image_quality.py`, `dedup.py`, `compositing.py`, `page_verification.py` | ✅ written |
| `gallery_extractors/` (7 files: registry + dawlance + haier + magento + json_ld + embedded_json + generic_gallery) | ✅ written |
| `sources.py` (fallback chain, S15) + `image_host.py`, `csv_io.py`, `decisions.py`, `pipeline.py`, `main.py` | ✅ written |
| `tests/` (10 modules + `conftest.py` fakes + 10 HTML fixtures) | ✅ written |
| `assets/` (2 templates + 2 banners) and `tools/make_placeholder_assets.py` | ✅ written |
| `README.md` | ✅ written |
| `pytest` | ✅ **172 passed** |
| `mypy --strict .` | ✅ **Success: no issues found in 36 source files** |
| live dry-run on the 10-row sample sheet | ✅ done three times (see §3.7) |

**Verified commands** (safe to re-run):

```bash
python3 -m pytest tests -q          # 172 passed
python3 -m mypy --strict .          # Success: no issues found in 36 source files
python3 tools/make_placeholder_assets.py --assets-dir assets
python3 main.py --input sample_input.csv --dry-run --workers 4
python3 main.py --input sample_input.csv            # real run (needs FREEIMAGE_API_KEY)
```

Planned extension (approved 2026-10-06): the fallback source chain - see section 11.

The only remaining step is the real (non-dry-run) upload, which needs `FREEIMAGE_API_KEY`; nothing in
the code path changes between dry-run and real run except the two upload calls and the CSVs
(`Title,Brand,Image_Urls`).


---

## 11. Fetch strategy: official brand sites first, then retail - and getting only product images

**Status: IMPLEMENTED and verified live on 2026-10-06 (final ship run): 10/10 products published**
(ok **5** / partial **5** / failed **0**; 19 + 19 pictures on the two platforms, **40/40**
freeimage.host URLs HTTP 200; 11/11 acceptance checks; every Brand filled; banner last on every row).
Defects found and fixed the same day: opaque-alpha template hiding the product; a dry-run banner URL
poisoning the live CSV; the Dawlance official-gallery downloads (three separate causes - see
`verification/VERIFICATION.md`); plus a dead-code / dead-config purge. Evidence:
`/home/user/verification/VERIFICATION.md` and, for the visual proof, `verification/sheet_digimall.jpg`
+ `sheet_alfamall.jpg` + `sizing.txt`. `ruff` = clean, `mypy --strict` = **clean (36 files)**,
`pytest` = **213 passed**.

**Measured result on the real 10-row sheet** — final live run: **ok 5 / partial 5 / failed 0** with
19 + 19 pictures (earlier in the day, for the record: ok 1 / partial 4 / failed 5 before the fallback
work; ok 3 / partial 5 / failed 2 right after it; ok 4 / partial 6 with the brand fix; ok 5 /
partial 5 once the Dawlance gallery downloads were fixed).
The extra win came from the per-page download budget (Task 22b) - the Dawlance row's brand page used
to burn the row's whole allowance on dark renders, which hid the retail candidates that actually had
usable photos. The two rescued rows are exactly the ones the chain was built for:
* `Haier 50" (H50S80EUX)` - the sheet's Haier URL now serves the successor model `H50S80GUX`; two
  images came from `json-search:www.alfatah.com.pk` and `json-search:hadielectronics.com.pk`.
* `PEL PAWM-1100` - images from `json-search:modernelectronics.pk` and `json-search:www.alfatah.com.pk`.
* `Haier H55K85FX` (partial) - one image from `sitemap:www.haier.com` (the cached sitemap index).
Each of those rows carries `fallback used: yes` in `run_report.csv` and
`source=fallback:<origin>` on every kept image in `decisions.jsonl`.

Also delivered with this change (customer decisions 2026-10-06): the two output sheets are now
`Title,Brand,Image_Urls` with `|`-separated URLs and no spaces; `target_images_per_product = 3`
(the row stops collecting once it has 3, hard cap `max_images_per_product`); the per-row download
budget (12) and the 64 KB header probe from §11.5 are in place, and every row logs its own
`1 recognition -> 2 sheet pages -> 3 fallback search -> 4 images -> 5 compose -> 6 upload ->
7 status` line.

Everything below was measured against the live sites in this session; the measurements are in
§11.2 so nothing here is guesswork.

### 11.1 Priority chain (per row)

```
0. the sheet's own URLs, in the order given            (unchanged behaviour, always first)
1. official brand sources for the detected brand       (HaierMall / haier.com.pk / dawlance / TCL / PEL eshop / Homage)
2. retail Shopify shops (JSON search)
3. retail WooCommerce shops (JSON search)
4. stop -> row fails, Note lists every source tried
```

* **Trigger (customer decision, 2026-10-06):** the chain runs **only when the row is not already solved**
  by the sheet's own URLs - either no sheet URL yielded a matched page, **or** a page matched but
  **zero images** passed the gates. If any sheet URL already produced >= 1 accepted image, no search
  request is made at all (asserted by a test).
* **Search term is always the sheet's model key(s)** (never a code found on a page). That is what keeps
  a stale URL from "fixing itself" into the successor model - the exact failure this feature must avoid.
* **Every fallback page is verified before use** (new safeguard S15, §11.6).
* Images obtained this way are tagged `fallback:<host>:<method>` in `decisions.jsonl` and produce a
  `run_report.csv` Note (`images from fallback: alfatah.com.pk (woo search)`), so a human can always
  see that a row was rescued and from where.

### 11.2 Verified source list (measured 2026-10-06)

| Source | Platform / capability | What was measured | Verdict |
|---|---|---|---|
| `www.haier.com/pk` | own CMS + **usable product sitemap** | `/pk/sitemap.xml` = 1,102 `<loc>`, **1,067 `.shtml` product pages** (31 TV pages, e.g. `h32d2m.shtml`, `h98s90eux.shtml`); `/sitemap.xml` at the root is 404 | **rung 1 (sitemap) works** |
| `haiermall.pk` | own shop | `sitemap.xml` 200 but **0 `<loc>`** (HTML page, not XML) | no sitemap: HTML/JSON search or skip |
| `www.dawlance.com.pk` | own CMS | `sitemap_index.xml` 200, index with ~5-6 child maps | rung 1, children parsed lazily + cached |
| `www.tcl.com/pk` | marketing site, JS gallery | `sitemap.xml` = 90 roots (language pages), product pages are JS-rendered | weak: retail first for TCL |
| `eshop.pel.com.pk` | **Shopify** | `suggest.json` OK (0 hits only for a Haier model), `products.json` OK | rung 0 JSON |
| `homage.com.pk` | **WooCommerce** (also the brand store) | `wc/store/products` OK | rung 0 JSON |
| `www.surmawala.pk` | **Shopify** | `suggest.json` for `H50S80EUX` -> **1 hit** ("Haier 50\" 4K HDR Google TV QLED - S80S...") | rung 0 JSON |
| `modernelectronics.pk` | **Shopify** | `suggest.json` OK, 0 hits for that model | rung 0 JSON |
| `www.telex.pk` (Telemart) | **Shopify** | `suggest.json` OK | rung 0 JSON |
| `www.alfatah.com.pk` | **WooCommerce** | `wc/store` for `H50S80EUX` -> **1 hit** ("Haier 50 Inches 4K QLED Google TV H50S...") | rung 0 JSON |
| `www.almumtaz.com.pk` | **WooCommerce** | `wc/store` -> **1 hit** | rung 0 JSON |
| `hadielectronics.com.pk` | **WooCommerce** | `wc/store` -> **1 hit** | rung 0 JSON |
| `subhanelectronics.pk` (customer-provided) | **WooCommerce** | `wp-json/wc/store/products` 200 with live products; `products.json` 404; 3.7 MB homepage, `woocommerce`/`wp-content` markers | rung 0 JSON |
| `etech.pk` | - | HTTP 403 to every attempt | **excluded** |
| `symbios.pk` | - | TLS certificate verification failure | **excluded** (optional `insecure_hosts` allow-list off by default) |
| `zeesolstore.pk` / `zeesol.com.pk` | - | domain dead / no JSON | excluded until a working domain exists |
| `pkelectronics.pk`, `kharido.com.pk` | Woo, no Store API | `wc/store` disabled | optional rung 3 (HTML search) |

### 11.3 Transport layer (`fetch.py`), curl_cffi specifics

1. **One `requests.Session(impersonate="chrome120")` per host**, created lazily, reused for the whole
   run (`dict[host, Session]` behind a lock). Reuse keeps the TLS/JA3 fingerprint, the connection and
   the consent/region cookies - a fresh session per request is what gets a crawler blocked. The
   impersonation profile is pinned, never `"chrome"`/`"random"`.
2. **Serialise per host:** 1 in-flight request per host + `domain_min_interval_s = 1.2` (existing
   `DomainRateLimiter`), so `--workers 4` parallelises *across* domains, never against one server.
3. **Retries:** 3 attempts, exponential backoff with jitter, honour `Retry-After`.
   * `429/5xx` -> retry. `403` -> **no retry** (log once, mark the source skipped for the run -
   a 403 is a policy answer, not a hiccup). `404/410` -> permanent, next candidate.
   * `DNSError` / `CertificateVerifyError` -> mark the whole host dead for the run.
4. **Headers that matter:** `Referer` on every image request (hotlink protection on several PK CDNs),
   a normal `Accept-Language`, and `Accept: image/avif,image/webp,image/*` for images.
5. **Record redirects:** `response.url` is kept. Haier redirected `h50s80eux.shtml` ->
   `h50s80gux.shtml`; a redirect that changes the model code is "successor model" evidence and is
   written into the decision detail.
6. **robots.txt once per host** (cached in `state/robots.json`, TTL 7 days); if a path is disallowed,
   that source is skipped with a `source skipped: robots` decision line.

### 11.4 Finding the product page - cheapest rung first

| Rung | Technique | Cost | Notes |
|---|---|---|---|
| R0 | **Platform JSON search**: Shopify `/search/suggest.json?q=<model>&resources[type]=product` ; Woo `/wp-json/wc/store/products?search=<model>` | 1 request | returns the product `url`/`permalink`, title and often the image list. Works on 11 of the sources in §11.2 |
| R1 | **Sitemap index, cached**: fetch once per brand into `state/sitemaps/<host>.jsonl` (TTL 7 d), then lookup is a local dict hit on the normalised model token | 1 crawl per brand, not per product | child maps filtered by name (`product`, `tv`, `appliance`, `refrigerator`, `washing`, `led`), skip `news`/`blog`/`review`; gunzip; cap `sitemap_max_urls = 20000`; verified feasible for haier.com/pk (1,067 product URLs) and dawlance |
| R2 | **URL template guess** (per-brand table): Haier `/pk/<category>/<model>.shtml`, Dawlance `/...<slug>`, TCL `/pk/en/<category>/<model>` | 1-3 requests | the row that started this whole feature *was* exactly the Haier template; verified by shape |
| R3 | **HTML search page** | 1 request | last resort, only when the results are server-rendered links |

Never a breadth-first crawl of a brand site: `~90%` of rows resolve at R0/R1 with <= 2 requests, and
R1 makes every later row free.

### 11.5 Image discovery - only product images, and as few bytes as possible

1. **DOM triage before any image request** (zero requests spent on nav/footer/related):
   JSON-LD `Product.image` -> `og:image` -> `data-large_image` / `data-zoom-image` / `data-highres` ->
   the largest declared `srcset` entry -> dedicated gallery container -> (last, restricted) whole page.
   Every node still passes S2/S3/S5.
2. **URL upscale rewrite** (the single biggest saving; Haier's page advertises only 60 px thumbs):
   | Observed | Rewrite to |
   |---|---|
   | Haier `W0202408..._60.jpg` | `_1000.jpg` -> `_480.jpg` -> original `.tif` variant |
   | Dawlance `/media/resize/<ASSET>.png/1000Wx1000H/image.webp` | larger transform, then the bare asset path |
   | Shopify `..._60x60.jpg` | `_1024x1024.jpg`, then `master` |
   | Magento `/media/catalog/product/cache/<hash>/x.jpg` | strip `/cache/<hash>` -> original asset |
   | Woo `x-300x300.jpg` | strip the `-<W>x<H>` suffix |
   The largest form is tried first; a 404 falls back to exactly the URL the page gave. The rewrite
   table lives in `config.url_rewrite_rules` (data, not code branches) and `url_rewrite_enabled` turns
   it off.
3. **64 KB `Range` probe before the full body**: parse the dimensions from the header bytes alone
   (JPEG `SOF0/SOF2`, PNG `IHDR`, WebP `VP8X`/`VP8`) and reject S8 (too small) / S6 (strip) after
   ~2 KB instead of ~4 MB. Servers that ignore `Range` (200 + full body) are handled; a
   `Content-Type: text/html` body means a dead/soft-404 link and is skipped.
4. **Ordering + early exit:** model-bearing URL -> colour-matching -> largest declared size ->
   gallery order; stop the moment `max_images_per_product = 5` images have passed everything.
5. **`state/image_cache.jsonl`**: image URL -> sha256 + dimensions + gate verdict. Re-runs, `--redo`
   and threshold calibration re-gate from the local copy instead of re-downloading.

### 11.6 Safety: safeguard S15, and what happens to S12

* **S15 (new, named function + direct test): "a search result is never trusted".** A page found by any
  fallback source is only a *candidate*: it must pass the same `verify_page(page_url, html, sheet_keys)`
  as a sheet URL, and the model used for verification is always the **sheet's** model. A found page
  that does not match yields zero images and a `S15` decision line.
* **S12 stays "no fallback *image* sources"**: inside a page nothing is taken without a code anchor;
  there are still no description images, no CSV/primary-image columns, no unanchored scraping.
  What changes - knowingly, with customer approval - is that a *page* may now be found by search
  instead of only being supplied in the sheet. `plan.md` §8/§9 and the README are updated to say this
  in exactly these words.
* `fallback_enabled=False` restores the original behaviour completely (one switch, tested).

### 11.7 Config surface (every number named, no magic constants)

`fallback_enabled=True`, `fallback_trigger="unsolved"`, `official_sources_first=True`,
`brand_sources: dict[str, tuple[str, ...]]`, `shopify_sources: tuple[str, ...]`,
`woo_sources: tuple[str, ...]`, `excluded_hosts: tuple[str, ...]` (etech, symbios),
`source_search_timeout_s=12.0`, `sources_rate_limit_s=1.2`, `per_host_max_inflight=1`,
`fallback_max_pages_per_row=6`, `fallback_max_search_per_source=1`,
`sitemap_ttl_days=7`, `sitemap_max_urls=20000`, `sitemap_max_child_maps=12`,
`respect_robots=True` (a per-host `robots.txt` check, cached for the whole run; it applies to
fallback-discovered pages only - the sheet's own URLs are always fetched),
`image_probe_bytes=65536`, `image_probe_enabled=True`, `image_full_download_max_per_row=8`,
`image_cache_enabled=True`, `url_rewrite_enabled=True`, `url_rewrite_rules=<table above>`.

### 11.8 Files touched

* **new** `sources.py` - the chain: `SourceKind`, `SourceTarget`, `build_source_plan(brand, model_keys,
  config)`, `search_shopify()`, `search_woo()`, `sitemap_index()` (+ cache), `guess_urls()`,
  `robots_allowed()`, all returning `PageCandidate(url, origin, method, title_hint)`.
* `fetch.py` - session registry, in-flight lock per host, 403/DNS/TLS classification, `Range` probe
  helper `probe_image_header()`, redirect capture.
* `url_filters.py` - `apply_upscale_rewrite(url)` + the rewrite table + `is_excluded_host()`.
* `pipeline.py` - the chain runs after the sheet's URLs are exhausted/unsolved; S15 enforcement;
  `source=` tagging; Notes.
* `config.py`, `protocols.py` (a `ShopSearcher` protocol so tests use fakes), `csv_io.py`
  (sitemap/robots/image caches live in `state/`), `main.py` (`--no-fallback`, `--refresh-sitemaps`).
* `tests/` - see §11.9.

### 11.9 Test plan (fakes only, no network)

`test_fallback_not_used_when_sheet_url_solved_row`, `test_shopify_search_result_is_verified_before_use`,
`test_woo_search_result_that_fails_s1_yields_nothing`, `test_fallback_note_names_host_and_method`,
`test_fallback_disabled_restores_original_behaviour`,
`test_sitemap_cache_roundtrip_and_token_lookup`, `test_sitemap_filters_child_maps_and_caps_urls`,
`test_upscale_rewrite_table` (Haier `_60`->`_1000`, Shopline/Shopify, Magento cache, Woo suffix),
`test_range_probe_parses_jpeg_png_webp_headers`, `test_probe_skips_oversize_and_strip_before_download`,
`test_full_download_budget_is_enforced`, `test_403_is_not_retried`, `test_robots_disallow_skips_source`,
`test_image_cache_prevents_second_download`, `test_redirect_to_another_model_is_logged`.

### 11.10 Rollout

1. (done) platform/sitemap verification - §11.2.
2. implement `sources.py` + the fetch/url/pipeline/config changes + tests; `pytest` and
   `mypy --strict` must stay green.
3. `--dry-run` on `sample_input.csv` and diff against today's baseline (**ok 1 / partial 4 /
   failed 5**). Expectation: row 2 (`H50S80EUX`, whose Haier page now serves the GUX) is solved by a
   Woo/Shopify search hit; the Haier `H55K85FX` and PEL rows gain candidate pages;
   `decisions.jsonl` shows `source=fallback:...` lines for exactly those rows.
4. inspect every fallback-sourced row by hand on the first run (third-party photos deserve a look),
   tune budgets, then do the real upload run.

### 11.11 Risks and limits (customers' eyes on this list)

* Fallback images come from **third-party shops**: they are usually correct catalog shots, but they are
  the shop's choice of photo (possibly a series/other colour). Gates S2-S14 still apply, colour/variant
  differences are flagged, and every fallback row is marked in the report.
* Shop search APIs only prove *the shop says* this is the product - hence S15 re-verification on the
  sheet's model, and never accepting the found page's own model as the search term.
* Marketplace JSON endpoints are public but rate limits/policies change: 1 request/source/row,
  1.2 s/host, robots respected, and any source can be removed from the list with one config edit.
* TCL and HaierMall publish marketing pages with dark renders: those rows are expected to end up on
  retail sources even though the brand site is searched first.

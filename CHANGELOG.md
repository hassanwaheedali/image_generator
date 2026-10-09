# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **A row whose title has no model code can now use the page's own declared image.**
  `json_ld` accepted only a node whose name/sku/model matched a model key - and a title
  like `Frost 18W Classic White | Orient Electronics Pakistan` owns no key to match, so
  the page's own `Product` node was refused and its declared image never entered the
  candidate list. Verified on the owner's live capture (`uploads/live/orient_frost.html`,
  2026-10-10): `extract()` returned **0** candidates while the node carried
  `assets/og-images/frost-18w-classic-white.jpg`. Such a node is now accepted when it
  names **this page** as its product page (`url`/`@id`/`mainEntityOfPage`, or a `sku`
  equal to the page slug) - a claim a cross-sell tile cannot make, which is why the
  exception is closed for every row that *does* own a model code. On the same capture the
  row goes 7 candidates (all cross-sell tiles, all correctly S3-rejected) -> 8, first =
  the Frost image. 6 of 34 rows own no model code; the run's only `failed` row is one of
  them. **Needs the owner's live re-run**: this sandbox cannot download from
  `orient.com.pk`, so the image's quality-gate pass is unproven here.
- **S18 - a crop may not cut the product.** The isolation box followed the strict
  foreground mask, and a white shell on a white background fades out *inside* that
  mask, so the box stopped at the product's waist. Measured on the owner run of
  2026-10-10 (`output/decisions.jsonl` + its 72 accepted images): **41 of 72 crops
  cut ink** - `Candy By Haier 0.8 Ton #01` kept 1342x195 of a 1342x695 unit (only the
  blue vent strip), `EcoStar Emperor #01` kept 1066x87 (only the louvers),
  `eComfort Pro KEC-1267S #01` 427x107. The box is now *closed*: ink connected to the
  crop border is pulled in, a mark standing away from the product never widens it, and
  nothing is ever trimmed. Grow-only, and switched off by
  `isolation_crop_closure = False`. Verified on the owner's own images (height
  +270 %, +203 %, +347 %, +55 %, +46 % on the five worst; the other 67 crops byte-size
  identical) and eyeballed in `docs/riper/evidence/crop-closure-before-after.png`. **Untested
  against a live re-run** - the composed result on the shop template still needs the
  owner's eyes.
- **Haier sized thumbnails may now be re-fetched as PNG.** `image.haier.com` serves
  `W020230419477905891117_60.jpg`; the size ladder tried `.jpg` rungs only, so five
  gallery images died at S8 `too small 60x60` in the same row whose two `.png` assets
  were accepted. An official page now ends the ladder with the asset's `.png` /
  `_480.png` sibling. *Tried, never forced* - the page's own URL stays the last
  candidate, so a wrong guess costs one request and no decision. **Constructed, not
  verified**: the sandbox cannot reach `image.haier.com`, so whether that sibling
  exists is only answerable from the owner's machine.
- **A row with no model code no longer crashes the run.** `_tolerated_spelling_reason`
  indexed `keys.identities[0]` for rows that own neither a model token nor a glued
  identity, so `Frost 18W Classic White | Orient Electronics Pakistan` came back as
  `failed: unexpected error: tuple index out of range` - a crash inside the code that
  *explains* a failure. Such a row now reports a reason (owner run 2026-10-09).
- **Background removal stops cutting white products.** Isolation measured the raw
  difference mask, and a body whose shading is softer than `isolation_bg_distance` is
  *hollow* to that mask: the surviving component was a vent strip or a side panel, so the
  unit lost its top and kept its bottom. It now measures the product **silhouette**
  (outline bridged, enclosed background filled). 46 images in a 33-product run had the
  product pressed against a crop border; 39 do now, and the 10 that changed are the
  cut-unit rows (Orient Megatron, Gree Charmo, Dawlance Dual Pro, Kenwood eComfort...).
- **A frame that is not one product is never cropped.** An "In-box items" diagram or a
  promo banner holds several comparable objects; cropping it to the largest published a
  fragment (the Inspire Pro row shipped a line-art icon, the LG remote rows a grey blob).
  Those frames now go through whole, which is strictly better than a fragment.

### Added
- **Safeguard S10 now says why.** A duplicate rejection records the signal and the
  measurement (`re-served copy of accepted image #1 (dHash 3 bits apart, correlation
  0.9688)`) instead of the string "near-duplicate", and an *ambiguous* pair - close
  enough to look like a repeat, not close enough to prove it - is logged as
  `S10.review` and kept. Tightening the rule further was measured and rejected: the
  pair the tests guard (same AC, louver flap open) scores **closer** on dHash (2 vs
  3-4 bits), silhouette IoU (1.000 vs 0.985) and body correlation (0.965 vs 0.802/0.555)
  than the two duplicates the owner reported, so no threshold catches one without
  deleting the other.
- `config.dedup_shape_iou_min` (calibrated: copies 0.985, best different angle 0.754),
  `config.isolation_merge_radius_ratio`, `config.isolation_multi_body_ratio`;
  `image_quality.body_shape` / `shape_overlap` for silhouette comparison;
  `dedup.ProductDedup.duplicate_reason` and `review_shape_match`.

### Performance
- One fingerprint per accepted image instead of two (`is_duplicate` and `add` were each
  resampling the same 1200px photo).


## [1.1.0] - 2026-10-08

### Added
- **Visual Color Gate (Safeguard S16)**: Eliminates wrong-color gallery images (e.g. Purple or Red refrigerators published for `Homage 18 Cu Ft Golden (HRF-47662-GD)`). Extracts foreground HSV color profiles from isolated product crops and rejects candidates whose dominant hue directly conflicts with the title colourway. Supports 13 canonical color families (`gold`, `red`, `purple`, `blue`, `green`, `white`, `black`, `grey`, `pink`, `orange`, `yellow`, `beige`, `brown`). Controlled via `config.visual_color_gate_enabled`.
- **Intra-Gallery Color Consistency (Safeguard S17)**: Prevents mixing conflicting chromatic color variants within the same product gallery (e.g. golden primary with purple secondary images).
- **Display Product Screen Exemption**: Adds `is_display_product()` identifying LED TVs, smart displays, OLED, QLED, and monitors. Automatically bypasses S16 for display screens so colorful demonstration wallpapers and scenic artwork are never falsely rejected.
- **Catalogue Header Alias Support**: Added `"image"` (singular) to `URL_ALIASES` in `csv_io.py`, enabling native out-of-the-box processing for catalogue sheets such as `Products.csv` (`Title`, `Image`).

### Changed
- **Unified Alphanumeric Tokenizer (`code_like_tokens`)**: Replaced competing, fragile regex passes with a single structural tokenizer that splits on word boundaries and glues consecutive prefix+digits+suffix segments (e.g. `HRF` + `47552` + `GD` → `hrf47552gd`, `DW` + `7500` → `dw7500`). Fixes S4 model mismatch detection for URLs containing hyphenated or separated model codes without false positives on capacity numbers (`15 Cu Ft`).
- **Capacity Policy Formalization**: Confirmed capacity tokens (`15cuft` vs `18cuft`) remain non-discriminating for S4 rejection; mismatched capacity specs between title and page are recorded as flagged annotations in `run_report.csv` (`variant mismatch (flagged)`) and `review_products.csv`, never blocking publication.

### Performance
- **Optimized Foreground Hue Extraction**: Employs 96x96 vectorized box downsampling and NumPy array filtering in `extract_foreground_color_profile()`, completing visual color verification in <2ms per candidate image with zero latency impact on processing throughput.

### Fixed
- **Fetches now go over IPv4, curing curl 28 connect timeouts on IPv6 black-holed hosts.**
  Live runs showed `eshop.pel.com.pk` and `www.surmawala.pk` dying with `curl: (28) Failed to
  connect ... after 21000 ms` - a TCP connect timeout that fires *before* any HTTP, because curl
  burns its budget on an unreachable IPv6 record while the IPv4 address (what a browser uses) is
  fine.  `fetch_curl_options()` now sets ``CURL_IPRESOLVE_V4`` on every session when
  ``fetch_force_ipv4`` (default on).  This is the connect-level fix; the separate `curl: (92)`
  HTTP/2 stream resets on Dawlance are transient and already self-heal through retry, so they are
  deliberately left alone.  Tests `tests/test_fetch.py`.  330 tests, ruff + mypy --strict clean.
- **A fallback search-result page can no longer "match" via its own search query.**
  `relaxed_page_match` built its haystack from the whole URL, and fallback search URLs echo the
  searched number in the query string (`?_psq=PWM+1250`).  A wrong product's result therefore
  self-matched - live, a `PWM-1250` row was tolerated on a `PWMS-1050` page purely because `1250`
  appeared in the query.  The query (and fragment) are now excluded; only host + path + page text
  count, so a model number must appear in the page itself, not in the search that found it.
  Legitimate sibling matches (a `DW-7500` page for a `DW-7500C` row) are unchanged.  Regression
  `test_fallback_match_ignores_the_search_query_in_the_url`.  330 tests, ruff + mypy --strict clean.
- **A page showing another colourway is no longer accepted as this product.** The colour a
  product comes in is part of its identity, so `... 7 Cu Ft Burgundy (9160-WB-GD)` and its
  `Pearl Red` twin are different products and need different pictures - the wrong colour costs
  the company a penalty. `match_product` now rejects a page whose primary colour differs from
  the title's (S1 rule `colour_conflict`, its own line in `decisions.jsonl`), the relaxed
  fallback tier (`S15.relaxed`) never rescues such a page, and the row keeps looking for the
  colour it was asked for. This supersedes the 2026-10-05 "flag it and publish" rule, so a sheet
  URL serving the wrong colour now **fails the row** instead of publishing it with a note.
  Owner decision 2026-10-07; regressions `test_a_page_showing_another_colour_is_rejected`,
  `test_a_different_colour_fails_the_row_instead_of_publishing_it`.
- **A model code's letter tail no longer counts as exact proof.** `HRF-538 IFGA` and
  `HRF-538 IFPA` are different variants of one model, so a page printing another tail is now
  *tolerated* rather than exact: it is deferred while the row looks for a page that prints its
  own model, published only as a flagged last resort, and the note names both tails
  (`page shows variant IFPA, sheet model is IFGA`). Optional feature letters still never
  discriminate (`DSD 4890 Silver SD` ≡ `DSD 4890 Silver`), a page that omits the tail is judged
  exactly as before, and `9160-WB` / `9160LF` stay the same product because there the tail is
  part of the code itself. Owner decision 2026-10-07; regressions
  `test_a_page_printing_another_variant_suffix_is_a_last_resort_not_proof`,
  `test_a_page_that_omits_the_suffix_is_judged_as_before`.
- **A tolerated (different-variant) page can no longer top up a partially-solved row.** Stage 3b
  consumed the deferred last-resort page whenever the row had *fewer than `min_required_images`*
  pictures, so a row with 1 correct retailer image got 4 different-colour official photos mixed in
  (live: DW-7500C). It now runs **only when the row found zero images**: 1 correct image publishes
  as `PARTIAL` + flagged, and the tolerated page still rescues rows that found nothing.
  Owner decision 2026-10-07; regression
  `test_a_row_with_one_exact_image_is_never_topped_up_by_the_tolerated_page`.
- **S7 no longer rejects genuine white-background studio shots whose product touches the frame
  edges.** Root cause: `border_is_clean` assumed the whole outer ring is background, so a tall
  appliance / top-down view (dark top deck + base crossing the top/bottom edges) split the ring
  and failed at ~0.5 fraction even with 4/4 neutral corners (live: Surmawala DW-7500 1200×1200,
  and the repo's `uploads/image-3.png`). A universal, host-agnostic rescue
  (`config.studio_background_rescue`, `studio_rescue_min_neutral_corners=4`) now accepts such an
  image when **all four corners are neutral studio white**; busy/lifestyle frames never reach four
  neutral corners at `corner_patch_ratio` and stay rejected. The previous official-only rescue is
  unchanged; the new one is bounded and switchable.
- **S8 header-probe judged the smallest listed variant, not the asset.** Shopify pages list a
  `_288x` thumb whose `_1024x` sibling is the real photo; the probe hit the 288px file and rejected
  at S8 before the large form was requested. The probe now targets the largest rewrite first, and a
  `shopify-single` rule (`_NNNx.ext` → `_1024x1024.ext`) makes single-dimension thumbs rewrite at
  all. Live log showed dozens of spurious `too small 288x288 (header probe)` rejects.
- **JSON-LD / embedded-JSON `image` fields that are page routes no longer burn downloads.** A shop
  may put a timestamped page URL in `image` (live: telex.pk `/products/2026-06-28T…` → HTTP 404).
  Both JSON sources now emit only URLs with a real raster extension (`url_filters.has_image_extension`),
  mirroring the DOM path's `_looks_like_image` guard.

### Added
- **Official Haier pages now get a size ladder so their galleries yield 3 images.**
  Haier's CDN ships 60px thumbs and often lacks the ``_1000`` rewrite the pipeline bet on, so a
  product page's 3 gallery photos collapsed to 1 accepted image (live: H55K85FX).  For pages on the
  brand's own domain only, ``upscale_candidates`` now tries ``_1000`` -> ``_800`` -> ``_480`` before
  falling back to the thumb; retailers keep the single ``_1000`` rewrite, so their S8 protection is
  unchanged.  The S8 gate itself is untouched - a genuinely tiny image still rejects; we only look
  harder for the large sibling on official pages.  Regression
  `test_official_haier_page_tries_a_size_ladder_retailers_do_not`.  330 tests, ruff + mypy --strict
  clean.
- **Variant pages that print the colour only in the body now match their own colourway.**
  A PEL dispenser page names no colour in its title; the colour lives in a labelled body line
  (`Color: Red Blaze` near the swatches and a `| Colour | Red Blaze |` spec row) while the swatch
  menu lists *every* option.  `page_verification.selected_colourway()` reads only a labelled value
  (a spec-table row or an inline `Color:` label whose value is a short phrase), never the swatch
  list, and `match_product` counts that value exactly as if the title had printed it - but only when
  the page's strong text names no colour of its own, so ordinary pages are untouched.  Result for the
  PEL 425 page: the `Red Blaze` row clean-matches its own label, the `Marine Impression` row is
  flagged, and the `Maroon Blaze` row is rejected (`colour_conflict`) - instead of all three rows
  sitting unmatched.  Pages with no label stay fail-open, as today.  330 tests, ruff + mypy --strict
  clean, radon 45.
- **Every catalogue brand now has its official domain, and category words can never veto.**
  `KNOWN_BRANDS` was missing three brands that appear in the shipped catalogue and in the owner's
  input sheets - `Superasia` (45 rows), `Hisense` (4 rows) and `Midea` - so their own pages got no
  priority in `prioritize_source_urls` and no curated-gallery acceptance, and a reseller outranked
  the manufacturer whenever it came first in the sheet.  Added with each domain verified against the
  brand's own site (`superasiagroup.com` / `superasiastore.com`, `hisensepakistan.com` /
  `hisense.com.pk`, `mideapakistan.store` / `mideapakistan.com`), because a wrong "official" host
  would relax the S7 gallery test and let worse images through.  `detect_brand` now also tries the
  first **two** title words, so the catalogue's `Super Asia` reaches the one-word key `superasia`
  (it returned `None` before, i.e. no priority at all).  And every word in the catalogue's 21
  category paths - `microwaves`, `geysers`, `purifier`, `humdifier`, `sound bars`, `appliances`,
  ... - is now in `_GENERIC_NAME_WORDS`, so an appliance category can never be mistaken for a
  series name and veto a correct page.  330 tests, ruff + mypy --strict clean, radon 45.
- **Probabilistic, format-agnostic variant matching** (`docs/riper/probabilistic-matching-plan.md`,
  owner-approved 2026-10-07).  `similarity.py` (new, stdlib `difflib` only) adds `similar()` and
  `same_digit_runs()`; `ModelKeys.name_words` captures the positional "name" words - series lines
  (`Daimond`/`Cuby`) and unknown colourways (`Ocean Mist`) - with category noise removed; and
  `score_match()` returns per-attribute vetoes (series / colour / size) plus a confidence, each
  fail-open.  `match_product` now demotes a matched page that *names a different* series line or
  colourway to a flagged last resort, so `Daimond`/`Cuby`/`Ruby` and *future* unknown colourways no
  longer silently mix, with **zero per-name tables**.  The hand-added `marine impression` vocabulary
  line was removed - the dynamic rule covers it.  A series veto needs a **substitution** (a word on
  each side the other lacks), never a mere omission: the first cut vetoed whenever the page did not
  repeat the row's line name and that demoted **75 of 94** products on a terse `brand + code + colour`
  page; the substitution rule keeps all **94/94** clean, and brand words are dropped before the
  comparison so `Super Asia` ≡ `Superasia` cannot veto.  Measured: same-code colour pairs separated
  274, **0 false rejects**, 157 diff-colour pairs with **0** clean matches; 100-product offline smoke
  GREEN; 330 tests, ruff + mypy --strict clean, radon baseline unchanged (45).
- **`image_pipeline/variant_terms.py`** - the variant vocabulary and its pure rules: colour
  families with a neutral (achromatic) carve-out, `suffix_agreement`, `colour_word_conflict`.
  It holds no brand, suffix or colour-code tables, so a colour the vocabulary has never seen
  simply fails open instead of guessing. Measured against the shipped catalogue (752 distinct
  titles): **269 of the 304** same-code pairs whose titles both name a colour are now separated;
  of the 35 kept, 24 name the same word, 8 are one finish under two names (`silver` ≡ `chrome`
  ≡ `grey`, `graphite` ≡ `grey`) and 3 fail open because the row's only colour word is the
  series name `Chrome`.
- **`image_pipeline/textnorm.py`** - `normalize_text` / `normalize_key` moved out of
  `model_matching` into a leaf module, so the variant vocabulary can normalize punctuation
  (`(9149-WB)-Pearl Red` now reveals *both* colour words) without an import cycle.
- **One config field for the whole rule: `variant_separation_enabled`** (replaces
  `check_colour_match`), plus `ModelKeys.colour_word`, which carries the title's own colour word
  so the matcher never has to re-parse a title it already cleaned.
- **The model in the title now outranks a wrong sheet link.** A page that matches only because
  a trailing letter was tolerated (live: the row `Dawlance Semi Automatic Washing Machine 10kg
  (DW-7500C)` carries the Dawlance link that prints `DW-7500`; the C-slugged URL 404s) is no
  longer treated as the answer. The row keeps searching - brand, then Surmawala and the other
  retailers - for a page that really prints the model; that page's pictures are published
  unflagged. Only when the whole chain finds nothing better is the tolerated page published,
  as `PARTIAL`, `S15.tolerated`, with `flagged: tolerated spelling - page shows DW7500, sheet
  model is DW-7500C` on the row and in `review_products.csv`. One switch:
  `config.tolerated_spelling_as_last_resort` (`False` = the old behaviour).
- **The search terms are the sheet's model in every shop spelling**, derived mechanically from
  the key: as printed (`DW 7500`), hyphenated (`DW-7500`), glued (`DW7500`) and - for a key the
  sheet already hyphenates - spaced (`DW 7500C`). Live: `suggest.json?q=DW-7500C` is what
  returns Surmawala's DW-7500C listing, while `q=DW-7500` returns the plain model first.
- **A shop's own product JSON now supplies the whole gallery.** A Shopify search result names
  one picture; the product page's `.json` carries every gallery photo and every
  `media[].preview_image`, so the pipeline fetches it whenever the page it verified offers
  fewer than the target images. WooCommerce stores get the same treatment through the Store
  API (`/wp-json/wc/store/v1/products?slug=…`). Both run only after the page's model is
  verified, only when the row is short of images, and a failure is silent - the page's own
  gallery stands (`gallery_extractors/shopify_json.py`, `gallery_extractors/woo_json.py`,
  `config.structured_gallery_enabled`).
- **Fallback matching is relaxed, and every relaxed row is flagged for review.** A fallback
  page that carries the brand and the model's own number (``DW-2500`` page for a
  ``DW-2500C`` row) now publishes its pictures instead of failing, with decision
  `S15.relaxed`, status `PARTIAL` and a row in `output_dir/review_products.csv`; the sheet's
  own URLs keep strict S1/S3 (`config.relaxed_fallback_matching`, `model_matching.py`.
- **A spaced model spelling is a model spelling.** ``DW 7500 C`` and ``DW 2500 C`` produced
  *no* model key because the sheet prints the spaces the brand uses; the tokenizer now reads
  the glued identity through flexible separators, with guards so a generic ``TV 55`` or
  ``TCL 85`` never becomes a model.
- `review_products.csv` (`Title,Status,Note`) lists every row that needs a human eye - also
  written on `--dry-run`.

### Fixed
- **The fallback chain could never reach the priority-2 retailers.** A brand sitemap lists
  the whole model family, filled the row's candidate budget, and the loop stopped before any
  retail shop was searched - live: a Dawlance row never queried `surmawala.pk`, which stocks
  the exact product, so the row failed with "no page matched". Each source now contributes at
  most `fallback_max_pages_per_source` (3) of the `fallback_max_pages_per_row` (6) budget,
  and within a source the candidates are ranked (never filtered) so the sheet's own variant -
  colour word in the page URL or the shop's title, then the model in the slug - is tried first.
  Regression tests replay the real Surmawala `suggest.json` payload and the real Dawlance
  sitemap shape.
- **The product's own gallery was cut off at six downloads.** A page offering a *curated*
  gallery (gallery container, brand extractor, JSON-LD, embedded payload) is now read in full
  up to `max_images_per_product`, bounded by the row budget; scraped pages and the S2/S3 audit
  nodes keep the strict per-page cap (the live Dawlance "dark renders starve the row" fix).
  Live case: the official DW 7500 TWIN TUB CLEAR LID (WHITE) page publishes 10 photos and only
  four were previously published.
- **Relaxed matching could match inside a longer model code.** On normalized text ``dw7500``
  was found inside ``dw7500c``, so a DW-7500 row could be illustrated with the DW-7500C
  black/glass machine's photos. Identities and number stems now have to be whole tokens,
  matched through whatever separators the page uses (caught by today's acceptance run;
  regression test `test_relaxed_matching_never_swallows_a_sibling_model_code`).
- **The Shopify/Woo JSON extractors did not canonicalise their URLs**, so the page's own copy
  of a picture (``…?v=123``) and the JSON's counted as two different assets.

### Verified
- **Deep smoke test over the ten real sheet rows** (`verification/smoke_test_10_products.py`,
  offline, 3 workers, every branch: exact match, wrong link, dead URL, thin gallery, sibling
  variant refused, no-usable-image, relaxed rescue, no-match failure). It pins the closing
  banner rule the owner asked about: uploaded **once per template for the whole run**, the
  same URL appended **last** to every published row on both platforms, reused from
  `state/banner_urls.json` on a second run (no re-upload), and a `--dry-run` cache that can
  never leak into a real run. 26/26 checks green; output kept in
  `verification/smoke_test_10_products.output.txt`.

### Changed
- `max_images_per_product` 5 -> 10 and `--max-images` default 5 -> 10 (the target stays 3):
  up to ten of the product's own gallery pictures may be published.
- `dedup_key` no longer lets the same asset count twice when one form is the zoom link
  (`/FILE.png`) and the other the served render (`/FILE.png/1000Wx1000H/image.webp`).
- A sheet model key is searched in the three spellings shops actually use - the sheet's own
  (`DW 7500`), the hyphenated slug form (`DW-7500`) and the glued form (`DW7500`). A live
  check of Surmawala's Shopify suggest endpoint answered the hyphenated form while the sheet
  prints spaces, so a spaced title could not reach any shop. The *term* is still always the
  sheet's own model, never a code read off some page.

## [1.0.0] - 2026-10-06

### Added
- **Core Pipeline**: 7-stage deterministic product-image extraction, quality gating, template compositing, and cloud hosting pipeline (`image_pipeline`).
- **Recognition & Verification Safeguards (S1–S4)**:
  - Model key tokenization and brand+code recognition rules (`model_matching.py`).
  - Strict page model verification, description region isolation, and related link exclusion (`page_verification.py`).
- **Computer Vision Quality Gates (S5–S13)**:
  - URL keyword rejection for ads, badges, and marketing graphics (`S5`).
  - Extreme aspect ratio rejection for banners and strips (`S6`).
  - Border ring cleanliness and neutral bright corner verification (`S7`).
  - Minimum pixel dimensions validation (`S8`).
  - OpenCV headless QR code and barcode detector (`S9`).
  - Per-product image deduplication using 64-bit dHash and Pearson correlation (`S10`).
  - Byte integrity, MIME type, and EXIF orientation normalization (`S13`).
- **Scoped Quality Rescues**:
  - `S7.band`: Promo strip trimming for glued edge banners.
  - `S7.seam`: Divider seam trimming for layout boundaries.
  - `S7.official`: Relaxed border check for brand-official gallery photos with neutral corners.
- **Pluggable Gallery Extractors**:
  - Modular extractor registry (`gallery_extractors/`) with handlers for Dawlance, Haier, Magento, Schema.org JSON-LD, embedded Next.js/Shopify JSON, and generic galleries.
- **Template Compositing**:
  - Foreground product isolation using connected-component analysis (`compositing.py`).
  - Centered 1:1 square canvas expansion with light unsharp masking.
  - Brand template compositing for DigiMall and Alfa Mall with guaranteed logo-band margin protection.
- **Fallback Candidate Search Chain (S15)**:
  - Multi-tier search fallback querying brand sitemaps, Shopify `suggest.json`, and WooCommerce store endpoints (`sources.py`).
  - Strict re-verification enforcing that discovered pages match the row's model before reading images.
- **Hosting & Atomic Export**:
  - Direct integration with `freeimage.host` API with exponential retry backoff.
  - Platform closing banner attachment as the final image URL.
  - Atomic writes for `digimall_product_images.csv`, `alfamall_product_images.csv`, `failed_products.csv`, and `run_report.csv`.
  - Granular JSONL audit trail tracking every keep/reject decision and rule ID (`decisions.jsonl`).
- **Testing & Verification**:
  - 213 unit and integration tests executing completely offline using protocol test doubles.
  - Visual contact sheet generator and bounding box analyzer (`verification/build_sheets.py`).
- **Developer Context & Agent Skills**:
  - Architectural mapping and reading order (`docs/PROJECT_MAP.md`).
  - Formal agent coding rules, constraints, and boundaries (`AGENTS.md`).
  - Workspace agent skills in `.agents/skills/`.

### Changed
- Refactored deduplication key generation to ignore file format extensions (`.png` vs `.webp`).
- Re-architected download quota budgeting to account per unique asset rather than per HTTP retry attempt.
- Standardized project configuration into `pyproject.toml` supporting Python 3.10+, Ruff, Mypy, and Pytest.

### Fixed
- Fixed false rejection of Dawlance gallery photos filling the frame in official studio shots.
- Fixed dry-run cache contamination where local `file://` URIs leaked into live banner caches.
- Fixed semi-transparent logo bleed when pasting isolated products over template headers.

### Removed
- Purged 20 dead/unused functions, 4 obsolete configuration options, and temporary dev caches, reducing repository size from 30MB to under 1MB.

### Security
- Added secure environment loader (`load_env_file`) with explicit hierarchy and precedence for exported environment variables.
- Strictly prohibited API key logging across stdout, logs, CSVs, decision logs, and checkpoints.
- Added git pre-commit hook preventing secret files (`secrets.env`, `.env`) and private keys from being committed.

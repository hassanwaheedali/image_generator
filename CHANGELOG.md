# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
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

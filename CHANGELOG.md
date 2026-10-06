# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

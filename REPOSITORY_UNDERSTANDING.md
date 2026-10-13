# REPOSITORY UNDERSTANDING REPORT

**Repository:** `hassanwaheedali/image_generator`
**Branch / commit analysed:** `arena/79280a67-image-generator` @ `daa2922`
**Method:** structure → dependencies → entry points → patterns → data flow → testing → synthesis; every number measured by a command run on 2026-10-13, not copied from docs.
**Date:** 2026-10-13

> Supersedes the 2026-10-07 report, preserved at `REPOSITORY_UNDERSTANDING.2026-10-07.archive.md` (it analysed `3d71601`; ~30 commits landed since). The 2026-10-06 report is at `REPOSITORY_UNDERSTANDING.2026-10-06.archive.md`.

---

## PROJECT OVERVIEW

| | |
|---|---|
| **Name** | `image-pipeline` v1.0.0 (repo also carries workspace `plan.md`, `tasks/`, `verification/`) |
| **Type** | Offline-deterministic **CLI batch tool** — no web service, no API, no LLM calls. Two modes: Option 1 (scrape + verify pages) and Option 2 (`--direct`, sheet holds direct image URLs, no scraping) |
| **Primary language** | Python `>=3.10`; sandbox venv runs **3.14** |
| **Framework** | None. Flat modules + frozen dataclasses + `typing.Protocol` seams |
| **Purpose** | Read a sheet of `Title` + source URL rows. Per row: prove a page shows that exact model, harvest only that product's own gallery photos, reject anything that is not a clean white-background product shot, de-duplicate per product, isolate/square/sharpen, composite onto the DigiMall and Alfa Mall 1200×1200 templates, append each platform's closing banner last, upload to an image host (freeimage.host with ImgBB failover), write two import CSVs plus a full audit trail. |

**Governing rule** (`image_pipeline/AGENTS.md`): *rows that fail loudly are cheap; wrong pictures in a shop are not.* A `failed` row is a correct, deliberate outcome.

---

## ARCHITECTURE

**Staged batch pipeline with an explicit safeguard registry** (rules `S1`–`S21` plus scoped sub-rules). One process, one config (frozen dataclass + optional JSON overlay), a thread pool of product workers, four network boundaries behind `Protocol`s, and every quality decision attributed to a numbered rule id in `decisions.jsonl`.

Per-row flow (`pipeline.py:450`):
```
1 recognition   build_model_keys() + detect_brand()
2 pages         Path 1 (sheet has an official brand URL):
                  official sheet URLs -> non-official sheet URLs -> fallback chain (brand sitemaps -> Shopify -> Woo)
                Path 2 (all sheet URLs are retailers):
                  fallback search first; official candidates -> sheet URLs -> other retailer candidates
                (owner rule 2026-10-11: brand official always outranks marketplaces)
3 last resorts  S15.tolerated (flagged PARTIAL), then S15.relaxed (flagged PARTIAL + review CSV)
4 image gates   S5/S4/S2/S3 -> 64 KB probe (S6/S8) -> download (S13, S13.blur) -> pixel gates (S6/S7/S7.band/S7.seam/S7.official/S9/S16/S17/S20/S21) -> dedup (S10)
5 compose       crop-first isolation -> dynamic 2D bilinear studio whitening -> 1:1 canvas -> unsharp -> apply_template x2
6 upload        finalize_and_upload() (S14: both platforms or neither; banner last; FreeImageHost -> ImgBB fallback)
7 status        write_output_csvs() (per-mode failed sheets)
```

### Key components (line counts measured 2026-10-13)

| Component | Lines | Owns |
|---|---|---|
| `pipeline.py` | 2,278 | per-row flow, Path-1/Path-2 ordering, download budgets, S14 gate, S20/S21 layout rescue, `BatchRunner`, all writers — largest file, still growing |
| `model_matching.py` | 1,840 | the two recognition rules, series veto, colour/variant facts, variant separation |
| `image_quality.py` | 1,516 | every pixel gate (S6–S9, S13, S16, S17, S20, S21) + the scoped rescues |
| `config.py` | 1,038 | frozen `PipelineConfig` (all thresholds), `load_env_file()`, `load_config_file()` (JSON overlay), `ConfigError` |
| `url_filters.py` | 726 | S4/S5/S11, `KNOWN_BRANDS` (14 brands -> domains, incl. Midea -> mideapakistan.store), official-host checks, size rewrites |
| `compositing.py` | 686 | crop-first bg removal, dynamic 2D bilinear studio whitening, isolation, templates, JPEG export |
| `main.py` | 674 | CLI, fail-fast order, sheet auto-discovery, `--check-image` judge, env/config wiring |
| `page_verification.py` | 571 | S1/S2/S3 + ProductView selectors |
| `sources.py` | 567 | S15 fallback chain (sitemap cache, robots, Shopify/Woo JSON search) |
| `direct_pipeline.py` | 296 | Option 2 `--direct` flow (no scraping; S19 branded-template guard) |
| `fetch.py` | 337 | the only real HTTP client (`curl_cffi`) |
| `csv_io.py` | 296 | sheet reading, atomic writes, checkpoint, banner cache, slugs |
| `image_host.py` | 340 | FreeImageHost + ImgBBHost + APILayerHost + FallbackImageHost |
| `dedup.py` | 265 | S10 dHash + S17 colour consistency |
| `variant_terms.py` | 254 | variant vocabulary (colour families, code tails) |
| `filter_blurry_images.py` | 174 | standalone blur-report utility (not imported by the pipeline) |
| `similarity.py` | 65 | 32x32 blurred-grayscale upload fingerprinting |
| `gallery_extractors/` | 11 files | registry + brand/format extractors (find only; gates still run) |

Total: **13,621 lines** across 32 source files (21 top-level + 11 gallery_extractors), measured by `wc -l`.

---

## DIRECTORY STRUCTURE

```
/                       repo root (workspace: plan.md, tasks/, verification/, uploads/)
├── input/              sheets live here now (moved from image_pipeline/input by daa2922):
│                       1800_products.csv, sample_products.csv, website_ac.csv
├── pipeline_config.json   dynamic run-config overlay (identical copy inside image_pipeline/)
├── image_pipeline/     the project (self-contained, pip-installable)
│   ├── main.py              CLI entry point
│   ├── config.py            frozen PipelineConfig + env/JSON loaders
│   ├── pipeline.py          per-row flow + BatchRunner + writers
│   ├── direct_pipeline.py   Option 2 (--direct) flow
│   ├── model_matching.py / page_verification.py / url_filters.py / image_quality.py
│   ├── sources.py / dedup.py / compositing.py / fetch.py / image_host.py
│   ├── csv_io.py / decisions.py / models.py / protocols.py / similarity.py
│   ├── textnorm.py / variant_terms.py / filter_blurry_images.py
│   ├── gallery_extractors/  registry + per-brand extractors
│   ├── tests/               18 modules + HTML fixtures + conftest fakes
│   ├── assets/ input/ docs/ AGENTS.md README.md pyproject.toml
│   └── pipeline_config.json (second copy of the JSON overlay)
├── verification/     live-run evidence (contact sheets, probe scripts)
├── tasks/ docs/      plan.md, todo.md, riper notes
└── .agents/          skills + repo-wide rules (dry_dynamic_algorithm, pricing_engine)
```

`output/` and `state/` are runtime artefacts, gitignored. Each input sheet gets its own `output/<stem>/` and `state/<stem>/` so two sheets never mix.

---

## KEY DEPENDENCIES

Runtime (`image_pipeline/pyproject.toml`): `curl_cffi` (TLS-impersonating HTTP), `Pillow`, `numpy`, `scipy`, `opencv-python-headless` (QR/barcode, S9), `beautifulsoup4` + `lxml`, `requests` (upload only), `openpyxl` (.xlsx), `soupsieve>=2,<3` (pinned: v3 breaks the region-strip selectors, 2026-10-10).

Dev: `pytest`, `mypy --strict`, `ruff`, `types-beautifulsoup4`, `radon`.

Image hosts: freeimage.host (`FREEIMAGE_API_KEY`) with automatic ImgBB failover (`IMGBB_API_KEY`) and APILayer failover (`APILAYER_API_KEY`) via `FallbackImageHost`. The packaging `py-modules` list must match the top-level modules exactly — see concern 1.

---

## ENTRY POINTS

| Entry | What it does |
|---|---|
| `python3 main.py --input sheet.csv` | Option 1 full run (needs `FREEIMAGE_API_KEY` unless `--dry-run`) |
| `python3 main.py --direct --input links.csv` | Option 2: direct image URLs, no scraping |
| `python3 main.py --check-image FILE [--check-title T]` | run the real gate chain on saved files; exit 0 publishes / 1 gate stops / 2 unreadable |
| `python3 filter_blurry_images.py` | standalone blur report over `input/` |
| `pip install -e ".[dev]"` | editable install (flat layout needs the explicit `py-modules` list) |

Config precedence: **CLI flag > JSON config file > code default** (`main.py:163-190`). The JSON file (`--config`, default `pipeline_config.json`) is searched cwd -> project dir -> parent dir and currently overrides: early stop, gallery limits, image quotas, fallback budget, extra generic words.

---

## DATA FLOW

```
CSV/XLSX sheet --read_input--> ProductTask(index, title, source_urls)
   -> build_model_keys -> ModelKeys(tokens, identities, colours, variant_facts)
   -> per row:
      brand detect -> Path 1 or Path 2 page ordering (see ARCHITECTURE)
      _visit_page: S11 fetchable -> fetch -> S1 verify -> extract -> _consume_candidates
      _consume_candidates: S5/S4/S2/S3 -> probe S6/S8 -> download S13 -> S6/S7/S9/S16/S17/S20/S21 -> S10 dedup
      _compose_and_save: crop-first isolation + whitening -> 1:1 -> unsharp -> both templates
      finalize_and_upload: S14 both-or-neither, banner last, http(s) only
   -> ProductResult -> per-sheet CSVs (digimall/alfamall/failed_optionN/report/review) + decisions.jsonl + checkpoint
```

Resume: `state/<stem>/checkpoint.jsonl` append-only; skip only ok/partial with unchanged source-URL hash. Audit: one JSON line per kept/rejected image and per rejected page, each with its `rule_id`.

---

## TESTING

| | |
|---|---|
| **Framework** | `pytest`, `testpaths = ["image_pipeline/tests"]` |
| **Measured 2026-10-13** | **525 passed, 1 skipped** (the skip is OpenCV QR-encode capability, `test_image_quality.py:194-202`) |
| **Static gates** | `ruff check .` -> All checks passed! · `mypy --strict .` -> Success: no issues found in **49** source files |
| **Network** | none — conftest fakes (`FakePageFetcher`, `FakeImageFetcher`, `FakeImageHost`, `FakeSourceSearcher`) + fixtures |
| **CI/CD** | **none** — the three gates are run by hand |
| **Radon** | 58 blocks at grade C or worse (excluding tests) — documented baseline says 45 |

Test distribution (526 collected): e2e 88, image_quality 94, model_matching 87, direct_pipeline 34, page_verification 37, variant_terms 29, url_filters 29, sources 25, gallery_extractors 21, csv_io_resume 21, input_folder 11, env_file 11, similarity 11, dedup 8, json_helpers 8, state_json 4, fetch 4, performance_controls 4.

---

## CODE CONVENTIONS

- One rule = one named function + one direct test + one `rule_id`. Rule ids now span `S1`–`S21` plus sub-rules (`S7.band`, `S7.seam`, `S7.official`, `S11.fetch`, `S13.blur`, `S13.fetch`, `S15.fetch/relaxed/tolerated`).
- One config field per threshold; ranges validated in `__post_init__`.
- `pipeline.py` never imports `requests`/`curl_cffi` — network work goes behind a `Protocol`.
- Frozen dataclasses in `models.py`; parsers are pure functions; every write is atomic.
- `except Exception` allowed only in `BatchRunner` (logged + row marked failed); `PermanentAPIError` aborts.
- Line length 120; naming `snake_case`/`PascalCase`/`SCREAMING_SNAKE`; no dead code, no TODO without owner.

---

## AREAS OF CONCERN

All findings were produced by running commands, not reading docs.

### 1. Packaging drift broke the suite — found and fixed 2026-10-13
`direct_pipeline.py` and `filter_blurry_images.py` were never added to `[tool.setuptools] py-modules`, so the editable install could not import them: **the suite failed at collection** (`ModuleNotFoundError: No module named 'direct_pipeline'` in 3 test modules). Fixed by adding both modules to `image_pipeline/pyproject.toml` and re-running `pip install -e ".[dev]"`; suite then passed 525+1. Without CI, this class of breakage only shows when someone runs the suite by hand.

### 2. Every documented count is stale — again
| Source | Claims | Measured |
|---|---|---|
| `image_pipeline/AGENTS.md`, `README.md` | 330 tests | **525 passed, 1 skipped** |
| `image_pipeline/pyproject.toml` header, radon note | 45 blocks at C or worse | **58** |
| mypy message quoted in the 2026-10-07 report | 42 files | **49** |

Same lesson as last time: prose counts rot. Say "all green", not a number.

### 3. Two `pipeline_config.json` files, resolved by working directory
`pipeline_config.json` exists at both repo root and `image_pipeline/`. `load_config_file` searches cwd first, then project dir, then parent — so the two copies are interchangeable today (verified identical) but can silently diverge, and behavior then depends on where you run from. One file, or a startup warning, would remove the trap.

### 4. `pipeline.py` is 2,278 lines
It now owns Path-1/Path-2 ordering, budgets, upload gating, the S20/S21 layout rescue, `BatchRunner`, and all writers. Well-sectioned and tested, but it is the obvious split candidate.

### 5. `.agents/rules/dry_dynamic_algorithm.md` still contradicts the code
The rule forbids hardcoded brand lists; the code hardcodes exactly those (KNOWN_BRANDS, brand_sources, retail sources). Carried over unresolved from the 2026-10-07 report — still a human decision (scope the rule to `pricing_engine`, delete it, or dynamicise).

### 6. Still no CI
No `.github/workflows`. The three static gates plus `radon` are the entire safety net, run manually.

---

## RECOMMENDATIONS

In order:

1. **Add a minimal CI workflow** running `ruff` + `mypy --strict` + `pytest`. The suite is offline and hermetic; it will just work. This would have caught concern 1 on the day it was introduced.
2. **Stop naming counts in prose** — "must stay all-green" instead of "330 tests"; same for the radon baseline.
3. **Single `pipeline_config.json`** (or a drift warning at startup).
4. **Split `pipeline.py`** — the page-ordering/visit logic is the natural first extraction.
5. **Resolve the `dry_dynamic_algorithm.md` conflict** explicitly.
6. Live-verify a Midea row end to end: `mideapakistan.store` is in `KNOWN_BRANDS` but **not** in `config.brand_sources`, so the brand-first search is a no-op for Midea rows today (their brand coverage comes only from sheet URLs).

---

## WHAT I ACTUALLY RAN (2026-10-13)

```
git log --oneline 3d71601..HEAD        -> ~30 commits (S16-S21, --direct, ImgBB failover, JSON config, root input/)
wc -l image_pipeline/*.py gallery_extractors/*.py -> 13,621 lines / 32 files
.venv/Scripts/python.exe -m ruff check .          -> All checks passed!
.venv/Scripts/python.exe -m mypy --strict .       -> Success: no issues found in 49 source files
.venv/Scripts/python.exe -m pytest                -> 525 passed, 1 skipped in 258.68s
.venv/Scripts/python.exe -m pytest --co -q        -> 526 collected (distribution above)
.venv/Scripts/python.exe -m radon cc image_pipeline -n C -s --exclude "tests/*" -> 58 blocks
.venv/Scripts/python.exe image_pipeline/main.py --help -> exit 0
diff image_pipeline/pipeline_config.json pipeline_config.json -> identical
git ls-files | wc -l                              -> 249 (142 excluding .agents/)
```

**Fix applied this session:** added `direct_pipeline` + `filter_blurry_images` to `py-modules` and re-installed the editable package — before the fix the suite could not even collect.

**Not verified here:** any live upload (needs keys + unrestricted network), real gallery extraction, compositing output quality — those rest on `verification/` from earlier live runs.

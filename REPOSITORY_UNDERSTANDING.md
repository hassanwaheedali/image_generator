# REPOSITORY UNDERSTANDING REPORT

**Repository:** `hassanwaheedali/image_generator`
**Branch / commit analysed:** `arena/d868546a-image-generator` @ `527a58d` (single commit, 185 tracked files)
**Method:** the `context-engineering/Repository Understanding Framework` (structure → dependencies → entry points → patterns → data flow → testing → synthesis)
**Date:** 2026-10-06 · every number below was **measured in this session**, not copied from the project's own docs (discrepancies are called out explicitly).

---

## PROJECT OVERVIEW

| | |
|---|---|
| **Name** | `image-pipeline` v1.0.0 (the repo also carries a workspace-wide `plan.md` and the build plan for this feature) |
| **Type** | Offline-deterministic **CLI batch tool** (no web service, no API, no LLM calls) |
| **Primary language** | Python **3.10+** (verified on 3.11.2 in this sandbox) |
| **Framework** | None — plain modules + `dataclasses`; a hand-rolled 7-stage pipeline |
| **Purpose** | Read a product sheet (`Title` + source URL per row), **prove each page shows that exact model**, harvest only that product's own gallery photos, reject everything that is not a clean white-background product shot, de-duplicate per product, isolate/square/sharpen the product, composite it onto the DigiMall and Alfa Mall 1200×1200 templates, append each platform's 1200×420 closing banner **last**, upload to freeimage.host, and write two import CSVs plus a full audit trail. |

**Governing rule of the whole codebase** (stated in `plan.md` §0 and enforced by the gates):

> It is always better to publish **nothing** for a product than to publish the **wrong picture** for it.

A row that fails is a correct, deliberate outcome (`failed_products.csv`), never a crash.

---

## ARCHITECTURE

**Pattern: pipeline / staged batch processor with explicit safeguard registry.**
Not MVC, not microservices. One process, one frozen config, a thread pool of product workers, four network boundaries hidden behind `Protocol`s, and every quality decision attributed to a numbered rule id (`S1`..`S15`) written to `decisions.jsonl`.

```
main.py  (CLI, fail-fast order, logging, exit codes)
  └─ PipelineConfig (frozen, ~121 fields, validated in __post_init__)
       └─ BatchRunner  (ThreadPoolExecutor, resume via checkpoint, interrupt-safe, one writer lock)
            └─ process_product()  ── one sheet row ──────────────────────────────
                 1 recognition      model_matching.build_model_keys() + url_filters.detect_brand()
                 2 sheet pages      pipeline._visit_page() → page_verification.verify_page() (S1)
                                    + gallery_extractors.extract_candidates()
                 3 fallback search  sources.SourceChain.find_candidates()   (S15, only if unsolved)
                 4 image gates      pipeline._consume_candidates()
                                      cheap URL/DOM filters  S5, S4, S2, S3, S10-key, S12
                                      header probe (64 KB)   S8, S6
                                      download + bytes       S13
                                      page-furniture cuts    S7.band, S7.seam
                                      pixel gates            S7 (+S7.official), S9
                                      per-product dedup      S10 (dHash + correlation)
                 5 compose          compositing.isolate_main_product() → to_square_canvas()
                                    → sharpen() → apply_template() ×2
                 6 upload           pipeline.finalize_and_upload()            (S14, both-or-neither)
                 7 status           pipeline.write_output_csvs()
```

### Key components

| Component | Owns | Why it matters |
|---|---|---|
| `main.py` | CLI, fail-fast order (assets → config → sheet → run), exit codes 2/3, summary | the only entry point |
| `config.py` | **every** path, threshold and source table; `ConfigError`; `load_env_file()` | "one config field per threshold" is enforced by convention and review |
| `models.py` | frozen dataclasses + `Status` / `Decision` / `ImageRole` enums | results are comparable/serialisable, no aliasing |
| `protocols.py` | `PageFetcher`, `ImageFetcher`, `SourceSearcher`, `ImageHost` | the seam that keeps `pipeline.py` network-free and the 213 tests offline |
| `fetch.py` | curl_cffi sessions, per-domain rate limit, retry+backoff, HTTP status classification | only real HTTP client for pages/images/JSON |
| `image_host.py` | freeimage.host upload + permanent-vs-transient classification | a permanent error aborts the whole run |
| `model_matching.py` | the two recognition rules (model key; brand + distinctive code), truncation variants, colour/variant facts | the heart — loose about recognising, strict about the page |
| `page_verification.py` | S1 page strength 2/1/0, S2 description regions, S3 link-to-another-page, colour/variant flags | nothing is extracted from an unproven page |
| `url_filters.py` | S5 bad keywords, S11 fetchability, S4 other-model URLs, canonical/dedup keys, size rewrites, brand registry | all cheap checks run before any network call |
| `gallery_extractors/` | registry (`@register_extractor`) + 6 extractors: Dawlance, Haier, Magento, Schema.org JSON-LD, embedded Next.js/Shopify JSON, generic gallery | adding a brand = one file + one decorator line |
| `image_quality.py` | every pixel/byte gate: S6 shape, S7 border ring + neutral corners, S8 size, S9 QR/barcode, S13 bytes, plus the scoped rescues `S7.band` / `S7.seam` / `S7.official` | the "no wrong picture" guarantee lives here |
| `dedup.py` | S10 dHash + correlation, strictly per product | state is created inside `process_product`, never module-level |
| `compositing.py` | background knock-out, 1:1 canvas, unsharp, template compositing, JPEG export | never paints over the template logo band |
| `pipeline.py` | per-row flow, S14 upload gate, `BatchRunner`, all writers | 1 226 lines — the largest module by far |
| `csv_io.py` / `decisions.py` | atomic writes, checkpoint, banner cache, input reading; JSONL audit log | every write is temp-file + `os.replace` |
| `sources.py` | S15 fallback chain (brand sitemap → Shopify `suggest.json` → Woo `wc/store` → HTML), robots courtesy check, sitemap cache | only ever *finds pages*; the pipeline re-verifies each one |

**Import graph (local modules only, no cycles observed):** `config` / `models` / `protocols` are leaves; `model_matching`, `url_filters`, `image_quality`, `dedup`, `compositing`, `csv_io`, `decisions` depend only on leaves; `page_verification` → `model_matching` + `url_filters`; `pipeline` → 12 local modules; `main` → `pipeline`, `fetch`, `image_host`, `sources`.

---

## DIRECTORY STRUCTURE

```
/                                (repo root = a workspace, not the package)
├── image_pipeline/              ← THE PROJECT (58 tracked files)
│   ├── README.md                user manual (install, flags, rules, limitations)
│   ├── AGENTS.md                project rules: stack, commands, conventions, boundaries
│   ├── docs/PROJECT_MAP.md      module map + safeguard registry + "where to look for a task"
│   ├── main.py                  CLI entry point
│   ├── config.py  models.py  protocols.py
│   ├── pipeline.py             per-row flow, batch runner, writers
│   ├── fetch.py  image_host.py  network layers
│   ├── model_matching.py  page_verification.py  url_filters.py  sources.py   recognition
│   ├── image_quality.py  dedup.py  compositing.py                            image side
│   ├── csv_io.py  decisions.py  persistence / audit
│   ├── gallery_extractors/      registry + one file per brand/platform (+ jsonutil.py)
│   ├── tests/                   11 test modules + conftest fakes + 10 HTML fixtures
│   ├── assets/                  2 templates + 2 closing banners (placeholders, meant to be replaced)
│   ├── sample_input.csv         10-row sample sheet
│   ├── secrets.example.env      committed template (`.env` / `secrets.env` / `env.txt` also work)
│   └── pyproject.toml           dependencies + ruff + pytest + mypy (single config home)
├── plan.md                      the build contract/spec (72 KB, sections 0–11)
├── tasks/plan.md, tasks/todo.md implementation plan + checklist (phases 0–5 done, 9–10 open)
├── verification/                manual live-run evidence: VERIFICATION.md, contact sheets, sizing.txt
├── uploads/                     4 stray files (unreferenced — see concerns)
├── CHANGELOG.md  .gitignore  .gitattributes
└── .agents/                     vendored agent config (107 of 185 tracked files) — skills, rules, commands
```

**Size:** 10 596 lines of Python in `image_pipeline/` → ~7 321 production + ~3 275 test lines. Largest modules: `pipeline.py` 1 226, `model_matching.py` 755, `config.py` 642, `image_quality.py` 575, `url_filters.py` 535. 286 functions, mean 17 lines.

**No CI and no root README/LICENSE:** `.github/` does not exist; the repo root has no `README.md` and no `LICENSE`; there is no lockfile or constraints file.

---

## KEY DEPENDENCIES

All declared in `image_pipeline/pyproject.toml` (the only dependency list), all with open-ended lower bounds, **no pins**:

| Dependency | Constraint | Purpose |
|---|---|---|
| `curl_cffi` | `>=0.7` | TLS-impersonating HTTP (pages + images) — needed because some CDNs 403 a plain client |
| `Pillow` | `>=10` | decode / resize / composite |
| `numpy` | `>=1.26` | pixel maths (border ring, masks, dHash) |
| `scipy` | `>=1.11` | connected-component labelling for product isolation |
| `opencv-python-headless` | `>=4.8` | QR / barcode decode (S9), no GUI deps |
| `beautifulsoup4` + `lxml` | `>=4.12` / `>=5` | DOM parsing and region stripping |
| `requests` | `>=2.31` | plain HTTP for the image-host upload |
| `openpyxl` | `>=3.1` | `.xlsx` input sheets |
| dev extras | `pytest>=8`, `mypy>=1.8`, `ruff>=0.9`, `types-beautifulsoup4` | quality gates |

`mypy` is configured `strict = true` with three written overrides (`scipy`, `requests`, `openpyxl`, `cv2` ship no stubs). `pip-audit` against the declared runtime set: **no known vulnerabilities**.

---

## ENTRY POINTS

* **`image_pipeline/main.py`** — the single entry point (`python3 main.py --input sheet.csv|xlsx`). Flags: `--dry-run`, `--workers`, `--limit`, `--redo`, `--no-resume`, `--no-fallback`, `--refresh-sitemaps`, `--min-images` / `--max-images` / `--target-images`, `--assets-dir` / `--output-dir` / `--state-dir`, `--log-level`, `--env-file`. Exit codes: **2** = config/input error, **3** = cannot write output.
* **`image_pipeline/tests/`** — the offline entry point (`pytest tests -q`); `tests/conftest.py` provides `FakePageFetcher`, `FakeImageFetcher`, `FakeImageHost`, `FakeSourceSearcher`, `LocalPathHost`.
* **`verification/build_sheets.py`** — manual, not part of the package: rebuilds the visual contact sheets from a previous `output/` run.
* No API routes, no CLI subcommands, no daemons.

**Secrets / initialisation order:** `FREEIMAGE_API_KEY` (optional `FREEIMAGE_UPLOAD_URL`) read from the environment at call time; a real exported variable always wins over `.env` / `secrets.env` / `env.txt` (searched in cwd → project dir → parent). Missing key without `--dry-run` fails immediately; the value is never logged. Startup order is assets → config → sheet → checkpoint → run.

---

## DATA FLOW

1. **Input** — `.csv` (utf-8-sig) or `.xlsx`; headers matched case/space/underscore-insensitively (`Title`/`Product Title`/`Name`…, `Images`/`Source URL`/`URL`…); several URLs per cell split on `|` or newline. Duplicate titles are **merged and processed once**; input order is preserved.
2. **Recognition** — `build_model_keys(title)` extracts model-like tokens, glued identities, colour families and variant facts. Two rules only: (a) the **model key** must appear on the page (URL, title, `og:title`, any `<h1>`, body) with free separators, glued spellings and a tolerated dropped trailing letter — but a key is never *extended* (`HSU-12` ≠ `HSU-12HFPAA`); (b) **brand + distinctive code** (≥5 chars, ≥3 digits, prefix guards, never on another maker's domain).
3. **Page proof** — `verify_page` returns strength 2/1/0 with the match rule recorded; strength 0 extracts nothing (S1). Colour/variant differences only annotate `run_report.csv`; they never reject a row (settled customer decision).
4. **Extraction** — the registry extractor for the host (or JSON-LD / embedded JSON / gallery containers / whole-page fallback) yields `ImageCandidate`s with provenance + gallery order + DOM flags. Extractors only *find* candidates; all decisions stay in the shared filter chain.
5. **Gating** — cheap URL/DOM filters → 64 KB header probe (S8/S6, no download) → one full download with one budget slot per *asset* (upscale rewrite first) → S13 bytes → S7.band/S7.seam cuts → S7 ring (+ scoped S7.official) → S9 → S10 per-product dedup. Every keep/reject is one line in `decisions.jsonl` with its rule id.
6. **Compose** — background knock-out via connected components, centred 1:1 canvas at `product_fill_ratio` 0.95 of the template's content box, light unsharp, both templates; saved to `raw/`, `isolated/`, `final_digimall/`, `final_alfamall/`.
7. **Upload** — nothing is uploaded until the set is final (S14); a row publishes on **both platforms or neither**; each platform's banner is uploaded once per content hash (cache keyed `dry:`/`live:` so a dry run can never leak a `file://` URL) and appended **last**. A permanent host error aborts the whole run after flushing the checkpoint.
8. **Output & resume** — `output/{digimall,alfamall}_product_images.csv` (`Title,Brand,Image_Urls`, pipe-separated, no spaces), `failed_products.csv`, `run_report.csv`, `decisions.jsonl`, `pipeline_run.log`, generated `output/README.md`; `state/checkpoint.jsonl` skips `ok`/`partial` rows whose source-URL hash is unchanged (`--redo` overrides); `state/sitemaps/<host>.jsonl` (7-day TTL) feeds only the fallback chain.

---

## TESTING

| | |
|---|---|
| **Framework** | `pytest` 8+ (`testpaths = ["tests"]`, `-q`), plus `mypy --strict` and `ruff` |
| **Result measured here** | **213 passed, 0 failed, 0 skipped** in ~27 s — exactly the number the CHANGELOG claims |
| **Coverage** (pytest-cov, tests omitted) | **82 %** — 3 495 statements, 624 missed |
| **Test types** | unit + integration/end-to-end through the protocols with fakes. **No network tests** — deliberate; live proof lives in `verification/` |
| **Fixtures** | 10 small HTML pages (listing, Magento gallery, JSON-LD, description images, related carousel, weak page…) |
| **Per-module coverage** | `models` 98 %, `magento` 98 %, `jsonutil` 96 %, `model_matching`/`page_verification` 95 %, `csv_io` 93 %, `dedup` 91 %, `gallery_extractors/__init__` 91 %, `url_filters` 89 %, `pipeline` 88 %, `sources` 86 %, `config` 85 %, `compositing` 84 %, `image_quality` 75 %, `image_host` 52 %, **`fetch` 43 %**, `dawlance` 31 %, **`main` 0 %** |
| **CI/CD** | **None.** No `.github/`, no pre-commit config, no Makefile/tox/nox. |

---

## CODE CONVENTIONS

* **Naming:** `snake_case` everywhere; `_private` helpers; rule ids `S1`..`S15` + scoped `S7.band`, `S7.seam`, `S7.official`, `S11.fetch`, `S13.fetch`, `S15.fetch`.
* **File organisation:** **by responsibility**, one rule-family per module (recognition / verification / URL filters / pixel gates / dedup / compositing / persistence), plus one file per brand extractor.
* **Data:** frozen dataclasses in `models.py`; enums for status/decision/role; no mutable globals except two lock-guarded caches (banner URLs in `PipelineDeps`, template images in `_ASSET_CACHE`).
* **Errors:** a row never raises out of the batch — `BatchRunner` catches `Exception`, logs a traceback and marks the row failed, while `PermanentAPIError` deliberately aborts the run (this is why `BLE001`/`S110` are relaxed, with the reason written in `pyproject.toml`). Missing assets/config fail before any work starts; output failures return exit code 3.
* **I/O:** every file write is atomic (`atomic_write_csv` / `atomic_write_text` / `save_image_atomic`); CSVs always follow input order.
* **Style:** line length 120; broad ruff set (`E,F,W,I,B,UP,C4,SIM,PIE,PERF,RUF,FURB,ISC,S,BLE`); no commented-out code, **no TODO/FIXME/XXX/HACK in any `.py`** (verified), docstrings say *why* in plain English.
* **Extension pattern:** one rule = one named function + one config field + one direct test + one `rule_id`; one new brand = one file + one `@register_extractor` line.
* **Boundaries written into `AGENTS.md`:** never weaken S1/S3/S7 to make a row pass; never publish a non-`http(s)` URL; never let the product touch the template logo band; never add an unnecessary dependency; never log a secret.

---

## VERIFIED STATE OF THE REPOSITORY (this session)

The project's own docs (`AGENTS.md`, `verification/VERIFICATION.md`) state the three tool commands must be green. On the shipped tree **as found**, that was **not reproducible**:

| Command | As found | After the fix applied in this session |
|---|---|---|
| `pip install -e ".[dev]"` (the documented install) | **FAILS** — setuptools flat-layout error: `Multiple top-level packages discovered in a flat-layout: ['assets', 'gallery_extractors']` | installs cleanly |
| `ruff check .` with the declared lower bound `ruff>=0.6` | **aborts**: `Unknown rule selector: RUF059` (0.6–0.8.x cannot parse the config) | `ruff>=0.9`; **All checks passed!** |
| `mypy --strict .` | 18 errors (12 with `types-beautifulsoup4` added) under an older pinned set (mypy 1.14 / numpy 2.2 / Pillow 11) | **Success: no issues found in 36 source files** on a fresh install of today's versions |
| `pytest tests -q` | **213 passed** in 26.9 s | unchanged (213 passed) |
| coverage | not configured anywhere (no `[tool.coverage]`, no CI) | measured: **82 %** |

**Fix applied in this session (2 files' worth of change, `image_pipeline/pyproject.toml` only):**
1. added `[build-system]` + explicit `[tool.setuptools] packages` / `py-modules` lists so the documented editable install works;
2. raised the dev extra to `ruff>=0.9` (with a comment explaining why) and added `types-beautifulsoup4` (dev-only, needed by `mypy --strict` for the DOM code).

No production code was touched. `git status` shows only `image_pipeline/pyproject.toml` modified plus this report added.

---

## AREAS OF CONCERN

1. **Reproducibility is unpinned.** Every dependency is a `>=` bound with no lockfile. Evidence: `ruff 0.8.6` (allowed by the old `>=0.6`) cannot even parse the project config; an older-but-plausible mypy/numpy/Pillow set produced 12 `mypy --strict` errors while today's resolution is clean. The "green tree" claim silently depends on the day you install.
2. **No CI.** The only proof of the quality claims is a hand-run `verification/VERIFICATION.md`. Nothing re-runs the tests/lints on change, and nothing runs the live acceptance checks after a code change.
3. **Biggest test holes are exactly the risky paths:** `main.py` **0 %** (arg parsing, exit codes, fail-fast order), `fetch.py` **43 %** and `image_host.py` **52 %** (transient-vs-permanent classification — the switch that decides "retry" vs "abort the whole batch"), `gallery_extractors/dawlance.py` **31 %**. The fakes are excellent for the row logic, but they mean the error-classification branches never execute in tests.
4. **Two god-functions in one god-module.** `pipeline.py` is 1 226 lines; `process_product()` is **197 lines**, `_consume_candidates()` is **175 lines with 13 parameters** (task, page, candidates, keys, config, deps, dedup, seen_keys, accepted, rejections, source_label, budget, official_page). The S-gate ordering inside them is covered mainly through the end-to-end tests, so a refactor there is the highest-risk change in the repo.
5. **Documentation ↔ code drift (minor but real).** `docs/PROJECT_MAP.md` attributes `url_belongs_to_other_model` to `url_filters.py`; it exists only in `model_matching.py:416`. The `CHANGELOG` claims a "git pre-commit hook preventing secret files" was added; **no hook is versioned** (`.git/hooks/` contains only a local `commit-msg` that appends an `arena-agent` co-author trailer, which is a sandbox artifact, not repo content). The README's install command was broken until this session.
6. **Repo-root noise / dead weight.** `uploads/` (4 files, 212 KB — two brand PNGs and a copy of the sample sheet) is referenced by **no** code or doc and duplicates `image_pipeline/sample_input.csv`. `verification/` hardcodes the absolute path `/home/user/verification/` and depends on a generated `output/` folder that is not in the repo. There is no root `README.md` and no `LICENSE`.
7. **Conflicting agent instructions in `.agents/`.** 107 of the 185 tracked files are a vendored agent-skill library. Two rules do **not** belong to this project: `pricing_engine.md` (an AlfaMall/DigiMall repricing engine) and `dry_dynamic_algorithm.md`, which mandates *zero hardcoded brand lists / category conditionals / SKU prefixes* — directly contradicting this pipeline's deliberate `config.PipelineConfig.brand_sources` / `source_platforms` / `url_filters.KNOWN_BRANDS` tables. An agent that reads them could "refactor" intended configuration.
8. **Small residual coupling risks.** `pipeline._ASSET_CACHE` hands the same PIL template objects to every worker thread (read-only by convention, but not documented as such); the fallback chain's sitemap/robots caches are shared state; `--workers 4` plus per-domain rate limiting is the only ordering protection.
9. **Live-only verification path.** `verification/build_sheets.py` and the contact sheets are manual, path-dependent, and untested — the acceptance bar (fill 94.3–95.0 %, logo band byte-identical, 38/38 re-identification) can only be re-checked by a human running a real upload run with a key.
10. **Scope of a "row failed" default.** By design, light-grey products on white, JS-only galleries, and link-wrapped promo images fail safely rather than publishing a guess — correct, but it means real-world yield depends on per-brand calibration (`README` §12 admits one calibration pass per new brand).

---

## RECOMMENDATIONS

**Highest value first.**

1. **Add CI** (GitHub Actions, one job: `pip install -e ".[dev]"` → `ruff check .` → `mypy --strict .` → `pytest -q` → upload coverage). This turns the project's own claims into something that stays true — and the install/ruff bugs found this session would have been caught by the first run.
2. **Pin the dev toolchain** (`constraints.txt` or exact dev extras, e.g. ruff 0.16.x, mypy 2.x, numpy 2.3+, Pillow 12, plus `types-beautifulsoup4`) and record the verified combination in `AGENTS.md`. Keep the runtime bounds loose if you like, but make the *quality gate* reproducible.
3. **Close the untested error paths**: unit-test `classify_http_status`/retry/backoff and the freeimage response classification with recorded payloads, and add CLI tests for `main()` (exit 2 on bad config/input, exit 3 on write failure, dry-run without a key). Those are the branches whose bugs cost an entire batch.
4. **Refactor `_consume_candidates` before touching it.** Introduce a small `CandidatePipeline` state object (budget, dedup, accepted list, seen keys, rejections) so the 13-parameter call becomes 3, and keep the gate order as data (`GATE_ORDER` list of `(rule_id, fn)`) so it is directly testable. Do it under the existing end-to-end tests, then split `pipeline.py` into `runner` / `row` / `writers`.
5. **Reconcile the docs with the code**: fix the `PROJECT_MAP` row for `url_belongs_to_other_model`; either version the promised secret-blocking pre-commit hook under `hooks/` or correct the CHANGELOG; note the corrected install in the README (done in this session's diff, worth a CHANGELOG line).
6. **Tidy the repo root**: delete or move `uploads/`; add a 5-line root `README.md` pointing at `image_pipeline/README.md`, `AGENTS.md` and `docs/PROJECT_MAP.md`; add a `LICENSE`; make `verification/` paths relative (`Path(__file__).parent`) and document that it needs a prior `output/` run.
7. **Quarantine the unrelated agent rules** — move `pricing_engine.md` and `dry_dynamic_algorithm.md` out of `.agents/rules/` (or namespace per project), and add one line to `AGENTS.md` saying which rule files apply to this repo. Otherwise they will keep steering agents away from the deliberate config design.
8. **Raise coverage where it protects money**: `image_quality` 75 % → add adversarial cases for the S7 rescues (band/seam/official must not become a general bypass), and `dawlance` 31 % → add a fixture for the brand gallery that caused the live "only 1 image" bug.
9. **Optional, cheap:** a marked, opt-in smoke test (`pytest -m live --dry-run --limit 3 sample_input.csv`) that exercises the real fetchers without uploading, so the fake/live gap is bridged deliberately rather than manually.
10. **Document the concurrency contract** in `AGENTS.md`: which caches are shared, which objects are read-only across threads, and that `_ASSET_CACHE` images must never be mutated in place.

---

## QUICK REFERENCE (for the next reader/agent)

| I want to… | Start here |
|---|---|
| understand one row end to end | `docs/PROJECT_MAP.md` §"One row, end to end" |
| change a threshold | `config.PipelineConfig` — never a stage function |
| add a brand extractor | `gallery_extractors/<brand>.py` + `@register_extractor` + a fixture + a test |
| add a safeguard | `AGENTS.md` → *Patterns* (config field → named function → gate runner line → test → README table) |
| debug "this row found nothing" | `output/decisions.jsonl` (rule id per keep/reject), then `run_report.csv` |
| change sources searched | `config.brand_sources` / `retail_shopify_sources` / `retail_woo_sources` / `source_platforms` |
| re-run without uploads | `python3 main.py --input sample_input.csv --dry-run` |
| see the live proof | `verification/VERIFICATION.md` + the contact sheets |

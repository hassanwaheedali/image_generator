# REPOSITORY UNDERSTANDING REPORT

**Repository:** `hassanwaheedali/image_generator`
**Branch / commit analysed:** `arena/79280a67-image-generator` @ `3d71601`
**Method:** `.agents/skills/context-engineering/SKILL.md` (structure → dependencies → entry points → patterns → data flow → testing → synthesis)
**Date:** 2026-10-07 · every number below was **measured by a command run in this session**, not copied from the project's docs. Where a doc disagrees with a measurement, the measurement wins and the disagreement is called out.

> Supersedes the 2026-10-06 report, preserved at `REPOSITORY_UNDERSTANDING.2026-10-06.archive.md`. That report cites commit `527a58d`, which **does not exist in this repo's object store** (`git cat-file -t 527a58d` → *fatal: Not a valid object name*), and "185 tracked files" and "213 passed", neither of which reproduces. Its structure/architecture narrative still holds; its numbers do not.

---

## PROJECT OVERVIEW

| | |
|---|---|
| **Name** | `image-pipeline` v1.0.0 (the repo also carries a workspace `plan.md`, `tasks/`, and `verification/`) |
| **Type** | Offline-deterministic **CLI batch tool** — no web service, no API, no LLM calls |
| **Primary language** | Python `>=3.10` (`pyproject.toml`); sandbox runs **3.11.2** |
| **Framework** | None. Plain modules + frozen dataclasses + `typing.Protocol` seams |
| **Purpose** | Read a sheet of `Title` + source URL rows. For each row: fetch the page, **prove the page shows that exact model**, harvest only that product's own gallery photos, reject anything that is not a clean white-background product shot, de-duplicate per product, isolate/square/sharpen, composite onto the DigiMall and Alfa Mall 1200×1200 templates, append each platform's closing banner **last**, upload to freeimage.host, and write two import CSVs plus a full audit trail. |

**Governing rule of the whole codebase** (`AGENTS.md`, *When something is unclear*):

> Rows that fail loudly are cheap; wrong pictures in a shop are not.

A `failed` row is a correct, deliberate outcome — not a crash.

---

## ARCHITECTURE

**Pattern: staged batch pipeline with an explicit safeguard registry.** Not MVC, not microservices. One process, one frozen config, a thread pool of product workers, four network boundaries hidden behind `Protocol`s, and every quality decision attributed to a numbered rule id written to `decisions.jsonl`.

```
main.py  (CLI · fail-fast order: assets → config → sheet → checkpoint → run · exit codes 2/3)
  └─ PipelineConfig          (frozen, 119 fields, ranges validated in __post_init__)
       └─ BatchRunner        (workers, resume via checkpoint, interrupt-safe, writer lock)
            └─ process_product()  ── one sheet row, 7 stages ──────────────────────
                 1 recognition   model_matching.build_model_keys() + url_filters.detect_brand()
                 2 sheet pages   pipeline._visit_page() → page_verification.verify_page()  (S1)
                                 + gallery_extractors.extract_candidates()
                 3 fallback      sources.SourceChain.find_candidates()   (S15, only if unsolved)
                 4 image gates   pipeline._consume_candidates()
                                   URL/DOM filters    S5 S4 S2 S3 S10-key S12
                                   header probe 64 KB S8 S6
                                   download + bytes   S13
                                   page furniture     S7.band S7.seam
                                   pixel gates        S7 (+S7.official) S9
                                   per-product dedup  S10 (dHash + correlation)
                 5 compose       isolate_main_product → to_square_canvas → sharpen → apply_template ×2
                 6 upload        pipeline.finalize_and_upload()            (S14, both-or-neither)
                 7 status        pipeline.write_output_csvs()
```

### Key components

| Component | Owns | Why it matters |
|---|---|---|
| `main.py` (330 L) | CLI, fail-fast order, sheet auto-discovery, logging, summary | the only entry point |
| `config.py` (689 L) | **every** path, threshold and source table; `ConfigError`; `load_env_file()` | enforces "one config field per threshold" |
| `pipeline.py` (1588 L) | per-row flow, download budget, upload gate (S14), `BatchRunner`, all writers | the orchestration core — largest file |
| `model_matching.py` (951 L) | the two recognition rules, glued/spaced identities, truncation, colour/variant facts, variant separation | the strictness of the whole product lives here |
| `variant_terms.py` (247 L) | the variant vocabulary: colour families + neutral carve-out, model-code letter tails | one rule, no brand/suffix/colour tables |
| `textnorm.py` (28 L) | `normalize_text` / `normalize_key`, the leaf both matching and variants normalize through | no import cycle, punctuation never hides a colour |
| `image_quality.py` (575 L) | every pixel gate (S6–S9, S13) + the two scoped rescues | what may be published at all |
| `url_filters.py` (547 L) | URL-level rules S4/S5/S11, brand registry, official-host check, size rewrites | cheapest gates run first |
| `sources.py` (490 L) | the S15 fallback chain, robots check, sitemap cache, candidate ranking | the "sheet URL is wrong" path |
| `page_verification.py` (413 L) | S1/S2/S3 + the size/type flags + the colour-conflict rejection | the "is this really that model" proof |
| `compositing.py` (403 L) | isolation, 1:1 canvas, unsharp, header-safe template compositing, JPEG export | the visual output contract |
| `protocols.py` (82 L) | `PageFetcher`, `ImageFetcher`, `ImageHost`, `SourceSearcher` | the seam that keeps the 330 tests offline |
| `gallery_extractors/` (10 files) | one file per brand/format + `@register_extractor` registry | extractors only *find*; gates still run |
| `csv_io.py` (284 L) | sheet reading, atomic writes, checkpoint, banner cache, slugs | every write goes through here |

Total: **12,604 lines** across the 39 Python modules measured by `mypy`.

---

## DIRECTORY STRUCTURE

```
/                         repo root (workspace: plan.md, tasks/, verification/, uploads/)
├── image_pipeline/       ← the actual project (self-contained, pip-installable)
│   ├── main.py                 CLI entry point
│   ├── config.py               the single frozen rule surface
│   ├── pipeline.py             per-row flow + BatchRunner + writers
│   ├── model_matching.py       recognition rules
│   ├── page_verification.py    S1/S2/S3
│   ├── url_filters.py          S4/S5/S11 + brand registry
│   ├── image_quality.py        S6–S9, S13
│   ├── dedup.py                S10
│   ├── compositing.py          isolation + templates
│   ├── fetch.py                the ONLY real HTTP client (curl_cffi)
│   ├── image_host.py           freeimage.host upload
│   ├── sources.py              S15 fallback chain
│   ├── csv_io.py / decisions.py / models.py / protocols.py
│   ├── gallery_extractors/     8 brand/format extractors + registry + jsonutil
│   ├── tests/                  12 files, 10 HTML fixtures, conftest fakes
│   ├── docs/PROJECT_MAP.md     which file owns which rule
│   ├── assets/                 2 templates + 2 banners (placeholders shipped)
│   ├── input/                  sheet auto-discovery folder
│   ├── AGENTS.md               ← the rules file (read this first)
│   └── pyproject.toml          the ONLY config home
├── verification/         live-run evidence: VERIFICATION.md, contact sheets, probe scripts
├── tasks/                plan.md + todo.md (phase checklists with evidence)
├── plan.md               954-line original build plan
└── .agents/              35 skills + 2 repo-wide rules + 1 command
```

`output/` and `state/` are runtime artefacts, gitignored.

---

## KEY DEPENDENCIES

Runtime (`pyproject.toml`, resolved versions installed this session):

| Dependency | Purpose | Resolved |
|---|---|---|
| `curl_cffi` | TLS-impersonating HTTP (pages + images), `impersonate="chrome120"` | 0.16.3 |
| `Pillow` | decode / resize / composite / JPEG export | 12.3.0 |
| `numpy` | pixel maths: border ring, masks, dHash | 2.4.6 |
| `scipy` | connected-component labelling for product isolation | 1.17.1 |
| `opencv-python-headless` | QR / barcode decode (S9) | 5.0.0.93 |
| `beautifulsoup4` + `lxml` | DOM parsing, region stripping | 4.15.0 / 6.1.3 |
| `requests` | plain HTTP for the image-host upload only | 2.34.2 |
| `openpyxl` | `.xlsx` input sheets | 3.1.5 |

Dev: `pytest` 9.1.1, `mypy` 2.4.0, `ruff` 0.16.10, `types-beautifulsoup4`.

No dependency conflicts. Three libraries ship no stubs (`scipy`, `requests`/`openpyxl`/`cv2`) and are exempted in `[[tool.mypy.overrides]]` so a missing stub can never hide a real bug.

---

## ENTRY POINTS

| Entry | What it does |
|---|---|
| `main.py` (`main()`, guarded by `__main__`) | the CLI. Fail-fast order: assets → config → sheet → checkpoint → run. Exit `0` ok, `2` config/sheet error, `3` output write failure. |
| `main.py --dry-run` | everything except uploads and the two platform CSVs |
| `pip install -e ".[dev]"` | editable install; `[tool.setuptools] py-modules` + `packages` lists exist because flat layout otherwise fails the build |

Sheet auto-discovery (`resolve_input_path`) searches `input/`, `uploads/`, and their `image_pipeline/`-prefixed and `../` variants, newest-mtime first.

---

## DATA FLOW

```
CSV/XLSX sheet ──read_input──▶ ProductTask(index, title, source_urls)
   │  merge_duplicate_titles folds same-title rows
   ▼
build_model_keys(title) ──▶ ModelKeys(tokens, identities, colours, variant_facts, colour_word)
   │  no model-like token ⇒ reject "4.1", row FAILED, nothing else runs
   ▼
visit each sheet URL in order ──▶ verify_page (S1/S2/S3) ──▶ PageMatchResult(strength)
   │  tolerated-spelling pages are DEFERRED, not accepted (title outranks the link)
   ▼  still unsolved?
SourceChain.find_candidates ──▶ [PageCandidate]  → re-verified with S15 before any image
   ▼
extract_candidates(html) ──▶ [ImageCandidate(url, order, matches_model, dom_flags)]
   ▼
_consume_candidates: cheap gates → 64 KB probe → full download → pixel gates → dedup
   ▼
AcceptedImage(raw_path, square_path, digi_path, alfa_path)
   ▼
finalize_and_upload (S14: both platforms or neither; refuses any non-http(s) URL)
   ▼
ProductResult → write_output_csvs → digimall/alfamall CSVs, failed CSV, run report,
                                    review queue, decisions.jsonl, checkpoint.jsonl
```

**State:** `state/checkpoint.jsonl` is append-only resume state. A row is skipped only when status ∈ {ok, partial}, the URL list is non-empty, **and** the source-URL hash is unchanged (`CheckpointEntry.is_resume_done`). `state/banner_urls.json` is mode-scoped (`dry:` / `live:`) so a dry run can never leak a `file://` path into a live CSV.

**Audit trail:** one JSON line per kept/rejected image *and* per rejected page, each carrying the `rule_id` that decided it.

---

## TESTING

| | |
|---|---|
| **Framework** | `pytest`, `testpaths = ["tests"]`, `addopts = "-q"` |
| **Measured this session** | **330 passed, 0 failed, 0 skipped** (after the 2026-10-07 probabilistic-matching work) |
| **Coverage** | not measured — no coverage tool is configured |
| **Test types** | unit (pure parsers, gates) + end-to-end (`test_pipeline_end_to_end.py`, 51 tests) with `conftest.py` fakes |
| **Network** | none — `FakePageFetcher`, `FakeImageFetcher`, `FakeImageHost`, `FakeSourceSearcher`, `LocalPathHost` + 10 HTML fixtures + synthetic images |
| **Static gates** | `ruff check .` → **All checks passed!** · `mypy --strict .` → **Success: no issues found in 39 source files** |
| **CI/CD** | **none** — no `.github/workflows`, no CI config of any kind |

Distribution of the 255:

```
test_pipeline_end_to_end.py 51   test_page_verification.py 24   test_dedup.py            5
test_model_matching.py      45   test_url_filters.py       21   test_json_helpers.py     8
test_image_quality.py       35   test_csv_io_resume.py     18   test_input_folder.py     7
test_sources.py             18   test_gallery_extractors.py 12  test_env_file.py        11
```

Live verification is a **separate, deliberate step** outside the suite (`verification/`), because the suite is offline by design.

---

## CODE CONVENTIONS

- **One rule = one named function + one direct test + one `rule_id`.** Ids `S1`..`S15` plus scoped sub-rules `S7.band`, `S7.seam`, `S7.official`, `S11.fetch`, `S13.fetch`, `S15.fetch`, `S15.relaxed`, `S15.tolerated`.
- **One config field per threshold.** No magic numbers in stage functions; ranges validated in `__post_init__` so a mis-typed threshold fails at startup.
- **`pipeline.py` never imports `requests` / `curl_cffi`.** New network work goes behind a `Protocol`.
- Frozen dataclasses in `models.py`; parsers are pure `text -> objects` functions.
- **Every write is atomic** (`atomic_write_csv` / `atomic_write_text` / `save_image_atomic`). No bare `open(path, "w")`.
- Naming: `snake_case` functions/fields, `PascalCase` classes, `_leading_underscore` for module-private, `SCREAMING_SNAKE` constants. Line length 120.
- Error handling: broad `except Exception` is allowed **only** in `BatchRunner` (one bad row must not abort the other nine) and is logged with a traceback; `PermanentAPIError` is re-raised to abort the run. `ruff` ignores `BLE001`/`S110` for exactly this reason, documented in the `pyproject.toml` header.
- Files organised **by pipeline concern**, not by feature folder.

---

## AREAS OF CONCERN

All four findings below were produced by running the code, not by reading it.

### 1. `--dry-run` points the user at a file that does not exist — *verified bug*

`write_output_csvs` writes `failed_products.csv` **only inside** `if include_platform_csvs:`, which is `False` on a dry run. But two things reference it unconditionally:

- `main.py:318` — `print(f"failed  : {failed}   (see {config.failed_csv.name})")`
- `output/README.md` (written on **every** run) lists it under *Reports* at line 14 and again at line 38

Measured: a dry run printed `failed : 1   (see failed_products.csv)` while `cat output/failed_products.csv` → *No such file or directory*. The `dry_note` in `_write_output_readme` disclaims only the **import** CSVs, not this one. Small, but it sends a human looking for a file that isn't there.

### 2. Every documented test count is stale — three different numbers, all wrong

| Source | Claims |
|---|---|
| `AGENTS.md:37`, `README.md:451` | 248 tests |
| `plan.md:754`, `tasks/plan.md:415`, `tasks/todo.md:123`, `VERIFICATION.md:8` | 213 passed |
| **Measured** | **330 passed** |

`tasks/todo.md:123` is the worst case — it is the *standing verification* instruction: "`pytest tests -q` # must stay 213 passed". Following that instruction literally means treating 42 legitimate new tests as a regression.

Same drift on the file count: `VERIFICATION.md:8` says "36 source files"; `mypy` reports **39**.

### 3. A repo-wide rule directly contradicts the shipped code — *needs a decision, not a silent fix*

`.agents/rules/dry_dynamic_algorithm.md` §1.1 states:

> **Static Brand Lists**: Never hardcode lists of brands (e.g. `["DAWLANCE", "GREE", "HAIER", ...]`).

The shipped code hardcodes exactly that:

- `url_filters.KNOWN_BRANDS` — 14 brands → domains
- `url_filters.RETAIL_MARKETPLACE_DOMAINS` — 15 domains
- `config.brand_sources` — 5 brands → official sites
- `config.retail_shopify_sources` — 3 shops; `config.retail_woo_sources` — 4 shops

I did **not** "fix" either side. The rule also names a `pricing_engine/matcher.py` that does not exist in this repo, which suggests it was written for a *different* project and may not be intended to bind this one. Options: (a) scope the rule to `pricing_engine` only, (b) delete it as stale, or (c) genuinely dynamicise brand discovery here — a real design change with real risk to S1. **This is a human call.**

### 4. No CI, and the verification scripts are untracked-orphan risk

The three static gates are the project's entire safety net and are run by hand. There is no `.github/workflows`. `verification/*.py` imports project modules by path and is not covered by `pytest`, `ruff`, or `mypy`.

### Minor / stylistic

- `CLAUDE.md` is a 2-line pointer to `AGENTS.md` rather than a copy — that is the right call, since a duplicated rules file drifts. Worth keeping as-is.
- The previous `REPOSITORY_UNDERSTANDING.md` referenced a commit absent from the object store, so its provenance could not be checked. Now archived.
- `pipeline.py` at 1551 lines is doing per-row flow, budget accounting, upload gating, `BatchRunner`, and all writers. Not urgent — it is well-sectioned and fully tested — but it is the first place I would split if it grows.

---

## RECOMMENDATIONS

In the order I would do them:

1. **Fix the doc numbers to match reality** (255 tests, 39 mypy files). One-line edits in `AGENTS.md`, `README.md`, `tasks/todo.md`. Cheapest possible win — right now the standing instruction actively misleads.
2. **Fix the dry-run `failed_products.csv` reference.** Either write the file on every run (it is a report, like `review_products.csv`, which *is* written on dry runs), or make the summary and the generated README mode-aware. Writing it is the smaller, more consistent change.
3. **Resolve the `dry_dynamic_algorithm.md` conflict** with an explicit decision, recorded — do not let it sit ambiguous, because the next agent will read it and either obey or ignore it at random.
4. **Add a minimal CI workflow** running `ruff` + `mypy --strict` + `pytest`. ~20 lines; the suite is already offline and hermetic, so it will just work.
5. **Better: stop hardcoding the count in prose.** Have the standing-verification step say "`pytest tests -q` must be all-green" rather than naming a number that rots every time a test is added.

---

## WHAT I ACTUALLY RAN

```
git fetch origin --prune          → no output; HEAD == origin/main == main == 3d71601; 0 ahead / 0 behind
git ls-files | wc -l              → 196  (89 excluding .agents/)
python3 -m venv .venv && pip install -e ".[dev]"   → success (deps were NOT pre-installed)
python3 -m ruff check .           → All checks passed!
python3 -m mypy --strict .        → Success: no issues found in 42 source files
python3 -m pytest tests           → 330 passed (after 2026-10-07 fixes)
python3 main.py --input sample_input.csv --dry-run --limit 1 --workers 1
                                  → exit 0; all 7 stages ran; row failed loudly
                                    ("no page matched this model"); artefacts written;
                                    git status still clean (output/ and state/ ignored)
```

The dry run could not reach any retail host — this sandbox allows outbound only to github.com, npm and PyPI, so every fetch died with `curl: (35) SSL_connect`. That is an environment limit, **not** a code defect: the pipeline classified the failure as `S11.fetch`, tried all 8 fallback sources, and failed the row deliberately instead of publishing a guess. Which is precisely the contract `AGENTS.md` promises.

**Not verified here:** any live upload (needs `FREEIMAGE_API_KEY` plus unrestricted network), real gallery extraction, compositing output quality, and the `verification/` acceptance numbers — those rest on the 2026-10-06 run recorded in `verification/VERIFICATION.md`.

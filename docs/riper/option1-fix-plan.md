# RIPER — Option 1 fix pass (scrape → decisions → isolation → compose → upload → CSV)

Owner: hassanwaheedali · Method: `.agents/skills/riper-workflow` · Date: 2026-10-09
Scope: **Option 1 only.** Option 2 (direct image URLs, no scraper) is planned in G10 as a
modularity constraint, not implemented here.

## Governing rule of this pass

A green test suite is a **regression net, never a verdict**. Proof of a fix is:

1. `decisions.jsonl` + `run_report.csv` explaining every candidate that was considered, and
2. the actual rendered picture, viewed before/after, and
3. a measurement (height kept, silhouette margin, duplicate distance, seconds per row).

Every claim is tagged **verified** (real output), **constructed** (synthetic fixture) or
**untested**. A fix is not "done" until the owner has seen the before/after image.

Never weaken a safeguard to hide a symptom. Fixes are additive and rollback-flagged.

## 1 · RESEARCH (read-only) — what is already established

| # | Finding | Evidence |
|---|---|---|
| R1 | `isolate_main_product()` unions only *kept* components: siblings smaller than `isolation_min_component_ratio` (0.02) of the largest, or farther than `isolation_neighbor_distance_px` (35), are dropped → the crop keeps a fragment and the rest of the product is thrown away | measured; the vertical fridge sliver in `verification/sheet_alfamall.jpg` (row 2, tile 3) |
| R2 | White-on-white path: `white_fallback_bbox` sees only pixels ≥10 levels from 255; a white shell is invisible, dark vents/display sit at the bottom → bbox collapses to a bottom strip; `white_fallback_min_area_ratio` (0.20) measures *area*, so a full-width × 30%-high strip (0.30) passes the guard instead of being rescued | measured on a constructed AC frame: 10% of product height kept |
| R3 | `_pad_box()` clamps padding at the image border → product touches the frame and its silhouette is edge-cut | seen in real output |
| R4 | `to_square_canvas()` centres with `canvas.paste(resized, ((side-w)//2, (side-h)//2))`; for a wide AC the top margin equals the bottom margin, so a clipped-top product is centred *with the cut* and nothing signals it | read + measured |
| R5 | Colour gate S16 runs on the **raw downloaded frame** (`_consume_candidates`), while `extract_foreground_color_profile` samples only the central 76% box — a 16:9 wall-AC inside a square frame leaves that window mostly white background | read; verdict pending owner's raw AC photo |
| R6 | S16's chromatic conflict table lists `black`/`white`/`grey` as targets but **never as offenders**; `dominant_family` only ever returns `purple/red/gold/blue/green/chromatic/neutral` → a black unit on a white row is arithmetically unable to be rejected | read (code quoted in G2) |
| R7 | Promo art and TV wallpapers survive the quality gates and reach the shop sheet | seen in real output |
| R8 | Same angle accepted twice; `_product_patch` correlation (0.985/0.995) is the only real defence and it runs **after** the download | seen in real output + read |
| R9 | `ScoreMatch.confidence` is computed and consumed by nothing in the codebase | measured (grep) |
| R10 | Live fetching from this sandbox is blocked (`000` connect failure to every host except GitHub/PyPI), so the pipeline's own network layer cannot be exercised here | measured |

**Still unknown (needs the owner's committed `output/` folder):** which branch fired for the
real AC row (R1 component-drop vs R2 white-fallback), and whether S16 was reached at all.

## 2 · INNOVATE — options weighed

| Defect | Options | Chosen, and why |
|---|---|---|
| Crop cuts the product | (a) lower thresholds (b) keep the full foreground bbox and reassemble components (c) flood-fill the background from the borders and take the complement | **(c) + (b) as guard.** "What is not background" is the product; thresholds stop being the whole decision, so one faint edge cannot delete a body. |
| AC colour wrong | (a) sample a bigger window (b) judge colour on the isolated product, not the frame (c) per-category rule tables | **(b).** Reuses the fixed isolator; no tables (DRY rule). |
| Duplicate angles | (a) tighter dHash (b) asset/URL identity **before** download (c) both + a post-compose check | **(c).** (b) is free and it is also the speed fix. |
| Promo / wallpaper accepted | (a) stricter S7 (b) reject text-heavy/saturated non-product art before compose, with an official-gallery exemption | **(b)** — targeted, keeps the calmer studio bar the owner asked for on brand pages. |
| Slow | (a) fewer candidates (b) stop at the per-angle target (c) dedup-before-download + probe reuse + per-asset cache | **(c) then (b)**, measured per row; no gate loosened. |

## 3 · PLAN — stage gates, in order

| Gate | Stage | Files | Measurable pass condition |
|---|---|---|---|
| **G0** | Ingest + attribute every bad row | — | every reported defect traced to a named function and logged with its `rule_id`; baseline counts recorded |
| **G1** | Background removal / crop | `compositing.isolate_main_product`, `_pad_box`, `to_square_canvas` | owner's AC row re-rendered: 100% of the product inside the frame, top intact; the sliver case produces a full-frame image; nothing cropped at the border |
| **G2** | Colour rule (S16) | `image_quality.verify_image_color` + its call site | black-titled AC rejects the white photo, white-titled AC keeps it, Homage fridge unchanged |
| **G3** | Duplicates (S10) | `dedup.py`, `dedup_key` at `_decide_candidate` | zero repeated angles across all rows; every rejected repeat recorded with its distance |
| **G4** | Promo / wallpaper gates | `run_quality_gates`, `image_quality` | the AFL tile and the wallpaper tiles are rejected; a real product photo of the same model still passes |
| **G5** | **Decisions + logs are the truth** | `decisions.py`, call sites in `pipeline.py`, `main.py` | see the log contract below — this gate blocks G6-G9 |
| **G6** | Speed | `_consume_candidates`, `_DownloadBudget`, probe | measured seconds-per-product and downloads-per-product before/after on the same sheet |
| **G7** | Clarity / upscale stage | `to_square_canvas`, `sharpen`, blur floor | sharpness score up, no halo/banding artefacts, `max_upscale` respected |
| **G8** | Template, banner last, logo band | `apply_template`, `finalize_and_upload` | logo band byte-identical, content inside the safe margin, banner appended last |
| **G9** | Hosting + CSV | `image_host.py`, `write_output_csvs` | both-or-neither upload; `Title,Brand,Image_Urls` pipe format exact; failed rows in `failed.csv` with a reason; `run_report.csv` agrees with `decisions.jsonl` |
| **G10** | Option 2 seam audit | — | download / isolate / colour / compose / CSV callable with an image URL as the only input, no scraper import |

### The log contract (G5) — what "full checking" means

For every row the trail must be complete and self-explanatory, otherwise the run is not auditable:

1. **Every candidate considered leaves exactly one line** — accept *or* reject with a `rule_id` from the registry. A candidate that disappears without a line is a bug in itself.
2. **No `rule_id` outside the registry** (`S1…S17` + `accept`, `S13.fetch`, `S7.band`, `S7.seam`, `S7.official`) — an unknown id means a gate that nobody documented.
3. **Counts reconcile**: `accept` lines == files under `final_digimall/` == files under `final_alfamall/` == image URLs in the row's CSV line. Any mismatch is a silent loss.
4. **Every `failed` row names the reason** in `run_report.csv` and has at least one reject line explaining it; `_failure_reason` must never paper over an empty page list.
5. **Repairs are recorded, not silent**: promo-band removal, seam trimming, white-fallback and degraded isolation each log what they changed and by how much.
6. **Logs exist in `--dry-run`** and after a `KeyboardInterrupt` (append-safe, per-worker, no interleaved JSON).
7. **Concurrency safety**: two workers never write a torn line into one file.
8. Every audit line carries the URL that was actually used (`downloaded=`), not only the URL found in the page.

Each gate ends with: patch → `pytest -q` (regression only) → re-run the stage on the owner's real
images → before/after sheet → measurement → evidence tag → owner approval before the next gate.

## 4 · REVIEW — deliverables

* Results table per angle and per category (AC, refrigerator, washing machine, microwave, TV, other).
* Defect → fix → passing evidence, one row per defect, each tagged verified / constructed / untested.
* `CHANGELOG.md` + `docs/PROJECT_MAP.md` rule-registry updates, ruff + `mypy --strict` clean, complexity not increased.
* An explicit list of what could not be verified from this sandbox (live fetch, owner-side visual pass).
* Note for Option 2: which components are reusable unchanged and what small seam each needs.

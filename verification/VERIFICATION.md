# Live run verification — 10 products, real uploads to freeimage.host

**Date:** 2026-10-06 (final ship run, after the dead-code purge and the Dawlance download fix)
**Command:** `cd image_pipeline && python3 main.py --input sample_input.csv --workers 4`
**Result:** ok **5** · partial **5** · failed **0** · **19 + 19 pictures** uploaded (DigiMall / AlfaMall) + 2 banners

Tool gate on the shipped tree: `ruff check .` → *All checks passed!* · `mypy --strict .` → *Success:
no issues found in 36 source files* · `pytest tests -q` → **213 passed** (fakes only, no network).

---

## What you import

| File | Rows | Format |
|---|---|---|
| `image_pipeline/output/digimall_product_images.csv` | 10 | `Title,Brand,Image_Urls` — Brand on every row |
| `image_pipeline/output/alfamall_product_images.csv` | 10 | `Title,Brand,Image_Urls` |

`Image_Urls` = `https://iili.io/....jpg`, pipe-separated, **no spaces**, product photos first, that
platform's closing banner **last**.

---

## Acceptance checks (all passed)

| # | Check | Result |
|---|---|---|
| 1 | Header exactly `Title,Brand,Image_Urls` | PASS (both files) |
| 2 | No spaces in the URL field | PASS (20/20 rows) |
| 3 | No `file://` or local path anywhere | PASS (20/20 rows) |
| 4 | Banner is the last URL, exactly once per row | PASS (`n1Oo7pe` DigiMall / `n1OoaTu` AlfaMall) |
| 5 | No duplicate image inside a row | PASS (20/20 rows) |
| 6 | Every URL answers HTTP 200 with an image content type | PASS (**40/40 unique URLs**, all `image/jpeg`) |
| 7 | Every uploaded picture is the product's own picture | PASS (**38/38 identified back to their own local final**; worst self-distance 0.06 vs 0.97 for the nearest other picture) |
| 8 | No two products share a final picture | PASS (19 distinct DigiMall, 19 distinct AlfaMall) |
| 9 | API key appears in no CSV, log, decision record or checkpoint | PASS (0 files) |
| 10 | Template logo band untouched | PASS (max pixel delta ≤ 122 = JPEG noise; products start ≥ 22 px below the band) |
| 11 | Product centred and large, per the user's sizing bar | PASS (longest edge 94.3–95.0% of the content box, x-centre within 2.5 px) |

---

## Per-row result

| Title | Brand | Status | Pictures | Banner last |
|---|---|---|---|---|
| Dawlance Semi Automatic Washing Machine 10kg (DW-7500C) | Dawlance | ok | **4** | yes |
| Haier 50 Inch QLED Google LED TV (H50S80EUX) | Haier | ok | 2 | yes |
| Haier 55 Inch 4K Smart QLED TV (55S80EUX) | Haier | partial | 1 | yes |
| Haier 55 Inch Smart Google LED TV (H55K85FX) | Haier | partial | 1 | yes |
| PEL Top Load Fully Automatic Washing Machine (PAWM-1100) | PEL | ok | 2 | yes |
| PEL Kitchen Pro Grill Microwave Oven (PMO–23 Kitchen Pro GR) | PEL | ok | 4 | yes |
| TCL 85 Inch SQD Mini LED TV (85C8L) | TCL | partial | 1 | yes |
| TCL 98 Inch SQD Mini LED TV (98C8L) | TCL | partial | 1 | yes |
| PEL Top Load Washing Machine White 12.5 Kg (PWM-1250) | PEL | ok | 2 | yes |
| Homage Freezer-on-Top Refrigerator 18 Cu Ft Golden (HRF-47662-GD) | Homage | partial | 1 | yes |

**partial** = published, but fewer than the 3-picture target passed the gates (flagged in
`run_report.csv`). **failed** = nothing published (this run: none). Every row that produced a picture
was matched to that exact model first — the pipeline still refuses to publish a picture it cannot prove
belongs to the row's model, and no `H50S80EUX` picture was taken from the `H50S80GUX` page that the
sheet's own URL serves.

---

## The three download bugs found and fixed in this pass

1. **Official gallery shots were rejected as "busy background" (the "only 1 image" complaint).**
   Dawlance serves four real studio photos of `DW-7500C`; all four failed the strict S7 border ring
   (within-tolerance 0.964 / 0.983 / 0.725 / 0.130 against a 0.92 bar) because the product fills the
   frame in a top view / open-lid shot, so there is no clean ring left to measure. Fix: a **scoped**
   rescue `S7.official` — only for a curated gallery source *and* a brand-official host *and* at least
   two neutral corners. Retail pages keep the strict rule. Result: 4 of 4 official photos accepted.
2. **The same photo in two formats counted twice.** `/…/image.png` and `/…/image.webp` produced two
   de-duplication keys, so the picture was downloaded twice and the page allowance ran out before the
   fourth real photo. Fix: `dedup_key` now ignores the trailing extension.
3. **The download allowance was charged per HTTP attempt instead of per picture.** The "try the bigger
   size first, then the published size" rewrites burned a slot each, starving the last real photo.
   Fix: one slot per asset, before the retry loop. (`decisions.jsonl` shows 4 accepted / 1 rejected.)

---

## Production-readiness pass (what was reviewed and changed)

| Area | Finding | Action |
|---|---|---|
| Dead code | 20 unused functions/constants (superseded sizing helper, dead parsers, debug helpers) | removed; `ast`-validated, suite re-run |
| Config | `input_path` write-only; `prefer_colour_images` never implemented; `wide/tall_aspect_threshold` only validated each other after the sizing rewrite | removed (with their validation and call sites) |
| Fallback chain | `robots.txt` was checked, but the documented `respect_robots` switch did nothing; `mark_host_dead`/`_dead_hosts` were never written to; `parse_shopify_products` unreachable | switch wired, dead bookkeeping and parser removed, new test `test_respect_robots_switch_disables_the_check` |
| Output folder | "messy / not self-explanatory" | every run now writes `output/README.md` (what each file is + this run's counts); pictures live in `images/<product-slug>/{raw,isolated,final_digimall,final_alfamall}/` |
| Failure handling | a full output disk crashed with a traceback | `main` now reports `cannot write run outputs under …` and exits 3 (checkpoint intact, rerun resumes) |
| Onboarding context | no rules file, knowledge only in README | `AGENTS.md` (rules, commands, boundaries, patterns) + `docs/PROJECT_MAP.md` (module + rule map) + `CLAUDE.md` pointer |
| Files | 30 MB tree with old template art, a placeholder generator, duplicate dependency/secret templates, caches, dev output | pruned to **692 KB**: `tools/`, `assets/previous_placeholder/`, `.env`, `.env.example`, `requirements.txt`, `output/`, `state/`, caches removed; `pyproject.toml` is the only dependency list |

---

## Verification method (reproducible)

`/home/user/verification/build_sheets.py` rebuilds the visual sheets and the sizing table from the last
run in `output/`:

* `sheet_digimall.jpg` / `sheet_alfamall.jpg` — contact sheets of all 38 finals.
* `sizing.txt` — per picture: fill height/width, gap above/below the product, horizontal centre offset.
* Sizing measured against the bare template (diff, threshold 60) inside the content box
  (DigiMall `43,142,1037,1048`; AlfaMall `43,139,1037,1048`): longest edge 94.3–95.0%,
  top gap ≥ 22 px, bottom gap ≥ 23 px, centre offset ≤ 2.5 px, logo band delta ≤ 122 (JPEG noise).
* Upload identity: each uploaded URL is fetched and identified against all local finals with a 32×32
  blurred grayscale fingerprint — 38/38 landed on their own file.

## Re-run / resume

`state/checkpoint.jsonl` holds all 10 rows, so the next run skips them:

```bash
python3 main.py --input sample_input.csv                 # nothing to do: all rows published
python3 main.py --input sample_input.csv --redo "TCL 85 Inch SQD Mini LED TV (85C8L)"
python3 main.py --input sample_input.csv --no-resume     # start over, reuse the banner upload
```

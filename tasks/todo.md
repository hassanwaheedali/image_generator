# Task List — Verified Product-Image Pipeline

Checklist view of `tasks/plan.md`. Legend: `[x]` done (evidence in plan.md) · `[ ]` remaining.

## Phase 0 — Foundation
- [x] Task 1: Rule surface and paths (`config.py`)
- [x] Task 2: Domain models + network protocols (`models.py`, `protocols.py`)
- [x] Task 3: Sheet reader, atomic writers, checkpoint, decisions log (`csv_io.py`, `decisions.py`)
- [x] **Checkpoint 0:** writer tests green, config validation fails fast

## Phase 1 — Recognition slice
- [x] Task 4: Model keys from the sheet title (`model_matching.py`)
- [x] Task 5: The two recognition rules + colour/variant flags (`match_product`, `verify_page`)
- [x] Task 6: S2 description guard + S3 other-page-link guard (`page_verification.py`)
- [x] **Checkpoint 1:** 47 tests green; stale Haier URL correctly rejected live

## Phase 2 — Gallery slice
- [x] Task 7: Extractor registry + brand / JSON-LD / embedded-JSON / gallery sources (`gallery_extractors/*`)
- [x] Task 8: Image gates S5/S6/S7/S8/S9/S13 (`image_quality.py`, `url_filters.py`)
- [x] Task 9: Per-product dedup S10 (`dedup.py`)
- [x] **Checkpoint 2:** zero-tolerance guarantees proven by tests

## Phase 3 — Compose slice
- [x] Task 10: Isolation + 1:1 centre canvas + light unsharp (`compositing.py`)
- [x] Task 11: Template compositing with header-safe margin; banner last (`apply_template`, `finalize_and_upload`)
- [x] Task 11b: Logo-band pixel regression test — **done**, and mutation-verified:
      `test_logo_band_is_byte_identical_after_compositing` (semi-transparent band, so a product pasted
      underneath would bleed through), `test_product_is_never_pasted_above_the_safe_margin`,
      `test_template_decoration_inside_the_content_area_survives`, `test_real_asset_templates_protect_their_band`.
      Mutation A (paste from the top) and mutation B (product composited over the template) each fail
      the suite, so the guarantee is genuinely enforced.
- [x] Task 12: Local artefact layout (`raw/`, `isolated/`, `final_digimall/`, `final_alfamall/`)
- [x] **Checkpoint 3:** complete — logo-band guarantee holder is now tested (4 tests, mutation-verified)

## Phase 4 — Hosting and outputs
- [x] Task 13: freeimage.host client, banner cache, permanent-error abort (`image_host.py`, `pipeline.py`)
- [x] Task 14: `Title,Brand,Image_Urls` sheets (pipes, no spaces) + failed CSV + run report
- [x] **Checkpoint 4:** exact CSV format asserted by test; nothing uploaded on failure

## Phase 5 — Batch runtime
- [x] Task 15: BatchRunner — workers, input order, resume, `--dry-run`, `--redo`, `--limit`, abort
- [x] **Checkpoint 5:** 29 end-to-end tests green; resume/skip/retry proven

## Phase 6 — Fallback chain
- [x] Task 16: Source registry, platform map, brand-before-retail ordering
- [x] Task 17: Shopify + WooCommerce JSON search parsers
- [x] Task 18: Sitemap index crawl + `state/` cache + robots check
- [x] Task 19: S15 candidate verification, trigger rule, source labelling
- [x] **Checkpoint 6:** live **ok 3 / partial 5 / failed 2**, 12 images kept (5 via fallback)

## Phase 7 — Efficiency
- [x] Task 20: Per-host sessions + retry classification (403 = no retry)
- [x] Task 21: 64 KB header probe before any full download
- [x] Task 22: Largest-size rewrite + download budget + target 3 images
- [x] Task 22b: Per-page **and** per-row download budget (live find: Dawlance's dark renders starved
      the retail candidates) → Dawlance row **failed → ok**, 4/5/1 on the real sheet
- [x] **Checkpoint 7:** probe/rewrite/budget proven by tests; live re-run 4/5/1

## Phase 8 — Documentation
- [x] Task 23: README + plan.md §11 (S1–S15 table, maintenance map, tuning loop)
- [x] **Checkpoint 8:** docs match the shipped code

## Phase 9 — Acceptance on real data
- [ ] Task 24: Real upload run with `FREEIMAGE_API_KEY` → import sheets with live freeimage URLs
- [ ] Task 25: Review the fallback-sourced images; tune with `--dry-run` if needed
- [ ] **Checkpoint 9:** sheets open in Excel with correct columns; fallback rows human-approved

## Phase 9b — Secrets
- [x] Task 28: secrets files (`config.load_env_file`) — **`secrets.env`** (visible, ready to paste into)
      + `.env` / `env.txt` alternatives, `secrets.example.env` template, `.gitignore` for all three
      names, key never logged; an unfilled template never shadows a filled file
- [x] **Checkpoint 9b:** dry-run prints `freeimage key: not needed (dry-run)`; filled `.env` prints `set`; empty ⇒ fail fast

## Phase 9c — first live run fixes
- [x] Task 29: opaque alpha channel hid the product behind the template (fixed + mutation-verified)
- [x] Task 30: dry-run banner URL poisoned the live run (mode-scoped cache + non-http guard)
- [x] **Checkpoint 9c:** 11/11 acceptance checks pass (see `/home/user/verification/VERIFICATION.md`);
      live `ok 4 / partial 5 / failed 1`; 193 tests; mypy clean
      (superseded by Checkpoint 9d: live `ok 5 / partial 5 / failed 0`)

## Phase 9d — the PWM-1250 false negative
- [x] Task 31: `S7.band` promo-strip cut (config-bounded, logged, mutation-verified)
- [x] Task 32: `S7.seam` layout-seam trim (config-bounded, logged, mutation-verified)
- [x] **Checkpoint 9d:** `--redo` of PWM-1250 → **ok**; totals **ok 5 / partial 5 / failed 0**;
      10 rows per CSV; 34/34 URLs HTTP 200; 198 tests; mypy clean

## Phase 9e — user's templates + sizing + Brand column
- [x] Task 33: supplied DigiMall/AlfaMall templates installed; product sized from its own content box,
      perfectly centred, padding never pasted over the logo (mutation-verified 3/3)
- [x] Task 34: `CheckpointEntry.brand` - the Brand column survives resume (mutation-verified)
- [x] Task 35: `ruff` wired in via `pyproject.toml`, 81 findings fixed, mypy override for stub-less libs
- [x] **Checkpoint 9e:** live run with the new templates - **10 rows, brand filled on all 10, 32/32 URLs
      HTTP 200, ok 4 / partial 6 / failed 0**; product 76-90% tall, top edge 23px below the logo band;
      `ruff` clean, `mypy --strict` clean, **206 tests**

## Phase 11 — Ship pass (user order 2026-10-06: fix → clean → re-test → review → skill)
- [x] **T36 — Dawlance download bug fixed** (strict S7 rejected the brand's own gallery shots):
      scoped `S7.official` rescue (curated gallery + official host + 2 neutral corners); the same photo
      served as `.png`/`.webp` no longer double-counts; the download allowance is charged per picture,
      not per HTTP attempt. Live: **4 of 4** official photos accepted, row `ok`.
- [x] **T37 — Dead code and files removed**: 20 unused defs, 4 dead config fields (+ their validation),
      5 unused imports, `tools/`, `assets/previous_placeholder/`, `.env`, `.env.example`,
      `requirements.txt`, all caches, old `output/` + `state/` → workspace 30 MB → **692 KB**.
- [x] **T38 — Re-tested after the purge**: `ruff` clean · `mypy --strict` clean (36 files) ·
      **213 tests** green.
- [x] **T39 — Live end-to-end run on the cleaned tree**: `python3 main.py --input sample_input.csv
      --workers 4` → **ok 5 / partial 5 / failed 0**, 19 + 19 pictures, **40/40 URLs HTTP 200**,
      Brand filled on 10/10, banner last, Dawlance 4 pictures. See `verification/VERIFICATION.md`.
- [x] **T40 — Production review + fixes**: `respect_robots` switch wired, dead host bookkeeping and the
      unreachable Shopify parser removed, `output/README.md` written on every run, disk-failure exit
      code 3, upload-identity verification switched to nearest-neighbour fingerprinting.
- [x] **T41 — Context-engineering skill applied**: `AGENTS.md` (rules file), `docs/PROJECT_MAP.md`
      (hierarchical map + rule registry), `CLAUDE.md` pointer, README updated to link them.

## Phase 10 — Optional (not needed for this shipment)
- [ ] Task 26: Brand extractor work for the 2 stubborn rows (Dawlance DWF, Homage/Daraz)
- [ ] Task 27: Source-list extension (more PK shops)

## Standing verification (run after every change)
```
python3 -m ruff check .            # must print: All checks passed!
python3 -m mypy --strict .         # must print: Success: no issues found
python3 -m pytest tests -q         # must stay 213 passed (fakes only, no network)
python3 main.py --input sample_input.csv --dry-run --workers 4
```

## Blockers
- (cleared) `FREEIMAGE_API_KEY` — received and stored in `/home/user/secrets.env`; Task 24 done.
- Open Question 2: replace the placeholder template art in `assets/` with the final DigiMall /
  AlfaMall artwork, then re-run (nothing else changes).
- Open Question 3: publish `PEL PWM-1250`? It is now `ok` with 2 photos from Surmawala and Modern
  Electronics. The sheet URL still points at an alfamall.com listing (capacity conflict flagged),
  so the images came from the fallback chain - worth a human glance before importing.
- Open Question 2 (accept Dawlance's black-background renders, or keep rejecting?) — affects Task 25.

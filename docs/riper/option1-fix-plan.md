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

## 5 · EXECUTION LOG (measured, per gate)

**2026-10-10 · S18 Crop Closure — product clipping in isolation. SHIPPED, needs one live re-run to confirm on the shop template.**

Evidence trail, in order:
1. Owner complaint: "the AC got cut. The product must not be touched, only the background."
2. First measurement (bbox of raw vs bbox of isolated) was **discarded**: aspect "shifts"
   it reported were soft-shadow removal, not clipping. Wrong metric, wrong conclusion.
3. Second measurement, the one that means something: run the shipped `isolate_main_product`
   on all 72 accepted raws and ask whether the crop's own border cuts ink (scan 6 levels
   below pure white; the scan is calibrated - the ink box is flat from 2 to 24 levels on
   these images, so JPEG noise cannot fake it). Result: **41 of 72 crops cut ink.**
4. Cause: the crop box comes from `_single_body_box(silhouette)`, and the silhouette is
   built with `isolation_bg_distance = 32`. A white shell fades 250 -> 243 -> 235, so its
   lower body is invisible to that mask and the box ends at the waist. Lowering 32 was
   rejected - it feeds the fraction test, the composite guard and dedup too.
5. Fix: grow-only closure. Label the loose ink, pull in only the bodies touching the crop
   border, never trim, `isolation_crop_closure = False` restores the old behaviour exactly.
6. Verified on the owner's own images: +270 % / +203 % / +347 % / +55 % / +46 % height on
   the five worst crops, the other 67 unchanged, `pytest -q` green, `ruff check` clean,
   `mypy --strict` clean, sheet `docs/riper/evidence/crop-closure-before-after.png`.
7. Open, deliberately not hidden: **13 products where the ink already touches the source's
   own frame edge.** For those the site's file is the cut one; no crop can repair it. That
   is a *candidate choice* problem (prefer an uncut source), not an isolation problem.

**2026-10-10 · the Frost row — a scraping gap, after two wrong conclusions. SHIPPED, needs a live re-run.**

1. Claim A (mine, from the old log): "the crash hides the reason". Half true - fixing it
   only produced a *reason*, not an image.
2. Claim B (mine, from the live capture): "Orient publishes no gallery on this page, the
   7 S3 rejects are all correct, there is nothing to scrape". **Wrong, and the owner
   caught it by pointing at the page.** The capture does contain the product image - the
   page's own JSON-LD `Product` node declares
   `assets/og-images/frost-18w-classic-white.jpg?v=20260618c`.
3. Mechanism, reproduced with the shipped code: `json_ld.extract()` returned 0
   candidates because the identity gate needs a model-key match and this title has
   `tokens=() identities=()`. So *the extractor never looked at the one image the page
   admits to having*. `<img>`-only scanning + a model-key gate = a blind spot for every
   model-code-free row (6 of 34 rows here; the run's only `failed` row is one of them).
4. Fix, kept narrow on purpose: the node is accepted when it *names this page*
   (`url`/`@id`/`mainEntityOfPage` == page slug, or `sku` == page slug) - and only for
   rows that own no model code, so no row that can be matched by model is affected.
   Proved non-leaky: a page whose JSON-LD also lists `megatron-titanium-12x` yields only
   the Frost image.
5. Measurement: same capture, all five sources - 7 candidates -> 8, first = `frost-18w-classic-white.jpg`.
6. Lesson recorded so it is not repeated: "the page has no images" is a claim about *the
   extractor's* reach, not about the page. Check `og:image` / `ld+json` before saying it.

**2026-10-10 · doubt cycle on the Haier sibling-extension rung. Result: my claim was only half true; shipped contained.**

Stop condition met after 1 cycle (no reviewer CLI in this sandbox - `gemini`/`codex`
absent - so Step 3 ran as the skill's degraded self-questioning fallback, and it is
labelled degraded rather than counted as an independent review).

- CLAIM examined: "adding a `.png` rung is harmless because a rewrite is only *tried*".
- Finding 1 (real, mine): "only tried" is **not** "only considered". `pipeline.py` probes
  `upscale_candidates(url, official)[0]` and downloads the tuple in order, so the first
  rung that answers **replaces** the page's own URL. A guess that resolves to any
  large-enough file becomes the published picture, and no code compares it to the
  thumbnail the page named. The blast radius is exactly the failure the repo exists to
  prevent: right product page, wrong image on the shop.
- Finding 2 (why the old rung survives): the stripped-`.jpg` rung is *proven* by the run
  itself - the page listed `…91117.jpg` too, and dedup logged `reject:S10 near-duplicate
  of an already accepted angle`. Same photo, confirmed by evidence, not by faith.
- Finding 3 (pre-existing, not caused by me, left open on purpose): that probe has no
  fallback - if a *guessed* URL measures small while the page's own URL was fine, the
  candidate dies at S8 without the page's URL ever being probed. Needs its own gate.
- Containment shipped: `url_rewrite_cross_extension_enabled = False` by default; the
  ladder only grows when the owner turns it on after a live probe, or after Finding 3 is
  fixed and the substitution is identity-checked.
- What I got wrong in the earlier report to the owner: I wrote "a wrong guess costs one
  request and no decision". The cost is potentially a wrong published image. Corrected
  here so the paper trail does not carry a false reassurance.

**2026-10-10 · doubt cycle #2: every fix shipped in this pass, adversarially re-examined.**
Degraded self-review again (no cross-model CLI in this sandbox), and every finding was
settled by measurement on the owner's real images/log rather than by argument.

| fix | what the review attacked | measured result | action |
|---|---|---|---|
| F1 tolerated-spelling crash | does the note hide a real failure? | the run's Frost row now logs a reason instead of `IndexError`; nothing else reads that string | confirmed safe |
| F2 white-on-white before silhouette | does the order let a bad crop through? | yes - and that gap is what cut products; closed by S18 | fixed via S18 |
| F7 shape-IoU dedup (0.90 + hamming<=6) | can it drop a legitimate second angle? | on 56 pairs of this run only the 2 owner-confirmed repeats collide; the "both close angles kept" fixture stays green | kept, watch-list: two full-bleed *posters* of one series normalise to similar shapes - revisit when S19 rejects layouts |
| F8 one fingerprint per candidate | does the gate compare one image and store another? | `compute_fingerprint(image)`, `duplicate_reason(image, ...)` and `add(image, ..., fingerprint=...)` all take the same object; the re-check inside `add` is now dead work (harmless) | confirmed safe |
| S18 crop closure | ink measured from pure white on dark backgrounds | 413x213 -> 900x600 on a synthetic dark frame = would ship a coloured background | fixed: background-relative ink, near-white only |
| Haier sibling-extension rung | can a guessed URL replace the page's own file? | yes - probe uses `[0]`, download takes the first answer | contained: `url_rewrite_cross_extension_enabled = False` |
| JSON-LD page identity | can a related tile claim this page and steal its image? | the first guard was circular (word check read the node's `url`); 3 of 4 lies passed | fixed: name/brand only; `url`/`sku` excluded from vouching |
| open (pre-existing, not mine) | probe measures only the *guessed* URL | a small guess kills the candidate; the page's own URL is never probed | needs its own gate; proposed next |

**2026-10-10 · the advertisement kept getting accepted - two root causes, both now closed.**

Owner evidence, in their words: the raw picture is clean and perfect, the isolated one is
cut in half (that was S18, above); and separately `…/og-images/pulse-19c-ultra-white-t3.jpg`
"is an ad picture, brand logo and price written, we cannot use this", while the real photo
is `…/assets/products-images-2026/air-conditioners/15-ton-pulse-ultra-white-dc-inverter.png`.

1. Why the ad got in: the JSON-LD node of an Orient page declares `image` = its
   `og-images/<slug>.jpg` social card. That card is a *marketing render* - logo + price.
   Before this session the card still reached the same place via the page-wide scan, so
   the ad was already being accepted in the owner's earlier run; my `json_ld` exception
   widened it (8 cards accepted in the 02:28 run). Fixed at the safeguard layer, not by
   narrowing the extraction: S5 blocks share-card paths for every source.
2. Why the right photo was missing: Orient's hero `<picture>` lives in
   `.pdp-hero__image`, and the gallery-container selector list knows only
   `.product-gallery`, `.woocommerce-product-gallery`, `.product-images`, `.product__photos`,
   `.gallery-thumbs`, `.thumbnails`, `.product-thumbnails`, `.slick-slider`, `.flexslider`,
   `.product-media-gallery`, `.gallery-placeholder`, `.fotorama`. Sources 1-4 produced the
   cross-sell tiles, so the "only when nothing else was found" whole-page fallback never
   ran. Net effect: the page's one true image was structurally invisible.
3. Both fixed with data (selector list + keyword corpus), no new code branches. Measured on
   the live capture: candidates 8 -> 9, the 9th being the file the owner found by hand; the
   hero passes S3/S4/S5; category page candidate count unchanged (no extra download cost).
4. Structural lesson for this repo: "no images on this page" and "this image is fine" are
   both claims about *which container the extractor looked at*, not about the page. When a
   row fails, list what the DOM actually holds before concluding anything - the same mistake
   produced the wrong "Orient publishes no Frost gallery" statement earlier today.

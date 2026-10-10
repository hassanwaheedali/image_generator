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

## 6. Round 3 - the owner's three Orient finals and the Haier emblem (2026-10-10, evening)

Three more holes were reported after §5 shipped. All three were measured on the owner's own
run output (`output/images/*/raw/*.jpg`, 33 rows, 72 published files) and on the markup they
pasted, not on reasoning.

1. **Haier: "the page gallery has 6 crystal clear images, why download a blurry one".** Root
   cause is one missing attribute: Haier writes
   `<img src="…_60.png" data-imgurl="…_1200.png">`. `_ATTR_PRIORITY` did not include
   `data-imgurl`, so the extractor collected the 60px thumb; the size-guess ladder then
   invented a URL, and the row published a soft unnamed file. Fixed in
   `gallery_extractors/__init__.py`, tested with the owner's pasted gallery markup.
2. **Haier: the AI/laurel emblem.** The gate written for this emblem (S14) only fires on a
   *non-white* ground; the 2026-10-10 files are `logo_white_bg.png`, so its very first test
   bailed them out. No URL rule can help: the emblems are `W020260404458942582487.png` - random
   asset ids with no `logo` token in the name. Fixed with S20, a content gate measured on the
   run: ink share, ink-box span and coarse texture. It rejects 14 of 72 published files, all
   of them complaints, and no real photo.
3. **"why here not go to brand official, clearly bug".** It is not a quota bug: `run_report.csv`
   line 14 for the Hyper 18X row reads `sources tried: www.orient.com.pk:sitemap:0, …`, so the
   official Orient host *was* queried first (Path 2 in `process_product`) and its sitemap yielded
   zero candidate pages for that model. The row then filled up from the sheet's retailer pages.
   What *was* broken is what those retailer pages were allowed to publish - the wrong-model file
   in item 4 below - and that is fixed here. Remaining gap (G5): an official source that returns
   0 pages is logged in `run_report.csv` but not per-image in `decisions.jsonl`, so a "we asked
   Orient and Orient had nothing" decision is invisible in the file the owner reads.
4. **"the 3rd is fully wrong … clearly different color and different product picture".** Root
   cause found in S4 itself: it compares alphabetic prefixes, and this row's key is `18X` - no
   letters, nothing to compare, `return False`. The capacity-code half added tonight rejects the
   `18G` file (1 of 72 published images across the whole run).
5. **Blur.** Measured before touching anything: the blurry `01-raw` of the Hyper 18X row has
   laplacian variance 330.9, while the good population of the same run runs min 189.9 / p10 298.3
   / median 617.0. **An absolute blur floor is therefore not safe** - any threshold that kills
   331 also kills hundreds of legitimate photos. The correct lever is a *preference*: when S10
   calls two candidates the same angle, the sharper one must win instead of the first one
   downloaded. That is the next change, and it needs the same-angle pair to be visible to the
   dedup at all (see G5 logging gap: the crop box and the dedup verdict are not both logged).

## 7. "Haier shows 6 images, the script downloads 1" - measured, and it is not an extractor bug

Their question, 2026-10-10. Live page `haier.com/pk/air-conditioners/hsu-13hfcp023wdc-w-.shtml`
(`www.haier.com` is not reachable from this sandbox - `SSL_ERROR_SYSCALL` - so the page was
replicated from the owner's own pasted `.sd_imgs` markup and run through the real code path).

* `extract_candidates` on that markup: **8 candidates, all the crisp `_1200.png` files**
  (6 product views, then the 2 emblem files), `source=registry:haier`.  Nothing is being
  skipped by the extractor any more and no `_60.png` thumb survives.
* `_page_download_allowance` for that page: **8** - a curated gallery is read in full, so the
  two-level download budget is not the limit either.
* The limit is the *customer target*: `_row_is_done` = `len(accepted) >= min(target_images_per_product,
  max_images_per_product)` and the candidate loop breaks on it (`pipeline.py:1351`).  With the
  default `--target-images 3`, the row publishes 3 and stops - in their run those 3 slots were
  `1 photo + 2 emblems`, which is exactly why the folder looked like "only one image".
* So: S20 alone turns that row into **3 real photos** (the emblems can no longer spend slots);
  `--target-images 6` reaches all six.  Dedup S10 will still fold near-identical angles - the
  page's views 1 and 4 are both the flat front - and that folding is what the owner asked for
  ("never publish the same picture twice"), so 6 gallery images do not mean 6 published files.
* Deliberately NOT changed: the default target stays 3, because every extra slot is also an extra
  slot a retailer's junk can fill, and downloads per row drive the run's wall time.

## 8. "download all images, not 3" + the manual sheets (owner, 2026-10-10, night)

Their log lines told the whole story in one place.  For `Inspire Pro T3 Inverter 1 Ton`:
`_1` was rejected by S20 (in-box sheet), while `_10`, `_11` and `_2` were **accepted** - two
manual pages and one contents sheet - and the row then stopped, because the loop broke at the
3-image target.  The gallery's own DOM order is the reason those three came first: Dawlance
lists its slides `1, 10, 11, 2, 3, 4, … 11`, so the good product photos (`_3`-`_9`) sat behind
the junk and were never downloaded.

1. **Cap removed for curated galleries** (`_page_publish_goal`): a page offering >= 4 curated
   gallery images is published up to `max_images_per_product` (10) instead of the row target.
   The target still governs whether *other pages* are visited, so the official gallery cannot be
   topped up with retailer images.  Flag: `gallery_publishes_all` (True).
2. **S21 added** so "publish all" does not mean "publish all the junk": caption sheets are
   identified by arrangement (many comparable parts, no dominant body), which is the one thing a
   white-appliance photograph does not have.  On their 72 published files it rejects exactly the
   2 in-box sheets.  Flag: `diagram_sheet_enabled`.
3. Where a rejection is visible, since the owner asked: `output/decisions.jsonl` carries one line
   per image with `rule_id` and the sentence (their own paste is that line), and
   `run_report.csv` carries the per-row counters (`images rejected: S20 x1`).  Rejected files are
   not written to disk, so a row's `raw/` folder only ever shows what was published.  A
   `rejected/` debug folder is a separate decision.
4. The `_1` request "download it and crop the bottom labels, publish only the product" was NOT
   implemented, on purpose: for that file the artwork is six line-drawn icons, so cutting the
   captions leaves a drawing of an indoor unit on the product page.  The mechanism they described
   does exist and runs - `strip_promo_band` plus `isolate_main_product` is what produced
   `degraded_isolation=True` on two of the accepted files.  If a specific Dawlance file is a real
   photo with a label strip, I need the bytes to gate it correctly: `www.dawlance.com.pk` is
   blocked from this sandbox (`SSL_ERROR_SYSCALL`), so I cannot fetch it myself.

## 9. The Dawlance badge-panel file: cut it, do not reject it (owner's screenshot, 2026-10-10 night)

The file they sent is a studio shot of the unit with a **flat black panel** pasted under it
(4 feature badges, a "1.5 Ton" pill, a 6/12-year warranty block).  S20 called the layout artwork -
correctly - and the whole image was lost.  The instruction was the same one given for Orient's
hero: cut the labels, keep the product.

1. First idea, killed by measurement: "if the bottom band is dark, cut it".  Three of their own
   accepted photos are dark products on dark grounds (Orient Titanium olive-black 78 % band,
   EcoStar Duke 67 %/56 %), so that rule would truncate real photographs.
2. What actually separates them is *flatness*, not darkness - a pasted panel has one hard straight
   edge and uniform rows, a product has highlights.  Five conditions, all required: boundary jump
   >= 150, band mean <= 70, band row-mean std <= 45, kept side mean >= 170, edge flat across >= 95 %
   of the width.  Their panel measures 238 / 28.5 / 23.7 / 222 / 1.00; the three real photos
   56-82 / 164-181 / 89-106 / 236-255 / 0.67-0.88.  Scan of all 72 published files: **0 cut**.
3. The kept area is a *light grey plate*, so S20 still called it full-bleed ink (>6 from white
   everywhere).  Raising that threshold globally would re-admit all four Orient posters (same
   light wash) - so the exemption is bound to the cut itself: `panel_cut` is the detector's
   `("bottom", 270)` verdict, the line-art branch and S21 stay in force, and the crop is then
   judged as a product and passes.
4. Also caught while doing this: `_layout_rescue_verdict` had a stale call signature after the
   refactor (would have raised `TypeError` for any official gallery image hitting S20/S21).  It
   is now covered by an end-to-end test, which fails without the fix.
5. The rescue stays narrow for the same reason: judging the *padded canvas* rather than the tight
   crop rescued 12 of their 16 junk files.  Measured first, so it is not in the tree; 0 rescues on
   their corpus with the shipped rule.
6. Not done, on purpose: the attachment did not reach this sandbox (`/home/user/uploads` does not
   exist) and `www.dawlance.com.pk` is blocked at TLS, so the panel numbers were calibrated on a
   faithful replica of their screenshot plus their own run's badge files.  If their real file's
   boundary is soft (a gradient into the panel rather than one row), the flatness test will refuse
   the cut and the row loses the image again - `--keep-rejected` will show exactly that in one run.


## 10. "Is brand-official Orient working now?" - answered from their own log (2026-10-10, late night)

The owner's Orient row is *Frost 18W Classic White* (`.../air-conditioners/15-ton-frost-classic-white-dc-inverter.png`,
served with a `<picture><source type="image/webp" srcset="...png.webp">` in front of the `<img src>`).
Their question was whether the fix means the official hero is now published.  I did not guess; I replayed
their captured page and read their own `decisions.jsonl`.

1. **The root cause of the missing image was the collector, not the pixel gates, and it is already closed.**
   On their Orient page the hero sits in `div.pdp-hero__image > picture` - no anchor, so no S3.  The captured
   page yields the hero as a `gallery_container` candidate with `dom_flags=[]`, `order=0`, and
   `_decide_candidate` says GO ON.  Before `d082bce` the `pdp-hero` container was not in
   `GALLERY_CONTAINER_SELECTORS`, so the official page produced only the 7 cross-sell `ac/ultron*` tiles
   (correctly rejected as S3, they belong to other product pages) plus the `og-images` render - hence the
   old row: `failed, 4 candidates, 1 published, 0 wanted`, `images rejected: S3 x7, S1 x1`.
   Their run was cut from `31ae576`, which predates that fix: the numbers they saw cannot be used to judge
   today's tree, and no new code was needed for the Orient half.
2. **Two of my own hypotheses were tested and thrown out** (writing them down so no one re-chases them):
   * *WEBP is refused somewhere* - no.  `validate_image_bytes` accepts PNG, WEBP and JPEG payloads alike, and
     `has_image_extension("...png.webp")` is already True.  `url_rewrite_cross_extension_enabled` stays False:
     the `<img src>` gives the lossless `.png` directly, so chasing the webp twin would only add risk.
   * *The blur floor kills soft official renders* - no.  The floor on the download path is
     `blur_sharpness_floor=salvage_min_sharpness=0.02` on a normalised scale; **0 of their 72 published files**
     fall under it, and 0 fall under it after re-encoding to WEBP.
3. **The one part I still cannot certify from this sandbox** is the pixels of that specific PNG, because
   `www.orient.com.pk` is blocked at connect here.  The prediction is favourable and it is theirs, not mine:
   the page's CSS paints the studio look (`.pdp-hero__image { background:#fafbfe; border:1px solid #e7eefc;
   padding:40px }`), so the tint lives in CSS and not in the file - S20 reads pixels only, so a unit on a
   white/transparent plate cannot trip the painted-artwork branch.  To keep me honest about it, `--check-image`
   was added instead of a promise.
4. **`python main.py --check-image FILE [FILE ...]`** runs the *pipeline's own* chain on a saved file - promo
   band, edge seams, `marketing_panel_split`, `run_quality_gates` with the official rescues - and prints both
   bars (brand page and retailer) plus the sizes the run would write.  No sheet, no assets, no API key, no
   network.  Exit 0 publish / 1 a gate stops it / 2 unreadable.  Verified against their corpus: 72 files give
   **56 PUBLISH on the official bar and 55 on the retailer bar** (1 extra S7 stop, the official studio-corner
   rescue doing its job), S20 28 = 14+14, S21 4 = 2+2 - i.e. identical to the numbers in section 9.

## 11. The Orient row's real killer: S16 called a studio wash a colour (owner's log line, 2026-10-10 late night)

The owner's pasted `decisions.jsonl` line settled it - the hero was found and downloaded, then died here:

    {"title":"Frost 18W Classic White | Orient Electronics Pakistan",
     "url":".../15-ton-frost-classic-white-dc-inverter.png","decision":"reject","rule_id":"S16",
     "detail":"product area is a saturated colour (60% chromatic) - conflicts with target 'white' (family white)"}

Their instruction: "fix this ugly test and don't force this in brand official, only in retailer - but with working
fix, this safeguard should work correctly, I strictly say, or remove it".  So it was fixed twice, not deleted:

1. **The metric was measuring the wrong thing.** A pixel counted as "a colour" when its channel spread
   exceeded 0.10 - which is true of a pale blue-grey studio plate (spread 0.16 at value 0.94) and is exactly
   how a brand photographs a white unit.  Now a pixel is a colour only above saturation 0.22 *and* below value
   0.95.  Measured on 64x64 centre crops (share of the product area): pale plate #e9eefb 0.01, mid cool plate
   #c9d9f2 **0.45 -> 0.01** (that 0.45 is the trip line their hero fell over), purple/red/blue/green bodies
   0.61-0.62 unchanged, **golden body 0.61**, white/black/silver bodies 0.00.  The bar is 0.22 and not 0.30+
   because a golden body sits at saturation 0.24 - a higher bar re-opens the gold-on-white hole the existing
   tests guard, which is how the first attempt failed here and was re-tuned by measurement.
2. **The branch is scoped, per the owner's words.** On a brand-owned page the *chromatic-dominance* test no
   longer rejects: the measurement is logged as `S16.official` and the picture is kept.  The two identity
   signals stay in force on every page - bright-on-black and dark-body-on-white - so the official-Haier-CDN
   dark-body catch of 2026-10-09 is untouched (unit test 4).  And the exemption cancels itself when the
   brand's own file name names a different colourway ("...-gold-..." against a white row), because Orient-style
   assets are per variant and a contradiction there is a real sibling mix-up.  Rollback:
   `color_gate_official_chroma_exempt=False`.
3. **Not applied to the salvage path on purpose.** `_salvage_row` still calls the gate with retailer
   semantics; it inherits the metric fix but not the policy exemption, because that stage has no idea which
   host an offer came from (threading it through `SalvageOffer` was not worth touching three working
   functions for an edge case).  If it ever costs a row, `S16.salvage` in `decisions.jsonl` will say so.
4. **Proof on the real code path** (`tests/test_pipeline_end_to_end.py::test_a_tinted_studio_wash_cannot_cost
   _the_row_the_brands_own_hero`, with a fixture that measures 0.45 under the old rule): the same image, the
   same model, two hosts - old behaviour **0 published / S16 reject on the brand page**, today **1 published
   with an `S16.official` keep line**, while the retailer page still rejects with `S16`.  Their 72 published
   files: the chromatic branch fires on **0 of 20** colour-worded rows either way, so nothing that used to
   publish can now be lost to this gate.
5. **`--check-image FILE --check-title "TITLE"`** now runs S16 too (per bar, with the file-name carve-out
   mirrored), so "will my official file survive?" is one command on their own machine and not a promise from me.

## 12. The real Orient root cause: transparency was composited onto BLACK (owner's file, 2026-10-10 late night)

The owner sent the actual asset - `15-ton-platinium-matt-white-dc-inverter-230fb832.webp` - a cut-out of a
white unit, with the plate colour (`#e5e5e5`) coming from the page's CSS, and the log line:

    "rule_id":"S16","detail":"product area is dark (mean 0.42) - conflicts with target 'white' (family white)"

That is not a marketing-layout problem and not the wash/saturation problem of section 11.  **`validate_image_bytes`
returned the decoded image in its native mode (RGBA), and eight pixel readers in `image_quality` called
`image.convert("RGB")` - which composites transparency onto black, not white.**  Every one of those gates was
therefore judging the brand's product photo against a ground the brand never published.

Measured on a faithful replica of the owner's frame (2000x1873 cut-out webp), before vs after one change
(`to_rgb` at the decode, and the same helper in the eight readers):

| what the gate saw | BEFORE (black) | AFTER (studio white) |
|---|---|---|
| centre mean | 0.54 (their log: 0.42) | 0.98 |
| S16 colour vs `white` | **REJECT** - "product area is dominated by a dark body" | ok |
| S7 border ring | **REJECT** - "border ring is not a clean background" | ok |
| `strip_promo_band` | **invented a 489 px bottom crop** on a picture with no band | no cut |
| S20 / S21 / S14 | abstained: their white-ground precondition never held | judging real pixels |

Two directions of damage from one hole, which is why "brand official" kept failing only for Orient: the
rejecting gates (S16, S7) fired on a black ground that does not exist, the rejecting gates that need a *white*
ground (S14/S20/S21) went silent, and a cutter chopped 489 px off the product.  Brands that publish flat JPEGs
(most of the corpus) never trip it; Orient, Haier and Dawlance publish cut-outs.

Deliberate choices: **no flag and no new knob** - `to_rgb` already existed and already meant "flatten on white",
eleven call sites in `compositing`/`dedup` were already using it, and a switch back to "judge the ground PIL
invented" has no rollback value.  Nothing in S16's thresholds was touched, so the colour gate keeps every
correct reject it had (their own 3 S16 catches from the last run are unaffected).

Proved without reasoning from logs: `tests/test_image_quality.py` (3 tests: the decode flattens; S16 on the
cut-out is ok while the pre-fix read of the *same bytes* is the reject they logged; a wide cut-out loses no
band and no panel) and `tests/test_pipeline_end_to_end.py::test_a_cut_out_brand_png_publishes_with_a_white_ground`
(drives the row, then opens the saved JPG and asserts its four corners are light).  Regression: their 72
published files are all mode `RGB` with no alpha - `--check-image` on all 72 returns **111 publishes, S20 28,
S21 4, S7 1, 0 panel cuts**, identical to the measurement in section 9.

---

## 13. Round thirteen (2026-10-10): a brand page that spells the code differently was no page at all

### Checkpoint
- Branch: `arena/fe3fb54c-image-generator` (on top of `3048089`). No new module, no config
  change, no new threshold for the image gates.

### Honest status: **closed**.

### The complaint
The owner pasted the decision lines for the Orient Megatron row and asked why it went to Daraz,
Lazada and `aysline` "instead of orient official site which has image
`https://www.orient.com.pk/products/titanium-12s-olive-black-t3-plus.html`":

```
S1 page strength 0 - no model key found on the page      <- also on the brand page and 3 retailers
S15.tolerated - shop does not write the model at all     <- on aysonline.pk, and no line for orient.com.pk at all
S3 image is linked to a different page - the product page  (dozens, lazcdn.com/*)
```

### Investigation
- **`G1` is not missing config**: `config.source_platforms["www.orient.com.pk"] = "sitemap"` and
  `config.brand_sources["orient"] = ("https://www.orient.com.pk",)` - Orient is a *tier-one*
  source, and `KNOWN_BRANDS` knows the domain. The earlier guess in this plan's notes that Orient
  "is in neither tier" was wrong, and so was the guess that the en-dash or `&amp;` caused it.
- **the crawl found the page, the matching threw it away**: the cached sitemap
  `image_pipeline/state/sitemaps/www.orient.com.pk.jsonl` has 272 urls, 233 of them under
  `/products/`, including the exact one the owner quoted.
- **`S1`'s own key rule refused the row twice over.** For that title
  `build_model_keys` returns `tokens=('12x','ac-t3')`, `identities=('12xolive','act3tropicalwind')`
  and - because a code exists - `keywords=()` (documented at `model_matching.py:490`: "the brand+tail
  route only runs for a row that names no code at all"). Then:
  - `model_tokens_in_url` needs a variant of length >= 4; `12x` is 3, and `12xolive` is not a
    substring of `titanium12soliveblackt3plushtml` because the brand writes `12s`. **0 of 272 urls.**
  - The one row that *did* work in the owner's log (`QG 12X Aura White`) is exactly the case where
    Orient's slug happens to carry the sheet's spelling: `qg-12x-aura-white-t3-plus` contains
    `12xaura` -> strength 1 -> `accept source=sheet`.
  - `S1` on the page itself: identity `12xolive` is not in `titanium12soliveblack...` ->
    `strength=0 - no model key found on the page`, the line in their log. The QG row's
    `near-miss code qg12x -> QG 12X Aura White T3+` proves the same mechanism in reverse.
- **The QG row also showed the other half of the story**: `1 of 2 product images were dropped`
  with `S3 image is linked to a different page` on a `lazcdn.com` url - an image found *near* a
  product page is not that product's picture, and that rule is working as designed.

### The fix
`fuzzy_identity_match(text, keys)` in `model_matching.py`: for each identity shaped like a code
plus a name (`12xolive`), match the same text with the code's letter optional (`12[a-z]?olive`)
- so a brand that writes `12S` for a row that says `12X` is understood. Deliberately narrow:
- it needs >= 2 digits **and** a name tail of >= 4 letters, so `12`, `t3`, `500` alone can never
  match anything ("this is not a wildcard");
- it is tried **only** on `keys.identities` (the exact code+name keys), never on `keywords`;
- in `sources.model_tokens_in_url` it is the *last* route, after the existing exact and truncated
  ones;
- in `page_verification.verify_page` it lifts a page from 0 to **1 only when the host is the brand's
  own domain** - the owner's rule that a safeguard must not be forced on brand-official pages cuts
  both ways: a shop is not owed that trust, because a shop writing `12S` could be selling the `12U`.
  Retailer pages behave exactly as before.

### Verification
| measure | before | after |
| --- | --- | --- |
| Orient sitemap urls matching the Megatron row | **0 of 272** | **1 of 272**, the page the owner named, ranked first |
| `S1` on `orient.com.pk/products/titanium-12s-...html` | `strength=0 rule=none` - never offered | `strength=1 rule=fuzzy_identity` |
| `S1` on the *same* page on a retailer's domain | `strength=0` | `strength=0` (unchanged) |
| `S1` on Orient's `titanium-18u-olive-black` page | refused | refused (unchanged) |
| suite | green | **green** (5 new tests), `ruff` clean, `mypy --strict` at baseline |


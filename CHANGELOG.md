# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **S16 no longer mistakes a studio wash for a coloured product, and does not judge brand pages on it at
  all.**  The owner's Orient `Frost 18W Classic White` row of 2026-10-10 *found* the brand's own hero and
  then threw it away: `S16 product area is a saturated colour (60% chromatic) - conflicts with target
  'white'`.  The measure was raw channel spread (>0.10), which is what a pale blue-grey plate under a white
  unit looks like; it is now HSV saturation (>0.22) with a brightness cap (<0.95) - a wash measures 0.01
  where it measured 0.45, purple/red/blue/green/gold bodies keep 0.61-0.62, and the bar cannot go higher
  than 0.24-ish without re-opening the gold-on-white hole.  Per the owner's instruction the
  chromatic-dominance branch is then not enforced on a brand-owned page (logged as `S16.official`, image
  kept) while the two identity signals - bright-on-black, dark-body-on-white - stay in force everywhere, and
  a file name naming another colourway cancels the exemption.  Measured on their 72 published files: 0
  affected; end to end: the same image goes 0 -> 1 published from a brand page and is still rejected from a
  retailer.  `color_gate_official_chroma_exempt=False` restores the old behaviour
  (`docs/riper/option1-fix-plan.md` section 11).
- **A pasted marketing panel is now cut off the product instead of condemning the image**
  (`image_quality.marketing_panel_split`, rule id `S7.panel`).  The owner's Dawlance file of
  2026-10-10 is a studio shot of the indoor unit with a flat black panel glued underneath it -
  feature badges, a "1.5 Ton" pill, a warranty block - and S20 correctly called the *layout*
  artwork, which threw the product away.  Their instruction: "it should download but on isolated
  it cuts bottom labels advertisement marketing labels and add only product".  `strip_promo_band`
  could not do it (it caps a strip at `promo_band_max_ratio` and demands a bright gap row, while
  this panel covers ~45 % of the frame and sits under the unit's dark lip).  Widening that rule
  was measured and rejected: 3 of the owner's own accepted photos are dark products on dark
  grounds (Orient Titanium olive-black, EcoStar Duke x2), so "cut the dark bottom band" would
  truncate real photographs.  What actually separates the two is flatness: the panel measures
  band mean 28 / row-std 24 / jump 238 / edge flatness 1.00, the real photos 164-181 / 89-106 /
  56-82 / 0.67-0.88, so all five conditions must hold.  **Measured on their 72 published files: 0
  cut, and the verdicts are unchanged (56 ok, S20 14, S21 2)**; on their file's shape the panel is
  cut and the remaining unit passes every gate.  Flag: `marketing_panel_split_enabled=False`.
- **S20's "painted artwork" branch no longer fires on an image whose panel this pipeline just
  cut** (`run_quality_gates(..., panel_cut=…)`).  Dawlance and Orient shoot on a light grey plate,
  so after the cut the kept area still measures as 100 % "ink" (any pixel >6 from white) and the
  image was lost anyway.  Raising the tolerance globally was measured and refused - it lets all
  four Orient marketing posters back in, because their wash is the same light grey.  Instead the
  exemption is reachable only through the detector's own verdict: `panel_cut` is the
  ``("bottom", 270)`` tuple `marketing_panel_split` returned, so a caller cannot claim it, the
  line-art branch and S21 stay in force, and an uncropped poster is still rejected.
- **A crash on any official gallery image that reached S20/S21** (`_layout_rescue_verdict` was
  called with the pre-refactor arity, so the rescue raised `TypeError` instead of deciding).  It
  survived the suite because no test drove that path; `test_a_pasted_marketing_panel_is_cut_before_
  the_row_is_judged` now does, end to end through `process_product`.
- **The official-gallery layout rescue (S20/S21) is narrower than first written, and that is the
  point.**  Judging the padded canvas instead of the tight crop rescued 12 of the owner's 16 junk
  files (every Haier emblem among them) - `to_square_canvas` pads to 1200 px and that drops the
  ink span under S20's threshold; measuring that before shipping is why it is not in the tree.
  The rescue now (a) applies only to the painted-artwork branch, never to line art or caption
  sheets, and (b) re-runs S20/S21/S14/S8/S6/S9 on the isolated body.  **On their 16 files: 0
  rescued.**  Flag: `official_layout_rescue=False`.
- **`--keep-rejected`** (`keep_rejected_images`): writes the pixels a pixel gate refused to
  `output/images/<product>/rejected/<rule>-<hash>.jpg`, named after the rule.  Answer to "if
  rejected, where can we find [it]" - until now a rejected candidate left nothing to look at, so
  a row's folder could not distinguish "never downloaded" from "downloaded and refused".
- **A brand's own gallery is no longer truncated at 3 images** (`pipeline._page_publish_goal`,
  owner: "download all product only images … remove this hardcoded concept").  The candidate
  loop broke on `_row_is_done`, i.e. at `target_images_per_product` (3) - which was fine until
  a page's gallery turned out to be *ordered by junk*: Dawlance's own markup lists its slides as
  `_1, _10, _11, _2, _3, …`, so the row filled its 3 slots with an in-box sheet and two manual
  pages and never reached the seven real photos behind them (`_3` to `_9`).  Same shape as the
  Haier row of the same day: 6 views on the page, 1 published.  A page offering at least
  `gallery_publishes_all_min` (4) curated gallery images is now read to the end, bounded by the
  row's hard cap `max_images_per_product` (10); retailer pages with stray `<img>`s keep the
  target, and `_row_is_done` still decides whether *more pages* are visited, so a full official
  gallery is never topped up with marketplace junk.  Measured end to end: the same fixture that
  the old test pinned at "3 downloads for a six-image gallery" now requests all 6 and publishes 3
  (the repeats still fold at S10).  Rollback: `gallery_publishes_all=False` restores the old
  cost guarantee, which the renamed test now asserts.
- **S21 - in-box and manual caption sheets are rejected on layout**
  (`image_quality.body_layout`).  The owner's Dawlance review: "It downloads the inbox items
  pictures and remote manual please fix".  Those sheets are flat black-on-white line art, so
  colour, sharpness and the border ring all look like a white appliance photo, and only some of
  them are full-bleed enough for S20.  What does identify them is *arrangement*: a photograph has
  one dominant body, a caption sheet is a grid of comparable parts.  Measured on the owner's 72
  published files - the two live sheets form **9 parts with the largest covering 12 %** of the
  ink (saturation 0.0), while the most a real photo forms is 3 parts with its leader at 94 % -
  so `diagram_min_parts=4` with `diagram_max_dominant_share=0.20` sits in the gap and rejects
  exactly those 2 files of 72.  Rollback: `diagram_sheet_enabled=False`.
- **Haier's crisp gallery file was being ignored, and the run published a blur instead**
  (`gallery_extractors/_ATTR_PRIORITY`). Haier's markup is
  `<img src="…_60.png" data-imgurl="…_1200.png">`: the thumbnail sits in `src` and the page
  names the sharp asset in `data-imgurl`, an attribute this pipeline did not read. So the
  60px thumb was collected, the size-guess ladder then invented a URL for it, and the row
  published a blurry unnamed file while the six crystal-clear images the owner saw on the
  page stayed untouched. `data-imgurl` now sits between `data-original` and `data-lazy-src`.
  Proved by `tests/test_gallery_extractors.py::test_haier_gallery_uses_the_declared_crisp_file`
  (built from the owner's pasted markup): the `_1200.png` files are collected, no `_60.png`
  survives.
- **S20 - full-bleed artwork on a white canvas is rejected on content, not on URL words**
  (`image_quality.is_marketing_layout`). This is the hole behind "this link clearly show a
  emblem ai label add picture why this script keep downloading and not rejecting", reported
  three times. The cause was not a missing gate but a *scoped* one: S14 (`is_monochrome_emblem`)
  was written for exactly the Haier "AI + laurel" emblem, but it opens with
  `if corner >= config.logo_background_max_value: return False` - it only fires when the ground
  is *not* white, because the live case on 2026-10-09 was the grey-disc variant. The 2026-10-10
  files are the same emblem on a white ground, so S14 bailed out on its first test and nothing
  else in the chain asks about content: square (S8 ok), crisp (S6 ok), no banned word (S5 ok),
  no other model named (S4 ok), white-on-white (S7 ok). S20 asks the one question that separates
  them: a product photo leaves its studio whitespace to the edges, an emblem or poster does not.
  It reads three numbers - non-white ink share, the ink box's span of the frame, and coarse
  texture - and only judges frames whose artwork touches all four sides on a white canvas.
  Measured on the owner's 72 accepted raws: emblems ink 0.13-0.23 / span 1.00 / texture 28-56,
  badge strips 0.57-0.59 / 1.00 / 23-26, Orient posters 0.92-0.95 / 1.00 / 8, real product
  photos 0.22-0.46 / 0.21-1.00 / 3-10. **S20 rejects 14 of the 72 published files and all 14
  are files the owner had complained about** (8 Haier emblem files, 2 Dawlance badge strips,
  4 Orient marketing posters); not one real photo is in the list. Evidence sheet:
  `docs/riper/evidence/s20-marketing-layout.png`. Rollback flag: `marketing_layout_enabled=False`.
- **S4 now also reads the capacity code, so another model's photo cannot be sold as this row's**
  (`model_matching.url_names_other_capacity_code`). The owner's image 3 was a *silver* 18G EVA
  wearing an orange "1.1 Meter Long" badge, accepted for the row `Orient Split AC Hyper 18X
  Super White` from `…/Orient-Super-18G-EVA-T3-1.5-Ton-DC-Inverter-Air-Conditioner2.jpg`. S4
  compares *alphabetic* prefixes of model codes and this row's only key is `18X`, whose
  alphabetic prefix is empty, so `url_belongs_to_other_model` returned False at
  `if not product_prefixes: return False`. The new half compares the digit-plus-letter pair
  (`18x` vs `18g`) that this catalog uses as its identity, and it needs a code on both sides and
  a disjoint pair, so `-800x800` size suffixes, `2024/12` upload folders and `W02026…` asset ids
  cannot fire it. Re-checked against every accepted image in the owner's run: **72 accepted
  images -> exactly 1 newly rejected**, the wrong-product file above; the correct
  `…-Hyper-18X-T3-Super-White-Turbo-Pro.webp` stays. Rollback flag:
  `capacity_code_gate_enabled=False`.
- **The product hero is now read as a gallery** - this is the hole behind "the script
  downloaded the wrong picture". Orient keeps one photo per model in
  `<div class="pdp-hero__image"><picture>…`, with no `.product-gallery` widget anywhere on
  the page, and source 5 (the whole-page scan) only runs when sources 1-4 found nothing.
  So the extractor collected eight cross-sell tiles from the old `products-images/ac/ultron`
  folder, rejected all eight at S3, and never once saw the real photo carrying
  `alt="Frost 18W Classic White"`. Five hero-shaped containers are now gallery containers
  (`pdp-hero`, `product-hero`, `hero__image`, `product-image`, `product-media`). Measured
  on the live capture `uploads/live/orient_frost.html`: **8 candidates -> 9, and the 9th is
  `products-images-2026/air-conditioners/15-ton-frost-classic-white-dc-inverter.png`**,
  which passes S3 (no anchor), S4 (not another model) and S5. The owner's own file check on
  Orient's site found exactly this asset, which is what confirmed the direction.
  Category pages are unchanged (20 candidates before and after), so no extra downloads.
- **Generated social-share cards can no longer be mistaken for product photos.** S5 now
  blocks `og-images`/`og_image`/`social-card`/`share-image`/`meta-image` paths plus the
  single tokens `ogimage`/`socialcard`/`sharecard`. The trigger: the owner's run accepted
  `assets/og-images/pulse-19c-ultra-white-t3.jpg`, which is an advertisement - brand logo
  and the price printed into the picture - and eight such cards were kept across
  `qg-12x-aura`, `pulse-19c`, `platinium-12x` and `platinium-18x`. This was *aggravated by
  this session's own JSON-LD fix*: reading the page's declared image hands in whatever the
  shop put in `og:image`, and Orient puts a marketing card there. Rejected by every source
  now, at the same layer as `logo` and `banner`. A row whose only image is a share card
  stays empty on purpose - per the owner's rule, a wrong picture on the shop is worse than
  none. Bare `og.png` is not blocked (the phrase must be `og` + `image(s)`).
- **S18 crop closure now measures ink against the studio background, and only runs on a
  near-white one.** Doubt-reviewing my own fix found a regression it would have caused:
  "ink" was every pixel a few levels from *pure white*, so on a dark or gradient
  background the whole frame is one connected body touching the crop border and the crop
  grew to the entire picture (measured on a synthetic 900x600 before the fix: 413x213 ->
  900x600) - which publishes a coloured background into a white template. The closure
  exists for the white-on-white blindness only, so that is now its only territory.
  Re-measured on the owner's 72 accepted images after the restriction: 34 crops grown,
  38 identical, **0 shrunk** - the guard is grow-only by construction.
- **The JSON-LD page-identity exception cannot be talked past.** First version required
  one shared word between the node's identity text and the title - worthless, because the
  identity text *includes* the node's `url`, which is the page slug the check just
  trusted (measured: a "Megatron Titanium 12X" tile with the Frost URL copied in sailed
  through). Now only `name`/`brand` are compared, and `url`/`sku` are excluded from
  vouching for identity - the `sku`-copied-the-slug lie is blocked too, the honest node
  and the real Orient capture still pass, and rows that own a model code are untouched.
- **A row whose title has no model code can now use the page's own declared image.**
  `json_ld` accepted only a node whose name/sku/model matched a model key - and a title
  like `Frost 18W Classic White | Orient Electronics Pakistan` owns no key to match, so
  the page's own `Product` node was refused and its declared image never entered the
  candidate list. Verified on the owner's live capture (`uploads/live/orient_frost.html`,
  2026-10-10): `extract()` returned **0** candidates while the node carried
  `assets/og-images/frost-18w-classic-white.jpg`. Such a node is now accepted when it
  names **this page** as its product page (`url`/`@id`/`mainEntityOfPage`, or a `sku`
  equal to the page slug) - a claim a cross-sell tile cannot make, which is why the
  exception is closed for every row that *does* own a model code. On the same capture the
  row goes 7 candidates (all cross-sell tiles, all correctly S3-rejected) -> 8, first =
  the Frost image. 6 of 34 rows own no model code; the run's only `failed` row is one of
  them. **Needs the owner's live re-run**: this sandbox cannot download from
  `orient.com.pk`, so the image's quality-gate pass is unproven here.
- **S18 - a crop may not cut the product.** The isolation box followed the strict
  foreground mask, and a white shell on a white background fades out *inside* that
  mask, so the box stopped at the product's waist. Measured on the owner run of
  2026-10-10 (`output/decisions.jsonl` + its 72 accepted images): **41 of 72 crops
  cut ink** - `Candy By Haier 0.8 Ton #01` kept 1342x195 of a 1342x695 unit (only the
  blue vent strip), `EcoStar Emperor #01` kept 1066x87 (only the louvers),
  `eComfort Pro KEC-1267S #01` 427x107. The box is now *closed*: ink connected to the
  crop border is pulled in, a mark standing away from the product never widens it, and
  nothing is ever trimmed. Grow-only, and switched off by
  `isolation_crop_closure = False`. Verified on the owner's own images (height
  +270 %, +203 %, +347 %, +55 %, +46 % on the five worst; the other 67 crops byte-size
  identical) and eyeballed in `docs/riper/evidence/crop-closure-before-after.png`. **Untested
  against a live re-run** - the composed result on the shop template still needs the
  owner's eyes.
- **Haier sized thumbnails can now be re-fetched as PNG - switch shipped OFF.** The old
  run's log shows `image.haier.com` serving `W020230419477905891117_60.jpg` (60 px) with
  the same asset full-size at the extension-less URL, and five gallery images dying at S8
  `too small 60x60` while two `.png` assets in the same folder were accepted. The size
  ladder therefore grew an optional sibling-extension rung. **A doubt review of this
  change (2026-10-10) found the risk in my own reasoning**: `upscale_candidates` output is
  consumed as "first candidate that answers wins" (`pipeline.py` probes `[0]`, then
  downloads `attempts` in order), so *any* rung that happens to resolve replaces the file
  the page named - with no check that it is the same picture. The stripped-`.jpg` rung is
  safe on evidence (the run's own S10 line proves the page listed that exact file, i.e. it
  is the same photo), but the `.png` rung has no such proof. So `url_rewrite_cross_extension_enabled`
  defaults to `False`: the code ships, nothing changes until a live probe on haier.com
  confirms the sibling exists. **Untested against the live CDN - this sandbox cannot reach it.**
- **A row with no model code no longer crashes the run.** `_tolerated_spelling_reason`
  indexed `keys.identities[0]` for rows that own neither a model token nor a glued
  identity, so `Frost 18W Classic White | Orient Electronics Pakistan` came back as
  `failed: unexpected error: tuple index out of range` - a crash inside the code that
  *explains* a failure. Such a row now reports a reason (owner run 2026-10-09).
- **Background removal stops cutting white products.** Isolation measured the raw
  difference mask, and a body whose shading is softer than `isolation_bg_distance` is
  *hollow* to that mask: the surviving component was a vent strip or a side panel, so the
  unit lost its top and kept its bottom. It now measures the product **silhouette**
  (outline bridged, enclosed background filled). 46 images in a 33-product run had the
  product pressed against a crop border; 39 do now, and the 10 that changed are the
  cut-unit rows (Orient Megatron, Gree Charmo, Dawlance Dual Pro, Kenwood eComfort...).
- **A frame that is not one product is never cropped.** An "In-box items" diagram or a
  promo banner holds several comparable objects; cropping it to the largest published a
  fragment (the Inspire Pro row shipped a line-art icon, the LG remote rows a grey blob).
  Those frames now go through whole, which is strictly better than a fragment.

### Added
- **`python main.py --check-image FILE [FILE ...]` - the gates, for one file, with no crawl.**
  Whether an official image gets published has two halves: the URL/DOM half can be replayed offline
  against a saved page, but the pixel half (S7 ring, S8 size, S20/S21 layout, the panel cut, the
  official rescues) needs the real bytes, and until now the only way to ask was to run a crawl and read
  `decisions.jsonl`.  This applies the pipeline's *own* functions - imported, not reimplemented, so the
  check cannot drift from the run - and prints both bars (`brand-official page` and `retailer page`),
  what the promo-band/edge-seam/marketing-panel cuts did to the size, and the files the run would write.
  Needs no sheet, no assets, no API key, no network; exit 0 publish, 1 a gate stops it, 2 unreadable.
  Cross-checked on the owner's 72 published files: **56 PUBLISH on the official bar, 55 on the retailer
  bar** (one file saved only by the official studio-corner pass), 14 S20 and 2 S21 on each - the same
  numbers as the end-to-end sweep in `docs/riper/option1-fix-plan.md` section 9.
  `--check-title "Frost 18W Classic White"` adds the row's title, so the model and colour gates (S16) are
  judged exactly as they would be for that row - the check that settled the Orient question.
- **Safeguard S10 now says why.** A duplicate rejection records the signal and the
  measurement (`re-served copy of accepted image #1 (dHash 3 bits apart, correlation
  0.9688)`) instead of the string "near-duplicate", and an *ambiguous* pair - close
  enough to look like a repeat, not close enough to prove it - is logged as
  `S10.review` and kept. Tightening the rule further was measured and rejected: the
  pair the tests guard (same AC, louver flap open) scores **closer** on dHash (2 vs
  3-4 bits), silhouette IoU (1.000 vs 0.985) and body correlation (0.965 vs 0.802/0.555)
  than the two duplicates the owner reported, so no threshold catches one without
  deleting the other.
- `config.dedup_shape_iou_min` (calibrated: copies 0.985, best different angle 0.754),
  `config.isolation_merge_radius_ratio`, `config.isolation_multi_body_ratio`;
  `image_quality.body_shape` / `shape_overlap` for silhouette comparison;
  `dedup.ProductDedup.duplicate_reason` and `review_shape_match`.

### Performance
- One fingerprint per accepted image instead of two (`is_duplicate` and `add` were each
  resampling the same 1200px photo).


## [1.1.0] - 2026-10-08

### Added
- **Visual Color Gate (Safeguard S16)**: Eliminates wrong-color gallery images (e.g. Purple or Red refrigerators published for `Homage 18 Cu Ft Golden (HRF-47662-GD)`). Extracts foreground HSV color profiles from isolated product crops and rejects candidates whose dominant hue directly conflicts with the title colourway. Supports 13 canonical color families (`gold`, `red`, `purple`, `blue`, `green`, `white`, `black`, `grey`, `pink`, `orange`, `yellow`, `beige`, `brown`). Controlled via `config.visual_color_gate_enabled`.
- **Intra-Gallery Color Consistency (Safeguard S17)**: Prevents mixing conflicting chromatic color variants within the same product gallery (e.g. golden primary with purple secondary images).
- **Display Product Screen Exemption**: Adds `is_display_product()` identifying LED TVs, smart displays, OLED, QLED, and monitors. Automatically bypasses S16 for display screens so colorful demonstration wallpapers and scenic artwork are never falsely rejected.
- **Catalogue Header Alias Support**: Added `"image"` (singular) to `URL_ALIASES` in `csv_io.py`, enabling native out-of-the-box processing for catalogue sheets such as `Products.csv` (`Title`, `Image`).

### Changed
- **Unified Alphanumeric Tokenizer (`code_like_tokens`)**: Replaced competing, fragile regex passes with a single structural tokenizer that splits on word boundaries and glues consecutive prefix+digits+suffix segments (e.g. `HRF` + `47552` + `GD` → `hrf47552gd`, `DW` + `7500` → `dw7500`). Fixes S4 model mismatch detection for URLs containing hyphenated or separated model codes without false positives on capacity numbers (`15 Cu Ft`).
- **Capacity Policy Formalization**: Confirmed capacity tokens (`15cuft` vs `18cuft`) remain non-discriminating for S4 rejection; mismatched capacity specs between title and page are recorded as flagged annotations in `run_report.csv` (`variant mismatch (flagged)`) and `review_products.csv`, never blocking publication.

### Performance
- **Optimized Foreground Hue Extraction**: Employs 96x96 vectorized box downsampling and NumPy array filtering in `extract_foreground_color_profile()`, completing visual color verification in <2ms per candidate image with zero latency impact on processing throughput.

### Fixed
- **Fetches now go over IPv4, curing curl 28 connect timeouts on IPv6 black-holed hosts.**
  Live runs showed `eshop.pel.com.pk` and `www.surmawala.pk` dying with `curl: (28) Failed to
  connect ... after 21000 ms` - a TCP connect timeout that fires *before* any HTTP, because curl
  burns its budget on an unreachable IPv6 record while the IPv4 address (what a browser uses) is
  fine.  `fetch_curl_options()` now sets ``CURL_IPRESOLVE_V4`` on every session when
  ``fetch_force_ipv4`` (default on).  This is the connect-level fix; the separate `curl: (92)`
  HTTP/2 stream resets on Dawlance are transient and already self-heal through retry, so they are
  deliberately left alone.  Tests `tests/test_fetch.py`.  330 tests, ruff + mypy --strict clean.
- **A fallback search-result page can no longer "match" via its own search query.**
  `relaxed_page_match` built its haystack from the whole URL, and fallback search URLs echo the
  searched number in the query string (`?_psq=PWM+1250`).  A wrong product's result therefore
  self-matched - live, a `PWM-1250` row was tolerated on a `PWMS-1050` page purely because `1250`
  appeared in the query.  The query (and fragment) are now excluded; only host + path + page text
  count, so a model number must appear in the page itself, not in the search that found it.
  Legitimate sibling matches (a `DW-7500` page for a `DW-7500C` row) are unchanged.  Regression
  `test_fallback_match_ignores_the_search_query_in_the_url`.  330 tests, ruff + mypy --strict clean.
- **A page showing another colourway is no longer accepted as this product.** The colour a
  product comes in is part of its identity, so `... 7 Cu Ft Burgundy (9160-WB-GD)` and its
  `Pearl Red` twin are different products and need different pictures - the wrong colour costs
  the company a penalty. `match_product` now rejects a page whose primary colour differs from
  the title's (S1 rule `colour_conflict`, its own line in `decisions.jsonl`), the relaxed
  fallback tier (`S15.relaxed`) never rescues such a page, and the row keeps looking for the
  colour it was asked for. This supersedes the 2026-10-05 "flag it and publish" rule, so a sheet
  URL serving the wrong colour now **fails the row** instead of publishing it with a note.
  Owner decision 2026-10-07; regressions `test_a_page_showing_another_colour_is_rejected`,
  `test_a_different_colour_fails_the_row_instead_of_publishing_it`.
- **A model code's letter tail no longer counts as exact proof.** `HRF-538 IFGA` and
  `HRF-538 IFPA` are different variants of one model, so a page printing another tail is now
  *tolerated* rather than exact: it is deferred while the row looks for a page that prints its
  own model, published only as a flagged last resort, and the note names both tails
  (`page shows variant IFPA, sheet model is IFGA`). Optional feature letters still never
  discriminate (`DSD 4890 Silver SD` ≡ `DSD 4890 Silver`), a page that omits the tail is judged
  exactly as before, and `9160-WB` / `9160LF` stay the same product because there the tail is
  part of the code itself. Owner decision 2026-10-07; regressions
  `test_a_page_printing_another_variant_suffix_is_a_last_resort_not_proof`,
  `test_a_page_that_omits_the_suffix_is_judged_as_before`.
- **A tolerated (different-variant) page can no longer top up a partially-solved row.** Stage 3b
  consumed the deferred last-resort page whenever the row had *fewer than `min_required_images`*
  pictures, so a row with 1 correct retailer image got 4 different-colour official photos mixed in
  (live: DW-7500C). It now runs **only when the row found zero images**: 1 correct image publishes
  as `PARTIAL` + flagged, and the tolerated page still rescues rows that found nothing.
  Owner decision 2026-10-07; regression
  `test_a_row_with_one_exact_image_is_never_topped_up_by_the_tolerated_page`.
- **S7 no longer rejects genuine white-background studio shots whose product touches the frame
  edges.** Root cause: `border_is_clean` assumed the whole outer ring is background, so a tall
  appliance / top-down view (dark top deck + base crossing the top/bottom edges) split the ring
  and failed at ~0.5 fraction even with 4/4 neutral corners (live: Surmawala DW-7500 1200×1200,
  and the repo's `uploads/image-3.png`). A universal, host-agnostic rescue
  (`config.studio_background_rescue`, `studio_rescue_min_neutral_corners=4`) now accepts such an
  image when **all four corners are neutral studio white**; busy/lifestyle frames never reach four
  neutral corners at `corner_patch_ratio` and stay rejected. The previous official-only rescue is
  unchanged; the new one is bounded and switchable.
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
- **Official Haier pages now get a size ladder so their galleries yield 3 images.**
  Haier's CDN ships 60px thumbs and often lacks the ``_1000`` rewrite the pipeline bet on, so a
  product page's 3 gallery photos collapsed to 1 accepted image (live: H55K85FX).  For pages on the
  brand's own domain only, ``upscale_candidates`` now tries ``_1000`` -> ``_800`` -> ``_480`` before
  falling back to the thumb; retailers keep the single ``_1000`` rewrite, so their S8 protection is
  unchanged.  The S8 gate itself is untouched - a genuinely tiny image still rejects; we only look
  harder for the large sibling on official pages.  Regression
  `test_official_haier_page_tries_a_size_ladder_retailers_do_not`.  330 tests, ruff + mypy --strict
  clean.
- **Variant pages that print the colour only in the body now match their own colourway.**
  A PEL dispenser page names no colour in its title; the colour lives in a labelled body line
  (`Color: Red Blaze` near the swatches and a `| Colour | Red Blaze |` spec row) while the swatch
  menu lists *every* option.  `page_verification.selected_colourway()` reads only a labelled value
  (a spec-table row or an inline `Color:` label whose value is a short phrase), never the swatch
  list, and `match_product` counts that value exactly as if the title had printed it - but only when
  the page's strong text names no colour of its own, so ordinary pages are untouched.  Result for the
  PEL 425 page: the `Red Blaze` row clean-matches its own label, the `Marine Impression` row is
  flagged, and the `Maroon Blaze` row is rejected (`colour_conflict`) - instead of all three rows
  sitting unmatched.  Pages with no label stay fail-open, as today.  330 tests, ruff + mypy --strict
  clean, radon 45.
- **Every catalogue brand now has its official domain, and category words can never veto.**
  `KNOWN_BRANDS` was missing three brands that appear in the shipped catalogue and in the owner's
  input sheets - `Superasia` (45 rows), `Hisense` (4 rows) and `Midea` - so their own pages got no
  priority in `prioritize_source_urls` and no curated-gallery acceptance, and a reseller outranked
  the manufacturer whenever it came first in the sheet.  Added with each domain verified against the
  brand's own site (`superasiagroup.com` / `superasiastore.com`, `hisensepakistan.com` /
  `hisense.com.pk`, `mideapakistan.store` / `mideapakistan.com`), because a wrong "official" host
  would relax the S7 gallery test and let worse images through.  `detect_brand` now also tries the
  first **two** title words, so the catalogue's `Super Asia` reaches the one-word key `superasia`
  (it returned `None` before, i.e. no priority at all).  And every word in the catalogue's 21
  category paths - `microwaves`, `geysers`, `purifier`, `humdifier`, `sound bars`, `appliances`,
  ... - is now in `_GENERIC_NAME_WORDS`, so an appliance category can never be mistaken for a
  series name and veto a correct page.  330 tests, ruff + mypy --strict clean, radon 45.
- **Probabilistic, format-agnostic variant matching** (`docs/riper/probabilistic-matching-plan.md`,
  owner-approved 2026-10-07).  `similarity.py` (new, stdlib `difflib` only) adds `similar()` and
  `same_digit_runs()`; `ModelKeys.name_words` captures the positional "name" words - series lines
  (`Daimond`/`Cuby`) and unknown colourways (`Ocean Mist`) - with category noise removed; and
  `score_match()` returns per-attribute vetoes (series / colour / size) plus a confidence, each
  fail-open.  `match_product` now demotes a matched page that *names a different* series line or
  colourway to a flagged last resort, so `Daimond`/`Cuby`/`Ruby` and *future* unknown colourways no
  longer silently mix, with **zero per-name tables**.  The hand-added `marine impression` vocabulary
  line was removed - the dynamic rule covers it.  A series veto needs a **substitution** (a word on
  each side the other lacks), never a mere omission: the first cut vetoed whenever the page did not
  repeat the row's line name and that demoted **75 of 94** products on a terse `brand + code + colour`
  page; the substitution rule keeps all **94/94** clean, and brand words are dropped before the
  comparison so `Super Asia` ≡ `Superasia` cannot veto.  Measured: same-code colour pairs separated
  274, **0 false rejects**, 157 diff-colour pairs with **0** clean matches; 100-product offline smoke
  GREEN; 330 tests, ruff + mypy --strict clean, radon baseline unchanged (45).
- **`image_pipeline/variant_terms.py`** - the variant vocabulary and its pure rules: colour
  families with a neutral (achromatic) carve-out, `suffix_agreement`, `colour_word_conflict`.
  It holds no brand, suffix or colour-code tables, so a colour the vocabulary has never seen
  simply fails open instead of guessing. Measured against the shipped catalogue (752 distinct
  titles): **269 of the 304** same-code pairs whose titles both name a colour are now separated;
  of the 35 kept, 24 name the same word, 8 are one finish under two names (`silver` ≡ `chrome`
  ≡ `grey`, `graphite` ≡ `grey`) and 3 fail open because the row's only colour word is the
  series name `Chrome`.
- **`image_pipeline/textnorm.py`** - `normalize_text` / `normalize_key` moved out of
  `model_matching` into a leaf module, so the variant vocabulary can normalize punctuation
  (`(9149-WB)-Pearl Red` now reveals *both* colour words) without an import cycle.
- **One config field for the whole rule: `variant_separation_enabled`** (replaces
  `check_colour_match`), plus `ModelKeys.colour_word`, which carries the title's own colour word
  so the matcher never has to re-parse a title it already cleaned.
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

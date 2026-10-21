# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`S15.file_code` — a file that writes a number must write THIS row's number (owner
  2026-10-20).** A page that reached the row only through a variant spelling
  (`S15.tolerated`, `S15.tail`, `S15.relaxed`, `S15.unverified`) used to lend any file it held,
  because `page_proved_model` only excluded `relaxed`/`unverified`. Live run
  `refrigerators_50_titles`: the `KRF-24457` rows published `22257-5.jpg`, `22257-4.jpg`,
  `2257-b.jpg` and `23357-int.jpg` — four other model numbers — and `S4` could not accuse them,
  because those numbers belong to no row of that sheet (S4 only knows the sheet's own codes) and
  `S4.code` only fires when two codes *share* a digit group. So the gap was a missing **positive**
  rule, not a broken one: on a non-exact page, an image whose file name carries a code-shaped
  number must name one of this row's codes. A file with no number in its name (`a1-19-5.webp`)
  still passes — that is settled tolerated-publishing law — and `S16.square` is what judges it
  then. Your own 60-row run refused **45** files on this rule.
  `model_matching.url_claims_a_code` + the `page_matched_exactly` limb of
  `pipeline._consume_candidates`; test `test_a_variant_spelling_page_cannot_lend_a_file_naming_another_number`.
- **`S16.square` — the row's colour is judged on the picture that actually ships (owner
  2026-10-20).** `S16` measured the download, and a glossy door hides its coloured print behind
  the raw frame's own dark trim: eye-checked on the run's published squares, a **red patterned
  door** passed for the `HR-47452 … Black` row, a black door with a **red flame print** for
  `PRINVO-2550 … Gold Silk`, and a bronze fridge badged **"New Classic Plus"** for a
  `KRF-24457 … Black` row — the same lesson `S10` already learned for duplicates ("judge the
  picture we publish"). The verdict now runs again on the composed square, through
  `image_quality.hero_colour_conflict` at `S16`'s own naming bar (a chromatic foreground big
  enough to name a colour must name the row's colour). **Image 01 only** — his instruction of
  2026-10-20, and the right one: images 2+ are parts and detail shots, and an open fridge's
  cool-white shelves are no opinion about its colour (`DMD7951_4-Door_6.jpg` is literally a
  shelf shot). Measured on the owner's 101 published squares before it shipped: **5 heroes
  refused, all five a different colourway, 96 kept.** It inherits every `S16` exemption
  untouched — the two switches, display products, a row stating no colour, a grey/white/black
  studio shot, and the brand's own page (a safeguard must not cost a brand its own hero).
  Tests: `test_a_hero_reading_another_colour_is_refused`,
  `test_a_grey_studio_hero_says_nothing`, `test_a_small_colour_print_says_nothing`,
  `test_a_row_that_states_no_colour_is_never_refused`, and the exemption case
  `test_a_tinted_studio_wash_cannot_cost_the_row_the_brands_own_hero`.
- **The empty-row hunt (`fallback_max_pages_per_row_when_nothing_found`) — and the owner turning
  it off.** `refrigerators_50_titles` proved the six-page budget too small: every failed row read
  `sources tried: dawlance:sitemap:0, surmawala:shopify:3, modernelectronics:shopify:3` — two
  answering shops ate it and `japanelectronics.com.pk`, which sells the row, was never queried
  (the same run crawled it 30× for other rows). Stage 3a re-runs the chain with a bigger budget
  **only while the row has published nothing**, and reports `search budget ran out: N source(s)
  asked, M page(s) looked at and refused` so `failed` means *not available*, never "we stopped
  looking". Then the 60-row run measured the other half of the answer: **all 14 wide hunts
  crawled the 18-source catalogue and published zero images**, so the owner set the field to
  `3` — below the ordinary cap — and the hunt is off as we ship, while any value at or below
  `fallback_max_pages_per_row` means a failing row searches once. `__post_init__` no longer
  rejects a small value: it is a setting, and refusing to start the run over it was a bug.
  `sources.SourceChain.find_candidates(wide=...)` + `pipeline._wide_hunt_for_an_empty_row`;
  tests `test_a_row_that_found_nothing_asks_the_whole_catalogue`,
  `test_the_wide_hunt_off_switch_is_its_budget_equal_to_the_ordinary_one`,
  `test_the_shipped_wide_budget_is_smaller_than_the_row_budget_so_no_row_pays_for_it`.
- **A row with no `Source_url` now runs the search chain instead of being skipped (owner
  2026-10-20: "if sourceurl not available … start going on its default fallback").** The sheet
  arrives from Step-1 with titles filled and links still empty, and `main.py` reported every one
  of them `no source url` — 50 refrigerator rows, 0 processed. The partition is now one named
  function, `main.split_sheet_rows(tasks, urls_required=...)`: a title-only row is runnable in
  the scrape mode — the same chain §3 describes (brand sources, then the retailer search), with
  every page rule still applied to what comes back — and `urls_required`
  is set where the URL column *is* the picture (Option 2 `--direct`, Option 3 `--images-dir`) or
  where the search itself is off (`config.fallback_enabled=False`), because there an empty cell
  genuinely leaves nothing to try. `process_product` needed no change: it already read the brand
  as `task.source_urls[0] if task.source_urls else ""` and its Path-2 comment says "(or empty)".
  Tests: `test_a_row_without_a_url_is_still_searchable_in_the_scrape_mode`,
  `test_the_url_column_is_the_picture_when_options_2_and_3_run`,
  `test_an_empty_title_is_a_bad_row_in_every_mode` and the live
  `test_a_title_only_row_publishes_from_the_search_chain`.
- **`last_resort_marketplace_fallback` — when nothing proves the model, ask the named marketplace
  (off by default).** The owner's instruction for the rows no shop proves: "if not found at end it picks surmawala
  … if not found simple says not found". Stage 3d runs only when a row has published nothing after sheet
  pages, fallback search, tolerated spelling and last-resort salvage. It visits the pages offered by
  `last_resort_marketplace_hosts` (default `www.surmawala.pk` and `japanelectronics.com.pk`) and
  publishes whatever clears every image
  gate, flagged `UNVERIFIED marketplace source: <page>` in the review CSV and logged as `S15.unverified`.
  An unproven page still vouches for no picture: `S15.image` requires each file to name a code of this row.
  With the default `False` the stage does not run and the row reports "no page matched this model".
- **S22.source_lock — one album is one shop's gallery (owner 2026-10-17).** "it must download image
  from one source not too many, because too many can cause different varieties ... we choose the best
  one from one, not one here and one from other source". The first page that both publishes a picture
  and carries `album_lock_min_curated` (2) curated candidates owns the row's album, keyed on the
  **page's** host — never the image host, because `cdn.shopify.com` serves thousands of shops. Other
  shops' pictures are refused with `S22.source_lock`; a row that ended short of `min_required_images`
  may still be topped up, but only by a file whose own name proves this row's code
  (`url_anchored_to_row`). Stage 3c's last resort is held to the same rule — an offer collected before
  the home existed is not a side door afterwards. Every published picture now records the page it came
  from (`AcceptedImage.page_url`), so the audit shows the album's home. Measured by replaying the
  recorded runs: washing machines 39 -> 35 pictures (4 refused, 1 of them kept by that proof, across 4
  rows), AC 88 -> 86, Midea untouched. `album_source_lock_enabled=False` reverts publication in one line.
- **S22.coherence — an album holding two bodies says so (report-only).**
  `image_quality.album_coherence_note` groups the published squares by foreground value and notes the
  row when two tight groups (`album_coherence_min_group`, `album_coherence_value_gap`,
  `album_coherence_group_spread_max`) sit apart. It **never rejects**: measured on both recorded runs
  the bar fires on 2 of 28 albums — the real `haier-6050` mix and one Gree false flag — and correct
  albums span as much value as mixed ones (a detail shot and a shop's flash move one machine from 0.43
  to 0.84), so any bar that deletes the beige pair in `haier-6050` deletes real pictures elsewhere.
- **S4.code — a model's identity read from its digit structure (owner 2026-10-17).** The 13 kg
  `1301217E` row published the 8 kg `hwm80-1217` twice because the files share the `1217` tail and no
  earlier limb compares the digits *before* it. `model_matching.code_digit_clusters` reads each
  contiguous code as the groups the shop actually wrote, drops photo counters and collapses a doubled
  sheet title, and accuses only when two codes share a whole digit group of >= 3 (the family) and
  disagree on the head; a code that confirms this row clears the file, and a colourway inside the code
  (`…1217E` vs `…1217B`) is identity, not decoration. Measured blast radius: 2 of 147 published
  pictures across the 4 recorded runs (both of them the wrong-product files), 0 of 88 in the AC run, 0
  of 20 in the Midea run, and 0 accusations over the 1504 (title, url) pairs in
  `input/1800_products.csv`. Switch: `model_code_shape_gate`, tail size `model_code_tail_min`.
- **`verification/run_diff.py` — proof of what a gate change published.** Diff two run folders:
  published pictures that appeared or disappeared, rejections by rule, and rows that changed source.
  Selection only, not pixels. Exit 0 identical / 1 it moved / 2 bad input, so the diff is a gate on
  every future matcher or colour change, not a judgement call.
- **Option 2: `main.py --direct` — direct image URLs, no scraping.** The sheet's URL column is
  read as the product's own image URLs; per row the pipeline downloads, validates, composes on
  both templates, uploads (both-or-neither, banner last) and writes the same import CSVs. No
  pages are fetched and nothing is model-proven: `direct_pipeline.process_direct_product`
  orchestrates the existing Option 1 machinery (`BatchRunner` gained a keyword-only
  `processor` seam, default unchanged).
- **S19 branded-template guard** (Option 2): store theme art (iShopping-style yellow frames,
  promo banners carrying the shop logo) is skipped before compositing. Evidence is
  background-only — ≥3 of 4 saturated corner patches (`branded_template_reason`,
  `branded_corner_min_saturation`, `branded_min_saturated_corners`); white/grey studios and
  colourful products on white never fire it, and a frame-filling product stays owned by the
  S7 corner bar exactly as in Option 1. Verified end to end on real web photos (AC,
  refrigerator, washing machine, yellow sale template) and over a loopback-HTTP `--dry-run`.

### Changed
- **Colour is judged strictly on the row's first picture, with S16's own verdict (owner
  2026-10-21, `hero_colour_conflict` + `pipeline`/`direct_pipeline`).** The 49-folder run refused
  five correct heroes and three correct detail shots, and the owner's rule is absolute: *no
  correct picture may be rejected*.  Root cause, measured on his files: `S16.square` rendered its
  verdict by exact label equality against `extract_foreground_color_profile`'s five crude bins -
  which have **no brown at all** (his walnut fridges bin `red` at hue 17°) and bin a 335° purple
  as `red` although 335° sits inside S16's own purple arc (255-25).  Brown rows were therefore
  refusable whatever picture they carried.  Three fixes, in order of generality:
  (1) `hero_colour_conflict` now **delegates** to `verify_image_color` once per family the row
  names, inheriting the arcs, shade centroids, conflicts table and achromatic path the pipeline
  has been tuned with for months - a row naming several colours accepts a hero matching any one;
  (2) the achromatic branch stopped counting **dark sheen as a colourway**: glossy black/grey glass
  reflects the cool studio at hue ~207° (the Floral Black CB1: 50 % chromatic pixels, 97 % of the
  body dark, 2 % visible colour), so black and grey targets now weigh only bright chromatic
  coverage - a visible blue body on a black row still refuses;
  (3) the per-file `S16` and the gallery rule `S17` no longer judge images 2+ in **any** mode, and
  `S16.square` steps aside entirely for `--images-dir`: hue cannot tell a side or interior view of
  the right product from another colourway (the Floral Brown CW1's cabinet side measures 100 % blue
  and is a correct picture), so the deletion moved to where the signal is trustworthy - the hero of
  a scraped row.  `S17` keeps recording the album's families and the run report still asks the
  owner when they mix ("write the colour in the row").  A curated folder's contradictions are
  logged, never enforced: `S16.curated` / `S17.curated` keep-notes.  Cost of the rule, stated: a
  wrong-colour *detail* shot on a scraped page now publishes (the two tests written against the
  old behaviour carry the decision in their docstrings).  Proof on his own folders: the 7 failing
  folders went 0/7 → 5/7 published, CW1 keeps all 5 pictures, zero colour rejections in the run;
  the two that still fail are file defects, not gates (a 225×225 source at S8, poster artwork at
  S20).  `color_gate_enabled` is back on - the off state in `7e93ae5` was a temporary unblock.
  Tests: `test_a_brown_hero_stays_on_a_brown_row`, `test_a_purple_hero_stays_on_a_purple_row`,
  `test_a_wrong_colour_hero_is_still_refused_after_delegation`,
  `test_black_row_keeps_a_glossy_black_glass_door`, `test_grey_row_keeps_a_glossy_dark_glass_unit`,
  `test_black_row_still_refuses_a_visible_colour_body`, `test_curated_side_views_survive_the_colour_gates`,
  `test_curated_hero_that_disagrees_is_recorded_not_deleted`.
### Changed
### Changed
- **A black studio ground is a clean ground, and a curated folder's picture is his choice
  (owner 2026-10-21, `run_quality_gates`/`studio_background_pass`/`neutral_corner_count`).**
  His Moiz folder held a white Dawlance fridge on a pure black studio ground - perfectly
  keyable, because the isolation already keys off whatever colour the corners show - and S7
  refused it ("border ring is not a clean background"): the corner bar counted only
  *bright* neutral corners.  Curated `--images-dir` folders now also count corners that are
  near-neutral at the dark end (`neutral_dark_max_value`, 0.15; measured: black studio
  grounds 0.00-0.01, shadowed lifestyle corners >0.2).  Scraped pages keep the bright-only
  requirement - there the white rule is the lifestyle guard.  The second half: a picture
  with NO ground at all (the DFD 900 doors crop filling its frame) can never have a clean
  ring, so in a curated folder - after size, aspect, QR, emblem, poster S20, template S19
  and diagram S21 have all run - S7 accepts it and records the would-have-been reason as
  `S7.curated`, the same measured-not-silent pattern as `S16.curated`.  Proved on his two
  real files: the Cloud White fridge keys out of black cleanly (382x917, not degraded),
  the doors crop publishes as his full-frame product shot.
  Tests: `test_a_curated_white_product_on_a_black_ground_is_accepted`,
  `test_a_black_ground_needs_the_curated_source`,
  `test_a_curated_full_frame_photo_is_accepted_with_a_note`.
- **A failed Option 3 row now retries through the search, not through his own files
  (`main._write_failed_helper_csvs`).** The helper CSV keyed off `direct_mode`, and Option 3
  pairs with it - so a folder run's failures were written to
  `failed_products_option2.csv` with the owner's local file paths as "Images": re-running
  that sheet would refuse the same files again.  A curated run now writes
  `failed_products_option1.csv` with an empty Source URL, which makes Option 1 find
  pictures for those rows (owner 2026-10-21: "onki aik file baney gaye se... option 1 ke
  liye baney taake direct run kar diya jaye").
- **S19 stopped reading a black background as "saturated brand colour" (owner 2026-10-21,
  `image_quality.corner_patch_stats`).** The Moiz folder run accused three clean product
  photos of being themed shop frames - "4 of 4 corners are saturated brand colour" - and the
  corners were pure black: the patch mean of a JPEG-noisy black ground is something like
  (0.001, 0.0004, 0), and the ratio `(max - min) / max` divides that noise by ~0.001 and
  reports a saturation of 0.79.  A patch now carries a hue only when its channels differ in
  absolute terms (spread >= 0.08, the pipeline's own neutral-patch bar that the studio mask
  and the panel trim already use); the navy and crimson promo frames `test_s19_rejects_dark_brand_themes_too`
  protects sit at spread 0.31+ and stay accused.  This is the same class as the colour gates
  fixed the same day: a measurement that misreads a correct picture must not delete it.
  Test: `test_s19_a_black_background_is_not_read_as_brand_colour`.
- **A deep run folder no longer loses correct pictures to Windows' MAX_PATH (owner 2026-10-21,
  `csv_io.save_image_atomic`/`_win_extended`).** The Moiz folder re-run reported six rows as
  `unexpected error: [Errno 2] No such file or directory` on a file like
  `…\.dawlance-…-8eaa26_1.jpg.<32-hex>.tmp.jpg` and wrote their folders **empty** - not a gate
  refusing a bad picture, but a good picture vanishing on the way to disk. The atomic writer
  makes the temp in the final folder named `.<name>.<uuid>.tmp.jpg`, 42 chars longer than the
  file it becomes; the slug budget (`_final_path_overhead`) sizes the folder for the *final*
  path only, so a product whose final path fits (222 chars here) still overflows the 260 ceiling
  as a *temp* (264 chars) and `open` fails. Now the temp, the `fsync`, and the `os.replace`
  all run through `_win_extended`, which prefixes a plain drive path with `\\?\` so the Win32
  APIs switch to extended-length parsing and the ceiling disappears; UNC and relative paths and
  Linux/macOS are untouched. Proved against the real 264-char path that had failed: it writes a
  valid JPEG now and leaves no `.tmp.jpg` debris.
  Test: `test_save_image_atomic_survives_a_temp_path_over_windows_max_path`.
- **Every product's file name now says which product it is (`csv_io.assign_filename_prefixes`,
  2026-10-20).** The D7 code-only stem printed 60 different refrigerators as `9178LF_1.jpg`, and
  three colourways of one code overwrote each other in WordPress, which identifies media by file
  name. The stem is now `[code]_[SERIES]_[COLOUR]` - `9178LF_DAWLANCE_ICEMAX_MIST_GREEN_1.jpg` -
  in all three modes (sheet, `--direct`, `--images-dir`), built from the words `model_matching`
  already extracts, so capacity and warranty text (`14 Cu Ft`, `1.5 Ton`, `12 Years Compressor
  Warranty`) never enters a name. Rows that still fold to one name - same words, case-only, or cut
  by the 48-character budget - take a 6-char hash of their exact title; a title with no model code
  keeps the keyword+hash stem it always had. `--images-dir` goes one step further and adopts the
  stem the owner already wrote on his own pictures (`9178LF_ICEMAX_GREEN_1.png` keeps that name),
  falling back to the title only when a folder's files disagree, or when the trailing digits are a
  camera serial (`IMG_0001`) or part of the model (`DW-1051`) rather than a picture index. Measured:
  49/49 of the owner's refrigerator folders keep their written name with 0 duplicates; 630/752
  catalogue rows and 54/60 fridge rows renamed, 0 case-insensitive collisions, no row that states a
  colour lost it. Rows already in `state/checkpoint.jsonl` keep the name they published, so a rename
  needs `--no-resume` or `--redo TITLE` - and the old media in the shop then belongs to nothing.
- **A product line whose name is also a colour word is now read as a line word (owner 2026-10-20,
  `Dawlance 9173WB Chrome`).** The 9173WB is sold as the **Chrome**, the **Avante** and the
  **Digital Pro** - three products, one number - but `chrome` sits in the grey family
  (`variant_terms.COLOUR_FAMILIES`), so `_keep_name_segment` threw it away and the row's only line
  word was its own brand: no S4 veto, no sibling proof, nothing to tell a Chrome page from an Avante
  page. The evidence is now the **slot**, not a word list: a colourword written directly behind the
  model code names the line **only when the row sells a different colourway** elsewhere in the same
  title (`model_matching._line_slot_names` + `_slot_colour_words`, wired into both title readers -
  `_name_words` and the learner's `_name_segments`, so they can never disagree).
  `… 9173WB Chrome 12 Cu Ft Steel Door … - Pearl Copper` now yields
  `name_words = (dawlance, chrome)` and vetoes `9173WB Avante`, while
  `Dawlance 9173WB Chrome 12 Cu Ft Refrigerator` - where Chrome *is* the colour being sold - gains
  nothing, and a colourword **before** the code (`Haier Chrome … Pearl Blue`) is never promoted.
  Measured over 956 titles (`1800_products.csv`, the 62-row washing-machine sheet, the AC sheet,
  both refrigerator sheets): **12 rows gain a word, all 12 of them `chrome`** - no other word in the
  catalogue moves. New tests: `test_a_colour_word_in_the_line_slot_names_the_line`,
  `test_the_line_slot_word_vetoes_a_page_naming_a_sibling_line`,
  `test_a_colour_word_stays_a_colour_when_the_row_names_no_other_colour`,
  `test_a_colour_word_before_the_code_is_never_promoted`,
  `test_the_row_own_colour_in_another_spelling_is_never_promoted`.
- **A page's size is not a ticket (S22.source_lock, 2026-10-19).** The tightening above still
  let any page offering ≥2 gallery images **take the album over** from the shop that published
  first. On the washing-machine proof run that door published `hwm-85-1709.png` — another model
  — on the `851708` row, a nameless `UntitledProject_71_…` on a Midea row, and two
  `WhatsApp-Image-…` files on rows the owner had already complained about: 7 of 41 publishing
  rows still held more than one shop. A retailer page always "looks like a gallery", so the size
  of a page proves nothing about whether its files are *this* product. The exception is deleted:
  the first published picture's shop owns the album, full stop, and every other shop is admitted
  file by file only by the canonical proof (`url_anchored_to_row`) and only while the row is
  short. `album_lock_min_curated` is removed with it — no dead config field.
- **The album has one owner from its first picture (S22.source_lock, 2026-10-19).** The lock used
  to form only on a page that looked like a gallery (`album_lock_min_curated` curated candidates),
  so a row fed by two shops that each carried a single file had **no home at all** and mixed them
  freely — live in the final washing-machine dry-run: `haier-1301217GB` published one picture from
  `friendshome.pk` and one from `aysonline.pk`, the exact "one here and one from other source" the
  owner reported. Now the page that publishes the row's first picture owns the album; a page
  carrying its own gallery takes it over from a stray file (the gallery is the better album) and a
  stray never displaces a settled gallery; every other shop still fills a short row only with the
  canonical proof. Measured on that same run: 42 publishing rows, 4 of them mixed shops, and 3 of
  their pictures were second-shop files naming no code — that is the whole cost. `album_lock_min_curated`
  now decides who may *take over* an album, not who may start one (the entry above deletes that takeover).
- **`config.py` is the only home for run settings (owner 2026-10-16).** The JSON overlay is
  gone: `--config` no longer exists, `load_config_file` is deleted, and `build_config` reads its
  numbers straight from `PipelineConfig`.  Precedence is now **CLI flag > code default** — no
  file found by working directory, no second copy at the repo root that can silently diverge,
  and nothing that looks like a setting but is commented out.  Verified behaviour-neutral: the
  only live key in the old overlay was `extra_generic_name_words`, and all twenty of those words
  are already in the code's noise table; every other key was commented out.  To change a
  threshold, edit `config.PipelineConfig` (or pass the flag) — it is validated in
  `__post_init__`, which a loose JSON file never was.

### Fixed
- **`same_colour` ignored its own spelling aliases (found 2026-10-20 while building the line-slot
  rule).** `_FAMILY_OF` is keyed on the vocabulary's canonical spelling, but the arguments were
  looked up as written, so `same_colour("gray", "grey")` answered *False* - two spellings of one
  colour read as two different colours. Every caller inherits it (`shade_agreement`,
  `colour_word_conflict`), and it would have promoted a row's own colour into a line name
  (`Dawlance … 11kg Gray (DWT-11671-FLT)` sold as grey). Both sides are canonicalised through
  `_canonical` first now; `test_the_row_own_colour_in_another_spelling_is_never_promoted` guards it.
- **A page that never proved the model no longer vouches for its pictures (Safeguard S15.image,
  owner 2026-10-20, `HAIER HRF-418IDGA`).** "look at the pictures it download fallback incorrect pictures
  … I go manual there is only 1 picture not 3". Two Surmawala pages — `hrf-418-ipga-ipra` and
  `hrf-418-ifga-ifra-ifpa` — were admitted by the brand+number rule (`S15.relaxed`, "418", no exact
  proof), and each of those pages is a multi-colourway listing whose gallery also carries the 316, 276,
  346 and hash-named files of the neighbouring models. The row published 4 pictures that name none of
  its codes. A file from an unproven page is now judged like any other claim:
  `pipeline._consume_candidates` refuses it unless its own name anchors to a code of this row
  (`url_anchored_to_row`), logged as `S15.image`. Measured blast radius over the recorded runs — only a
  `relaxed` (or the new `unverified`) page can reach the limb, and the washing-machine and AC baselines
  used **0** such pages, so 0 pictures move there; the refrigerator baseline has exactly 4 relaxed
  pages and the rule removes the 4 wrong pictures on the `HRF-418IDGA` row, which becomes a loud
  `failed` ("no page matched this model") instead of another product's photos in the shop.
- **The artwork no longer prints over the template's own footer text (owner 2026-10-17,
  `midea-mf200-w90-g`).** What looked like a watermark at the bottom of the published picture was
  `assets/website/template.png`'s own `kiachahiye` caption: the product box was fitted to the frame,
  not to the space above the caption, so a tall product sat on top of it. Compositing now reserves the
  caption band and centres the product in what remains, which is also the "a little space will be
  better" ask — the unit stops at the ink instead of reaching the edge. Cropping was never the fix, and
  cropping the product to dodge the text would have cut the product.
- **S10 judges the picture we publish, not the bytes the shop served (owner 2026-10-17,
  `haier-htw-100-196s`).** "same product exactly same image repeated twice". Two of a gallery's
  downloads differed by a few pixels in the raw payload and so escaped the dHash + correlation dedup —
  and then both were cleaned, cropped to the same body box and composed onto the same square, where the
  duplicates the owner saw actually live. The fingerprint is now taken a second time on the finished
  square (`ProductDedup.record_published`), so a repeat that only appears after compositing is caught
  before it is saved.
- **The gallery colour rule finally runs — and it asks instead of guessing (Safeguard S17,
  owner 2026-10-16).** It was written on 2026-10-08, it had tests, and README §4 advertised it
  ever since as "never mix distinct colour families in one gallery" — but
  `dedup.check_color_consistency()` had **no caller** in either option while the gallery state
  beside it was filled and never consulted.  One product has one colour, so a red refrigerator
  photo beside a blue one is two products on one shop page — and `S16` cannot catch it, because
  S16 compares each picture against the *row's* colour word and says nothing when the row states
  none (`Homage … 18 Cu Ft (HRF-47662)`).  The rule now runs per candidate right after S16 in
  both options (`pipeline.gallery_colour_gate`, switch `config.color_gate_enabled`, the field
  that already governs S16), and it is deliberately slow: the family **locks only when two
  accepted pictures agree** (`gallery_colour_min_agreement`), so one odd detail shot — a white AC
  with a black louvre, a gold logo on a grey body — can never define the album or delete the
  row's real photos.  Neutral pictures are no opinion: they neither set the colour nor break it.
  Two findings from the fresh-context review are fixed in the same commit: an album that had
  already accepted two families used to publish as plain `ok` once one of them later won the
  vote (a lock is a veto, not a verdict — `colour_settled` now asks how many families were
  accepted), and the family names are no longer a hand-written table of nine, four of which the
  profiler can never emit; the rule now excludes only the labels that mean "no colour named"
  (`neutral`, `chromatic`), so a new family in `image_quality` is comparable without editing
  this file.
  **And when the album genuinely shows two colours and the row names none, nothing is guessed:**
  the row stays `PARTIAL`, carries the families it saw (`blue x1, red x1`), and the run ends with
  `COLOUR NOT STATED (n)` telling him to write the colour once.  Measured over every picture still
  on disk from past runs — 43 row albums, 196 files (composited finals plus the untouched
  `audit/raw` downloads): **0 refused, 0 asked**, so no published row shrinks today.  The shared
  "three angles of one product" fixture was redrawn in one colour, because three angles wearing
  blue, dark red and yellow are three products, and the guard was right to refuse them.  `--direct`
  refuses a wrong-family picture too, but the `COLOUR NOT STATED` question is asked only in the
  scraping option — there are no pages there to blame.
- **The shade, not the colour, decides the picture (Safeguard S20, owner 2026-10-16).**
  `9140-WB` is sold as Avante Pearl Red, Ruby Red and Diamond Red: one body, three products,
  and `Red` named none of them — so a Pearl Red row happily published a Ruby Red gallery.
  A shade word is not hand-listed: `learn_colourway_words` measures the catalogue and keeps
  a word that sits in front of a colour head, on at least two series lines, and is at home
  nowhere else (live: `hairline` 27/27, `coral` 12/12, `ruby` 4/4 against `inverter` 6/347).
  `colour_phrase` + `shade_agreement` then answer `same` / `omitted` / `different`, and only
  `different` rejects — as hard as any colour conflict, in the page match, in the tolerated
  variants and in the tail last resort (`_shade_veto`).  A shade readable only from the row's
  own sheet **URL** is reported, never matched on (`S20.unstated`: the run ends by naming the
  rows whose link says `pearl red` while their title says only `Red`), and one gallery never
  mixes two shades: the first shade-bearing page locks it and a later page selling another
  shade is skipped (`S20.mixed-shade`), while a page naming no shade stays allowed so a terse
  shop title can never starve a row.  Measured after the adversarial review fixed six
  findings: 0 of 753 rows lose their own sheet URL, 0 new blocks over 9,611 row-to-page pairs,
  382 bare-head rows unchanged, and all six protected leniencies (`/023WDC(W)`,
  `GS-12PITH17G-T3`, `DF-300-ES`, `DW-128-G`, `DW-7500C`, `(OW)`) still match.
- **A short-tailed page named for another row's series is refused (owner 2026-10-16).** The
  tail-chunk last resort armed on Midea's Xtreme and Titan pages for a row whose only free
  word after its code is a shared spec name (`T3 Plus`): those pages print `T3 Plus` too, so
  the complete tail, the series word and the tonnage all held.  The limb now also asks the
  sheet — the same evidence S4's file limb already uses (`annotate_sibling_terms`): a word
  naming exactly one *other* row names another product, and the page's own name carrying it
  refuses the rescue.  No new word table, and the two exceptions the repo already honours are
  kept: the word the limb itself demands can never veto it, and a name anchored by one of
  this row's own line words is kept (live 2026-10-11: `…Hyper-18X-T3-Super-White-Turbo-Pro`
  is decoration, not identity).  Measured on the live sheets: the three rows whose own pages
  arm today still arm, and no pair that arms today is newly refused — the guard closes the
  proven shape without touching a published row.
- **The colourway written inside a model code survives every tolerance (owner 2026-10-16).**
  A marker glued to the code (`HSU-18HWR-T3+(OW)`, `…-QC6(WB)`) names the colour of the page,
  yet three routes could still drop it: the dropped-trailing-letter tolerance, the
  brand-plus-code route and the truncation variants — each of which published a sibling
  colourway's photo for the row.  A marker is now part of the code for every one of them, and
  a page printing the same stem under a *different* short tail is refused outright
  (`_page_names_other_short_tail`) instead of merely downgraded.  The contradiction test is
  marker-scoped, not shape-scoped: an earlier over-broad version cost 86 of 598 truncation
  candidates, the shipped one costs 3.
- **`audit/raw/` is the picture the shop served (owner run 2026-10-15).** The promo-strip,
  seam and panel gates run before compose, and compose wrote *their output* to `raw/`, so the
  audit held the result of a cut and never where it landed — which is why the sliced AC above
  took a day to diagnose. `raw/` is now written from the download, and `clean/` appears beside
  it only for a row whose photo was actually cut, so the folder is evidence and not a twin of
  every file.
- **The crop keeps the whole product, and only the primary image is background-cleaned (owner
  sample 2026-10-15).** Three holes in one path. (1) `_single_body_box` reached only
  `isolation_neighbor_distance_px` (35px), so the air cooler whose white shell is invisible to
  the difference mask lost the half the mask could see: grille and black water tank sat 39px
  apart in a 990px frame and the crop published the top half alone. The reach now scales with
  the body (`isolation_neighbor_size_ratio`), and a body far away is kept when it is big enough
  to be a second unit beside the first (`isolation_product_frame_ratio`) — so a shot of three
  products keeps all three, while a 1.2% corner logo still never widens the crop. (2) The crop
  is padded 6px, so its corners land on the product's own pale shell; the corner median then
  reports the *body* as the wall and the flood erases it from the inside out — image 01 of
  `dawlance-single-door-deep-freezer-df-400-eds` lost 97.8% of its ink, three Kenwood cassettes
  ~53.7%. `clean_outer_studio_background` is now keyed on the wall colour measured *before* the
  crop and stops at the product's own edge, the largest per-channel step in the frame
  (`studio_edge_barrier`), then chokes the camera's blended rim (`studio_defringe_px`) and
  smooths the cut (`studio_matte_sigma`) so the outline is hard and even. (3) Keying now runs on
  image 01 of a row only — `isolate_main_product` gained `clean_background`, and
  `pipeline._compose_and_save` passes `index == 1` — because on detail shots it destroyed pale
  bodies and bought nothing back. Measured over the 697 crops of the live tree: erased products
  4 → 0, none newly broken; the cooler crop went 384x478 → 435x753.
- **A product's own edge line is not a shop layout seam (owner run 2026-10-15).** `S7.seam`
  trims a thin full-width divider when one side of it is empty — which the bottom edge of an
  air conditioner's body also is. It cut 520px off the Dawlance Dual Pro T3 hero and 366px off
  a Hyper 18X, and both published with the louvre sliced away. The scan now also asks what the
  *kept* side begins with: a shop's divider has blank space on both sides of itself, a
  product's edge has the product pressed against it. Measured over the five live assets the
  rule had already trimmed: the two product slices (0.90 and 0.94 product at the cut line) are
  refused, the two genuine furniture cuts (0.10 and 0.33) still trim, one no longer fires.
  Thresholds `seam_kept_band_ratio` and `seam_kept_max_content_ratio`. Refusing costs no row —
  the untrimmed photo passes S7 on its four neutral corners.
- **A camel-case file of the row's own line is ours (owner run 2026-10-12 false
  positive).** The `E-Comfort Pro KEC-1267S` row's own hero on kenwoodpakistan.pk is
  `eComfortPro.png` - the line name glued into one camel hump. The wrong-product limb
  read one unknown word (`ecomfortpro`), found nothing the row owns, and refused the
  row's own picture. File-name words are now read with camel humps as separators
  (`eComfortPro` → `comfort`), a glued word carrying an own line word is owned, and the
  anchor errs on the keep side for the same reason. Editor-default exports
  (`UntitledProject-…`) are noise, so they accuse nothing. Truly foreign names -
  hyphenated or camel-case - are still refused.
- **The shop search now asks for the row's own maker (owner run 2026-10-12).** The
  Megatron row's only key is the shared capacity code `18X`, and the unbranded query
  `_psq=18X` is exactly how a Panasonic `18XKF9` page surfaced on Surmawala. The brand
  now leads every search spelling ("Orient 18X" before "18X"); the bare spelling stays
  as a fallback term so a shop indexing only the code is still reached, and the page's
  brand gate remains the hard stop either way.
- **A file named after another maker is never this product.** The page gate judges the
  page's visible title, but a degraded fetch may print no maker at all and a direct
  image URL carries no page - S4 gained a maker limb: an image whose file name spells
  another known brand (`panasonic.jpg` on an Orient row) is rejected before download.
  Judged on the file name alone (retail CDN directories host every brand), whole-word
  only (`oriental` is not `orient`), and never anchored: no key of this row can make
  another maker's file ours.
- **A wrong product is never accepted - not even from the brand's own official sitemap
  (owner run 2026-10-11).** The `Pulse 19C Ultra White T3` row downloaded the **Innova
  19S Ultra** gallery off `orient.com.pk`. Root cause: the official-host fuzzy route in
  `verify_page` promoted any page whose URL spelled a capacity-glued shape - `19cultra`
  is spelled just as well by `blaze-19c-ultra`, `glide-19c-silver` and
  `innova-19s-white` - and that promotion overrode active rejections. The fuzzy route
  now applies only when no strict decision exists, and a capacity-glued identity
  (`19cultra`: a shared capacity family plus a word tail, detected only when the glued
  shape *starts* with the digits - a real part number like `hsu12hfpaa` that merely
  contains digits stays trusted) is evidence only when the URL also names the row's own
  line (`fuzzy_series_anchor`). The Pulse row now sees exactly one official page:
  `pulse-19c-ultra-white-t3.html` - audited against the cached orient.com.pk sitemap,
  row by row (QG → `qg-12x-aura…` only; Platinium/Titanium each their own; the bare
  `Orient 18X White` row still sees all three `…18x…` pages).
- **Once the brand-official page delivered, the search stops.** "Why then keep searching
  on other retailers' sites and downloading wrong images?" - when a page on the brand's
  own host accepts an image, the row is marked solved: every remaining candidate is
  skipped and the retailer fallback search never runs at all (the search itself is
  gated, not just the loops that consume it).
- **A multi-product retailer page publishes no wrong-product image.** Combo pages
  (Blaze/Glide/Pulse on one sheet) carry every sibling's photo. The S4 sibling limb
  gained a fourth rule: an image URL naming another product line is rejected
  (`url_names_other_product_line`) unless the file is anchored to this row - and the
  anchor was hardened: only a real part number or one of the row's own line words proves
  ownership; the shared capacity code (`18x`) and capacity-glued identities no longer
  anchor, because a sibling file of another series carries the same code.
- **The relaxed rescue tier never publishes another line's page.** The loose
  brand + number fallback still rescues a close page (`DW-7500` for `DW-7500C`, publish
  + flag), but a URL that names a foreign product line is refused outright - the same
  Innova page can no longer sneak in through the rescue door on a stray number mention.
- **Bare-capacity rows keep their wide official route.** `has_series_name` no longer
  counts brand and catalogue-noise words (`pakistan` from the marketplace pipe tail) as
  a series name, so `Orient 18X White` still matches the bounded capacity code in
  official URLs; named rows (QG, Pulse, Platinium) stay on their own line's pages only.
- **S1 now enforces the product brand on every page (owner run 2026-10-11).** The
  `Orient Split AC Hyper 18X Super White` row deferred **Panasonic** and **Gree** pages
  as tolerated spellings: a retail host is never "another manufacturer", so only the
  shared capacity code (`18X` inside `UE-18XKF9`, `18` inside `GS-18PITH`) was ever
  compared, and the variant-tail flow demoted the mismatch to "tolerated" instead of
  rejecting it. `verify_page` now reads the page's *visible name* (title, og:title, h1 -
  never the URL slug) with `page_product_brand`, and a page showing another known maker
  is strength 0 outright (`other_brand_product`): not tolerated, not rescuable by the
  relaxed fallback tier. Ten global makers (Panasonic, Mitsubishi, Daikin, Sharp,
  Toshiba, Hitachi, Whirlpool, Bosch, Siemens, Philips) join the brand lexicon.
- **The query string is no longer evidence.** Shopify search-result URLs echo the
  searched term (`?_psq=18X`), and that echo sat inside S1's URL evidence, letting
  wrong-brand pages "carry" the row's key. `_strong_fields` now judges host + path only -
  the same policy `relaxed_page_match` already had.
- **The official-brand route sees short capacity codes ("why not going to brand
  official strictly first").** `sources_for(brand)` already puts the brand's own site
  first, but `model_tokens_in_url` dropped three-letter codes (`18x` < the 4-char bar),
  so `Orient 18X White` never saw `platinium-18x-matt-white-t3-plus.html` and died on
  retailer search pages. A row whose only identity is the capacity code now matches the
  bounded `\d{2,3}+letter` shape in official URLs; named rows keep the fuzzy route that
  demands the series name, so `Megatron 12X` still matches only `titanium-12s…`.
- **The S4 sibling limb no longer kills a row's own anchored files.** Yesterday's fix
  rejected the Hyper row's own `…Hyper-18X-T3-Super-White-Turbo-Pro.webp` because
  `turbo` names another sheet row. A file carrying the row's own key/brand+code or its
  own series word is anchored (`url_anchored_to_row`) and stays; an unanchored file
  naming a sibling still dies.
- **S20 no longer rejects clean full-bleed product photos (LOW_1, Mega T3 Pro).** The
  line-art branch fired at `texture >= marketing_max_texture`, and that bar was 18.0 —
  *inside* the measured band of real badge-strip photos (23-26; the owner's LOW_1 measured
  25.9), below where any line art starts (37-55). The bar now sits in the measured gap
  (`marketing_max_texture` 32.0); emblem sheets (texture 37+) still die on S20, and the
  painted-poster branch is unchanged.
- **The panel cut works on panels that carry real artwork (LOW_11).** `marketing_panel_split`
  vetoed any band whose row variance exceeded `panel_max_row_std`, but a published panel
  carries badges, captions and a warranty block, and that artwork pushed the variance past
  the bar — so the owner's black badge panel was accepted uncut. A band now also qualifies
  when most of its pixels sit on one flat background shade (`panel_bg_flat_min`,
  `_PANEL_BG_TOLERANCE`), which a dark *product* never does (the three measured
  dark-product files are excluded by band mean alone); `panel_band_max_mean` rises 70 -> 95
  for panels that are bright with white text. The flat replica, the dark-product guard and
  the "no cut, no exemption" rule all stay exactly as they were.
- **A wrong product is never downloaded to top a row up (Orient Pulse / Innova).** The
  Pulse row's tolerated page also carried the Innova file, and no limb of S4 could see the
  difference — neither URL prints the `19C` key. S4 gains a third limb fed by the sheet
  itself: `sibling_series_terms` collects each row's unique series words (words on exactly
  one row, never variant tails or shared modifiers), and `url_names_sibling_product`
  rejects any candidate whose file name carries another row's word, whatever the page's
  status. The correct pulse image stays; a single-row or code-only sheet leaves the limb
  silent, and the tolerated DW-7500 / IFPA flows are untouched.
- **`--direct` now reads the `Images` / `image_urls` column, never `Source URL`.** Sheets that
  carry both columns (Source URL for Option 1 scraping, Images for Option 2) previously fed the
  page links into the direct downloader. `read_input(direct=True)` prefers image-column headers
  and only falls back to the generic URL headers when no image column exists; Option 1's column
  choice is unchanged. Verified live with a two-column sheet whose Source URLs point at a dead
  port: every row still published from `Images`, and the yellow template still died on S19.
- **Pinned transitive `soupsieve>=2,<3`.** A fresh install pulled soupsieve 3.0, whose stricter
  CSS parser rejects Option 1's region-strip selector list (two e2e tests failed on a clean
  checkout); 2.x is the verified major.

### Added
- **Option 2: `main.py --direct` — direct image URLs, no scraping.** The sheet's URL column is
  read as the product's own image URLs; per row the pipeline downloads, validates, composes on
  both templates, uploads (both-or-neither, banner last) and writes the same import CSVs. No
  pages are fetched and nothing is model-proven: `direct_pipeline.process_direct_product`
  orchestrates the existing Option 1 machinery (`BatchRunner` gained a keyword-only
  `processor` seam, default unchanged).
- **S19 branded-template guard** (Option 2): store theme art (iShopping-style yellow frames,
  promo banners carrying the shop logo) is skipped before compositing. Evidence is
  background-only — ≥3 of 4 saturated corner patches (`branded_template_reason`,
  `branded_corner_min_saturation`, `branded_min_saturated_corners`); white/grey studios and
  colourful products on white never fire it, and a frame-filling product stays owned by the
  S7 corner bar exactly as in Option 1. Verified end to end on real web photos (AC,
  refrigerator, washing machine, yellow sale template) and over a loopback-HTTP `--dry-run`.

### Fixed
- **A cut-out PNG/WEBP is no longer judged against a black ground** (`validate_image_bytes` now flattens
  through `to_rgb`, and the eight pixel readers in `image_quality` that called `image.convert("RGB")` use
  that same helper).  Orient's assets are cut-outs and their plate colour (``#e5e5e5``) lives in the page's
  CSS, so PIL's transparency-to-black fill invented a ground the brand never published: a white
  "Platinium Matt White" unit measured `centre mean 0.42` and S16 rejected it as
  "product area is dark - conflicts with target 'white'".  One hole, four kinds of damage, all measured on a
  faithful replica of their file (2000x1873 cut-out webp): S16 rejected (0.54 -> 0.98 after), S7's ring test
  rejected ("border ring is not a clean background"), `strip_promo_band` **invented a 489 px crop off the
  bottom of a picture that has no band**, and S14/S20/S21 - which require a white ground before they will
  say anything - went silent, i.e. marketing artwork on a cut-out could not be seen.  Fixing it at the
  decode is what the other eleven call sites (`compositing`, `dedup`) already did; no threshold moved, so
  every correct colour reject stays, and no new knob was added because "judge the ground PIL invented" is
  not a state worth preserving.  Proof: 3 unit tests + an end-to-end row that opens the saved JPG and
  checks its corners are light; their 72 published files are alpha-free RGB and re-measure identical
  (111 publish, S20 28, S21 4, 0 cuts) - `docs/riper/option1-fix-plan.md` section 12.
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

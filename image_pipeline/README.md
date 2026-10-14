# image_pipeline

Verified product-gallery extraction, compositing and hosting.

Reads a sheet (`Title`, source URL per row), fetches each product page, **proves the page shows that
exact product**, harvests only that product's own gallery photos, rejects everything that is not a
clean white-background product shot, de-duplicates within the product, isolates and normalises the
product, composites it onto the DigiMall and Alfa Mall templates, appends the fixed closing banner per
platform and uploads everything to freeimage.host — then writes the two import CSVs plus a full audit
trail.

Nothing is guessed and nothing is invented: every kept image and every rejection is one line in
`output/decisions.jsonl`, with the safeguard ID that decided it.

**Working on this code?** Read `AGENTS.md` first (the project rules file: stack, commands,
conventions, boundaries) and `docs/PROJECT_MAP.md` (where every rule lives).  Both are written for
whoever - human or agent - opens this repo next, so nothing has to be re-discovered.

---

## 1. Install

```bash
git clone <this folder>            # or copy image_pipeline/ anywhere
cd image_pipeline

python3 -m venv .venv && source .venv/bin/activate     # optional but recommended
pip install -e ".[dev]"        # runtime deps + pytest/mypy/ruff (pyproject.toml is the only list)
```

Python **3.10+** is required (`pyproject.toml: requires-python = ">=3.10"`).

Then check the artwork: one folder per platform, its template and its closing banner inside.

```
assets/website/template.png   assets/alfa/template.png   assets/digi/template.png
assets/alfa/last.png          assets/digi/last.png       # closing banners, appended LAST
```

Swap any file for the real brand art under the same name and nothing else changes.
`platforms.py` looks for `<stem>.png` then `<stem>.jpg` inside `assets/<platform>/`, so a
platform whose template is missing is skipped with a startup notice instead of stopping the
run, and a KEEP platform whose `last.png` is missing publishes without a banner.  Website
has no banner slot: its policy drops it, so `assets/website/last.png` is never looked for.
The set of platforms lives in `platforms.DEFAULT_SPECS` - a fourth platform is a row there
plus a folder here, not more code paths.

### The API key — paste it into `secrets.env`

**`secrets.env`** is a plain, visible file. There are two copies and either one works:

* the **top level of the workspace/repository** (`secrets.env` next to the `image_pipeline/` folder), and
* inside the project (`image_pipeline/secrets.env`).

Paste your key on the `FREEIMAGE_API_KEY=` line (no quotes, no spaces) and save:

```
FREEIMAGE_API_KEY=your-freeimage-host-key
```

`.env` (hidden on most systems) works exactly the same way if you prefer it, as does `env.txt`.
Search order, first file with usable values wins:

1. `--env-file PATH` (explicit),
2. `./.env`, `./secrets.env`, `./env.txt` in the folder you run the command from,
3. the same three names next to `main.py`,
4. the same three names in the folder **above** the project (so a key pasted at the top level of a
   workspace is picked up even when the command runs from inside `image_pipeline/`).

A **real exported variable always wins** over all of these, so CI/Colab secrets keep working, and an
unfilled template never shadows a filled file. All three names are in `.gitignore`, so a key can never
be committed.

The program prints one honest line about it at startup and **never logs the value**:

```
INFO pipeline.main secrets: loaded /path/to/secrets.env
INFO pipeline.main freeimage key: set            # or: not needed (dry-run) / MISSING
```

No key and no `--dry-run` ⇒ it fails fast with a clear message (there is no default and no
fallback path). `--dry-run` never needs the key.

---

## 2. Run

```bash
# the real thing (uploads)
python3 main.py --input sample_input.csv

# safe dry run: fetch, verify, download, gate, isolate, composite - but never upload
python3 main.py --input sample_input.csv --dry-run

# first 5 rows, 4 threads, force two titles to be reprocessed
python3 main.py --input sheet.xlsx --limit 5 --workers 4 --redo "Haier HSU-12 1.5 Ton" "PEL PWM-1250"

# ignore the checkpoint completely
python3 main.py --input sheet.csv --no-resume

# ask the gates about one saved file - no crawl, no sheet, no API key
python3 main.py --check-image output/images/orient-.../rejected/S20-1a2b3c4d.jpg

# Option 2: the URL column holds DIRECT image URLs - download, template and
# publish with no scraping (store theme templates are skipped by S19)
python3 main.py --direct --input my_links.csv --dry-run
```

| Flag | Meaning |
|---|---|
| `--input` | `.csv` or `.xlsx` sheet. Header aliases: `Title`/`Product Title`/`Name`/`Product Name` and `Image`/`Images`/`Source URL`/`URL`/`Source_URLs`. Several URLs per cell, separated by `\|` or a newline |
| `--assets-dir` | folder holding the 2 templates + 2 banners (default `assets`) |
| `--output-dir` | CSVs, images, logs (default `output`) |
| `--state-dir` | checkpoint + banner-URL cache (default `state`) |
| `--dry-run` | no uploads, no platform CSVs; everything else runs |
| `--direct` | Option 2: reads the sheet's `Images` / `image_urls` column as direct image URLs (a `Source URL` column on the same sheet is ignored - it belongs to Option 1); no pages are fetched, nothing is scraped; S19 skips store theme templates |
| `--redo TITLE...` | force re-process these exact titles even if the checkpoint says `ok` |
| `--limit N` | stop after N products (checkpoint still written) |
| `--workers N` | parallel product workers (default 4; per-domain requests are still rate-limited) |
| `--min-images` / `--max-images` | quotas (default 2 / 10; fewer than `--min-images` ⇒ `partial`) |
| `--target-images` | images to aim for per product (default 3). This is only how many are *sought*: a page with its own curated gallery is published in full up to `--max-images`, because the owner's "it should download all product only images" outranks the target |
| `--keep-rejected` | also write the rejected pixels to `<product>/rejected/<rule>-<hash>.jpg`, so "why did that one die" is answered from the folder, not only from `decisions.jsonl` |
| `--check-image FILE...` | no crawl, no sheet, no key: run the pipeline's real gate chain (promo band, edge seams, marketing-panel cut, S1-S21, the official rescues) on saved files and print the verdict for the brand-official bar and the retailer bar. Exit 0 = publishes, 1 = a gate stops it, 2 = unreadable |
| `--check-title TITLE` | with `--check-image`: judge the row title too, so the model and colour gates (S16) run exactly as they would for that row |
| `--no-fallback` | never search brand/retail sources; only the sheet's own URLs are used |
| `--refresh-sitemaps` | ignore `state/sitemaps/*` caches and re-crawl the sitemap indexes |
| `--no-resume` | ignore `state/checkpoint.jsonl` |
| `--log-level` | `DEBUG` / `INFO` / `WARNING` |
| `--env-file PATH` | use a specific secrets file instead of `./.env` |

Outputs land in `output/`:

```
output/
  digimall_product_images.csv   Title,Brand,Image_Urls   (| separated, no spaces, banner LAST)
  alfamall_product_images.csv   Title,Brand,Image_Urls   (| separated, no spaces, banner LAST)
  failed_products.csv           Title,Reason             (failed rows only)
  run_report.csv                Title,Status,Pages_Tried,Pages_Rejected,Images_Accepted,Note
  decisions.jsonl               one line per kept/rejected image and per rejected page
  pipeline_run.log              console log
  images/<title-slug>/raw|isolated|final_digimall|final_alfamall/
state/
  checkpoint.jsonl              append-only resume state (ok/partial skipped, failed/empty retried)
  banner_urls.json              banner content-hash -> URL (so banners upload once per run)
```

`ok` and `partial` rows are written to **both** platform CSVs; `partial` means "published, but fewer
than `min_required_images` passed the gates" (or a flag was recorded). Only `failed` rows go to
`failed_products.csv`. All CSV/JSON writes are atomic (temp file + `os.replace`).

### Colab / notebook cell

```python
#@title Run the pipeline
!git clone -q https://<your-git-host>/image_pipeline.git
%cd image_pipeline
!pip install -q curl_cffi pillow numpy scipy opencv-python-headless beautifulsoup4 lxml requests openpyxl

import os
from google.colab import userdata, files
os.environ["FREEIMAGE_API_KEY"] = userdata.get("FREEIMAGE_API_KEY")   # Colab secret

files.upload()                       # upload your sheet (Title,Images)
!python3 main.py --input sample_input.csv --dry-run --limit 3 --workers 2   # probe first
!python3 main.py --input sample_input.csv --workers 4                        # real run

from google.colab import files as colab_files
colab_files.download("output/digimall_product_images.csv")
colab_files.download("output/alfamall_product_images.csv")
colab_files.download("output/run_report.csv")
```

Resume works on Colab too: keep `state/` (mount Drive or download/upload it) and re-running skips the
rows that already finished with `ok`/`partial` and unchanged source URLs.

---

## 3. What "verified" means (the recognition rules)

Recognition is deliberately simple — two rules, no confidence scoring:

1. **Model key.** The model number from the sheet must appear on the page (title, `og:title`, any
   `<h1>`, the URL, or the body text). Separators are free (`DW-7500` = `DW 7500` = `dw7500`), glued
   spellings work (`HRF538IFGA` = `HRF-538 IFGA`) and a dropped **trailing letter** is tolerated
   (`DW-7500C` sheet ↔ `DW-7500` page). A key is never extended by characters the title does not
   contain: `HSU-12` does **not** match `HSU-12HFPAA`.
2. **Brand + code** (on by default, `brand_match_enabled=True`). When the page names the brand and
   shows the same distinctive code, that is the same product even without the brand's own prefix:
   `Haier HRF-538 IFGA` matches `Haier Refrigerator 538 IFGA INV`. Guards: the code needs
   ≥5 characters and ≥3 digits, the letters in front of it may only be a brand initial or the sheet's
   own family letters (`Haier HSF-538 IFGA` is **rejected**), and another manufacturer's domain can
   never satisfy the row.

**A different colour is a different product** (owner decision 2026-10-07, superseding the
2026-10-05 "flag it and publish" rule). A page that carries the model's own code but another
colourway - `Pearl Red` for a `Burgundy (9160-WB-GD)` row - is **rejected** as rule
`colour_conflict`, because publishing the wrong colour costs the company a penalty. The rule
compares the two *primary* colour words, so a longer spelling of the same colour (`Pearl
Burgundy`, `Champagne Gold`) still matches, two names of one finish do not count as a
difference (`chrome` ≡ `silver`, `graphite` ≡ `grey`), and a page that names no colour at all
is never rejected for one. Two chromatic words of different families (`burgundy` vs `red`,
`copper` vs `gold`) are treated as different products, because that is what the catalogue does.

**A model code's letter tail is a variant, not proof.** `HRF-538 IFGA` and `HRF-538 IFPA` are
different variants of one model, so a page printing another tail is no longer an exact match:
it becomes a *tolerated* match, which the row defers and - if nothing better turns up -
publishes flagged (`page shows variant IFPA, sheet model is IFGA`), exactly like the
dropped-trailing-letter case below. Two guards keep this from over-reaching: a page that
**omits** the tail is judged as before, and optional feature letters never discriminate
(`DSD 4890 Silver SD` ≡ `DSD 4890 Silver`). `9160-WB` and `9160LF` stay the same product - there
the letters are part of the code, not a glued-on tail.

Both signals share **one switch**: `config.variant_separation_enabled` (`False` restores
colour-blind, tail-blind matching). The vocabulary lives in `variant_terms.py` and holds no
brand, suffix or colour-code tables; a colour it has never seen simply fails open.

**Size and type differences still never reject a row** - they annotate it. `run_report.csv`
Note gets `variant mismatch (flagged): capacity_kg 10 (title) vs 12 (page)`, and the row is
still published. Photos are never rejected for colour either; colour is only used to **order**
them (matching-colour photos first).

What stays strict is the image side: S2-S15 below decide what may be published at all.

### When the sheet's URL is wrong: the fallback source chain

A row is processed from the sheet's own URLs first, **in order**, and nothing else is
contacted while one of them works. Only when the row is still unsolved — no page
matched, **or** a matched page yielded no usable image — the pipeline searches:

| Step | Source | How |
|---|---|---|
| 1 | the brand's **own** sites (HaierMall, haier.com.pk, dawlance.com.pk, TCL PK, PEL e-shop, Homage) | cached **sitemap index** (`state/sitemaps/<host>.jsonl`, 7-day TTL, `--refresh-sitemaps` to re-crawl) and the platform JSON API where one exists |
| 2 | retail **Shopify** shops (Surmawala, Modern Electronics, Telex) | `/search/suggest.json?q=<sheet model>` — one request per source |
| 3 | retail **WooCommerce** shops (Alfatah, Al Mumtaz, Hadi Electronics, Subhan Electronics) | `/wp-json/wc/store/products?search=<sheet model>` — one request per source |

Rules that make it safe: the search term is **always the sheet's model**, never a code
found on a page (so a stale URL can never "fix itself" into a successor model) — spelled
the ways a shop may know it, derived mechanically from the key itself: as printed
(`DW 7500`), hyphenated (`DW-7500`) and glued (`DW7500`); a key the sheet already
hyphenates (`DW-7500C`) also gets its spaced form (`DW 7500C`). Live reason: Surmawala's
Shopify suggest endpoint answered the hyphenated form while the sheet prints spaces, and
`q=DW-7500C` is what returns the DW-7500C listing. Every
found page is **re-verified with S15** before a single image URL is read; at most
`fallback_max_pages_per_row` (6) candidate pages, of which each source may contribute at
most `fallback_max_pages_per_source` (3), and one search request per source per row.
The per-source share is what keeps the chain priority-wise: a brand sitemap lists the
whole model family, and without it the first listing filled the budget and the retailers
were never searched (live: a Dawlance row never reached surmawala.pk, which had the
product). Within one source the candidates are **ordered, never filtered**, so the
sheet's own variant is tried first: a page whose URL or shop title carries the sheet's
colour word (`WHITE`) outranks the rest of the family, then a page whose slug carries the
model; `robots.txt` is respected; excluded hosts (`etech.pk`, `symbios.pk`, dead Zeesol
domains) and any host that failed earlier in the run are skipped. Every rescued row is
labelled: `run_report.csv` Note says `fallback used: yes` + which source, and
`decisions.jsonl` keeps `source=fallback:json-search:www.alfatah.com.pk` on each image.

#### When the link the sheet carries is the wrong page

The sheet's link is sometimes the *wrong page for the model in its title*. Live case
(2026-10-06): the row says `Dawlance Semi Automatic Washing Machine 10kg (DW-7500C)` and
its link points at the Dawlance page that **prints `DW-7500`** - and the C-slugged URL is a
404, so there is no brand page printing the model at all.

A page that only matches through the dropped-trailing-letter tolerance (S1's one approved
spelling tolerance) is therefore **not treated as the answer**:

1. the match is noted (`S15.tolerated`, "deferred as last resort") and the row keeps
   searching - brand first, then the retailers, **Surmawala first** - for a page that
   really prints the model (`q=DW-7500C` returns the C listing there);
2. a page that does print it wins and publishes its pictures, **unflagged**;
3. only if the whole chain found nothing better is the tolerated page published - status
   `PARTIAL`, note `flagged: tolerated spelling - page shows DW7500, sheet model is
   DW-7500C`, and a row in `output/review_products.csv` - never a failed row.

One switch: `config.tolerated_spelling_as_last_resort` (`False` restores the old
"the matching page is the answer" behaviour). The same rule applies to a fallback candidate
that only matches by tolerance - exact proof always outranks a tolerated spelling.

#### When a shop page shows one picture: the product's own JSON

A search result (Shopify `suggest.json`, Woo `wc/store` search) names one image; the page's
own structured data has the whole gallery. After the page's model is verified, and only
while the row is short of `target_images_per_product` (3), the pipeline fetches the page's
product JSON and merges every picture:

| Platform | Route | Read from |
|---|---|---|
| Shopify (Surmawala, Modern Electronics, Telex) | `/products/<handle>` → `<page>.json` | `product.images[].src`, `product.media[].preview_image.src`, `featured_image` |
| WooCommerce (Alfatah, Al Mumtaz, …) | `/wp-json/wc/store/v1/products?slug=<slug>` (then `/wp-json/wc/store/products?slug=`) | `images[].src` |

Both sources are canonicalised exactly like the page's own gallery, so a picture that
appears in both is one candidate, not two. Switch: `config.structured_gallery_enabled`.
The product JSON is fetched once per verified page and never for a page that already has
enough pictures; a failure is silent and the page's own gallery stands.

#### When nothing proves the model: publish with a flag

Matching the row's exact model is what S1/S3/S15 do, and the sheet's own URLs are never
relaxed. A **fallback** page, however, is accepted on the weaker evidence the owner approved
(2026-10-06): the page carries the row's identity as a whole token (`dw2500c`), or it names
the brand **and** carries the model's number (`2500` on the page for a `DW 2500 C` row).
Those rows are published and flagged rather than failed:

- `decisions.jsonl` gets `rule_id: S15.relaxed` with the reason;
- the row's status is forced to `PARTIAL`;
- `output/review_products.csv` (`Title,Status,Note`) lists them — written even with
  `--dry-run`, so the review list is ready before the images are hosted;
- switch: `config.relaxed_fallback_matching` (set `False` for strict-only).

Identities and number stems must match as **whole tokens** through the page's own
separators, so `dw7500` never matches inside `dw7500c`: a DW-7500 row is not illustrated
with the DW-7500C black/glass machine's photos.

Editing the list is data, not code: `config.source_platforms`, `config.brand_sources`,
`config.retail_shopify_sources`, `config.retail_woo_sources`, `config.excluded_hosts`.

---

## 4. Safeguards S1–S19 (each one a named function, a direct test and a rule ID)

Every decision is written to `output/decisions.jsonl` with its `rule_id`, so any image can be traced
back to the rule that kept or rejected it.

| ID | Safeguard | Function | Direct test |
|---|---|---|---|
| S1 | No page ⇒ no images (page matched to this product first) | `page_verification.verify_page()` | `test_strength0_page_yields_no_images`, `test_listing_page_is_rejected_by_s1` |
| S2 | Never take images from description/article content | `page_verification.is_in_description_region()` | `test_description_image_nested_deep_rejected`, `test_article_content_rejected`, `test_gallery_inside_tab_not_treated_as_description` |
| S3 | No images wrapped in links to another page | `page_verification.is_linked_to_other_page()` | `test_related_carousel_excluded`, `test_link_to_other_page_detected`, `test_lightbox_link_to_own_file_kept` |
| S4 | No image whose own URL belongs to another model | `model_matching.url_belongs_to_other_model()` | `test_other_model_url_rejected`, `test_neutral_and_own_model_urls_not_rejected` |
| S5 | No ads / banners / badges / marketing graphics | `url_filters.has_bad_keyword()` | `test_cartridge_not_rejected_cart_rejected`, `test_silicon_not_rejected_icon_rejected`, `test_banner_with_digits_rejected` |
| S6 | No banner-shaped images (`max/min > 3.5`) | `image_quality.has_extreme_aspect()` | `test_wide_strip_rejected` |
| S7 | Clean background only (whole border ring + ≥2 neutral bright corners) | `image_quality.border_is_clean()`, `neutral_corner_count()` | `test_white_corners_busy_middle_rejected`, `test_neutral_corner_count` |
| S7.studio | A white studio shot whose product touches the frame edges is accepted on **any** host when all 4 corners are neutral (`studio_background_rescue`, 4-corner bar) | `image_quality.studio_background_pass()` | `test_edge_touching_product_on_white_is_accepted`, `test_owner_washer_photo_is_accepted`, `test_studio_rescue_can_be_switched_off` |
| S7.official | Official verified-page curated gallery shot that fills the frame (≥2 neutral corners) | `image_quality.official_gallery_pass()` | `test_studio_shot_that_fills_the_frame_is_accepted_everywhere`, `test_official_gallery_acceptance_still_rejects_a_coloured_scene` |
| S8 | Minimum real size (short ≥180 px, long ≥350 px) | `image_quality.is_large_enough()` | `test_tiny_thumbnail_rejected` |
| S9 | No QR / barcodes (filename token **and** real decode) | `image_quality.is_qr_or_barcode()` | `test_qr_rejected`, `test_qr_filename_token_rejected` |
| S10 | De-dup **per product only** (state created inside `process_product`) | `dedup.ProductDedup` | `test_dedup_state_not_shared_across_products`, `test_dedup_state_not_shared_between_threads`, `test_same_image_resized_is_duplicate` |
| S11 | Excluded domains / non-product paths checked **before** any fetch | `url_filters.is_fetchable_product_url()` | `test_listing_and_blog_urls_not_fetched`, `test_non_http_and_blank_urls`, `test_non_product_and_dead_urls_never_fetched` |
| S12 | **Zero fallback image sources** — only a verified page's own gallery; the scoped whole-page fallback needs a code-anchored URL | `generic_gallery.extract_whole_page_fallback()` | `test_pipeline_has_no_fallback_source`, `test_no_gallery_and_no_model_urls_gives_nothing`, `test_whole_page_fallback_needs_a_verified_page_and_a_model_url` |
| S13 | Bytes must really be an image (Content-Type, no SVG/animated GIF, ≤15 MB, decode, EXIF rotation) | `image_quality.validate_image_bytes()` | `test_non_image_and_oversize_rejected`, `test_exif_rotation_applied` |
| S14 | Nothing uploaded until the selection is final; **both platforms or neither**; banner appended **last** | `pipeline.finalize_and_upload()` | `test_ok_product_urls_are_real_images_then_banner`, `test_failed_product_uploads_nothing`, `test_partial_product_gets_one_banner_per_platform` |
| — | **The template's logo band is never touched** (paste box starts below `header_safe_margin`, template composited on top, its artwork preserved) | `compositing.apply_template()`, `template_content_box()`, `header_safe_margin()` | `test_logo_band_is_byte_identical_after_compositing`, `test_product_is_never_pasted_above_the_safe_margin`, `test_template_decoration_inside_the_content_area_survives`, `test_real_asset_templates_protect_their_band` (mutation-verified) |
| S15.tolerated | A page that shows the model *without* our exact spelling (the sheet's dropped trailing letter) being published as if it proved the model; used **only when the row found zero exact images** — never to top up a partially-solved row | `model_matching.match_product` (`tolerated`) + `pipeline._visit_page` + stage-3b guard | `test_a_tolerated_spelling_is_not_an_exact_match`, `test_the_title_model_wins_over_a_wrong_sheet_link`, `test_a_tolerated_sheet_page_is_published_with_a_flag_when_nothing_else_matches`, `test_a_row_with_one_exact_image_is_never_topped_up_by_the_tolerated_page` |
| S15 | A page found by search is only a *candidate*: it must pass the same `verify_page` with the **sheet's** model before any image URL is read | `sources.SourceChain.find_candidates()` + `pipeline._visit_page()` | `test_fallback_candidate_is_verified_before_any_image_is_used`, `test_candidate_is_only_a_page_never_an_image`, `test_fallback_recovers_a_row_and_is_labelled_in_the_output` |
| S16 | Visual Color Gate: Rejects images whose dominant foreground hue conflicts with the title colourway (e.g. Purple/Red images for a Golden refrigerator); display products (TVs/monitors) exempted via `is_display_product()` | `image_quality.verify_image_color()` + `model_matching.is_display_product()` | `test_verify_image_color_rejects_conflicting_hue`, `test_verify_image_color_accepts_matching_or_neutral`, `test_is_display_product` |
| S17 | Intra-Gallery Color Consistency: Prevents mixing distinct chromatic color families within the same product gallery | `dedup.check_color_consistency()` | `test_check_color_consistency_blocks_mixed_chromatic`, `test_check_color_consistency_allows_same_or_neutral` |
| S19 | Branded-template guard (Option 2 `--direct`): a store's own theme frame (yellow iShopping-style art, promo with the shop logo) is never composited; evidence is background-only — ≥3 of 4 saturated corner patches; white/grey studios and colourful products on white never fire it | `image_quality.branded_template_reason()` | `test_s19_rejects_the_yellow_theme_template`, `test_s19_blocks_a_real_yellow_store_template`, `test_s19_needs_three_corners_one_coloured_corner_passes` |

**Zero-tolerance guarantees** (each proven by a test): no image from description/article content
(S2), no image from a link to another page (S3), no image carried between products or threads (S10).
Negative results also hold: an unverified page, a page with no gallery, a row whose images all fail
the gates — all publish **nothing** (S1, S12, S14).

Allowed image sources, and nothing else: (1) a registered brand/platform extractor, (2) a JSON-LD
`Product` node that matches this product, (3) embedded product JSON, (4) a dedicated gallery container,
(5) the scoped whole-page fallback above. The source of every kept image is recorded in
`decisions.jsonl`.

Failures that stop everything: an invalid/absent `FREEIMAGE_API_KEY` and any upload the host rejects
permanently (`PermanentAPIError`) ⇒ the checkpoint is flushed and the run aborts (nothing half-published).

---

## 5. Tuning thresholds with `--dry-run` + `decisions.jsonl`

Every number in the pipeline is a field in `config.PipelineConfig` (no magic constants), and every
rejection carries its rule ID, so calibration is a loop:

```bash
python3 main.py --input sample_input.csv --dry-run --workers 4
python3 - <<'PY'
import collections, json
counts = collections.Counter(json.loads(l)["rule_id"] for l in open("output/decisions.jsonl"))
print(counts.most_common())
PY
```

Then look at the actual reason and re-run only the affected rows:

```bash
python3 - <<'PY'
import json
for line in open("output/decisions.jsonl"):
    d = json.loads(line)
    if d["rule_id"] == "S7":
        print(d["title"][:40], "|", d["detail"], "|", d["url"][-60:])
PY
python3 main.py --input sample_input.csv --dry-run --redo "Dawlance ... (DW-7500C)" --limit 1
```

Typical calibrations (all in `config.py`):

| Symptom in the log | Field to adjust |
|---|---|
| Real photos rejected as "border ring is not a clean background" | `border_clean_min_fraction` (default 0.92), `border_bg_tolerance` (28.0), `border_std_max` (22.0) |
| Photos of products shot off-centre rejected | `min_neutral_corners` (2), `corner_patch_ratio` (0.12) |
| Good images rejected as "too small" | `min_short_side` (180), `min_long_side` (350) |
| A bad page starves other candidate pages of downloads | `image_full_download_max_per_page` (6), `image_full_download_max_per_row` (18) |
| The product's own gallery is cut off at 6 pictures | the curated-gallery allowance: a page offering a real gallery (brand **or** retailer) is read in full, bounded by `max_images_per_product` and the row budget |
| Two angles of the same product collapsed into one | `dedup_hamming_threshold` (6), `dedup_hamming_soft_threshold` (14), `dedup_corr_threshold` (0.995) |
| Product cropped too tightly / too loosely on the template | `product_fill_ratio` (0.92), `padding_px` (6), `isolation_bg_distance` (32.0) |
| Product touching the template header band | `template_top_scan_ratio` (0.35), `template_header_gap_px` (8), `template_default_top_margin_px` (90) |

Note that **tuning never weakens a safeguard**: the gates stay named functions with their tests; only
their numeric thresholds move. `--dry-run` writes no platform CSVs and uploads nothing, so it is safe
to iterate.

---

## 6. Download economy (why a run is cheap)

1. **Sheet URLs only** until the row is unsolved; then at most one search request per
   source, ≤ 6 candidate pages, and every request goes through one shared 1.2 s/host
   rate limiter with one session per host (kept alive for the run).
2. **Sitemap indexes are cached** per brand (`state/sitemaps/`), so the first row of a
   brand pays one crawl and every later row is a local lookup.
3. **Header probe before download** (`image_probe_bytes`, default 64 KB): dimensions are
   read from the first bytes (JPEG/PNG/WebP/GIF) so a 60×60 thumbnail (S8) or a 3000×100
   strip (S6) is rejected without downloading it. Measured on the live sheet.
4. **Largest-size rewrite first**: `config.UPSCALE_REWRITE_RULES` turns Haier `_60.jpg`
   into `_1000.jpg`, Shopify `_60x60` into `_1024x1024`, strips Magento `/cache/<hash>/`
   and Woo `-300x300`; the URL the page gave is the last attempt, so a rewrite can never
   lose an image that exists.
5. **Two-level download budget** — `image_full_download_max_per_page` (6) **and**
   `image_full_download_max_per_row` (18), plus the **target of 3 images**. The per-page cap exists
   because of a live finding: Dawlance's own page served only dark renders that failed S7, and those
   12 wasted downloads used to consume the row's whole allowance, so the retail candidates were never
   given a chance. Now every candidate page gets its own allowance and the row cap still bounds the
   worst case. **One exception, by owner request:** a page that offers a *curated gallery* (a gallery
   container, the brand's own extractor, JSON-LD or an embedded product payload) may be read in full —
   up to `max_images_per_product` and never past the row budget — because those are the product's own
   pictures (live: the official DW 7500 TWIN TUB CLEAR LID (WHITE) page publishes 10). Scraped pages
   and the S2/S3 audit nodes keep the strict per-page cap.

## 7. Adding a brand extractor

A brand extractor is **one file plus one decorator line** — there is no `if/elif` ladder anywhere.
The registry maps a domain substring to a function, and every candidate still has to pass the same
shared gates (an extractor can never bypass S2–S14).

```python
# gallery_extractors/bosch.py
"""Bosch (bosch-home.com.pk) gallery extractor."""

from __future__ import annotations

from gallery_extractors import ExtractionContext, candidates_from_nodes, register_extractor
from model_matching import find_matching_keys
from models import ImageCandidate


@register_extractor("bosch-home")           # any substring of the host, lowercase
def extract(context: ExtractionContext) -> list[ImageCandidate]:
    nodes = context.soup.select(".product-gallery img, .pdp-gallery .swiper-slide img")
    # keep only images that belong to this model (the shared S4 check runs later as well)
    nodes = [
        node for node in nodes
        if find_matching_keys(str(node.get("src", "")), context.keys, include_title_key=False)
    ]
    return candidates_from_nodes(nodes, context=context, source="registry:bosch")
```

Then register the module in `gallery_extractors/__init__.py` next to the existing ones:

```python
from gallery_extractors import bosch as _bosch  # noqa: E402,F401
```

Checklist when you add one:

1. Return `ImageCandidate`s with `source="registry:<brand>"` (the audit trail shows provenance).
2. Use `collect_img_urls(node, page_url)` so `srcset`, `data-*` attributes, `data:` URIs and relative
   URLs are handled the same way everywhere.
3. Never re-implement a gate: S2/S3 flags come from `dom_flags_for()`, everything else runs later.
4. Add a fixture page under `tests/fixtures/` and a test in `tests/test_gallery_extractors.py`
   (see `test_gallery_container_images_and_provenance`).
5. Verify with `python3 main.py --input sheet.csv --dry-run --limit 1` and read the `decisions.jsonl`
   lines for that page.

The three built-in extractors are `dawlance.py` (MDM code from the page URL), `haier.py`
(`.shtml` product pages) and `magento.py` (Magento/PEL media paths, prefers the full-size asset).

---

## 8. Tests and types

```bash
python3 -m ruff check .          # lint (must print: All checks passed!)
python3 -m mypy --strict .       # types (must print: Success: no issues found)
python3 -m pytest tests -q        # 330 tests, fakes only, no network
python3 -m radon cc . -n C -s --exclude "tests/*"
                                  # code health: 45 blocks at C or worse today (all
                                  # pre-existing hotspots) - a 46th means a function
                                  # gained a grade; extract a named helper instead
python3 ../verification/smoke_test_10_products.py   # the 10 real rows, every branch (offline)
```

The suite covers the S1–S14 table above, resume/checkpoint semantics (skip `ok`/`partial` with
unchanged source-URL hash, retry `failed`/empty/changed), atomic writes, input-header aliases,
`--redo`, the banner cache, `PermanentAPIError` abort behaviour, and adversarial cases (busy-centre
backgrounds, white-on-white products, description images three levels deep, QR codes, tracking pixels).

---

## 9. Folder layout

```
image_pipeline/
├── main.py                  CLI: flags -> PipelineConfig -> BatchRunner
├── config.py                every path and threshold (PipelineConfig, validated at startup)
├── models.py                frozen dataclasses (Task, ModelKeys, PageMatchResult, ImageCandidate, ...)
├── protocols.py             the interfaces (PageFetcher, ImageFetcher, ImageHost) for fakes/tests
├── fetch.py                 curl_cffi session, retries/backoff, per-domain rate limit, error classes
├── model_matching.py        the two recognition rules + colour/variant extraction
├── sources.py               S15: the fallback chain (Shopify/Woo JSON search, sitemap cache, robots)
├── page_verification.py     S1/S2/S3: verify_page, region stripping, link/description checks
├── url_filters.py           S5/S11: keyword blocklist, fetchability, canonical URLs, dedup keys, brands
├── gallery_extractors/      one file per brand + JSON-LD + embedded JSON + gallery containers
├── image_quality.py         S6/S7/S8/S9/S13 gates and `run_quality_gates`
├── dedup.py                 S10: dHash + correlation, per-product state
├── compositing.py           isolation, 1:1 canvas, unsharp, template compositing, JPEG export
├── image_host.py            freeimage.host client (upload + response classification)
├── decisions.py             decisions.jsonl writer
├── csv_io.py                input reading, atomic CSV/JSON writes, checkpoint, banner cache
├── pipeline.py              per-product pipeline, S14 upload gate, BatchRunner, report writers
├── tests/                   conftest.py (all fakes) + 11 test modules + fixtures/ HTML pages
├── assets/                  DigiMall.png, AlfaMall.png, digi_last.png, alfa_last.png
├── secrets.env              PASTE YOUR KEY HERE (visible, .gitignored)
├── secrets.example.env      committed template for secrets.env (`.env`/`env.txt` work too)
├── sample_input.csv         the 10-row sample sheet
├── AGENTS.md                the project rules file (read this before changing anything)
├── docs/PROJECT_MAP.md      module map: what each file owns, and where each rule lives
└── README.md, pyproject.toml, .gitignore
```

The run folder (`output/`, rewritten from scratch on every run) explains itself in
`output/README.md` - it is generated by the pipeline:

```
output/
├── README.md                      generated: what every file below is, plus this run's counts
├── digimall_product_images.csv    import this into DigiMall
├── alfamall_product_images.csv    import this into AlfaMall
├── failed_products.csv            Title,Reason  (rows that produced nothing)
├── run_report.csv                 one line per input row: status, pages, notes
├── decisions.jsonl                the audit trail (rule id per keep/reject)
├── pipeline_run.log               the full log
└── images/<product-slug>/
    ├── raw/          01-raw.jpg ...        exactly what was downloaded
    ├── isolated/     01-square.jpg ...     background knocked out, 1:1, sharpened
    ├── final_digimall/01-digimall.jpg ...  final picture for DigiMall
    └── final_alfamall/01-alfamall.jpg ...  final picture for AlfaMall
```

---

## 10. Assumptions

1. **Colour/variant differences are flags, not failures** (customer decision 2026-10-05): the row is
   published with a Note. Supersedes the earlier "colour conflict fails the row" choice.
2. **Body-text-only pages are normal pages** (customer decision): a page whose own title/URL does not
   carry the model but whose body mentions it keeps its gallery, still passing every image gate.
3. **Brand + code matching is ON** (customer decision): `Haier` + `538 IFGA` = `Haier HRF-538 IFGA`,
   guarded as in §3. The residual risk (two families sharing a code on a site that prints neither
   prefix) is accepted; `brand_match_enabled=False` restores strict behaviour.
4. Photos are never rejected for their colour — colour only reorders candidates.
5. `min_required_images=2`, `max_images_per_product=10`; fewer than 2 accepted images ⇒ `partial`.
   The *target* stays 3 (it stops the search for more pages); up to 10 of the product's own gallery
   pictures may be published (`--max-images` overrides).
6. Asset files are fixed names in `assets/`; placeholders are provided and are meant to be replaced.
7. Local final images are JPEG q95 on white (transparency flattened); uploads are the same bytes.
8. The banner is appended **last** per platform and only when at least one real image was published.
9. `FREEIMAGE_API_KEY` is the only credential source; it may come from the environment or from a
   secrets file — `.env`, `secrets.env` or `env.txt` (loaded by `config.load_env_file`, never
   overriding a real env var, never logged).
   Missing/blank + no `--dry-run` ⇒ immediate failure.
10. One sheet row = one product; duplicate titles are merged and processed once.

## 11. Where to edit what (maintainability map)

Every rule is one named place. Nothing is hard-coded in a stage function.

| I want to change… | Edit | Field / constant |
|---|---|---|
| the ten-row smoke test | `verification/smoke_test_10_products.py` (real titles, fixture pages, banner/share/CSV checks) |
| how many images per product | `config.py` | `target_images_per_product` (3), `min_required_images` (2), `max_images_per_product` (10) |
| the search sources, or add a shop | `config.py` | `brand_sources`, `retail_shopify_sources`, `retail_woo_sources`, `source_platforms`, `excluded_hosts` |
| fallback budgets | `config.py` | `fallback_max_pages_per_row` (6), `fallback_max_pages_per_source` (3), `fallback_max_search_per_source`, `image_full_download_max_per_page` (6) / `_max_per_row` (18), `image_probe_bytes`, `sitemap_ttl_days` |
| which candidate page of a source is tried first | `sources.py` | `rank_candidates()` (sheet colour → model-in-URL → source order; a preference, never a rejection) |
| largest-size URL rewrites | `url_filters.py` | `UPSCALE_REWRITE_RULES` (data table) |
| what counts as an ad/badge/banner URL | `url_filters.py` | `BAD_URL_KEYWORDS`, `MULTI_WORD_BAD_KEYWORDS` |
| brand list / official domains | `url_filters.py` | `KNOWN_BRANDS`, `DOMAIN_TO_BRAND`, `BRAND_DISPLAY` |
| the model-matching rules | `model_matching.py` | `match_product()` (model key → brand+code), `truncation_variants()`, `brand_match_cores()` |
| variant separation (colour, code tail) | `config.py` + `variant_terms.py` | `variant_separation_enabled` (colour rejects, a tail demotes to tolerated), `flag_variant_mismatch` (size/type flags only) |
| the `robots.txt` courtesy check | `config.py` + `sources.py` | `respect_robots`, `RobotsCache` (fallback crawling only; sheet URLs are always fetched) |
| image gates (size, border, QR, bytes) | `config.py` | the S6/S7/S8/S9/S13 blocks, one field per threshold |
| compositing (isolation, 1:1, margins) | `config.py` | the compositing block (`product_fill_ratio`, `padding_px`, `template_*`, `unsharp_*`) |
| a brand's gallery extractor | `gallery_extractors/<brand>.py` | one file + one `@register_extractor` line + 3 registry lines in `__init__.py` |
| a new safeguard | `image_quality.py` / `page_verification.py` / `sources.py` + `pipeline.py` | one named function + one test + `rule_id` in `decisions.jsonl` |
| output columns | `config.py` | `OUTPUT_HEADER`, `FAILED_HEADER`, `REPORT_HEADER` |

**The flow, stage by stage** (each stage is its own function, and each row logs its own
path as `1 recognition -> 2 sheet pages -> 3 fallback search -> 4 images -> 5 compose ->
6 upload -> 7 status`):

```
main.py (flags)                       -> PipelineConfig (all rules as fields)
BatchRunner (workers, resume)         -> process_product()   [one row]
   1 recognition     build_model_keys + detect_brand
   2 sheet pages     _visit_page()  (S11 fetchable -> fetch -> S1 verify -> extract -> _consume_candidates)
   3 fallback search SourceChain.find_candidates()  (only if unsolved) -> _visit_page() each (S15)
   4 image gates     _consume_candidates(): S5/S4/S2/S3 -> probe S6/S8 -> download -> S13 -> S6/S7/S8/S9 -> S10
   5 compose         _compose_and_save(): isolate -> 1:1 centre -> unsharp -> both templates -> save
   6 upload          finalize_and_upload(): both platforms or neither, banner last (S14)
   7 output          write_output_csvs(): digimall/alfamall (Title,Brand,Image_Urls) + failed + report
```

## 12. Known limitations

* Pages whose gallery exists only after JavaScript runs (and whose images are in no JSON payload)
  produce **no** images rather than guesses, until a brand extractor is added for that site.
* Very light-grey products on white backgrounds may be skipped: the gates are tuned to reject when
  unsure (S7).
* Thresholds for `border_is_clean` and isolation were calibrated against the sample sheet; expect one
  more calibration pass per new brand (§5).
* Logos or packaging shots that sit inside the real gallery are kept if they pass all gates — the S5
  keyword list is the only defence there.
* A page showing the same model in another colour, or with a different capacity, is published and
  flagged (§9.1) — re-uploading a corrected row is a human decision (`--redo "Title"`).
* CDN URLs with resize directives (`/media/resize/ASSET.png/1000Wx1000H/image.webp`) are de-duplicated
  by the asset segment, not the transform segment; the URL itself is never rewritten (only that URL is
  downloadable).
* S3 rejects images wrapped in links to other pages. On some shops the *promo* banner is the only
  linked image, so the row fails safely instead of importing an ad.
* A template carrying an **all-opaque alpha channel** (the shipped brand PNGs do) must still have its
  white background knocked out, or it paints over the product. The knockout is decided from pixels
  (`template_transparency_min_alpha`, default 250) and is limited to the product area so the logo band
  stays byte-identical.
* A `--dry-run` must never leave a `file://` URL behind for a live run: the banner cache is keyed
  `dry:`/`live:` and `finalize_and_upload` refuses to publish any non-`http(s)` URL.
* **Product size and position (user order, 2026-10-06):** the product is placed by its *own*
  content box, not by the padded square it travels in.  It is centred horizontally, never
  crosses the template's logo band, and fills `product_fill_ratio` (default **0.95**) of the
  template's content box along its longest side - a tall product fills the height, a wide one
  fills the width.  Live measurement on the shipped templates (38 finals, 2026-10-06 run): the longest side
  always reaches 94.3-95.0% of the content box (a tall product fills the height, a wide one the
  width), the top edge never comes closer than 22 px to the logo band, the logo band stays
  byte-identical, and horizontal centring is within 1 px (worst 2.5 px).
* **The Brand column must survive resume:** the checkpoint now stores the brand.  Live bug: every
  resumed row wrote an empty Brand (only rows processed in that run had one), because the
  checkpoint did not carry the field.
* **False negatives are the feared failure, and one was fixed live**: PEL `PWM-1250` fell through five
  pages because two *perfect* product photos were rejected by page furniture - a promo strip glued to
  the bottom edge (Surmawala) and a 5 px divider seam plus a 2 px top line (Modern Electronics). Both
  are now removed by named rules `S7.band` / `S7.seam` (each cut recorded in `decisions.jsonl`), and the
  row went from `failed` to `ok`. Nothing about S1-S14 was loosened: after the cuts every gate runs
  again, so a coloured background or a lifestyle shot is rejected exactly as before.
* Some CDNs (Dawlance's, observed live) occasionally reset HTTP/2 streams mid-download. Those are
  retried like any transient error and the per-page budget keeps them from stalling a row. Forcing
  HTTP/1.1 is **not** a workaround: the same CDN answers HTTP/1.1 with 403 and HTTP/2 with 200.

**Housekeeping note (2026-10-06):** the dead code and the development leftovers were removed in one
pass - unused helpers, the placeholder-asset generator, the old template art, the duplicate
`requirements.txt`/`.env.example` and every generated cache.  `pyproject.toml` is now the only
dependency list, `AGENTS.md` + `docs/PROJECT_MAP.md` are the only onboarding documents, and
`ruff` / `mypy --strict` / `pytest` are all green on the cleaned tree.

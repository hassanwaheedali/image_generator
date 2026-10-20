# Project map - image_pipeline

A reading order for humans and agents. Read only the section you need; `AGENTS.md` holds the rules,
`README.md` is the user manual.

## One row, end to end

```
sheet row (Title + source URLs)
  |
  1 recognition        model_matching.build_model_keys()            -> the sheet's model keys + brand
  |
  2 sheet pages        pipeline._visit_page(is_fallback=False)      -> S1/S11: does this page show
  |                    page_verification.verify_page()                 that model?  (+ S2/S3/S5)
  |
  3 fallback search    sources.SourceChain.find_candidates()        -> S15: only if step 2 found
  |                    (Shopify/Woo JSON -> sitemap -> HTML)            nothing; every candidate is
  |                                                                     re-verified in step 2 (S15);
  |                                                                     a page matching only by the
  |                                                                     tolerated spelling waits
 3b tolerated          pipeline.process_product (last resort)          -> S15.tolerated: used only
  |                                                                     if nothing prints the model
  4 images             gallery_extractors.extract_candidates()      -> the page's own gallery,
  |                    image_quality.run_quality_gates()                then S4/S5/S6/S7/S8/S9/
  |                    dedup.ProductDedup (S10)                          S12/S13 gates, per-product
  |                    pipeline._extend_with_structured_gallery()       dedup; if still short of
  |                                                                     the target, the verified
  |                                                                     page's own product JSON
  |                                                                     (Shopify/Woo) tops up
  5 compose            compositing.isolate_main_product()           -> isolate, 1:1 square, unsharp,
  |                    compositing.apply_template()                     both templates
  6 upload             pipeline.finalize_and_upload()               -> S14: nothing is uploaded
  |                    image_host.FreeImageHost                         until the set is final;
  |                                                                     banner appended last
  7 status             pipeline.write_output_csvs()                 -> ok / partial / failed
```

Every step writes its own line to `output/decisions.jsonl` (rule id + reason), and the per-row log line
`[stages] ... 1 recognition -> ... -> 7 status` shows the whole path in `output/pipeline_run.log`.

## Modules

| File | Owns | Key symbols |
| --- | --- | --- |
| `main.py` | CLI, fail-fast order (assets -> config -> sheet -> run), logging, summary, exit codes (2 = config/input error, 3 = cannot write output). Option 3 folder layout is read here too | `build_parser`, `build_config`, `check_assets`, `build_deps`, `main`, `discover_product_folders`, `_build_tasks` |
| `config.py` | **every** path and threshold, validated at startup; dependency-free of the pipeline | `PipelineConfig` (~115 fields), `load_env_file`, `ConfigError`, `OUTPUT_HEADER`, `REPORT_HEADER`, `FAILED_HEADER`, `sources_for()`, `platform_of()`, `asset_paths`, `*_csv` / `*_path` properties |
| `models.py` | frozen dataclasses shared by everything | `ProductTask`, `ModelKeys`, `PageCandidate`, `ImageCandidate`, `AcceptedImage`, `ProductResult`, `CheckpointEntry`, `Status`, `ImageRole` |
| `protocols.py` | the four network boundaries (so tests use fakes) | `PageFetcher`, `ImageFetcher`, `ImageHost`, `SourceSearcher`, `PageResponse`, `BinaryResponse` |
| `fetch.py` | the only real HTTP client for pages/images/JSON: curl_cffi session, retry+backoff, per-domain rate limit, status classification; plus Option 3's disk-only fetcher | `CurlCffiPageFetcher`, `CurlCffiImageFetcher`, `CurlCffiJsonHttp`, `LocalImageFetcher`, `DomainRateLimiter`, `FetchError`, `PermanentAPIError`, `classify_http_status` |
| `image_host.py` | freeimage.host upload + response classification | `FreeImageHost` |
| `model_matching.py` | the two recognition rules: model key (`HSU-12HFPAA`) and brand+code (`HRF-538 IFGA`); glued/spaced identities (`DW 7500 C`); truncation variants; colour/variant words; variant separation (a colour or shade conflict rejects, another code tail demotes to tolerated); the flagged relaxed fallback test; the tail-chunk last resort (a short-tailed code proved by series word + tonnage + unique tail owner); the colourway words learned from the catalogue; the digit structure of a model code (which family number a file really writes) | `match_product`, `build_model_keys`, `find_matching_keys`, `glued_model_token`, `numeric_stem`, `relaxed_page_match`, `url_belongs_to_other_model`, `extract_colour_families`, `extract_variant_facts`, `tail_chunk_match`, `annotate_tail_owners`, `code_tail_chunks`, `learn_series_differentiators`, `learn_colourway_words`, `colourway_words`, `shade_unstated_rows`, `code_digit_clusters`, `code_names_other_family_code`, `code_confirms_this_model`, `url_names_other_family_code` |
| `similarity.py` | the fuzzy half of the scored matcher: `similar()` (exact or
 fuzzy past a threshold, fail-open) and `same_digit_runs()` (letters may differ, digits may not) | `similar`, `same_digit_runs`, `ratio`, `words` |
| `textnorm.py` | the shared text normalization both matching and the variant vocabulary run on (leaf module, no project imports, so `(9149-WB)-Pearl Red` reveals both colour words) | `normalize_text`, `normalize_key` |
| `variant_terms.py` | the variant vocabulary and its pure rules: colour families + the neutral carve-out, the colour phrase (head word + the shade in front of it), model-code letter tails; no brand/suffix/colour-code tables, unknown colours fail open | `COLOUR_FAMILIES`, `NEUTRAL_COLOURS`, `colour_words`, `primary_colour_word`, `colour_phrase`, `shade_agreement`, `colour_word_conflict`, `identity_alpha_tail`, `suffix_agreement` |
| `page_verification.py` | S1/S2/S3 + the size/type flags + the colour-conflict rejection detail | `verify_page`, `strip_unwanted_regions`, `is_in_description_region`, `is_linked_to_other_page`, `page_product_name`, `MatchSettings` |
| `url_filters.py` | URL-level rules: bad keywords (S5), fetchability (S11), other-model URLs (S4), canonical/dedup keys, size rewrites, brand registry, official-host check | `has_bad_keyword`, `is_fetchable_product_url`, `dedup_key`, `upscale_candidates`, `detect_brand`, `display_brand`, `host_is_official`, `KNOWN_BRANDS`, `BAD_URL_KEYWORDS`, `UPSCALE_REWRITE_RULES` |
| `gallery_extractors/` | one file per brand + JSON-LD, embedded JSON, gallery containers, whole-page fallback | `extract_candidates`, `ExtractionContext`, `register_extractor`, `dawlance.py`, `haier.py`, `magento.py`, `json_ld.py`, `embedded_json.py`, `generic_gallery.py`, `jsonutil.py` |
| `image_quality.py` | every pixel gate: S6 shape, S7 border ring + neutral corners, S8 size, S9 QR, S13 bytes, S19 branded-template guard, S22.coherence advisory note, plus the two scoped rescues | `run_quality_gates`, `validate_image_bytes`, `probe_verdict`, `border_is_clean`, `neutral_corner_count`, `is_qr_or_barcode`, `strip_promo_band`, `trim_edge_seams`, `official_gallery_pass`, `branded_template_reason`, `album_coherence_note`, `to_rgb` |
| `direct_pipeline.py` | Option 2 (`--direct`) and Option 3 (`--images-dir`) per-row flow with no scraping: URL-identity dedup -> download (Option 3 reads the file via `LocalImageFetcher`) -> S13 bytes -> S7.band/seam cleanup -> S19 -> S8/S6/S9/S14/S7 -> S16 -> S17 -> S10 -> compose -> upload; same contract as `process_product`, reused Option 1 machinery only | `process_direct_product` |
| `dedup.py` | S10: dHash + correlation, strictly per product, judged twice - on the download and again on the published square (`record_published`); S17's `GalleryColour` beside it - the album's colour family, what it locked on and what it saw | `ProductDedup`, `GalleryColour`, `dhash`, `correlation`, `fingerprint` |
| `compositing.py` | product isolation, 1:1 canvas, unsharp, template compositing, JPEG export | `isolate_main_product`, `to_square_canvas`, `sharpen`, `apply_template`, `template_content_box`, `template_has_transparency`, `make_white_transparent`, `product_content_bbox`, `fit_product_size`, `to_jpeg_bytes` |
| `pipeline.py` | the per-row pipeline, the S14 upload gate, the album source lock (S22.source_lock: which page owns the gallery and what may still join it), the structured-gallery top-up (`_extend_with_structured_gallery`), the download budget (incl. the curated-gallery allowance), the batch runner (workers, resume, interrupt) and all writers | `PipelineDeps`, `process_product`, `finalize_and_upload`, `ensure_banner_url`, `BatchRunner`, `write_output_csvs`, `_visit_page`, `_consume_candidates`, `_page_download_allowance`, `_DownloadBudget`, `_AlbumSource`, `_source_allowed`, `_compose_and_save`, `DryRunHost`, `csv_escape` |
| `gallery_extractors/shopify_json.py` | a verified Shopify page's own product JSON: every gallery image, media preview and featured image (`<page>.json`) | `shopify_product_json_url`, `looks_like_shopify`, `parse_product_json` |
| `gallery_extractors/woo_json.py` | a verified WooCommerce page's own Store API product (`/wp-json/wc/store/v1/products?slug=…`) | `woo_store_api_urls`, `looks_like_woo`, `parse_store_products` |
| `decisions.py` | the audit trail writer | `DecisionLog.keep/reject/note` |
| `csv_io.py` | input reading (.csv/.xlsx), atomic writes, checkpoint, banner cache, hashing, slugs, file-name stems (`filename_prefix` for sheet modes - D7; `local_filename_prefix` for Option 3 - `[code]_[REST]`, digest-escaped so distinct titles never share a stem) | `read_input`, `merge_duplicate_titles`, `atomic_write_csv`, `atomic_write_text`, `save_image_atomic`, `load_checkpoint`, `append_checkpoint`, `hash_source_urls`, `sha256_bytes`, `safe_slug`, `filename_prefix`, `local_filename_prefix`, `timestamp_now`, `load_banner_cache`, `save_banner_cache` |
| `sources.py` | S15: the fallback chain (each key searched in its shop spellings via `shop_spellings`) (Shopify `suggest.json` -> Woo `wc/store` -> sitemap -> HTML), robots courtesy check, sitemap cache, per-source share of the page budget, candidate ranking, each key searched in its three shop spellings | `SourceChain`, `RobotsCache`, `SitemapCache`, `search_terms`, `rank_candidates`, `parse_shopify_suggest`, `parse_woo_products`, `parse_sitemap`, `model_tokens_in_url` |

## The safeguard registry (rule id -> function -> test)

| Rule | Protects against | Function | Test |
| --- | --- | --- | --- |
| S1 | page does not show this exact model | `page_verification.verify_page` | `tests/test_page_verification.py` |
| S2 | description/article images | `page_verification.is_in_description_region` (+ `strip_unwanted_regions`) | `test_page_verification.py` |
| S3 | images linked to another page | `page_verification.is_linked_to_other_page` | `test_page_verification.py` |
| S1 `colour_conflict` | a page printing **another colourway** of this model (owner 2026-10-07) - rejected, never rescued by `S15.relaxed` | `variant_terms.colour_word_conflict` via `model_matching.match_product` | `test_variant_terms.py`, `test_model_matching.py`, `test_page_verification.py` |
| S4 | another model's image URL | `url_filters.url_belongs_to_other_model` / `model_matching.url_belongs_to_other_model` | `test_model_matching.py` |
| S4.code | a **sibling capacity of the same number family** in the file name (`1301217E` row vs `hwm80-1217` file) - the two codes share a whole digit group of >=3 and disagree on the digits before it | `model_matching.url_names_other_family_code` (via `code_names_other_family_code`, `code_confirms_this_model`, `code_digit_clusters`) | `test_model_matching.py`, `test_pipeline_end_to_end.py` |
| S5 | ad/banner/badge keywords in the URL | `url_filters.has_bad_keyword` | `test_url_filters.py` |
| S6 | banner-shaped images | `image_quality.has_extreme_aspect` (`probe_verdict`) | `test_image_quality.py` |
| S7 | coloured / busy background | `image_quality.border_is_clean` + `neutral_corner_count` | `test_image_quality.py` |
| S7.band | promo strip glued to an edge | `image_quality.strip_promo_band` | `test_image_quality.py` |
| S7.seam | 1-2 px divider seam on an edge | `image_quality.trim_edge_seams` | `test_image_quality.py` |
| S7.official | brand's own gallery shot that fills the frame | `image_quality.official_gallery_pass` (needs `CURATED_GALLERY_SOURCES` + `host_is_official`) | `test_image_quality.py`, `test_pipeline_end_to_end.py` |
| S7.studio | universal: white studio shot whose product touches the edges, any host, 4 neutral corners | `image_quality.studio_background_pass` (`config.studio_background_rescue`) | `test_image_quality.py`, `test_pipeline_end_to_end.py` |
| S8 | tiny / thumbnail images | `image_quality.is_large_enough` (`probe_verdict`) | `test_image_quality.py` |
| S9 | QR codes / barcodes | `image_quality.is_qr_or_barcode` | `test_image_quality.py` |
| S10 | duplicates inside one product | `dedup.ProductDedup` (+ `dedup_key`) | `test_dedup.py`, `test_url_filters.py` |
| S11 | unfetchable / non-product URLs | `url_filters.is_fetchable_product_url` | `test_url_filters.py` |
| S12 | whole-page fallback without the model in the URL | `pipeline._decide_candidate` | `test_pipeline_end_to_end.py` |
| S13 | empty / truncated / broken image bytes | `image_quality.validate_image_bytes` | `test_image_quality.py` |
| S14 | half-published rows, local paths in a CSV | `pipeline.finalize_and_upload` | `test_pipeline_end_to_end.py` |
| S15 | a fallback page that shows a *different* model | `sources.SourceChain` + `pipeline._visit_page(is_fallback=True)` | `test_sources.py`, `test_pipeline_end_to_end.py` |
| S15.relaxed | a fallback page that only *probably* shows it (brand + number) - published **and flagged** (review CSV) | `model_matching.relaxed_page_match` | `test_model_matching.py`, `test_pipeline_end_to_end.py` |
| S15.tolerated | a page that shows the model *without* our exact spelling (the sheet's dropped trailing letter, or another variant tail such as `IFPA` for `IFGA`) being taken as proof instead of a flagged last resort; stage-3b consumes it **only when the row found zero exact images** (never as a top-up) | `model_matching.match_product` (`tolerated`) + `pipeline._visit_page` (`defer_tolerated`) + stage-3b guard | `test_model_matching.py`, `test_page_verification.py`, `test_pipeline_end_to_end.py` |
| S15.image | a page that reached the row only on brand + number (`S15.relaxed`) vouches for no picture: each file must carry this row's own code (`url_anchored_to_row`) | `pipeline._consume_candidates` (`page_proved_model`) | `test_pipeline_end_to_end.py` |
| S15.unverified | stage 3d, **off by default** (`last_resort_marketplace_fallback` + `last_resort_marketplace_hosts`): a row that found nothing anywhere may take a named marketplace shop's picture although its page never proved the model (codes shops write split by hyphens). Only a page *silent* about the model qualifies, the page list is filtered by the same URL guards as the canonical path (`_unverified_marketplace_pages`: named host, not excluded, a product page, sheet URLs keeping the sheet tier), every image gate still runs, and the row is capped `partial` + listed as `UNVERIFIED` | `pipeline._unverified_marketplace_pages` + `pipeline._unverified_rescue` (`_visit_page(unverified=True)`) + `_host_is_named` | `test_pipeline_end_to_end.py` |
| S1.tail / S15.tail | the **tail-chunk last resort**: a retailer dropped the series head of our code (`MSEF1C-18HRFN1-QC6` → `18HRFN1 QC6`), so the page's *own name* (`<title>`+`<h1>`) is published as a flagged `PARTIAL` only when seven guards hold at once (complete ≥2-chunk tail · a real series word, not a category noun · the row's tonnage, a slug counted once · no foreign series head on our tail · no series word that another row of the sheet owns (`annotate_sibling_terms`, the evidence S4's URL limb already uses, stood down by the same own-line-word anchor) · exactly one sheet row owns the tail · no colour conflict on an uncrowded name) — and only on a **neutral** rejection (`match_rule="none"`), never over a colour/brand veto; owner-accepted waiver for the AC rows whose sheet never prints the model | `model_matching.tail_chunk_match` + `annotate_tail_owners` + `pipeline._visit_page` (`config.tail_chunk_match_enabled`) | `test_model_matching.py`, `test_pipeline_end_to_end.py` |
| S17 | one gallery never mixes two **colour families** — the gap S16 cannot cover, because S16 only speaks when the row states a colour word. Deliberately slow: the family locks only after `gallery_colour_min_agreement` (2) accepted pictures agree, neutrals are no opinion, and a two-family gallery with no agreement is *reported* (`COLOUR NOT STATED`) rather than guessed for the owner. Written 2026-10-08, advertised, never called — wired 2026-10-16 | `dedup.GalleryColour` / `dedup.check_color_consistency` + `pipeline.gallery_colour_gate` (`config.color_gate_enabled`, both options) | `test_dedup.py`, `test_pipeline_end_to_end.py` |
| S19 | a store theme template (branded background + logo) fed as a direct image URL | `image_quality.branded_template_reason` (Option 2, `--direct`) | `test_direct_pipeline.py` |
| S20 | the **full shade** must match, not just the colour: `9140-WB` is sold as Pearl / Ruby / Diamond Red, so a `Pearl Red` row must never publish a `Ruby Red` page. Which words name a shade is **measured from the catalogue** (a word that sits in front of a colour name and is at home nowhere else), never listed; a shade either side leaves unread is no opinion, and the shade is read only from what the page itself names — never its URL | `variant_terms.colour_phrase` + `shade_agreement` via `model_matching._shade_veto`; `learn_colourway_words` | `test_variant_terms.py`, `test_model_matching.py` |
| S20.mixed-shade | one gallery never mixes two shades: a row naming a colour but **no** shade cannot veto a page for its shade, so the first shade-bearing page sets the gallery and a later page selling another shade of the same colour is skipped; a page naming no shade is never refused and never sets the shade | `pipeline._visit_page` (`gallery_colour`) over `variant_terms.shade_agreement` | `test_pipeline_end_to_end.py` |
| S20.unstated | report-only: the row states a bare colour (`Red`) while its **own** sheet link states a shade (`…pearl-red…`), so no matcher can know which was promised — the run ends by listing those rows with the shade to write in. The URL is never used to decide a match | `model_matching.shade_unstated_rows` + `main._report_unstated_shades` | `test_model_matching.py` |
| S22.source_lock | one album is one shop's gallery: the page that gives the row its **first published picture** owns the album and nothing takes it over - not even a page carrying a bigger gallery, whose size says nothing about whether its files are this product (live 2026-10-19: that exception published `hwm-85-1709.png` on the `851708` row). Keyed on the **page's** host, never the image CDN. Another shop's picture joins only while the row is short of `min_required_images` **and** it proves this row's code (`url_anchored_to_row`) - and stage 3c's last resort is held to the same rule, so a second shop's reject cannot slip in after the home is claimed | `pipeline._AlbumSource` (`settle`, `admits`) + `pipeline._source_allowed` (`config.album_source_lock_enabled`) | `test_pipeline_end_to_end.py` |
| S22.coherence | report-only: an album whose published squares split into two tight foreground-value groups is flagged in the row's note. It never rejects - measured on both recorded runs, a correct album spans as wide as a mixed one (detail shots and a shop's flash move one machine from 0.43 to 0.84), so any bar that deletes `haier-6050`'s beige pair deletes real pictures elsewhere | `image_quality.album_coherence_note` (`config.album_coherence_note_enabled`, `album_coherence_min_group`, `album_coherence_value_gap`, `album_coherence_group_spread_max`) | `test_image_quality.py`, `test_pipeline_end_to_end.py` |
| `*.fetch` | page/asset that could not be downloaded | `pipeline._visit_page` error paths | `test_pipeline_end_to_end.py` |

## Where to look for a task

| Task | Start here |
| --- | --- |
| "this row found nothing - why?" | `output/decisions.jsonl` for the title, then `README.md` §5 |
| "the product is too small / not centred" | `config.py` compositing block -> `compositing.fit_product_size` / `apply_template` |
| "a shop's gallery is missed" | `gallery_extractors/` (add a file) then `tests/fixtures/` |
| "a new source/shop must be searched" | `config.PipelineConfig.brand_sources` / `retail_shopify_sources` / `retail_woo_sources` |
| "the row never reaches the retailers" | `config.fallback_max_pages_per_source`, `sources.SourceChain.find_candidates` (per-source share), `sources.rank_candidates` |
| "only 6 pictures came back from a 10-picture gallery" | `pipeline._page_download_allowance` + `config.image_full_download_max_per_page` / `max_images_per_product` |
| "an image was rejected that looks fine" | the rule id in `decisions.jsonl` -> the table above -> one config field |
| "add a safeguard" | `AGENTS.md` -> *Patterns* |
| "resume / checkpoint semantics" | `csv_io.load_checkpoint`, `pipeline.BatchRunner._should_skip`, `tests/test_csv_io_resume.py` - |
| "upload / banner behaviour" | `pipeline.finalize_and_upload`, `ensure_banner_url`, `state/banner_urls.json` |

## Files a run creates

- `output/` - CSVs, `decisions.jsonl`, `pipeline_run.log`, `images/<product-slug>/{raw,isolated,final_digimall,final_alfamall}/`, plus a generated `README.md` that describes the folder (see `README.md` §9).
- `state/checkpoint.jsonl` - one JSON line per finished row: `{title, status, brand, urls, source_url_hash, timestamp}`. Resume skips `ok`/`partial` rows whose source-URL hash is unchanged; `failed` and changed rows are retried.
- `state/banner_urls.json` - uploaded closing-banner URLs, keyed `dry:`/`live:` so a dry run can never leak a `file://` path into a live CSV.
- `state/sitemaps/<host>.jsonl` - sitemap cache with `sitemap_ttl_days` (7) TTL, used by the fallback chain only.

## Live verification (outside the test suite)

`tests/` is offline and fake-based by design. The end-to-end proof lives in `/home/user/verification/`:
`VERIFICATION.md` (what was checked, with numbers), `sheet_digimall.jpg` / `sheet_alfamall.jpg`
(visual contact sheets of the last run's finals), `sizing.txt` (measured fill/centring per picture) and
`build_sheets.py` (regenerates the sheets from `output/`).

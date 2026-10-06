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
  |                                                                     re-verified in step 2 (S15)
  4 images             gallery_extractors.extract_candidates()      -> the page's own gallery,
  |                    image_quality.run_quality_gates()                then S4/S5/S6/S7/S8/S9/
  |                    dedup.ProductDedup (S10)                          S12/S13 gates, per-product
  |                                                                     dedup
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
| `main.py` | CLI, fail-fast order (assets -> config -> sheet -> run), logging, summary, exit codes (2 = config/input error, 3 = cannot write output) | `build_parser`, `build_config`, `check_assets`, `build_deps`, `main` |
| `config.py` | **every** path and threshold, validated at startup; dependency-free of the pipeline | `PipelineConfig` (~115 fields), `load_env_file`, `ConfigError`, `OUTPUT_HEADER`, `REPORT_HEADER`, `FAILED_HEADER`, `sources_for()`, `platform_of()`, `asset_paths`, `*_csv` / `*_path` properties |
| `models.py` | frozen dataclasses shared by everything | `ProductTask`, `ModelKeys`, `PageCandidate`, `ImageCandidate`, `AcceptedImage`, `ProductResult`, `CheckpointEntry`, `Status`, `ImageRole` |
| `protocols.py` | the four network boundaries (so tests use fakes) | `PageFetcher`, `ImageFetcher`, `ImageHost`, `SourceSearcher`, `PageResponse`, `BinaryResponse` |
| `fetch.py` | the only real HTTP client for pages/images/JSON: curl_cffi session, retry+backoff, per-domain rate limit, status classification | `CurlCffiPageFetcher`, `CurlCffiImageFetcher`, `CurlCffiJsonHttp`, `DomainRateLimiter`, `FetchError`, `PermanentAPIError`, `classify_http_status` |
| `image_host.py` | freeimage.host upload + response classification | `FreeImageHost` |
| `model_matching.py` | the two recognition rules: model key (`HSU-12HFPAA`) and brand+code (`HRF-538 IFGA`); truncation variants; colour/variant words | `match_product`, `build_model_keys`, `find_matching_keys`, `url_belongs_to_other_model`, `extract_colour_families`, `extract_variant_facts` |
| `page_verification.py` | S1/S2/S3 + the colour/variant flags | `verify_page`, `strip_unwanted_regions`, `is_in_description_region`, `is_linked_to_other_page`, `page_product_name`, `MatchSettings` |
| `url_filters.py` | URL-level rules: bad keywords (S5), fetchability (S11), other-model URLs (S4), canonical/dedup keys, size rewrites, brand registry, official-host check | `has_bad_keyword`, `is_fetchable_product_url`, `dedup_key`, `upscale_candidates`, `detect_brand`, `display_brand`, `host_is_official`, `KNOWN_BRANDS`, `BAD_URL_KEYWORDS`, `UPSCALE_REWRITE_RULES` |
| `gallery_extractors/` | one file per brand + JSON-LD, embedded JSON, gallery containers, whole-page fallback | `extract_candidates`, `ExtractionContext`, `register_extractor`, `dawlance.py`, `haier.py`, `magento.py`, `json_ld.py`, `embedded_json.py`, `generic_gallery.py`, `jsonutil.py` |
| `image_quality.py` | every pixel gate: S6 shape, S7 border ring + neutral corners, S8 size, S9 QR, S13 bytes, plus the two scoped rescues | `run_quality_gates`, `validate_image_bytes`, `probe_verdict`, `border_is_clean`, `neutral_corner_count`, `is_qr_or_barcode`, `strip_promo_band`, `trim_edge_seams`, `official_gallery_pass`, `to_rgb` |
| `dedup.py` | S10: dHash + correlation, strictly per product | `ProductDedup`, `dhash`, `correlation`, `fingerprint` |
| `compositing.py` | product isolation, 1:1 canvas, unsharp, template compositing, JPEG export | `isolate_main_product`, `to_square_canvas`, `sharpen`, `apply_template`, `template_content_box`, `template_has_transparency`, `make_white_transparent`, `product_content_bbox`, `fit_product_size`, `to_jpeg_bytes` |
| `pipeline.py` | the per-row pipeline, the S14 upload gate, the batch runner (workers, resume, interrupt) and all writers | `PipelineDeps`, `process_product`, `finalize_and_upload`, `ensure_banner_url`, `BatchRunner`, `write_output_csvs`, `_visit_page`, `_consume_candidates`, `_compose_and_save`, `DryRunHost`, `csv_escape` |
| `decisions.py` | the audit trail writer | `DecisionLog.keep/reject/note` |
| `csv_io.py` | input reading (.csv/.xlsx), atomic writes, checkpoint, banner cache, hashing, slugs | `read_input`, `merge_duplicate_titles`, `atomic_write_csv`, `atomic_write_text`, `save_image_atomic`, `load_checkpoint`, `append_checkpoint`, `hash_source_urls`, `sha256_bytes`, `safe_slug`, `timestamp_now`, `load_banner_cache`, `save_banner_cache` |
| `sources.py` | S15: the fallback chain (Shopify `suggest.json` -> Woo `wc/store` -> sitemap -> HTML), robots courtesy check, sitemap cache | `SourceChain`, `RobotsCache`, `SitemapCache`, `search_terms`, `parse_shopify_suggest`, `parse_woo_products`, `parse_sitemap`, `model_tokens_in_url` |

## The safeguard registry (rule id -> function -> test)

| Rule | Protects against | Function | Test |
| --- | --- | --- | --- |
| S1 | page does not show this exact model | `page_verification.verify_page` | `tests/test_page_verification.py` |
| S2 | description/article images | `page_verification.is_in_description_region` (+ `strip_unwanted_regions`) | `test_page_verification.py` |
| S3 | images linked to another page | `page_verification.is_linked_to_other_page` | `test_page_verification.py` |
| S4 | another model's image URL | `url_filters.url_belongs_to_other_model` / `model_matching.url_belongs_to_other_model` | `test_model_matching.py` |
| S5 | ad/banner/badge keywords in the URL | `url_filters.has_bad_keyword` | `test_url_filters.py` |
| S6 | banner-shaped images | `image_quality.has_extreme_aspect` (`probe_verdict`) | `test_image_quality.py` |
| S7 | coloured / busy background | `image_quality.border_is_clean` + `neutral_corner_count` | `test_image_quality.py` |
| S7.band | promo strip glued to an edge | `image_quality.strip_promo_band` | `test_image_quality.py` |
| S7.seam | 1-2 px divider seam on an edge | `image_quality.trim_edge_seams` | `test_image_quality.py` |
| S7.official | brand's own gallery shot that fills the frame | `image_quality.official_gallery_pass` (needs `CURATED_GALLERY_SOURCES` + `host_is_official`) | `test_image_quality.py`, `test_pipeline_end_to_end.py` |
| S8 | tiny / thumbnail images | `image_quality.is_large_enough` (`probe_verdict`) | `test_image_quality.py` |
| S9 | QR codes / barcodes | `image_quality.is_qr_or_barcode` | `test_image_quality.py` |
| S10 | duplicates inside one product | `dedup.ProductDedup` (+ `dedup_key`) | `test_dedup.py`, `test_url_filters.py` |
| S11 | unfetchable / non-product URLs | `url_filters.is_fetchable_product_url` | `test_url_filters.py` |
| S12 | whole-page fallback without the model in the URL | `pipeline._decide_candidate` | `test_pipeline_end_to_end.py` |
| S13 | empty / truncated / broken image bytes | `image_quality.validate_image_bytes` | `test_image_quality.py` |
| S14 | half-published rows, local paths in a CSV | `pipeline.finalize_and_upload` | `test_pipeline_end_to_end.py` |
| S15 | a fallback page that shows a *different* model | `sources.SourceChain` + `pipeline._visit_page(is_fallback=True)` | `test_sources.py`, `test_pipeline_end_to_end.py` |
| `*.fetch` | page/asset that could not be downloaded | `pipeline._visit_page` error paths | `test_pipeline_end_to_end.py` |

## Where to look for a task

| Task | Start here |
| --- | --- |
| "this row found nothing - why?" | `output/decisions.jsonl` for the title, then `README.md` §5 |
| "the product is too small / not centred" | `config.py` compositing block -> `compositing.fit_product_size` / `apply_template` |
| "a shop's gallery is missed" | `gallery_extractors/` (add a file) then `tests/fixtures/` |
| "a new source/shop must be searched" | `config.PipelineConfig.brand_sources` / `retail_shopify_sources` / `retail_woo_sources` |
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

# RIPER — S7 false-rejects & safeguard logic (2026-10-07)

Method: `riper-workflow` (Research → Innovate → Plan → Execute → Review), with
`debugging-and-error-recovery` (reproduce → localise → reduce → fix root cause → guard →
verify) and `python-pro` (strict typing, regression tests, ruff/mypy/pytest gates).

Live evidence supplied by the owner: a 10-product dry run on Windows plus
`decisions.jsonl`, and a hand-run probe of `https://www.surmawala.pk/cdn/shop/files/7500-c.png`
showing `Size (1200,1200)`, `Neutral corners 4/4`, `border_is_clean: False`,
`run_quality_gates -> S7 "border ring is not a clean background"`.

---

## 1. RESEARCH (read-only; every claim below was reproduced in this session)

### RC-1 (core) — S7 rejects genuine white-background shots whose product touches the frame.
- Reproduced on the repo's own `uploads/image-3.png` (the Dawlance washer photo):
  `run_quality_gates` -> `S7`, `neutral_corner_count` -> `4/4`, `border_is_clean` -> `False`.
- `border_is_clean` measures the whole outer ring against its own median and demands
  >= `border_clean_min_fraction` (0.92) within `border_bg_tolerance`. When the product's dark
  top deck / base touch the top/bottom edges, ~half the ring is product, so the fraction is
  ~0.5 -> False. The algorithm *assumes the ring is pure background*.
- The codebase already contains the correct geometric predicate for "this is a studio shot
  whose product fills the frame": `official_gallery_pass` = neutral corners. It is only
  *scoped* to official brand domains (`official_gallery=True`), so the identical clean retail
  shot (Surmawala) is rejected. **Root cause: the studio-background evidence (neutral corners)
  is gated by host, not by geometry.**

### RC-2 — S8 header-probe rejects a small variant without ever asking for the large one.
- Log: many `[reject:S8] ... too small 288x288 ... (header probe, no download)` for Shopify
  `_288x.png` URLs whose `_700x`/`_1024x` sibling exists and is what the download step would
  have used.
- Root cause: the probe runs on `candidate.url` (the smallest URL found), while the
  largest-size rewrite (`upscale_candidates`, largest-first) is only applied at *download*.
  So the probe judges the thumbnail, not the asset.
- Secondary: `UPSCALE_REWRITE_RULES` "shopify-size" needs `_WxH.ext`, so single-dimension
  `_288x.png` is never rewritten at all.

### RC-3 — non-image URLs reach the downloader and 404 (S13.fetch noise).
- Log: `[reject:S13.fetch] image https://www.telex.pk/products/2026-06-28T22:25:28+05:00 ... 404`
  and `.../products/image ... 404`.
- Root cause: `gallery_extractors/json_ld.py` and `embedded_json.py` read raw `image` fields and
  only drop `.js/.css/.json`; a page URL (no image extension) passes and burns a download.
  The DOM path already guards this with `_looks_like_image`; the JSON path does not.

### Deliberately NOT "fixed" (behaviour is correct, confirmed against tests):
- `busy_centre_image` (white corner patches over a noise frame) must stay rejected — the
  neutral-corner guard at `corner_patch_ratio` (0.12) already fails it (0/4 neutral).
- S1 (wrong model), S3 (image linked to another page), S2 (description region), S5 (logo/icon),
  S10 (duplicate) rejections in the log are correct and untouched.

## 2. INNOVATE (options considered)

| # | Idea | Verdict |
|---|------|---------|
| A | Lower `border_clean_min_fraction` | Rejected: weakens S7 globally, would admit coloured/busy backgrounds. |
| B | Universal neutral-corner studio rescue (geometry, not host), bounded to **4/4** corners + config switch | **Chosen** for RC-1. Reuses the proven `neutral_corner_count`; keeps busy/lifestyle out; mirrors the already-blessed official rescue. |
| C | Probe the largest rewrite before S8 | **Chosen** for RC-2. Probe and download then agree on the same asset. |
| D | Add Shopify single-dim `_NNNx` rewrite rule | **Chosen** (enables C for `_288x`). |
| E | Require an image extension on JSON-sourced candidate URLs | **Chosen** for RC-3, mirroring `_looks_like_image`. |

## 3. PLAN (success criteria)

1. `uploads/image-3.png` and a synthetic edge-touching white shot -> `run_quality_gates == "ok"`.
2. `busy_centre_image`, blue background, and white-corners+busy-middle all still -> `S7`.
3. Rescue is switchable (`studio_background_rescue=False` -> back to `S7`).
4. `_288x.png` Shopify URL now yields a `_1024x1024` upscale candidate; probe uses the largest URL.
5. JSON-LD / embedded-JSON page-URL `image` entries produce no candidates.
6. `ruff` clean, `mypy --strict` clean, full `pytest` green (count updated wherever docs hardcode it).

## 4. EXECUTE
Implemented in `config.py`, `image_quality.py`, `pipeline.py`, `url_filters.py`,
`gallery_extractors/json_ld.py`, `gallery_extractors/embedded_json.py` + tests. See diff.

## 5. REVIEW
See the *Verification* section appended at the bottom of this file after the gates run.

---

## 5. REVIEW (verification, measured 2026-10-07)

Success criteria from §3, checked by the suite (262 passed) and direct probes:

1. `uploads/image-3.png` -> `run_quality_gates == "ok"` (was `S7`); with
   `studio_background_rescue=False` -> `S7`.  ✔ (`test_owner_washer_photo_is_accepted`,
   `test_studio_rescue_can_be_switched_off`)
2. Adversarial controls still `S7`: `busy_centre_image` (0/4 neutral), blue background, and
   white-corners+busy-middle.  ✔ (`test_white_corners_busy_middle_rejected`,
   `test_official_gallery_acceptance_still_rejects_a_coloured_scene`)
3. Rescue switchable.  ✔
4. `upscale_candidates("..._288x.png")[0] == "..._1024x1024.png"`; pipeline probe now asks for the
   largest rewrite and never probes the thumbnail.  ✔ (`test_shopify_single_dimension_thumb_is_upscaled`,
   `test_header_probe_asks_for_the_largest_rewrite_not_the_tiny_variant`)
5. JSON-LD page-route `image` entries dropped.  ✔ (`test_jsonld_image_field_that_is_a_page_url_is_dropped`)
6. Gates: `ruff` -> *All checks passed!* · `mypy --strict` -> *Success: no issues found in 39 source
   files* · `pytest` -> **262 passed**.

### What was deliberately NOT changed
- S1 / S3 / S2 / S5 / S10 logic — the log shows them rejecting genuinely wrong assets; untouched.
- The official-gallery acceptance remains host-scoped; the new rescue is additive and stricter
  (4 corners vs 2) but universal.

### Remaining known gaps (need live page captures, out of scope offline)
- Daraz gallery images rejected as S2 (description-region) on `img.drz.lazcdn.com` — suspected
  over-broad description selector for Daraz's DOM; cannot confirm without the page HTML.
- Dawlance direct `.png` fetches hit `curl: (92) HTTP/2 stream reset`; the size-rewrite fallback
  already recovers them, but an HTTP/1.1 downgrade on curl-92 would cut retry latency.

# RIPER — tolerated page must not top up a partially-solved row (2026-10-07)

Method: `riper-workflow`; evidence = the owner's 10-product log + `decisions.jsonl`.

## 1. RESEARCH (read-only, reproduced against the code)

Symptom: the `DW-7500C` row published **5 mixed images** — 1 correct (Modern Electronics, exact
match) + 4 different-colour photos from the deferred official `DW-7500` page.

Root cause (verified, `pipeline.py` stage 3b): the deferred tolerated page was consumed whenever
`len(accepted) < min_required_images` (2), i.e. it acted as a **top-up**, while its comment and the
2026-10-06 owner decision say it is a **last resort, only when nothing better exists**. With 1
exact image the condition `1 < 2` fired and mixed in the different-variant page.

Confirmed the model check is *not* URL-only (title/og/h1/body all feed `match_product`); the page is
a deliberate near-match (tolerated), so the defect is *when* it may contribute images, not matching.

## 2. INNOVATE
- A) Run 3b only when the row has **zero** accepted images (`if accepted: break`). **Chosen** —
  matches intent, minimal, preserves last-resort rescue.
- B) New strict colour-reject guard. **Rejected this turn** — owner confirmed colour stays
  flag-only (settled 2026-10-05); the mixing complaint is the variant-page top-up, not colour per se.

## 3. PLAN (success criteria)
1. Row with ≥1 exact image never consumes a tolerated page (publishes the 1 as PARTIAL, flagged).
2. Row with zero images still uses the tolerated page (flagged PARTIAL).
3. Exact-match multi-image rows unchanged (still OK, no tolerated images).
4. ruff / mypy / full pytest green.

## 4. EXECUTE
- `pipeline.py`: 3b guard `len(accepted) >= config.min_required_images` → `if accepted: break`,
  comment updated to the owner-2026-10-07 rationale.
- Test: `test_a_row_with_one_exact_image_is_never_topped_up_by_the_tolerated_page`.

## 5. REVIEW (measured)
- New regression passes; `test_the_title_model_wins_over_a_wrong_sheet_link` (4 exact, no tolerated)
  and `test_a_tolerated_sheet_page_is_published_with_a_flag_when_nothing_else_matches` (zero → tolerated)
  both still pass → criteria 1-3 hold.
- Gates: ruff *All checks passed!* · mypy *39 files clean* · pytest **263 passed**.

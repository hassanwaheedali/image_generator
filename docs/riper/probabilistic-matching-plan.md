# RIPER + Task Breakdown — probabilistic, format-agnostic product matching (2026-10-07, rev 1)

Skills: `riper-workflow` (method) + `planning-and-task-breakdown` (task format). **Plan only — no code.**

Business driver (owner): matching must survive *any* title format and *future* colour/series names
the vocabulary has never seen, without ever publishing a wrong variant (penalty). The current rule is
boolean (match / tolerate / reject) and only reads what it already knows: an unknown colour name is
silent (silent mix), and a mid-title series word (`Daimond` vs `Cuby`) is ignored (silent mix). The
owner pointed at an attribute/probability reference implementation and to industry entity-resolution
practice and asked for the same idea, DRY, no per-product tables.

## 1. RESEARCH (read-only, verified against this repo)

What exists today (all pure, typed, tested):

- `model_matching.build_model_keys` → `ModelKeys(tokens, identities, colours, variant_facts, colour_word)`.
- `model_matching.match_product` → boolean `ProductMatch(matched, rule, tolerated, strength)`.
- `variant_terms` → colour vocabulary + `suffix_agreement` + `identity_alpha_tail` (no brand/suffix tables).
- `model_matching.extract_variant_facts` → capacity/load facts (`_UNIT_AXIS`), used only as flags today.
- No fuzzy matcher anywhere in the pipeline (`difflib` / `SequenceMatcher` are unused; adding `difflib`
  adds **no dependency** because it is stdlib).

Measured gaps (this session, shipped code, 752 distinct titles):

- same-code pairs, both name a colour: **273/304 separated**, 0 false rejects on the same-colour control.
- stem-sharing pairs that matched before: **399/538 = 74.2% separated**.
- **Unknown colour name is silent**: `Ocean Mist` vs `Maroon Blaze` → `matched=True rule=model_key`.
- **Mid-title series is ignored**: `9191 Daimond RED` vs `9191 Cuby RED` (same code+colour) would mix;
  with bare `9191` (no letters) there is no identity at all → row fails outright.
- **Series-name escape hatch**: `Dawlance Chrome …` vs `… Hairline Black` kept (3 pairs) because the
  row's only colour word is the series name and appears on the page.

Reference architectures studied:

- Industry entity resolution: parse to attributes, weight deterministic keys (model/MPN) first, score the
  rest with similarity, threshold into match / review / reject. Deterministic keys gate; fuzzy fills the
  gaps; ambiguous goes to a review queue. (Splink/Fellegi-Sunter achieved only ~0.71 precision — acceptable
  for listings, **not** for us, so we keep hard vetoes and use probability only for ranking + the grey band.)
- The owner's `sync_ac.py` reference: tiered CODE → WORDS(fuzzy, brand-gated) → SEGMENT → NEAR_MISS,
  colour resolution to disambiguate candidates, and a safe near-accept rule (digits identical, letters
  differ, unique top, no series conflict). This is the same architecture in miniature.

## 2. INNOVATE — three viable approaches

**A. Scored attribute matcher (recommended).** Parse each side into attributes positionally (brand, model
code variants, series word, colour-or-trailing-slot, size/type). Score each attribute 0..1 (exact=1,
fuzzy≥threshold=1, one-side-absent=ignore). Combine into a confidence, but with **hard vetoes**: a
positively-different series, colour, or size, or a code whose digit-runs differ, can never publish — it
demotes to the existing fallback / flagged-last-resort band. Probability is used only to rank candidates
and to place grey cases in the review band. `difflib.SequenceMatcher` supplies the fuzzy ratio. DRY (no
tables), future-proof (unknown names become opaque attributes), and reuses the existing tokenizers.

**B. Trailing-slot + series-slot vetoes only (boolean).** Keep the boolean rule but add two positional
vetoes (trailing colour slot, mid series word) with fail-open. Simpler, but stays boolean: it cannot rank
candidates, cannot express the grey band, and a reworded page risks a false veto that a similarity score
would absorb. Rejected as the end state; it is a subset of A.

**C. Learned/embedding matcher (RoBERTa etc.).** Rejected: heavy non-stdlib dependency (violates the
"no unneeded deps" rule), not explainable to the owner, and the penalty asymmetry needs explicit vetoes
that a black-box score does not give.

## 3. PLAN — task breakdown (vertical slices, ordered)

### Task 1 (S): `similarity.py` pure primitives
Add a leaf module with: `normalize`, `word_tokens`, `ratio(a,b)` (SequenceMatcher), `similar(a,b,threshold)`,
`same_digit_runs(a,b)` (letters may differ, digits must not). Unit tests incl. `9160-WB`~`9160LF` same digits,
`9160-WB`~`9178-WB` different digits.
Files: `similarity.py` (new), `tests/test_similarity.py` (new). Deps: none.

### Task 2 (M): attribute extraction
Extend `build_model_keys`/`ModelKeys` (backwards-compatible fields) with `series_word` (the alphabetic token
adjacent to the code that is not brand/colour/size, positional) and `variant_slot` (the trailing token after
the code when no known colour word is present). Pure; fail-open when absent. Unit tests: `Daimond`/`Cuby`,
`Marine Impression` slot, `Avante+` series, and a title with none.
Files: `model_matching.py`, `models.py`, `tests/test_model_matching.py`. Deps: T1.

### Task 3 (M): scored matcher core
New `score_match(keys, strong_text)` returning per-attribute verdicts + `confidence` + `veto` flags, **without
changing** `match_product`'s current boolean behaviour (additive). Vetoes: different series, different
colour/slot, different size, code digit-run mismatch. Unit tests for every case in §4 below.
Files: `similarity.py` or `model_matching.py`, `tests/test_similarity.py`. Deps: T1,T2.

### Task 4 (M): integration into the pipeline
`verify_page` / `_visit_page` consult the scored result only for the grey band (demote/flag/fallback) and for
the new vetoes; hard vetoes never publish; `variant_separation_enabled` still rolls the whole thing back.
End-to-end tests: `Marine Impression` vs `Red/Maroon Blaze`, `Ocean Mist` (unknown) separated, `Daimond` vs
`Cuby` vs `Ruby` separated, `Avante+` vs `Avante Plus` NOT false-rejected, `9160-WB` vs `9160LF` demoted not
rejected, `Chrome … Hairline Black` handled.
Files: `page_verification.py`, `pipeline.py`, `tests/test_pipeline_end_to_end.py`. Deps: T3.

### Task 5 (S): measure, docs, cleanup
Run the catalogue measurement: 0 false rejects on the same-colour control; stem separation ≥ current 74.2%;
update CHANGELOG / README / PROJECT_MAP / this plan; **remove the `marine impression` line** once the slot
covers it (zero per-name entries); ruff / mypy / pytest / radon. Deps: T4.

### Checkpoint after T2 — attributes extracted, helpers green, ruff/mypy clean, existing tests unchanged.
### Checkpoint after T4 — all §4 cases green end-to-end; full suite green; then owner review before T5 cleanup.

## 4. SUCCESS CRITERIA (measurable)

- `Marine Impression` vs `Red/Maroon Blaze` → not a clean match (separated), and `Ocean Mist` (never seen)
  also separated, with **no** new per-name table entry.
- `9191 Daimond/Cuby/Ruby` (same code+colour) → separated via the series attribute.
- `Avante+` vs `Avante Plus`, `Chrome FH` vs `Chrome` → NOT rejected (fuzzy absorbs rewording).
- `9160-WB` vs `9160LF` → demoted/flagged, never rejected.
- Same-colour control: 0 false rejects; stem separation ≥ 74.2%; tests ≥ 309 green; radon ≤ 45; ruff/mypy clean.

## 5. RISKS

- False rejects from a series/colour veto on reworded pages → mitigated by fuzzy threshold + one-side-absent
  = ignore + measuring 0 false rejects on the control before shipping.
- Performance of pairwise fuzzy scoring → mitigated by blocking on brand/code first (only score candidates
  that already share a code or brand), never all-pairs.
- Complexity budget → new logic lives in small pure functions; radon baseline (45 outside tests) must not grow.

## 6. CODE HEALTH (CodeScene-facing)

New logic in a new small leaf module; every function typed, one-line docstring, intent comments on vetoes;
reuses existing tokenizers/`variant_terms` (no duplication); `difflib` is stdlib (no dependency added); no
per-brand/per-product tables — attributes are positional.

## 7. OPEN QUESTIONS (owner)

- Fuzzy threshold (default 0.85) and confidence band cut-offs — to be tuned on the catalogue, one config
  field each.
- Should a series veto *reject* or *demote to flagged*? Recommend demote (safe), reject only for colour.
- Confirm removal of the `marine impression` line once the slot covers it.

## 8. EXECUTE / REVIEW (filled 2026-10-07)

Executed on `arena/79280a67-image-generator` with TDD: `92f61b8` tasks 1-2 · `48fb99e` task 3 ·
`9184c58` task 4 · task 5 (this commit).  Gates green at every checkpoint (319 tests, ruff +
mypy --strict clean, radon 45).  The `marine impression` vocabulary line was removed once the
name-word veto covered it (zero per-name entries).  Open-question resolutions: fuzzy threshold
0.85; series veto **demotes** to flagged (not rejects); marine line removed.

**Review:** all §4 success criteria verified - Marine/Red/Maroon and Ocean Mist separated with no
table; Daimond/Cuby/Ruby separated; Avante+~Avante Plus and 9160-WB~9160LF not rejected; 0 false
rejects on the same-colour control; separation 274 same-code pairs (>= before).  Remaining known
gap: multi-variant Shopify pages whose *title names no colour* (the colour lives in `?variant=` /
the body spec row) - not covered by this change; see `variant-separation-plan.md` residuals.

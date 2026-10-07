# RIPER + Task Breakdown — variant separation (suffix & colour) (2026-10-07, rev 2)

Skills: `riper-workflow` (method) + `planning-and-task-breakdown` (task format). **Plan only — no code.**

Business driver (owner): publishing a wrong-variant picture incurs a penalty. `HRF-538 IFGA` must not
accept an `IFPA` page, and `9160LF … Burgundy` must not accept a `… Red` page. Both currently match
at S1 (prefix-token match / synonym collapse) and mix silently.

Rev 2 adds: owner's hardcoded-colour-vocabulary concern (§3), the 1800-product corpus analysis
(Phase 0, **blocked — file missing**), and code-health / CodeScene commitments (§6).

## 1. RESEARCH (verified by probe in this session)

- `build_model_keys("… HRF-538 IFGA")` → matchable token `HRF-538`, identity `hrf538ifga`. A page
  printing `HRF-538 IFPA` matches on the bare prefix token → `strength=2, tolerated=False`.
- `DW-7500C` vs page `DW-7500` → `tolerated=True` (trailing-letter drop) → already protected (3b).
- `9160LF Burgundy` vs page `9160LF Red` → `strength=2`; `burgundy` collapses into the `red` family so
  `colour_conflict` is False → silent mix, not even flagged.
- Colour vocabulary = **one dict** `COLOUR_FAMILIES` at `model_matching.py:68`: **13 families / 53
  synonyms** (white, black, grey, gold, beige, brown, red, blue, green, purple, pink, orange, yellow).
- `extract_colour_families` on words **not** in the vocabulary → `frozenset()`, and
  `colour_conflict` then returns **False**. Verified for `teal, aqua, onyx, titanium, cyan, magenta,
  khaki, turquoise, lilac, rust`. So the table **already fails open**: an unknown colour cannot cause
  a false rejection — it only means *no protection*.
- `match_product` is already the largest function in `model_matching.py` (63 lines, ~9 branches,
  919-line file) — a CodeScene hotspot candidate, so new logic must not be dumped inside it.

## 2. INNOVATE — one rule, two signals, *omission never penalises, substitution does*

After the existing base S1 match (strength>0), apply variant agreement using only signals the title
carries:

1. **Suffix agreement.** Run the same tokenizer on the page's strong text to get page identities; for a
   page identity whose digit-stem equals the title's, compare the alpha tail:
   - same tail (`ifga`==`ifga`, incl. `538 IFGA INV` reformat) → exact;
   - **no** tail on the page (`HRF-538`) → *omitted* → tolerated (last resort, flagged) — same as DW-7500C;
   - **different** tail (`ifga` vs `ifpa`) → different variant → strength 0 (never used, never fallback).

   **Zero vocabulary.** Pure digits+letters from the existing tokenizer → works for any brand, any
   future model, no maintenance. This is the higher-value half of the fix.

2. **Chromatic-colour agreement (word level, fail-open).** Compare the *raw colour words* named by
   title vs page, neutral/achromatic words excluded:
   - both sides name a **known** colour and the words are disjoint (`burgundy` vs `red`) → strength 0;
   - same word, or one side names none (incl. one side naming only an *unknown* word) → exact.

### Why this is the simplest dynamic fix
It reuses two existing, tested machines (the identity tokenizer and the colour vocabulary) and adds one
guarded call in `match_product`. It extends the *existing* tolerated concept (omission) rather than
inventing a new path, and it cannot weaken S1 (it only ever downgrades matches).

## 3. OWNER CONCERN — "hardcoded colour families will break when new colours come"

You are right, and the current code proves both halves of it:

| Failure of a *big hardcoded table* | Verified example |
|---|---|
| synonyms get wrongly merged → conflict invisible | `burgundy` → family `red`; page `Red` → `red` → `conflict=False` (the live bug) |
| near-synonyms merged the same way | `graphite` → `grey`; `copper`/`champagne` → `gold`; `sand` → `beige` |
| unlisted word → invisible (no protection) | `teal` vs `Blue` → `conflict=False` |

Design consequences (this is what makes it non-hardcoded in the sense that matters):

1. **The decision is word-level, not family-level.** `burgundy` vs `red` is rejected because the
   *words* differ, so a richer synonym table is not needed to get it right — and the merging bug in
   row 1 disappears structurally.
2. **Fail-open by construction.** Demotion requires *both* sides to name a **known** colour. A colour
   that arrives tomorrow (`teal`, `onyx`, `titanium`…) yields an empty set → no demotion → **it can
   never reject a correct page**. Worst case = missing protection, never a wrong rejection. Locked in
   by a regression test.
3. **The table is data, not logic.** `COLOUR_FAMILIES` moves to a single data module
   (`colour_terms.py`) used for *recognition* and *neutral classification* only. Extending coverage =
   add one line of data, no logic change, no per-brand code. The neutral set is one config field
   (`variant_neutral_colours`), one field per threshold per `AGENTS.md`.
4. **The corpus tells us what to add, not guesswork.** Phase 0 extracts every colour word actually
   used across the owner's catalogue and reports which are missing from the vocabulary → the gap list
   is closed from real data, and future catalogues can be re-checked the same way.

## 4. PLAN — task breakdown (vertical slices, ordered)

### Phase 0 — Corpus analysis (owner's 1800-product sheet) — **BLOCKED: `Products.csv` not in workspace**
- **Task 0 (M):** read the catalogue, group titles by model stem, and report:
  (a) how many models have >1 colour variant in the catalogue;
  (b) every colour word used, with counts;
  (c) which of those words are **not** in `COLOUR_FAMILIES` (coverage-gap list);
  (d) for each same-model/different-colour pair, what today's `match_product` returns (how many would
  mix right now) — this quantifies the bug on the owner's real data.
  Output: `docs/riper/variant-colour-corpus-report.md` (no pipeline changes).

### Phase 1 — Foundation
- **Task 1 (S):** move `COLOUR_FAMILIES` to `colour_terms.py` (pure move, behaviour identical) and add
  pure helpers `identity_alpha_tail(identity)` + `extract_chromatic_colour_words(text)`
  (raw words, neutral families excluded, unknown words ignored). Unit tests incl. the fail-open test.
  Files: `colour_terms.py` (new), `model_matching.py`, `tests/test_model_matching.py`.

### Checkpoint 1 — helpers green, ruff/mypy clean, existing colour tests unchanged.

### Phase 2 — Core
- **Task 2 (M):** suffix agreement, behind `variant_separation_enabled`. Tests: IFGA vs IFPA→0;
  IFGA vs `HRF-538`→tolerated; `538 IFGA INV` page still exact; DW-7500C behaviour unchanged.
- **Task 3 (S):** chromatic colour-word conflict. Tests: Burgundy vs Red→0; Burgundy vs Burgundy→exact;
  no-colour page→exact; unknown colour word (`teal` vs `blue`)→exact (fail-open).

### Checkpoint 2 — model_matching suite green; full 263-test suite re-run (other models unaffected).

### Phase 3 — Integration + docs
- **Task 4 (M):** end-to-end: fallback never consumes a different-suffix/colour page; exact page wins;
  omitted-suffix still last-resort via 3b. Files: `tests/test_pipeline_end_to_end.py`.
- **Task 5 (S):** config fields (`variant_separation_enabled`, `variant_neutral_colours`) +
  `__post_init__` validation; registry rows (README/PROJECT_MAP/AGENTS), CHANGELOG. Run ruff/mypy/pytest.

### Checkpoint 3 — full gates green; review with owner.

## 5. RISKS

| Risk | Impact | Mitigation |
|---|---|---|
| brand+code tolerance regresses (`538 IFGA INV`) | High | explicit keep-match test in Task 2 |
| over-strict colour demotion when page omits colour | Med | omission never penalises; only stated-conflict rejects |
| new/unlisted colour word causes wrong rejection | Med | fail-open by construction + locked by test (§3.2) |
| moving `COLOUR_FAMILIES` breaks imports | Low | pure move, re-export, full suite in Checkpoint 1 |
| regression on other models | High | whole 263-test suite re-run at every checkpoint; new tests additive only |

## 6. CODE HEALTH (CodeScene-facing commitments)

- New logic lives in a **new small module** (`colour_terms.py` data + pure helpers) — **not** inside
  `match_product`, which is already the file's biggest function (63 lines). `match_product` gains one
  guarded call, staying under its current size.
- Every new function is small, pure, single-purpose, typed (`mypy --strict` clean) and carries a
  one-line docstring plus intent comments on the non-obvious branch.
- No duplication: the tokenizer and the colour vocabulary are reused, never re-implemented
  (`.agents/rules/dry_dynamic_algorithm.md`).
- Test coverage for every new branch, including the fail-open path; no test touches the network.

## 7. OPEN QUESTIONS (owner)
- Different suffix → strength 0 (never used) — confirm (recommended), vs tolerated.
- Neutral set (excluded from colour compare): `white, black, grey, silver, steel, chrome, pearl, ivory,
  cream, beige` — confirm.
- Re-attach `Products.csv` (any CSV with a `Title` column; the repo's sheets use `Title,Images`) so
  Phase 0 can run.

## 8. EXECUTE / REVIEW — filled in after approval.

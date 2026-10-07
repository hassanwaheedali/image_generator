# RIPER + Task Breakdown — variant separation (suffix & colour) (2026-10-07, rev 5)

Skills: `riper-workflow` (method) + `planning-and-task-breakdown` (task format). **Plan only — no code.**

Business driver (owner): publishing a wrong-variant picture incurs a penalty. `HRF-538 IFGA` must not
accept an `IFPA` page, and `9160LF … Burgundy` must not accept a `… Red` page. Both currently match
at S1 (prefix-token match / synonym collapse) and mix silently.

Rev 2 added: owner's hardcoded-colour-vocabulary concern (§3), the corpus analysis (Phase 0) and the
code-health / CodeScene commitments (§6).
**Rev 5 (this revision) records what was actually built**, because two measurements during
execution falsified the plan: (a) comparing colour words *raw* is not safe — the neutral finish
words (`pearl`, `chrome`, `silver`, …) need an explicit carve-out, and (b) comparing the title's
colour against *any* colour word on the page lets a series name ("Dawlance **Chrome** …") mask a
real `grey` vs `gold` difference. Both are fixed in the shipped rule and the numbers in §2/§7 are
the measured ones, not the estimates. Phase 0 is **done** (`variant-colour-corpus-report.md`).

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

1. **Suffix agreement — demote, never reject.** *(revised 2026-10-07 after the owner's correction:
   `9160-WB` and `9160LF` are the SAME model — the catalogue proves it, the description of the row
   titled `… 7 Cu Ft Burgundy (9160-WB-GD)` reads "Dawlance REF **9160LF** AVANTE PEARL BURGUNDY …
   7 cu ft … finished in Burgundy".)* Run the same tokenizer on the page's strong text to get page
   identities; for a page identity with the same digit-stem, compare the alpha tail:
   - same tail (`ifga`==`ifga`, incl. `538 IFGA INV` reformat) → exact;
   - either side has **no** tail (`HRF-538`, `DSD-4890`) → *omitted* → tolerated (last resort, flagged);
   - **different** tail (`ifga` vs `ifpa`, `wb` vs `lf`) → **tolerated**, i.e. deferred, used only when
     the row found nothing else, and flagged in the review CSV — **not** strength 0.

   Why not strength 0: a suffix is not reliably a variant code. Hard-rejecting it would break the
   Dawlance rows above, which today already fail to match at all (measured: `9160-WB` keys vs a
   `9160LF` page → `matched=False`). Demoting fixes both directions at once: the `IFGA`/`IFPA` page
   stops being an immediate silent match, and the `WB`/`LF` page becomes available as a flagged last
   resort instead of being lost.
2. **Colour agreement is the hard reject** — it is the signal that is actually about the picture, and
   it catches what the lenient suffix rule lets through (`9160-WB` Hairline **Black** vs the `9160LF`
   Pearl **Copper** page → colour conflict → strength 0).

   **Zero vocabulary on the suffix side.** Pure digits+letters from the existing tokenizer → works for
   any brand, any future model, no maintenance.

   Form of the colour rule *(revised after the corpus analysis: `variant-colour-corpus-report.md` §4)*:
   take the colour word that ends **last** in the text (series names such as "Dawlance **Chrome** …"
   sit early; the colourway "… Hairline **Black** (9178-WB)" sits next to the code), then
   - the title names a colour word **and** the page names at least one colour word **and** the title's
     word is not among the page's (`burgundy` vs `red`) → strength 0;
   - the page names none, or names the same word (`gray` and `grey` count as the same word) → exact.

   **Two words count as the same colour** when they are equal, or when they share a family *and* at
   least one of them is a neutral finish word (`pearl`, `ivory`, `cream`, `beige`, `white`, `black`,
   `grey`, `silver`, `steel`, `chrome`, `graphite`). So `chrome` ≡ `silver` ≡ `grey` (one finish,
   three names) while `burgundy` ≠ `red` and `copper` ≠ `gold` (two chromatic words, two products).

   The verdict is read from the page's **primary** word, with the page's full colour-word set as the
   only escape hatch (the title's word appearing anywhere means the page names it).

   **Measured with the shipped functions** over the catalogue's 752 distinct titles (ordered pairs,
   sheet row → page): of the **304** same-code pairs whose two titles both name a colour, **269 are
   separated**. Of the 35 kept, 24 name the *same* word (`white`/`white` …) and 8 are one finish
   under two names (`silver` ≡ `chrome` ≡ `grey`, `graphite` ≡ `grey`); the remaining 3 fail open
   because the row's only colour word is the series name `Chrome`, which the page repeats.
   Over the **538** ordered stem-sharing pairs that matched before this work: **395 separated
   (73.4%)** — 350 by colour alone, 20 by suffix alone, 25 by both — 143 unchanged.

**Optional feature letters are already harmless.** Verified with the tokenizer: `DSD 4890 Silver SD`,
`DSD 4890 Silver` and `DSD-4890` all produce the *same* identity `dsd4890`, because `Silver` is a
`TAIL_STOPWORDS` member (the glue stops there) and the trailing `SD` is never glued into the identity.
So an optional `SD` / `DD` / `INV` on either side cannot cause a mismatch, and needs no special case.

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
   (`variant_terms.py`) used for *recognition* and *neutral classification* only. Extending coverage =
   add one line of data, no logic change, no per-brand code. The neutral set is one config field
   (`variant_neutral_colours`), one field per threshold per `AGENTS.md`.
4. **The corpus tells us what to add, not guesswork.** Phase 0 extracts every colour word actually
   used across the owner's catalogue and reports which are missing from the vocabulary → the gap list
   is closed from real data, and future catalogues can be re-checked the same way.

## 4. PLAN — task breakdown (vertical slices, ordered)

### Phase 0 — Corpus analysis (owner's 1800-product sheet) — **DONE** (`1d66ca8`, report in `docs/riper/`)
- **Task 0 (M):** read the catalogue, group titles by model stem, and report:
  (a) how many models have >1 colour variant in the catalogue;
  (b) every colour word used, with counts;
  (c) which of those words are **not** in `COLOUR_FAMILIES` (coverage-gap list);
  (d) for each same-model/different-colour pair, what today's `match_product` returns (how many would
  mix right now) — this quantifies the bug on the owner's real data.
  Output: `docs/riper/variant-colour-corpus-report.md` (no pipeline changes).

### Phase 1 — Foundation
- **Task 1 (S) — DONE (`23f370b`)**: `COLOUR_FAMILIES` moved to **`variant_terms.py`** (not
  `colour_terms.py`, and deliberately free of project imports so it cannot cycle) with the pure
  helpers `identity_alpha_tail`, `colour_words`, `primary_colour_word`. Unit tests incl. the
  fail-open test.
  Files: `variant_terms.py` (new), `model_matching.py`, `tests/test_variant_terms.py`.

- **Task 1b (S) — DONE (`4a7a535`), unplanned**: measuring on the real catalogue exposed two holes in
  the raw-word form, fixed as `NEUTRAL_COLOURS` + `_same_colour` (finish tolerance) and a
  primary-word verdict (series names can no longer mask a real difference). That also required
  extracting `normalize_text` / `normalize_key` into **`textnorm.py`**, so punctuation like
  `(9149-WB)-Pearl Red` cannot hide a colour word any more.

### Phase 2 — Core
- **Task 2 (M) — DONE (`1dd3f9b`)**: suffix agreement, behind `variant_separation_enabled`.
  Tests: IFGA vs IFPA → **tolerated** (not 0 — see §7); IFGA vs `HRF-538` → unchanged (**deviation**:
  the plan said "tolerated", but an omitted tail is not evidence of another variant, so it is judged
  exactly as before and pinned by `test_a_page_that_omits_the_suffix_is_judged_as_before`);
  `538 IFGA INV` page still exact; DW-7500C behaviour unchanged.
- **Task 3 (S) — DONE (`4a7a535`, `6324642`)**: chromatic colour-word conflict → strength 0, rule
  `colour_conflict`. Tests: Burgundy vs Red→0; Burgundy vs Burgundy→exact; no-colour page→exact;
  unknown colour word (`teal` vs `blue`)→exact (fail-open); a colour conflict outranks a tolerated
  spelling. Carried on `ModelKeys.colour_word` so the matcher never re-parses a title it cleaned.

### Checkpoint 2 — **DONE**: model_matching suite green; full suite re-run at every commit
(285 → 291 → 296 → 307 → 308 passed, 0 failed) — other models unaffected.

### Phase 3 — Integration + docs
- **Task 4 (M) — DONE (`6324642`, `6aac5cd`)**: end-to-end coverage — a wrong-colour page fails the
  row and uploads nothing, `S15.relaxed` never rescues a colour rejection, a different-suffix page is
  deferred and published only as a flagged last resort whose note names both tails, and switching
  `variant_separation_enabled` off restores the old behaviour.
  Files: `tests/test_pipeline_end_to_end.py`, `tests/test_page_verification.py`.
- **Task 5 (S) — DONE**: **one** config field, `variant_separation_enabled`, which *replaces*
  `check_colour_match` (the superseded 2026-10-05 flag) instead of adding a second colour switch.
  `variant_neutral_colours` was **dropped**: the neutral set is vocabulary, not a threshold, so it
  lives in `variant_terms.NEUTRAL_COLOURS` next to the colour families it qualifies.
  Registry rows updated (README / PROJECT_MAP / AGENTS / REPOSITORY_UNDERSTANDING) + CHANGELOG.

### Checkpoint 3 — full gates green; review with owner.

## 5. RISKS

| Risk | Impact | Mitigation |
|---|---|---|
| brand+code tolerance regresses (`538 IFGA INV`) | High | explicit keep-match test in Task 2 |
| over-strict colour demotion when page omits colour | Med | omission never penalises; only stated-conflict rejects |
| new/unlisted colour word causes wrong rejection | Med | fail-open by construction + locked by test (§3.2) |
| moving `COLOUR_FAMILIES` breaks imports | Low | pure move, re-export, full suite in Checkpoint 1 |
| regression on other models | High | whole suite re-run at every checkpoint (308 passed); new tests additive only |

## 6. CODE HEALTH (CodeScene-facing commitments)

- New logic lives in a **new small module** (`variant_terms.py` data + pure helpers, 247 L) — **not** inside
  `match_product`, which is already the file's biggest function (63 lines). `match_product` gains one
  guarded call, staying under its current size.
- Every new function is small, pure, single-purpose, typed (`mypy --strict` clean) and carries a
  one-line docstring plus intent comments on the non-obvious branch.
- No duplication: the tokenizer and the colour vocabulary are reused, never re-implemented
  (`.agents/rules/dry_dynamic_algorithm.md`). The superseded family-level `colour_conflict()` helper
  was **deleted** rather than left beside the new rule, and normalization moved to `textnorm.py`
  instead of being re-implemented in the new module.
- Test coverage for every new branch, including the fail-open path; no test touches the network.

## 7. OPEN QUESTIONS (owner)
- ~~Different suffix → strength 0?~~ **Resolved 2026-10-07: no.** The owner corrected that `9160-WB`
  and `9160LF` are the same model, and the catalogue agrees (one row's title says `9160-WB-GD`, its
  description says `REF 9160LF`). A different suffix therefore **demotes to tolerated** (deferred,
  last resort, flagged); only a colour conflict is a hard reject.
- ~~Neutral set~~ **Reopened and resolved by measurement (rev 5).** The raw-word rule rejected
  `pearl` vs `pearl red`, `chrome` vs `silver` and `black` vs `chrome` — the same finish under two
  names — so `NEUTRAL_COLOURS` is part of the rule: two words agree when they are equal, or share a
  family while at least one is neutral. It is vocabulary in `variant_terms.py`, not a config field.
- **Superseded rule (2026-10-07):** the 2026-10-05 decision "a colour difference is a flag, not a
  failure" is withdrawn. A wrong colour now fails the row, so those rows go looking for the colour
  they asked for instead of publishing a penalised picture.

**Effect of the revised policy on the catalogue** (measured with the shipped functions; ordered
pairs, sheet row → page, over the 538 stem-sharing pairs that matched before this work): **350
hard-rejected by colour, 20 demoted by suffix, 25 by both, 143 unchanged — 395/538 = 73.4%
separated.** The residue is one-sided omission (by design), finish tolerances, and 3 pairs where the
row's only colour word is a series name. Zero false rejects on the same-colour control.

## 7b. CORPUS EVIDENCE (Phase 0, `variant-colour-corpus-report.md`)
- 1796 sheet rows → 752 distinct titles; 437 identities carry an alpha suffix.
- 43 codes / 116 titles / 149 pairs are **same code, different colour**; today **298/298** of those
  pairs match at `strength=2, rule=model_key, tolerated=False` — 100% silent mixing, nothing flagged.
- With both rules: **73.4%** of the currently-mixing stem pairs separated (395 of 538 ordered pairs;
  the earlier 82.6% came from the unsafe raw-word form and must not be quoted), **0** false rejects
  on the same-colour control; the residue is one-sided omission (by design), finish tolerances, and
  one unknown colourway name (PEL "Marine Impression", 2 pairs).
- Vocabulary coverage: all 20 colour words used in the catalogue are already known; marketing names
  are always modifier + known base colour (Ruby **Red**, Platinum **Silver**), so the gap is one name.

## 8. EXECUTE / REVIEW

**Executed** on `arena/79280a67-image-generator`, TDD (red → green at every step) with the
`incremental-implementation` skill: `23f370b` Task 1 · `4a7a535` Task 1b (colour primitives +
`textnorm`) · `1dd3f9b` Task 2 · `6324642` Task 3 + pipeline integration · `6aac5cd` Task 4.
Gates at every commit: `ruff check .` clean, `mypy --strict .` clean (42 files), `pytest tests`
**308 passed, 0 failed** (263 before this work — 45 new tests, no network).

**Deviations from the plan, all owner-visible:** omission never demotes (§4 Task 2); the neutral
carve-out exists (§7); the 2026-10-05 colour flag is superseded by a hard reject, so wrong-colour
rows now fail instead of publishing flagged; `check_colour_match` was replaced rather than joined by
a second switch; `S15.relaxed` cannot rescue a colour rejection.

**Review (Checkpoint 3):** pending owner sign-off.

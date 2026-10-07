# Phase 0 — corpus analysis: variant mixing in the owner's catalogue (2026-10-07)

**Input:** `image_pipeline/tests/fixtures/test_files/Products.csv` (commit `f2adf2f`) — 1796 rows,
columns `Title,Description,Sell Price,Original Price,Categories,Brand,Image,Weight`, 14 brands
(Dawlance 525, Haier 402, Pel 205, TCL 174, Kenwood 121, Gree 97, Orient 83, Homage 78, Ecostar 62,
Superasia 45, …).

**Method:** read-only. Every figure comes from the pipeline's own recognition code
(`build_model_keys`, `match_product`, `COLOUR_FAMILIES`), not a parallel implementation, so the
numbers describe what the shipped matcher really does. Counts are per **distinct title** — the sheet
repeats rows (1796 rows → **752 distinct titles**) and counting rows inflates every figure.

**Simulation caveat:** a page for variant B is approximated by variant B's *title*. Real pages carry
more text, so the "page names a colour" side is optimistic; the rule recommended in §7 is written to
stay safe when the page says more, not less.

---

## 1. Shape of the catalogue

| | |
|---|---|
| sheet rows / distinct titles | 1796 / **752** |
| titles with a model identity | 670 |
| identities carrying an alpha suffix (`9140wb`, `hrf578tsg`) | **437** |
| titles naming a colour word | 392 |
| — of which a **chromatic** colour (red/gold/green/…) | 162 |
| — of which **neutral only** (white/black/grey/silver/chrome/pearl) | 230 |
| titles naming **no** colour at all | **360** |
| distinct model codes | 572 (61 shared by >1 title) |
| distinct model stems | 474 (103 shared by >1 title) |

Nearly half the catalogue (360/752) states no colour in the title — which is why *omission must never
penalise*: a rule that demanded a colour on both sides would reject most rows.

## 2. Variant families found

| family | codes | titles | pairs |
|---|---|---|---|
| **2a** same full code, **different colour words** | 43 | 116 | 149 |
| **2b** same stem, **different alpha suffix** (`9160-WB` / `9160LF` / `9160-WB-GD`) | 57 | 197 | — |
| **2c** same code, same colour (control: must keep matching) | 18 | — | — |

Real examples of 2a — identical code, different picture:

```
9160-WB-GD  Red          Dawlance Avante Freezer-On-Top Refrigerator 7 Cu Ft Red (9160-WB-GD)
9160-WB-GD  Burgundy     Dawlance Avante Freezer-On-Top Refrigerator 7 Cu Ft Burgundy (9160-WB-GD)
9169-WB     Ruby Red     Dawlance Avante+ Inverter ... Ruby Red (9169-WB)
9169-WB     Silver       Dawlance Chrome Pro Freezer-On-Top Refrigerator Silver (9169-WB)
HSU-18HFT   Silver       Haier Thunder Inverter WiFi Smart AC 1.5 Ton (HSU-18HFT)-Silver
HSU-18HFT   White        Haier Thunder Inverter WiFi Smart AC 1.5 Ton (HSU-18HFT)-White
PWD-425-GD  Maroon Blaze PEL Straight 3 Taps Glass Door Water Dispenser (PWD-425-GD)-Maroon Blaze
PWD-425-GD  Red Blaze    PEL Straight 3 Taps Glass Door Water Dispenser (PWD-425-GD)-Red Blaze
```

## 3. What today's matcher does with them — the bug, quantified

All 298 ordered same-code/different-colour pairs, keys of A against the title of B:

| verdict today | pairs |
|---|---|
| `matched strength=2 rule=model_key tolerated=False` | **298 (100%)** |
| tolerated (deferred + flagged) | 0 |
| no match | 0 |

**Every colour variant of the same code is currently interchangeable, and none of it is flagged.**
This is not an edge case: 43 codes and 116 titles in this one catalogue.

## 4. Candidate colour rules, measured on those 298 pairs

| rule | separated | still mixed | same-colour pairs wrongly rejected |
|---|---|---|---|
| chromatic sets disjoint (neutrals ignored) | 122 (41%) | 176 | 0 |
| all colour-word sets disjoint | 230 (77%) | 68 | 0 |
| hybrid sets (chromatic disjoint **or** chromatic-vs-neutral-only) | 252 (85%) | 46 | 0 |
| primary colour word, compared **by family** | 256 (86%) | 42 | 0 |
| **primary colour word, compared RAW** (grey/gray unified) | **280 (94%)** | **18** | **0** |

Two findings decide the design:

1. **Family comparison defeats the purpose.** `burgundy` and `red` are the same family, as are
   `copper`/`gold` — comparing families keeps exactly the pairs the owner flagged. Measured on the
   headline case `Red (9160-WB-GD)` vs `Burgundy (9160-WB-GD)`: chromatic ✔, hybrid ✔, all-words ✔,
   **primary-by-family ✘**, primary-raw ✔. So the comparison must be **word level**.
2. **The *last* colour word in the title is the real colourway.** Series names sit early
   ("Dawlance **Chrome** Freezer-On-Top …"), the colour sits next to the code ("… Hairline **Black**
   (9178-WB)"). Taking the last colour word removes the series-name noise that costs the set-based
   rules 20-50 pairs.

Recommended form (safe when the page carries more text than a title):

> conflict ⇔ the title names a colour word **and** the page names at least one colour word **and**
> the title's colour word is not among the page's colour words (raw words, `gray`→`grey` unified).

On this corpus that is numerically identical to "primary raw" (280/298) but it never rejects when the
page lists several colours or none.

## 5. Suffix rule, measured on the 2b families

385 pairs share a stem with differing suffixes:

| case | pairs | outcome |
|---|---|---|
| both sides carry a suffix and they differ (`wb` vs `lf` vs `wbgd`) | **263** | → strength 0, never used |
| a suffix is shared | 122 | → stays an exact match (no false rejects) |
| one side omits the suffix | 0 in titles (pages do this — stays *tolerated*) | → last resort, flagged |

## 6. Combined effect over every pair that shares a model stem

458 pairs share a stem; **270 of them match today**.

| | pairs |
|---|---|
| separated by the suffix rule only | 31 |
| separated by the colour rule only | 140 |
| separated by both | 52 |
| **separated in total** | **223 = 82.6%** |
| still mixed | 47 |

The 47 break down as: **40 pairs have identical colour words** (same product written two ways — a
correct keep, not a miss), 6 are one-sided omissions (kept by design), 1 other. Only **7 residual
pairs** actually differ in colour:

```
(DW-MD4-N)            vs Black (DW-MD4-N)                     -> omission, by design
(HRF-578TSG)          vs Silver (HRF-578TSG)                  -> omission, by design
(PRLP-6360)           vs (PRLP-6360)-Graphite / -Metallic Grey-> omission, by design
Ruby Red (9169-WB)    vs Pearl Red (9169-WB)                  -> same colour word, different finish
Marine Impression     vs Maroon Blaze / Red Blaze (PWD-425-GD)-> unknown colour name (see §7)
```

## 7. The hardcoded-vocabulary question, answered with data

Colour words actually used in the catalogue (all 20 are already known to `COLOUR_FAMILIES`):

```
99 white*   63 black*   54 red      38 silver*  32 grey*    25 chrome*  20 maroon
19 purple   17 green    15 pearl*   14 golden   11 gold      9 blue      5 burgundy
 5 brown     5 copper    3 gray*     2 graphite  1 champagne 1 olive      (* = neutral set)
```

Marketing names are always *modifier + known base colour* — Ruby **Red**, Emerald **Green**, Mist
**Green**, Luxe **Brown**, Platinum **Silver**, Metallic **Gold**, Hairline **Black**, Pearl
**Copper**, Coral **Red** — so the base word is found even when the phrase is not in the table.

**Exactly one colourway in 752 titles is outside the vocabulary: PEL's "Marine Impression".** That is
the whole gap, and it costs 2 of the 298 pairs.

Design consequences (already in `variant-separation-plan.md` §3):

- the decision is **word level**, so a richer table is not needed to get Burgundy/Red right;
- **fail open**: an unlisted colour name yields no colour word → no conflict → it can never reject a
  correct page. Worst case is a missed protection (measured: 2 pairs), never a wrong rejection;
- the table stays **data, not logic** (`colour_terms.py`), so adding "marine impression" is one line;
- the **suffix rule needs no vocabulary at all** and independently separates 263 pairs.

## 8. Data-quality notes found on the way

- 1796 rows contain only **752 distinct titles** — the sheet has heavy duplication.
- 1 title carries mojibake: `PEL Kitchen Pro Grill Microwave Oven (PMOâ€“23 Kitchen Pro GR)`
  (en-dash mis-decoded). Harmless for matching here, worth fixing at export.

## 9. What this changes in the plan

1. Colour signal = **primary (last) colour word, compared raw**, with the page side treated as a set
   (title word absent from a non-empty page set ⇒ conflict). Replaces the "chromatic sets disjoint"
   wording in `variant-separation-plan.md` §2.2 — same spirit, measured better (280 vs 122).
2. Neutral list no longer decides the outcome, so the open question about it disappears; it is only
   used to pick the primary word when a title names several.
3. Expected effect on this catalogue: **82.6% of today's silent mixes separated, 0 false rejects** on
   the same-colour control, remainder are omissions or a single unknown colourway.

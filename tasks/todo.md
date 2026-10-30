# Master Todo Checklist

- [x] Phase 1: Foundation (Core Tokenizer & Separator Handling)
  - [x] Task 1.1: Multi-letter suffix parsing in `glued_model_token()` (`ES`, `ADS`, `PL`, `C`, `WB`)
  - [x] Task 1.2: Retain `+` modifier in `_token_and_identity()` and `glued_model_token()`
  - [x] Task 1.3: Separator permutation verification (`DW-7500` vs `DW 7500` vs `DW7500`)

- [x] Phase 2: Brand-Gated Prefix Equivalence & Strict Isolation
  - [x] Task 2.1: `brand_match_cores()` prefix expansion (`DWT` <-> `DW`, `HWM` <-> `HW`) strictly with verified brand
  - [x] Task 2.2: Strict brand isolation in `_prefix_is_allowed()` (foreign prefixes like `HWM` for Dawlance blocked)
  - [x] Task 2.3: Verification tests for `Dawlance DW 255 ES` vs `DWT 255 ES`

- [x] Phase 3: Dual Variant Resolution (`Clear Lid` <-> `C`)
  - [x] Task 3.1: Map `clear lid` <-> `c` in `variant_terms.py`
  - [x] Task 3.2: Dual identities (`dwt255c`, `dwt255clearlid`) in `build_model_keys()`
  - [x] Task 3.3: Verification tests for `Clear Lid` and `C` (rejecting `ES`)

- [x] Phase 4: Tiered Fallback Engine & Yellow Terminal Notice
  - [x] Task 4.1: Search terms preservation of `+` and dual aliases in `sources.py`
  - [x] Task 4.2: Emit `\033[93m[NOTICE: MARKET OMITTED MODIFIER] ...\033[0m` in `pipeline.py`
  - [x] Task 4.3: Anti-conflict hard guard verification (contradictory variants strictly rejected)

- [x] Phase 5: Verification & Full Regression Testing
  - [x] Task 5.1: Run `test_variant_tail.py` (30 passed)
  - [x] Task 5.2: Run full unit test suite (234 passed)

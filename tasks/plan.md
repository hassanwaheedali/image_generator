# Master Implementation Plan: Model Matching & Multi-Tier Variant Resolution

## Overview
Decompose the model matching and variant resolution overhaul into 5 focused vertical phases with explicit acceptance criteria and automated verification tests:
1. Phase 1: Core Tokenizer, Multi-Letter Suffixes (`ES`, `ADS`, `PL`, `C`, `WB`) & Separator Agnosticism (`DW-7500` $\leftrightarrow$ `DW 7500`).
2. Phase 2: Brand-Gated Soft Prefix Matching (`DWT` $\leftrightarrow$ `DW` strictly with Dawlance, `HWM` $\leftrightarrow$ `HW` strictly with Haier, foreign brands strictly forbidden).
3. Phase 3: Dual Variant Resolution for `Clear Lid` $\leftrightarrow$ `C`.
4. Phase 4: Tiered Fallback Engine & Yellow Terminal Notice (`\033[93m`).
5. Phase 5: Verification & Full Regression Testing.

## Architecture Decisions
- Separator tolerance: dashes, spaces, slashes, and dots evaluate identically across all tokens and patterns.
- Prefix equivalence is strictly conditional on verified presence of the brand name on the page.
- Suffixes (`ES`, `C`, `ADS`, `PL`, `WB`) are parsed as model code components while preserving the base family code.
- Omitted modifiers (e.g. market writes `ADS` instead of `ADS+`) safely fall back to verified true base product with a prominent yellow notice, whereas contradictory variants (`Clear Lid` vs `ES`) are strictly rejected.

## Task List
- [x] Phase 1: Foundation (Core Tokenizer & Separator Handling)
  - [x] Task 1.1: Multi-letter suffix parsing in `glued_model_token()` (`ES`, `ADS`, `PL`, `C`, `WB`)
  - [x] Task 1.2: Retain `+` modifier in `_token_and_identity()` and `glued_model_token()`
  - [x] Task 1.3: Separator permutation verification (`DW-7500` vs `DW 7500` vs `DW7500`)
- [x] Phase 2: Brand-Gated Prefix Equivalence & Strict Isolation
  - [x] Task 2.1: `brand_match_cores()` prefix expansion (`DWT` <-> `DW`, `HWM` <-> `HW`)
  - [x] Task 2.2: Strict brand isolation in `_prefix_is_allowed()`
  - [x] Task 2.3: Verification tests for `DW 255 ES` vs `DWT 255 ES`
- [x] Phase 3: Dual Variant Resolution (`Clear Lid` <-> `C`)
  - [x] Task 3.1: Map `clear lid` <-> `c` in `variant_terms.py`
  - [x] Task 3.2: Dual identities (`dwt255c`, `dwt255clearlid`) in `build_model_keys()`
  - [x] Task 3.3: Verification tests for `Clear Lid` and `C`
- [x] Phase 4: Tiered Fallback Engine & Yellow Terminal Notice
  - [x] Task 4.1: Search terms preservation of `+` and dual aliases in `sources.py`
  - [x] Task 4.2: Emit `\033[93m[NOTICE: MARKET OMITTED MODIFIER] ...\033[0m` in `pipeline.py`
  - [x] Task 4.3: Anti-conflict hard guard verification
- [x] Phase 5: Verification & Full Regression Testing
  - [x] Task 5.1: Run `test_variant_tail.py`
  - [x] Task 5.2: Run full unit test suite

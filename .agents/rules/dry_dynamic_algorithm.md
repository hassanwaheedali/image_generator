# Mandatory Rule: Strict DRY Concept & Dynamic Adaptive Algorithms

## 1. Zero Hardcoding Mandate
Under NO circumstances may any engineer or AI agent introduce:
1. **Static Brand Lists**: Never hardcode lists of brands (e.g. `["DAWLANCE", "GREE", "HAIER", ...]`). All brand metadata MUST originate from the input catalog or be dynamically extracted via morphological pattern analysis.
2. **Category / Form-Factor Conditionals**: Never use conditional statements checking specific category keywords (e.g. `if "MINI BAR"` or `if "REFRIGERATOR"`). All category fluff must be stripped universally via `CATEGORY_PATTERNS`.
3. **Hardcoded SKU Prefixes**: Never maintain static sets or regexes of brand prefixes (e.g. `(HWM|DWT|DW|WM|WF|GN|GS|GW|GF)` or `f"{b_clean[0]}mb"`). SKU prefixes MUST be extracted dynamically using generic regex patterns matching 2-4 letter codes preceding or hyphenated with numeric stems (e.g. `\b([A-Za-z]{2,4})[- ]?{stem_num}`).
4. **Static Modifier / Initialism Sets**: Never hardcode sets of variant codes (e.g. `{"SD", "DD", "MD", "WB", ...}`) or static word collision pairs (e.g. `"SINGLEDOOR"` vs `"DOUBLEDOOR"`). Initialisms must be processed dynamically by inspecting 2-letter acronyms against multi-word noun phrase boundaries.
5. **Static Color Lists in Anchor Extraction**: Color and marketing fluff must be isolated using mathematical information entropy and token length rules (e.g. `is_sku_variant_tag` checking uppercase length <= 3 vs descriptions >= 4 chars), preserving isolated model anchors intact.

## 2. Dynamic Adaptive Architecture in `matcher.py`
All matching and pattern generation logic in `pricing_engine/matcher.py` must strictly adhere to:
1. **Single Source of Truth**: Data dictionaries (such as capacity code maps `UNIVERSAL_CAPACITY_TO_TONNAGE` and `DAWLANCE_CAPACITY_TO_TONNAGE`) must never be redefined or duplicated as ad-hoc sets elsewhere in the code.
2. **Brand-Initial SKU Matching for Numeric Stems**: Pure numeric models (e.g. `4467`, `538`) must be validated dynamically against brand-initial SKU patterns (`{brand[0]}[a-z]{1,3}[- ]?{model}`) to automatically adapt across all brands (`Dawlance DMB-4467`, `Haier HRF-538`, `Haier HWM-120`, `PEL PR-2000`, `Gree GS-12`).
3. **Dynamic Acronym Collision Referee**: For any 2-letter modifier tag in target model (e.g. `SD`, `FL`), candidate listings with opposing initialisms (`DD`, `Double Door`, `TL`, `Top Load`) must be dynamically identified and rejected using word-initial comparison without static dictionary lookups.
4. **Preservation of Brand Invariance**: Candidate queries must never duplicate the brand name (e.g. preventing `Haier Haier 4607` or `DWT-DWT-1165`).

## 3. Strict Input Catalog Contract
Catalogs must strictly provide valid `Brand` values. If `Brand` is missing, empty, or a placeholder (`"-"`, `"N/A"`, `"NaN"`), ingestion must fail immediately with a descriptive `ValueError` prompting catalog correction. Guessing brands from title strings is strictly prohibited.

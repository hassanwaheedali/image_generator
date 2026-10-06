# AlfaMall & DigiMall Dynamic Repricing Engine

Automated, high-speed dynamic repricing pipeline for Pakistani e-commerce platforms (**AlfaMall** and **DigiMall / Faysal Bank**). It queries direct backend search APIs, cleans variant mismatches and out-of-stock items, and calculates winning psychological prices under strict profit floors.

---

## 1. Core Logistics & Cost Math

- **Volumetric Weight**:
  $$\text{Volumetric Weight (KG)} = \frac{\text{Length} \times \text{Width} \times \text{Height}}{5000}$$
- **Shipping Estimate**:
  $$\text{Shipping Estimate} = \text{Volumetric Weight} \times 70.0\text{ PKR (Zone Courier Rate)}$$
- **Cost Base**:
  $$\text{Cost Base} = \text{Net Price} + \text{Shipping Estimate}$$
- **Total Without Profit (0% Margin Floor)**:
  - AlfaMall (10%): $\frac{\text{Cost Base}}{0.90}$
  - DigiMall Leverage (8%): $\frac{\text{Cost Base}}{0.92}$
- **Baseline Calculation**:
  $$\text{Baseline} = \frac{\text{Cost Base} + \text{Profit}}{0.90}$$
- **Absolute Hard Floor (+Rs. 500 Minimum Profit Guarantee)**:
  - $\text{Floor}_{\text{Alfa}} = \frac{\text{Cost Base} + 500}{0.90}$
  - $\text{Floor}_{\text{Digi\_10}} = \frac{\text{Cost Base} + 500}{0.90}$
  - $\text{Floor}_{\text{Digi\_8}} = \frac{\text{Cost Base} + 500}{0.92}$

---

## 2. Pricing Decision Logic & Winning Conditions

- **Strict Winning Condition**:
  $$\text{Floor} \le \text{Our Price} < \text{Lowest Viable Competitor}$$
  Our price must **always** be strictly lower than the competitor to win the order.
- **Psychological Price Endings**:
  Prices must end strictly in standard psychological thresholds: `.999`, `.899`, `.799`, `.499`, `.299`, `.199`, `.099` (e.g., if competitor is at 73,000 and floor is 72,877, final price is set to 72,899).
- **AlfaMall Rules (Strict 10% Commission)**:
  - Discard sellers whose price is below $\text{Floor}_{\text{Alfa}}$ (loss-makers).
  - Undercut the lowest viable seller $\ge \text{Floor}_{\text{Alfa}}$.
  - If no viable sellers exist, output `NaN` (Skip / Unviable).
  - If 0 competitors found in market, apply **Monopoly Pricing** (+Rs. 4,000 margin with psychological ending).
- **DigiMall Rules (Two-Tier 10% $\rightarrow$ 8% Bank Switch)**:
  - **Tier 1 (Default 10%)**: If the lowest competitor is $\ge \text{Floor}_{\text{Digi\_10}}$, undercut and win at 10% tier. Bank only charges 8%, so we capture an extra 2% bank spread profit. Record `Digi Effective Commision` as `10.00%`.
  - **Tier 2 (Emergency 8% Switch)**: If the competitor is below $\text{Floor}_{\text{Digi\_10}}$ but $\ge \text{Floor}_{\text{Digi\_8}}$, switch to 8% bank rate, undercut to win. Record `Digi Effective Commision` as `8.00%`.
  - **Upper Band**: If the lowest competitor is below $\text{Floor}_{\text{Digi\_8}}$, drop them and attempt to undercut the next higher viable competitor at 8%.
  - If all competitors are below $\text{Floor}_{\text{Digi\_8}}$, output `NaN` (Skip / Unviable).

---

## 3. Scraping & API Architecture

- **Direct JSON Endpoints (No Selenium/DOM overhead)**:
  - **AlfaMall**:
    `https://be.alfamall.com/storefront/search_products?page=1&q={query}&limit=18&storefront=23&storefront_domain=alfamall.com`
  - **DigiMall**:
    `https://digimall-be.faysalbank.com/storefront/search_products?storefront=23&q={query}&page=1&limit=18`
- **Session & Headers**:
  - `curl_cffi.requests.AsyncSession(impersonate="chrome120")` for high speed & zero IP blocks.
- **Data Filtering & Variant Protection**:
  - Price: `item['variant_detail']['discounted_price']` (fallback: `original_price`).
  - Out-of-Stock Guard: Drop item if `item.get('sold_out') is True`.
  - Model Matcher: Strip prefixes like `GS-`, `GF-`, `GW-` (e.g. `12AITH21W-T3`).
  - Color collision prevention: Searching `12AITH21W` strictly rejects `12AITH21B` (Black), `12AITH24` (Pro), etc.

---

## 4. Input & Output Schemas

- **Input File**: `Gree_Uploading_on_DigiMall_and_Price.csv`
  - Columns: `S.NO`, `Brand`, `Name `, `Length`, `Width`, `Height`, `Net Price`, `Profit`
- **Output File**: `final_repriced_uploaded_products.csv`
  - Columns: `Shipping Estimate`, `Commision`, `Digi Effective Commision`, `Total Without Profit`, `Calculation`, `Minimum Price Alfa`, `Maximum Price Alfa`, `Minimum Price Digi`, `Maximum Price Digi`, `Final Price Alfa`, `Final Price Digi`, `Alfa Real Profit`, `Digi Real Profit`, `Alfa Reason`, `Digi Reason`
- **Active Script**:
  - `index.py` (Main repricing engine script)

---

## 5. Strict DRY Concept & Dynamic Adaptive Algorithms

- **Zero Hardcoding Mandate**:
  - Never hardcode lists of brands (e.g. `["DAWLANCE", "GREE", "HAIER", ...]`). Brand metadata comes strictly from the validated catalog column.
  - Never introduce category conditionals (e.g. `if "MINI BAR"` or `if "REFRIGERATOR"`).
  - Never maintain static prefix sets (e.g. `(HWM|DWT|DW|WM|WF|GN|GS|GW|GF)` or `f"{b_clean[0]}mb"`). SKU prefixes are extracted dynamically via generic regex: `\b([A-Za-z]{2,4})[- ]?{stem_num}`.
  - Never hardcode modifier sets (`{"SD", "DD", "MD", "WB", ...}`) or static word collision pairs. Collisions are evaluated dynamically via token initialisms.
- **Dynamic Adaptive Logic in `matcher.py`**:
  - Pure numeric models (e.g. `4467`, `538`) validate dynamically against brand-initial SKU patterns (`{brand[0]}[a-z]{1,3}[- ]?{model}`).
  - Acronym collisions (e.g. `SD` vs `Double Door`, `FL` vs `Top Load`) are detected dynamically at word boundaries without hardcoded keyword lists.
  - Single Source of Truth: All capacity mappings are defined once and referenced everywhere.


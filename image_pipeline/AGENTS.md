# Project rules - image_pipeline

Read this before changing anything. It is the short version of the whole project: what it is, how it
is checked, and what must never change. Deeper notes live in `README.md` (user manual) and
`docs/PROJECT_MAP.md` (which file owns which rule).

## What this is

A deterministic product-image pipeline. Input: a sheet of `Title` + source URL rows. Per row it
fetches the page, **proves the page shows that exact model**, collects only that model's own gallery
photos, rejects anything that is not a clean white-background product shot, de-duplicates them,
isolates and squares the product, composites it onto the DigiMall and Alfa Mall templates, appends the
closing banner (always last) and uploads everything to freeimage.host. Output: two import CSVs
(`Title,Brand,Image_Urls`, pipe-separated URLs, no spaces) plus a full audit trail.

**No LLM calls, no content-understanding services.** Everything is regex / DOM / pixel logic, so a run
is reproducible and every rejection can be pointed at a rule id.

## Tech stack

- Python **3.10+** (developed and tested on 3.13)
- `curl_cffi` (TLS-impersonating HTTP), `Pillow`, `numpy`, `scipy`, `opencv-python-headless`,
  `beautifulsoup4` + `lxml`, `requests`, `openpyxl`
- Dev tools: `pytest`, `mypy --strict`, `ruff`
- `pyproject.toml` is the **only** config home: dependencies, ruff rules, pytest options, mypy strict.

## Commands

```bash
pip install -e ".[dev]"                      # runtime + dev tools

python3 main.py --input sample_input.csv --dry-run          # safe: no uploads, no CSVs
python3 main.py --input sample_input.csv --workers 4        # real run (needs FREEIMAGE_API_KEY)

python3 -m ruff check .        # must print: All checks passed!
python3 -m mypy --strict .     # must print: Success: no issues found
python3 -m pytest tests -q     # 308 tests, fakes only, no network
python3 -m radon cc . -n C -s --exclude "tests/*"   # code health: 45 blocks at C or
                               # worse (baseline) - no function may gain a grade
```

Secrets: `FREEIMAGE_API_KEY` (and optional `FREEIMAGE_UPLOAD_URL`) come from the environment or from
`.env` / `secrets.env` / `env.txt` next to `main.py` (see `config.load_env_file`). A real exported
variable always wins. **Never print, log, commit or echo the key value.**

## Code conventions

- One rule = one named function + one direct test + one `rule_id` in `decisions.jsonl`. Rule ids are
  `S1`..`S15` plus the scoped sub-rules `S7.band`, `S7.seam`, `S7.official`, `S11.fetch`, `S13.fetch`,
  `S15.fetch`, `S15.relaxed`, `S15.tolerated`.
- **One config field per threshold.** No magic numbers in stage functions - everything tunable lives in
  `config.PipelineConfig` and is validated in `__post_init__`.
- `pipeline.py` never imports `requests` / `curl_cffi` / filesystem-network code directly: it talks to
  the `protocols.py` interfaces (`PageFetcher`, `ImageFetcher`, `ImageHost`, `SourceSearcher`). New
  network work goes behind a protocol, so tests stay fake-based and offline.
- Data structures are frozen dataclasses in `models.py`.
- Parsers are pure functions (`text -> objects`) and testable without network.
- Every file write is atomic (`csv_io.atomic_write_csv` / `atomic_write_text` / `save_image_atomic`).
  Never write a CSV or JSON path with a bare `open(path, "w")`.
- Docstrings say *why*, in plain English, for a future maintainer. Keep them short.
- Line length 120; `ruff` lint set is deliberately broad (`ignore = ["BLE001", "S110", "RUF001"]` -
  reasons are written in `pyproject.toml`).
- Style: no commented-out code, no placeholders, no "TODO" without an owner, no dead helpers. If
  something is unused, delete it - the suite is the safety net.

## Boundaries - never do these

- **Never weaken `S1` / `S3` / `S7` or `page_verification.verify_page` to make a row pass.** A row that
  refuses to publish is the correct outcome when the page cannot be proven to show that model
  (accepted example: sheet row `H50S80EUX` whose URL serves `H50S80GUX`).
  The `S7` studio-background rescue is *not* such a weakening: it accepts an image whose border
  ring is dirty only when **all four corners are neutral studio white**
  (`config.studio_background_rescue`, `studio_rescue_min_neutral_corners=4`) - the corners prove a
  white backdrop while the product legitimately touches the frame edges.  A busy/lifestyle frame
  never reaches four neutral corners at `corner_patch_ratio`, so it stays rejected (tests:
  `test_white_corners_busy_middle_rejected`, `test_studio_rescue_can_be_switched_off`).
  Variant separation (2026-10-07) is a *strengthening* of `S1`, not a weakening: a page printing
  another colourway is rejected as `colour_conflict`, and `S15.relaxed` never rescues such a page.
- **Never publish a non-`http(s)` URL.** `finalize_and_upload` refuses `file://` on purpose.
- **Never compare uploaded image bytes to the local finals** - freeimage.host re-encodes. Identify the
  uploaded picture back to its own local final with a 32x32 blurred grayscale fingerprint (live run
  2026-10-06: 38/38 identified correctly, worst self-distance 0.06 against 0.97 for the nearest other
  picture). A fixed MAE threshold alone is too brittle for high-detail TV shots.
- **Never let a product image touch the template's logo band**, and never measure the pasted product by
  scanning the composite for non-white pixels - diff against the bare template.
- **Never add a dependency** that is not needed for a shipped feature; the list in `pyproject.toml`
  must match the imports exactly.
- Never log secrets, never commit `secrets.env`/`.env`/`output/`/`state/`.
- Do not re-litigate settled product decisions. Colour is one, and it moved: another colourway of the
  same model is a **different product**, so it is rejected rather than flagged (owner decision
  2026-10-07, superseding 2026-10-05) - the wrong picture costs a penalty. Size/type differences are
  still flags only, photo-level colour differences still only affect ordering, and one field
  (`config.variant_separation_enabled`) rolls the whole rule back.
- **Relaxed matching never applies to the sheet's own URLs.** `relaxed_page_match` is only for
  `is_fallback=True` pages, it must always emit `S15.relaxed` **and** a `review_products.csv` row, and
  it must match whole tokens: `dw7500` never matches inside `dw7500c`.
- **A structured-gallery top-up (Shopify `.json`, Woo `wc/store`) runs only after the page's model is
  verified and only while the row is short of `target_images_per_product`.** It can add candidates,
  never by-pass a gate, and a fetch failure stays silent.
- **A tolerated spelling is not proof.** A page that only matches through the dropped-trailing-letter
  tolerance (live: `DW-7500C` row, page prints `DW-7500`) must keep the row searching - brand, then
  Surmawala and the other retailers - and may be published (`S15.tolerated`, `PARTIAL`, review CSV)
  only after the chain found nothing that prints the model. The model in the title outranks the link.

## Patterns

**Adding a safeguard rule** (the shape every rule follows):

```python
# 1. config.py      - one field per threshold, validated in __post_init__
my_rule_max_ratio: float = 0.10

# 2. image_quality.py (or page_verification.py / sources.py) - named function, config in, verdict out
def my_rule(image: Image.Image, config: PipelineConfig) -> bool:
    """True when the image passes; the caller logs the rule id on failure."""

# 3. the gate runner - keep the rule ids in order, one line each
if not my_rule(image, config):
    return "S16", "one sentence a human can act on"

# 4. tests - a direct test with the fake assets (no network), plus one adversarial case
def test_my_rule_rejects_the_bad_shape(tmp_path: Path) -> None: ...

# 5. README section 4 table - add the row, so the manual and the code stay in sync
```

**Adding a source/brand extractor:** one file in `gallery_extractors/<brand>.py` with a
`@register_extractor` line, wired in `gallery_extractors/__init__.py`, plus a fixture HTML page and a
test that reads it. Extractors only *find* candidate images; every gate still runs afterwards.

## Error-output discipline

When the gate fails, feed back the **specific** failure, not the whole log:

```
ruff  -> the one rule + file:line it printed
mypy  -> the single "error:" line(s) and their file:line
pytest -> the failing test name and its assertion line
```

Tests must never hit the network: they use `tests/conftest.py` fakes (`FakePageFetcher`,
`FakeImageFetcher`, `FakeImageHost`, `FakeSourceSearcher`, `LocalPathHost`) and synthetic images.
Live verification is a separate, deliberate step (see `verification/`).

## When something is unclear

Do not guess silently. If a requirement, a threshold or a page behaviour is ambiguous, write down the
options and the trade-off, pick the **conservative** one for image quality (when unsure, reject more),
and record the choice in the run report / `plan.md`. Rows that fail loudly are cheap; wrong pictures in
a shop are not.

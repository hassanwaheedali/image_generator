"""Pure model matching: model keys, glued identities, colours, variants.

Recognition is intentionally simple (customer decision, 2026-10-05):

1. **model key** - the model number from the sheet appears on the page, allowing
   flexible separators (``DW-7500C`` == ``DW 7500C``) and, optionally, a trailing
   letter the site dropped (``DW-7500C`` == ``DW-7500``).
2. **brand + code** - the brand is named *and* the code appears without the brand's
   own letters (``Haier HRF-538 IFGA`` page shows ``Haier ... 538 IFGA``).

Everything else is *conflict* checking, never confidence scoring: a colour or variant
that contradicts the sheet only annotates the report (customer decision), because the
images themselves are still protected by the S2-S14 gates.

Loose about *recognizing* the product, strict about never certifying the wrong one.
No I/O, everything cached and unit-testable.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from urllib.parse import unquote, urlsplit

from models import ModelKeys, ProductTask
from similarity import similar
from textnorm import WHITESPACE_RE, normalize_key, normalize_text
from url_filters import IMAGE_EXTENSIONS, KNOWN_BRANDS, page_product_brand
from variant_terms import (
    COLOUR_FAMILIES,
    colour_phrase,
    colour_word_conflict,
    is_colour_word,
    shade_agreement,
    suffix_agreement,
)

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
_PUNCTUATION = ".,;:!?()[]{}\"'`“”‘’–—-_/\\|+*&^%$#@~<>="
_SEP = r"[\s\-_/.]*"

#: A colourway written **inside** the code, glued to it and short: ``023WDC(OW)``.
#: The maker sells one body in several finishes spelled only by this marker, so it is
#: part of the model's identity, not a removable parenthetical.  Matched only when the
#: run before ``(`` already carries a digit (a code, not a word) and the bracket holds
#: 1-4 letters - a spaced note ``(2024 edition)``, a phrase ``(Dark Grey)`` and digits
#: stay ordinary brackets and are stripped.
_INLINE_COLOURWAY_RE = re.compile(r"([A-Za-z0-9]*\d[A-Za-z0-9]*)\(([A-Za-z]{1,4})\)")

UNIT_QUANTITY_RE = re.compile(
    r"^\d+(?:[.,]\d+)?"
    r"(?:kg|kgs|g|ltr|ltrs|l|liter|litre|ton|tons|btu|inch|in|w|kw|hz|mm|cm|m"
    r"|gb|tb|mah|v|cft|cuft)$"
)

_NOISE_TOKEN_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\d+x\d+$"),
    re.compile(r"^[0-9a-f]{16,}$"),
    re.compile(r"^(?:img|image|dsc|dscn|dscf|pxl|photo|screenshot)[\-_]?\d{3,}$"),
    re.compile(r"^(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:[0-3]\d)?$"),
)

_PREFIX_RE = re.compile(r"^\d*[a-z]+")

#: Words that end a model tail (they describe the product, not the model).
TAIL_STOPWORDS: frozenset[str] = frozenset(
    {
        "inverter", "converter", "white", "black", "grey", "gray", "gold", "golden",
        "silver", "red", "blue", "green", "beige", "brown", "smart", "digital",
        "automatic", "semi", "fully", "top", "front", "load", "tub", "twin",
        "single", "double", "door", "fridge", "refrigerator", "freezer", "washing",
        "machine", "tv", "led", "lcd", "oled", "qled", "inch",
        "pro", "plus", "series", "warranty", "years", "year", "and", "with",
    }
)

# --------------------------------------------------------------------------- #
# colour families (synonyms collapse; plan section 3.6)
#
# The vocabulary itself lives in :mod:`variant_terms`, the single source of truth for
# both colour *recognition* (below) and variant *separation*.  It is imported, not
# redefined, so the two can never drift apart.
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# variant axes (capacity / size / type words; plan section 3.6)
# --------------------------------------------------------------------------- #
_CAPACITY_RE = re.compile(
    r"(\d{1,4}(?:[.,]\d{1,2})?)\s*"
    r"(cu\s?ft|cft|cuft|kgs?|g|kg|ton(?:s)?|btu|ltrs?|liters?|litres?|l|"
    r"in(?:ch(?:es)?)?|cm|mm|ft|mah|gb|tb|kw|w|hz)\b"
)

_UNIT_AXIS: dict[str, str] = {
    "kg": "capacity_kg", "kgs": "capacity_kg",
    "g": "capacity_g",
    "l": "capacity_l", "ltr": "capacity_l", "ltrs": "capacity_l",
    "liter": "capacity_l", "liters": "capacity_l", "litre": "capacity_l",
    "litres": "capacity_l",
    "ton": "capacity_ton", "tons": "capacity_ton",
    "btu": "capacity_btu",
    "inch": "size_inch", "inches": "size_inch", "in": "size_inch",
    "cm": "size_cm", "mm": "size_mm", "ft": "size_ft",
    "cft": "capacity_cuft", "cuft": "capacity_cuft", "cu ft": "capacity_cuft",
    "w": "power_w", "kw": "power_kw", "hz": "freq_hz",
    "mah": "battery_mah", "gb": "memory_gb", "tb": "memory_tb",
}

#: Type-word axes.  Only axes where a mismatch really means "different machine".
#: Display technology (LED vs QLED) is intentionally absent: too noisy on real pages.
TYPE_AXES: dict[str, dict[str, tuple[str, ...]]] = {
    "load": {
        "top": ("top load", "top-load", "topload", "top loading"),
        "front": ("front load", "front-load", "frontload", "front loading"),
    },
    "automation": {
        "semi": ("semi automatic", "semi-automatic", "semi auto", "semi-auto", "semiautomatic"),
        "fully": ("fully automatic", "fully-automatic", "fully auto", "fully-auto", "fullyautomatic"),
    },
    "tub": {
        "twin": ("twin tub", "twin-tub", "twin tub washing"),
        "single": ("single tub", "single-tub"),
    },
    "door": {
        "single": ("single door", "single-door"),
        "double": ("double door", "double-door"),
        "side_by_side": ("side by side", "side-by-side", "french door"),
    },
    "freezer_type": {
        "chest": ("chest freezer", "chest type freezer", "chest-type"),
        "vertical": ("vertical freezer", "upright freezer"),
    },
}


# --------------------------------------------------------------------------- #
# basic normalisation lives in :mod:`textnorm`, shared with variant_terms
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=4096)
def _compiled(core: str) -> re.Pattern[str]:
    """Boundary-anchored, separator-tolerant pattern over an alphanumeric core."""
    if len(core) < 3:
        return re.compile(r"(?!x)x")
    body = _SEP.join(re.escape(ch) for ch in core)
    return re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])")


def key_pattern(key: str) -> re.Pattern[str]:
    """Compiled pattern for one key (``HSU-12`` matches ``hsu12`` / ``HSU 12``)."""
    return _compiled(normalize_key(key))


# --------------------------------------------------------------------------- #
# tokens, tails, identities
# --------------------------------------------------------------------------- #
def model_like_token(token: str) -> bool:
    """True when a title token can serve as a model key (letters + digits, >= 3)."""
    stripped = token.strip(_PUNCTUATION)
    core = normalize_key(stripped)
    if len(core) < 3:
        return False
    if not re.search(r"[a-z]", core) or not re.search(r"[0-9]", core):
        return False
    return not UNIT_QUANTITY_RE.match(core)


def _remove_balanced(text: str, open_ch: str, close_ch: str, keep_unbalanced: bool) -> str:
    out: list[str] = []
    depth = 0
    for char in text:
        if char == open_ch:
            depth += 1
            continue
        if char == close_ch:
            if depth > 0:
                depth -= 1
                continue
            out.append(char)
            continue
        if depth == 0:
            out.append(char)
    if depth > 0 and keep_unbalanced:
        return text
    return "".join(out)


def strip_bracketed(title: str, *, keep_if_unbalanced: bool = True) -> str:
    """Remove ``(...)`` / ``[...]`` content (rule 4.1 step 1)."""
    return _remove_balanced(
        _remove_balanced(title, "(", ")", keep_if_unbalanced), "[", "]", keep_if_unbalanced
    )


def _leading_letters(core: str) -> str:
    """Alphabetic prefix of a code core (``hrf538ifga`` -> ``hrf``)."""
    match = _PREFIX_RE.match(core)
    return match.group(0) if match else ""


#: A model written with a space: the prefix is 2-4 letters (``DW``, ``PEL``, ``HSU``) ...
_GLUE_PREFIX_RE = re.compile(r"^[a-z]{2,4}$")
#: ... and the number is 3-5 digits, optionally with its own suffix (``7500``, ``7500C``).
#: Two-digit numbers are deliberately excluded: "TCL 85" (a size, not a model) must never
#: become a key that accepts any 85-inch page.
_GLUE_NUMBER_RE = re.compile(r"^\d{3,5}[a-z]{0,2}$")
#: Words that are never a model prefix, so "TV 55" / "IN 20" never become a key.
GLUE_STOPWORDS: frozenset[str] = frozenset(
    {
        "tv", "led", "lcd", "oled", "qled", "uhd", "fhd", "hd", "smart", "digital",
        "kg", "kgs", "gb", "tb", "cm", "mm", "in", "inch", "ml", "ltr", "no", "the",
        "and", "for", "with", "of", "to", "model", "type", "size", "set", "sets",
        "pcs", "pc", "w", "kw", "v", "hz", "btu", "ton",
    }
)


def glued_model_token(
    words: list[str], index: int
) -> tuple[str, str] | None:
    """A model spelled with spaces: ``DW 7500 C`` -> ``("DW 7500 C", "dw7500c")``.

    The sheet writes what the shop prints, and Dawlance prints ``DW 7500 TWIN TUB CLEAR
    LID (WHITE)`` - two words that are one model (live case: that title produced **no**
    model key, so the row was skipped before a single page was fetched).  ``DW 7500C``
    also produced only ``7500c`` (the prefix was lost), which made the key weaker than
    the model really is.

    Guards: the prefix is 2-4 letters and not a generic word (``TV 55`` never glues), the
    number is 3-5 digits (``TCL 85`` - a size - never glues), and a trailing single letter
    is only glued when the number carries no suffix of its own (``DW 7500 C`` -> ``dw7500c``,
    but ``DW 7500C X`` stays ``dw7500c``).
    """
    prefix = words[index].strip(_PUNCTUATION)
    if not _GLUE_PREFIX_RE.match(prefix.lower()) or prefix.lower() in GLUE_STOPWORDS:
        return None
    if index + 1 >= len(words):
        return None
    number = words[index + 1].strip(_PUNCTUATION)
    if not _GLUE_NUMBER_RE.match(number.lower()):
        return None
    token = f"{prefix} {number}"
    suffix_is_glued = bool(re.search(r"[a-z]$", number.lower()))
    if not suffix_is_glued and index + 2 < len(words):
        suffix = words[index + 2].strip(_PUNCTUATION)
        if len(suffix) == 1 and suffix.isalpha():
            token = f"{token} {suffix}"
    identity = normalize_key(token)
    if len(identity) < 5 or not re.search(r"[a-z]", identity) or not re.search(r"\d", identity):
        return None
    return token, identity


def numeric_stem(identity: str) -> str:
    """The number that identifies a model family: ``dw2500c`` -> ``2500c``."""
    match = re.search(r"\d{2,6}[a-z]{0,3}", identity)
    return match.group(0) if match else ""


_MODEL_RUN_RE = re.compile(r"\d+|[a-z]+")


def _whole_token_pattern(needle: str) -> re.Pattern[str] | None:
    """A pattern for a model spelling that allows the page's own separators.

    ``dw7500`` finds ``DW-7500``, ``dw 7500`` and ``DW/7500`` - but never ``DW-7500C``:
    every run of the needle has to be there, and the hit has to be a whole token.  Without
    that boundary ``dw7500`` would swallow its ``C`` sibling, and the white machine's row
    would be illustrated with the black/glass model's photos.
    """
    runs = _MODEL_RUN_RE.findall(needle.lower())
    if not runs:
        return None
    body = _SEP.join(re.escape(run) for run in runs)
    return re.compile(rf"(?<![0-9a-z]){body}(?![0-9a-z])")


def _relaxed_disjoint_code(page_text: str, keys: ModelKeys) -> bool:
    """True when the page's own model code is *disjoint* from all of the row's codes.

    A page that carries the bare digit stem ``1035`` could belong to ``VF-1035GD``
    **or** to ``DBD-1035`` - they share the number but are different appliances.
    Parsing the page's own strong text (title / H1 / first strong tag) with the same
    tokenizer tells us which: if the page prints ``DBD-1035`` as a whole code and the
    row is ``VF-1035GD``, the two code sets are disjoint - the page names another
    product and the rescue match is rejected.

    A page whose own text carries *no* code at all (the body is marketing prose) has
    nothing to compare - the function returns ``False`` (no evidence to reject on).
    """
    clean_text = re.sub(r"<[^>]+>", " ", page_text)
    page_keys = build_model_keys(clean_text)
    if not page_keys.identities or not keys.identities:
        return False  # no code on the page - cannot accuse
    # Any overlap is enough: the page may carry our code alongside others.
    our_set = set(keys.identities)
    # Include truncation variants so ``dw7500`` still overlaps with ``dw7500c``.
    our_expanded = set(our_set)
    for identity in keys.identities:
        our_expanded.update(truncation_variants(identity))
    return not any(
        page_id in our_expanded
        or any(page_id.startswith(ours) or ours.startswith(page_id) for ours in our_set)
        for page_id in page_keys.identities
    )


def relaxed_page_match(
    page_url: str,
    page_text: str,
    keys: ModelKeys,
    brand: str | None,
) -> tuple[bool, str]:
    """Loose match for a *fallback* page: brand **and** the model's own number.

    Strict S1 asks whether the page proves this exact model; this asks the much weaker
    question "is this page about this brand's product with the same number", which is the
    owner-approved trade for rescued rows (2026-10-06): publish the pictures, flag the row
    for manual review.  It is never used for the sheet's own URLs.

    Accepts when:

    * the page carries our own identity as a **whole token** (``dw2500c`` - matched
      through the page's own spacing, but never inside a longer model code); or
    * the page names the brand **and** carries the model's number (``2500c`` or ``2500``),
      which covers the sibling-variant case (``DW-2500`` page for a ``DW-2500C`` row).

    Two guards added 2026-10-12:
    * the bare-digit stem must be **at least 3 digits** - a 2-digit number (e.g. ``15``
      from ``MD-15``) appears everywhere as a capacity, size or year and is too weak to
      identify a product;
    * when the page's own parsed model code (from its title/strong text) is **disjoint**
      from all of the row's codes, the page names a different product and is rejected
      even at the rescue tier.
    """
    if not keys.identities:
        return False, ""
    parts = urlsplit(page_url)
    # A search-result URL echoes the number we searched for in its query string
    # (``?_psq=PWM+1250``), so the query can never be evidence - a wrong product's
    # result would otherwise "match" itself.  Judge by host + path + page text only.
    url_evidence = f"{parts.netloc} {parts.path}"
    haystack = f"{url_evidence} {page_text}".lower()
    # Owner run 2026-10-11: the rescue tier stays loose by design (publish + flag), but
    # a URL that *names another product line* is not loose, it is wrong - the official
    # sitemap handed the ``Pulse 19C`` row an ``Innova 19S Ultra`` page, and a bare
    # number in the body text must not publish that gallery under the rescue flag.
    if url_names_other_product_line(page_url, keys):
        return False, ""
    for identity in keys.identities:
        pattern = _whole_token_pattern(identity)
        if len(identity) >= 5 and pattern is not None and pattern.search(haystack):
            # Guard: the page's own code (parsed from the page text) must not be
            # disjoint from ours.  A page that carries ``dw2500c`` as a whole token
            # also proves itself; a page whose title names ``dw2500e`` shares the
            # same digits but sells a different colourway - still the rescue tier.
            if _relaxed_disjoint_code(page_text, keys):
                return False, ""
            return True, f"page carries {identity}"
    stems: list[str] = []
    for identity in keys.identities:
        stem = numeric_stem(identity)
        if not stem:
            continue
        digits = re.match(r"\d+", stem)
        # Guard: a 2-digit bare number (``15``) appears as a capacity or size
        # in every product description - it names nothing.  Only 3+ digit runs
        # are specific enough to identify a model family.
        if not digits or len(digits.group(0)) < 3:
            continue
        stems.append(stem)
        if digits.group(0) != stem and digits.group(0) not in stems:
            stems.append(digits.group(0))
    hit = next(
        (
            stem
            for stem in stems
            if stem and (pattern := _whole_token_pattern(stem)) and pattern.search(haystack)
        ),
        "",
    )
    if not hit:
        return False, ""
    if brand and not brand_token_present(f"{url_evidence} {page_text}", brand):
        return False, ""
    # Guard: if the page's own model code is disjoint from the row's codes, the shared
    # digit stem is a coincidence (e.g. ``VF-1035GD`` and ``DBD-1035`` share ``1035``
    # but name different appliances).  Reject rather than rescue.
    if _relaxed_disjoint_code(page_text, keys):
        return False, ""
    return True, f"brand + number match ({hit}) without an exact model proof"


#: How many distinct model codes a page's *own name* may print before it reads as a
#: catalogue listing rather than one product - the tail limb refuses crowded names.
TAIL_MATCH_MAX_PAGE_CODES = 8

_TONNAGE_RE = re.compile(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)\s*[-.]?\s*(?:ton|tr)\b")
_TONNAGE_SLUG_RE = re.compile(r"(?<![a-z0-9])(\d)-(\d)\s*[-.]?\s*(?:ton|tr)\b")


def _tonnage_set(text: str) -> set[float]:
    """Every tonnage a text states: ``1.5 Ton``, ``1-5-ton`` (slug hyphens), ``2 TR``.

    Retail slugs write the decimal with hyphens, so the pair form is read too - and
    the lookbehinds stop ``18HRFN1-QC6`` or the tail of ``1.5`` from posing as a
    size.  Both sides of the corroboration use this one reader, so a row and a page
    agree exactly when they state the same number.
    """
    lowered = text.lower()
    tons = {float(f"{a}.{b}") for a, b in _TONNAGE_SLUG_RE.findall(lowered)}
    # A slug hyphen ("1-5 ton") is one stated size, but the general pattern also reads
    # "5 ton" out of it, so a 1.5 Ton page would double-count as {1.5, 5.0} and arm a 5
    # Ton row.  Blank the spans the slug form already claimed before the general pass,
    # so every stated tonnage counts exactly once.
    general = _TONNAGE_SLUG_RE.sub(lambda m: " " * (m.end() - m.start()), lowered)
    tons |= {float(hit) for hit in _TONNAGE_RE.findall(general)}
    return tons


def code_tail_chunks(keys: ModelKeys) -> tuple[str, tuple[str, ...]] | None:
    """``(head, tail)`` of the row's first model code, or ``None`` when it has none.

    Chunks split only where the sheet itself separated them (``-``, ``_``, ``/``,
    space) - never at case or digit boundaries, which shred ``MSEF1C`` into junk.
    The tail is every chunk after the first and must hold **two or more**: a single
    trailing chunk is a bare number again, and a bare number never names a variant
    (the (9173) lesson - family codes are shared by five series).
    """
    if not keys.tokens:
        return None
    chunks = [c.lower() for c in re.split(r"[-_ /]+", keys.tokens[0]) if c]
    if len(chunks) < 3:
        return None
    return chunks[0], tuple(chunks[1:])


#: The appliance-type, operating-mode and unit nouns a page prints for *every* product
#: in a category - they sit on a Go Easy page and an Xtreme page alike, so they name no
#: series and must never serve as corroboration.  This is deliberately NOT
#: ``_GENERIC_NAME_WORDS``: that table also holds series adjectives (``easy``) that this
#: limb must keep, because "MSEF1C = Go Easy" is spelled with exactly that word.
_TAIL_CATEGORY_WORDS = frozenset(
    word
    for word in (
        "conditioner", "conditioners", "inverter", "inverters", "cooling", "cooler",
        "coolers", "cool", "split", "window", "portable", "cabinet", "chest",
        "upright", "standing", "floor", "vertical", "horizontal", "heater", "heaters",
        "heat", "pump", "tower", "compressor", "mounted", "wall", "capacity", "watts",
        "watt", "inches", "inch", "feet", "filter", "refrigerator", "refrigerators",
        "freezer", "freezers", "washer", "washers", "dryer", "dispenser", "dispensers",
        "purifier", "purifiers", "microwave", "television", "televisions", "speaker",
        "speakers", "soundbar", "geyser", "geysers", "automatic", "manual", "model",
        "series", "warranty", "years", "type", "size", "only",
    )
    if len(word) >= 4
)


def _series_corroboration_words(title: str, keys: ModelKeys, brand: str | None) -> frozenset[str]:
    """The free words the sheet places *after* its own code - the series name.

    ``… MSEF1C-18HRFN1-QC6 Go Easy`` yields ``easy`` (two-letter ``go`` is too weak
    to mean anything on a page).  A row whose title ends at its code - or that states
    only the appliance type there (``…-QC6 Split Inverter Air Conditioner``) - has no
    series word to corroborate and the limb refuses to fire at all: a category noun is
    printed by every sibling page, so trusting it would arm the wrong series, and
    nothing is guessed.
    """
    if not keys.tokens:
        return frozenset()
    halves = title.lower().split(keys.tokens[0].lower(), 1)
    if len(halves) < 2:
        return frozenset()
    code = code_tail_chunks(keys)
    chunks = {code[0], *code[1]} if code else set()
    return frozenset(
        word
        for word in re.findall(r"[a-z]{4,}", halves[1])
        if word not in chunks
        and word not in _TAIL_CATEGORY_WORDS
        and word not in KNOWN_BRANDS
        and word != (brand or "").lower()
        and not is_colour_word(word)
    )


def _looks_like_series_head(token: str, brand: str | None) -> bool:
    """Whether a token glued directly before our tail names *another* model's series.

    ``model_like_token`` needs a digit, so it misses a pure-letter head like ``MSUS``
    of ``MSUS-18HRFN1-QC6`` - and that gap let a different Midea line arm on our Go
    Easy tail (live review 2026-10-15).  A head is any opaque token of that shape: not
    the brand, not a colour, not a plain number, and either digit-bearing or a name
    word the category noise never explains.  The caller has already cleared this row's
    own head and its own series words, so this only ever fires on a foreign prefix.
    """
    if not token or token == (brand or "").lower() or token in KNOWN_BRANDS:
        return False
    if token.isdigit() or is_colour_word(token):
        return False
    if len(token) < 4:
        return False
    return _keep_name_segment(token) or model_like_token(token)


def _foreign_series_veto(
    page_words: set[str],
    *,
    series: frozenset[str],
    sibling_terms: frozenset[str],
    own_terms: frozenset[str],
) -> bool:
    """True when the page's own name belongs to another row of this sheet.

    The same sheet evidence S4's file limb uses: a word naming exactly one *other* row
    names another product, and live run 2026-10-15 showed the Xtreme and Titan pages
    carrying our Go Easy tail - a row whose only free word after its code is a shared
    spec name (``T3 Plus``) armed on them.  The word this limb itself demands of the
    page can never veto it, and a name anchored by one of this row's own line words is
    kept (live 2026-10-11: ``…Hyper-18X-T3-Super-White-Turbo-Pro`` says ``turbo``,
    another row's word, beside its own name - decoration, not identity).
    """
    if page_words & _own_anchor_words(own_terms):
        return False
    return bool((sibling_terms - series) & page_words)


def _foreign_head_on_tail(token: str, *, head: str, series: frozenset[str], brand: str | None) -> bool:
    """True when a token glued directly before our tail run names another series."""
    if not token or token == head or token in series:
        return False
    return _looks_like_series_head(token, brand)


def tail_chunk_match(
    name_text: str,
    *,
    title: str,
    keys: ModelKeys,
    brand: str | None,
    tail_owner_count: int,
    sibling_terms: frozenset[str] = frozenset(),
    own_terms: frozenset[str] = frozenset(),
    max_page_codes: int = TAIL_MATCH_MAX_PAGE_CODES,
) -> str | None:
    """Safeguard S1.tail / S15.tail - the sheet's own code, written shortly by a retailer.

    Live run website_ac_clean 2026-10-15: mabdullah sells ``MSEF1C-18HRFN1-QC6`` as
    ``18HRFN1 QC6 … GO EASY 1.5 Ton`` - the complete tail of our code, the series word
    the sheet itself put after the code, and the row's capacity.  ``name_text`` is the
    page's **own name** (``<title>`` + ``<h1>``), not its body: a product states its
    model in its name, while the body of a content-rich Shopify page is stuffed with
    forty related-product codes that would drown every guard - and a catalogue listing
    names only "Air Conditioners", so it never carries our tail in its name.  Every
    guard must hold *at once*, because each one alone has a proven false-accept in that
    run: the tail also appears on Xtreme and Titan pages (the head veto, the sheet's own
    series word, and ``sibling_terms`` from :func:`annotate_sibling_terms` - a foreign
    series word in the page's name, stood down when that name carries one of this row's
    own line words exactly as S4's file limb is), ``18HRFN1`` alone is shared across
    series (complete run, whole chunks only), a crowded name is a listing not a product
    (code-count refusal), and another colourway is another product (colour veto).
    ``tail_owner_count`` is the sheet's verdict: a tail naming zero or several rows
    never arms - ambiguity is never resolved silently.  Returns the evidence line when
    armed, ``None`` when not; the caller still runs every image gate.
    """
    if tail_owner_count != 1:
        return None
    code = code_tail_chunks(keys)
    if code is None:
        return None
    head, tail = code
    series = _series_corroboration_words(title, keys, brand)
    if not series:
        return None
    row_tons = _tonnage_set(title)
    if not row_tons or not row_tons & _tonnage_set(name_text):
        return None
    if keys.colour_word and (
        colour_word_conflict(keys.colour_word, name_text)
        or _shade_veto(keys, name_text, "")
    ):
        return None  # another colour, or our colour under another shade, is another picture
    if len(set(code_like_tokens(name_text))) > max_page_codes:
        return None
    tokens = [t for t in re.split(r"[^a-z0-9]+", name_text.lower()) if t]
    page_words = set(tokens)
    if _foreign_series_veto(page_words, series=series, sibling_terms=sibling_terms, own_terms=own_terms):
        return None  # the page's own name belongs to another row of this sheet
    for index in range(len(tokens) - len(tail) + 1):
        if tuple(tokens[index : index + len(tail)]) != tail:
            continue
        before = tokens[index - 1] if index > 0 else ""
        if _foreign_head_on_tail(before, head=head, series=series, brand=brand):
            return None  # another series' head sits on our tail - refuse, loud
        if series.issubset(page_words):
            return (
                f"the page's name prints the complete code tail {'-'.join(tail)} "
                f"+ series {' '.join(sorted(series))} + the row's tonnage"
            )
        return None
    return None


def annotate_tail_owners(tasks: Sequence[ProductTask]) -> list[ProductTask]:
    """Set each row's ``tail_owner_count`` - how many rows of the sheet own its code tail.

    The tail-chunk limb may fire only where the tail names exactly one row: a sheet
    carrying both ``MSEF1C-24HRFN1-QC6`` and ``MSEF1D-24HRFN1-QC6`` (both Go Easy) has
    an ambiguous tail the sheet itself cannot resolve, so both rows get ``2`` and stay
    dead rather than one silently stealing the other's page.  A row with no armable
    tail (no code, no series word after it, no stated tonnage) gets ``0``.  Mirrors
    :func:`annotate_sibling_terms`: the sheet is threaded into a per-row decision.
    """
    tails: list[tuple[str, ...] | None] = []
    for task in tasks:
        keys = build_model_keys(task.title)
        code = code_tail_chunks(keys)
        armable = (
            code is not None
            and bool(_series_corroboration_words(task.title, keys, None))
            and bool(_tonnage_set(task.title))
        )
        tails.append(code[1] if armable and code is not None else None)
    counts: Counter[tuple[str, ...]] = Counter(t for t in tails if t is not None)
    return [
        replace(task, tail_owner_count=counts[tail] if tail is not None else 0)
        for task, tail in zip(tasks, tails, strict=True)
    ]


def _token_and_identity(words: list[str], index: int, max_extra: int) -> tuple[str, str]:
    """Return (token, glued identity) for ``words[index]``.

    ``HRF-538 IFGA`` -> ``("HRF-538", "hrf538ifga")``: alphabetic words that follow a
    model token are glued on (up to ``max_extra``, stopping at generic words), which is
    what lets one sheet model be recognised in its long and short spellings.
    """
    token = words[index].strip(_PUNCTUATION)
    identity = normalize_key(token)
    extras = 0
    for word in words[index + 1 :]:
        if extras >= max_extra:
            break
        candidate = word.strip(_PUNCTUATION)
        if not candidate or not candidate.isalpha():
            break
        if candidate.lower() in TAIL_STOPWORDS:
            break
        identity += normalize_key(candidate)
        extras += 1  # noqa: SIM113  (index is consumed by the break condition above)
    return token, identity


# Words that describe the appliance category or marketing, never a product *name*.
# They are dropped from ``name_words`` so a series line ("Daimond") or an unknown
# colourway ("Ocean Mist") is the only thing left to compare.  Generic vocabulary,
# not per-product data.
_GENERIC_NAME_WORDS = frozenset((
    "refrigerator", "fridge", "freezer", "washing", "machine", "washer", "dishwasher", "dryer",
    "dispenser", "conditioner", "inverter", "converter", "on", "top", "bottom",
    "glass", "door", "doors", "tap", "taps", "straight", "water", "cool", "cooler",
    "cu", "ft", "capacity", "litre", "liters", "gallon", "kg", "watt", "ton", "tons",
    "split", "window", "portable", "cabinet", "chest", "upright", "pro", "plus",
    "max", "special", "edition", "smart", "digital", "direct",
    "twin", "tub", "semi", "automatic", "manual", "standing", "floor",
    "vertical", "horizontal", "compact", "mini", "large", "small", "luxury",
    "premium", "original", "model", "series", "type", "style", "with", "without",
    "and", "the", "for", "hd", "led", "lcd",
    # Every word in the catalogue's 21 category paths (2026-10-08), plus the singular /
    # plural twins a title would actually print.  A category word describes the appliance,
    # never the product, so it must never take part in a series veto.
    "appliances", "electronics", "kitchen", "home", "energy", "green", "deep",
    "microwave", "microwaves", "oven", "geyser", "geysers", "purifier", "purifiers",
    # "humdifier" is the spelling the catalogue itself uses - kept so the typo cannot veto.
    "humidifier", "humdifier",
    "audio", "visuals", "sound", "soundbar", "speaker", "speakers", "bars",
    "refrigerators", "freezers", "dispensers", "coolers", "washers", "machines",
    "television",
    # Additional category / unit / marketing terms (e.g. Super Asia coolers, heaters, TVs)
    "litters", "litres", "heater", "heaters", "instant", "master", "crystal", "gas",
    "room", "auto", "easy", "inch", "inches", "qled", "oled", "uhd", "fhd", "google",
    "android", "dimming",
))

#: Dynamically registered generic words from config
_DYNAMIC_GENERIC_NAME_WORDS: set[str] = set()


def register_generic_name_words(words: Iterable[str]) -> None:
    """Register additional generic category/marketing words dynamically from config."""
    _DYNAMIC_GENERIC_NAME_WORDS.update(w.strip().lower() for w in words if w.strip())


#: Tier words proven by sibling rows to separate *different products of one series*
#: (``Avante`` vs ``Avante+``, ``Acce`` vs ``Acce Pro``).  Never hand-written:
#: :func:`learn_series_differentiators` derives it per run from the catalogue data.
_SERIES_DIFFERENTIATORS: frozenset[str] = frozenset()

#: The ``+`` symbol is the written form of this word (``AVANTE+`` == ``Avante Plus``).
_PLUS_WORD = "plus"

#: Trailing punctuation a sheet cell glues onto a ``+`` token (``"Avante+,"``).
_PLUS_TRAILING = "()\"',;."


def _glued_plus(token: str) -> str | None:
    """The word a ``+`` is glued to (``AVANTE+`` -> ``avante``), else ``None``.

    Both title readers use this: the ``+`` and the word ``plus`` are the same tier,
    and an extraction that sees only one of the two spellings makes siblings look
    different (``AVANTE+`` row vs ``Avante Plus`` page must not veto).
    """
    core = token.strip(_PLUS_TRAILING)
    glued = re.fullmatch(r"([A-Za-z]{2,})\+", core)
    return glued.group(1).lower() if glued is not None else None


#: A candidate that prefixes a brand or category word the corpus uses at least this
#: often is an abbreviation of it (``INV`` of ``inverter``, ``DAW`` of ``Dawlance``),
#: not a tier word.  ``pro`` is safe from this rule because ``processor`` names no
#: known brand or category.
_ABBREVIATION_MIN_COUNT = 5


def set_series_differentiators(words: Iterable[str]) -> None:
    """Install the differentiator set for this run (replaces, never unions).

    Title keys are cached and their name words depend on this set, so installing a
    new set invalidates the cache - a title built before learning must not keep the
    pre-learning words after it.
    """
    global _SERIES_DIFFERENTIATORS
    _SERIES_DIFFERENTIATORS = frozenset(w.strip().lower() for w in words if w.strip())
    _build.cache_clear()


def series_differentiators() -> frozenset[str]:
    """The tier words currently in force (for tests and callers that report state)."""
    return _SERIES_DIFFERENTIATORS


def _keep_name_segment(normalized: str, standalone: bool = True) -> bool:
    """Whether a pure-alphabetic, already-lowercased segment names the product line.

    A learned differentiator is kept however short or "generic" it is, but only when
    the segment stands alone in its token: inside ``9173WB`` the ``WB`` is a piece of
    an internal code, not the tier word the learner was proven against.  Everything
    else follows the standing rule (>= 4 letters, not category/marketing noise, not a
    colour).
    """
    if is_colour_word(normalized):
        return False
    if standalone and normalized in _SERIES_DIFFERENTIATORS:
        return True
    if len(normalized) < 4:
        return False
    return normalized not in _GENERIC_NAME_WORDS and normalized not in _DYNAMIC_GENERIC_NAME_WORDS


def _name_segments(
    title: str,
) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, str]]:
    """``(name words, tier candidates, anchors)`` of a title, in order.

    Name words are :func:`_name_words`.  Tier candidates are the words a *sibling* row
    could prove to be a differentiator (:func:`learn_series_differentiators`): a pure
    alphabetic segment of 2-4 letters sitting directly after a name word
    (``Acce Pro`` -> ``pro``; capacity digits are transparent, ``Frost 20 Pro`` still
    yields ``pro``), or a ``+`` glued onto a name word (``AVANTE+`` -> ``plus``).
    Colour words and code fragments are never candidates, so a candidate list can only
    ever hold short tier markers - which is what makes the sibling-pair proof sound.
    ``anchors`` maps each candidate to the name word it qualifies (``pro`` -> ``acce``),
    which the learner needs to tell a tier from a code fragment glued to nothing.
    """
    base: list[str] = []
    candidates: list[str] = []
    anchors: dict[str, str] = {}
    last_named = False
    last_name_word = ""
    for token in title.split():
        # A token mixing letters and digits is a part number (``(9193-WB)``); its short
        # alphabetic pieces are code fragments, never tier words (same principle as the
        # Midea code-fragment rule).  Pure-digit tokens (``20``) stay transparent.
        code_token = any(c.isdigit() for c in token) and any(c.isalpha() for c in token)
        standalone = not any(c.isdigit() for c in token)
        for raw in re.split(r"[^A-Za-z0-9]+", token):
            if not raw or not raw.isalpha():
                continue
            normalized = raw.lower()
            if _keep_name_segment(normalized, standalone):
                if normalized not in base:
                    base.append(normalized)
                last_named = True
                last_name_word = normalized
                continue
            if (
                not code_token
                and last_named
                and not is_colour_word(normalized)
                and 2 <= len(normalized) <= 4
                and normalized not in candidates
            ):
                candidates.append(normalized)
                anchors.setdefault(normalized, last_name_word)
                # Only a *name* word anchors a tier: ``GD`` in ``AVANTE GD INV DAW`` is
                # itself a stock-code fragment and must not teach the next short word.
                last_named = False
                continue
            last_named = False
        anchor = _glued_plus(token)
        if anchor is not None and anchor in base and _PLUS_WORD not in candidates:
            candidates.append(_PLUS_WORD)
            anchors.setdefault(_PLUS_WORD, anchor)
    return tuple(base), tuple(candidates), anchors


def _tier_pair_rows(
    pairs: Iterable[tuple[str | None, str]],
) -> tuple[
    dict[str, list[tuple[frozenset[str], frozenset[str], dict[str, str]]]],
    Counter[str],
    set[str],
]:
    """Scan the catalogue once: rows grouped by brand, word frequencies, brand keys."""
    grouped: dict[str, list[tuple[frozenset[str], frozenset[str], dict[str, str]]]] = {}
    word_freq: Counter[str] = Counter()
    brand_keys: set[str] = set()
    for brand, title in pairs:
        for word in re.split(r"[^A-Za-z0-9]+", title):
            if word.isalpha() and word:
                word_freq[word.lower()] += 1
        key = normalize_key(brand or "")
        if len(key) < 4 or not title.strip():
            continue
        brand_keys.add(key)
        base, candidates, anchors = _name_segments(title)
        if not base:
            continue
        grouped.setdefault(key, []).append((frozenset(base), frozenset(candidates), anchors))
    return grouped, word_freq, brand_keys


def _tier_pair_evidence(
    grouped: dict[str, list[tuple[frozenset[str], frozenset[str], dict[str, str]]]],
) -> dict[str, set[tuple[str, frozenset[str]]]]:
    """Words whose sibling rows differ by exactly that one tier word, per series line.

    The word must be anchored to a *series* word, never to the brand itself: a
    ``HAIER WB`` / ``HAIER`` pair teaches nothing because ``WB`` is a stock column,
    while an ``Avante`` / ``AVANTE+`` pair is exactly how a tier is written.
    """
    evidence: dict[str, set[tuple[str, frozenset[str]]]] = {}
    for brand_key, rows in grouped.items():
        for base_a, candidates_a, anchors_a in rows:
            for base_b, candidates_b, anchors_b in rows:
                difference = candidates_a ^ candidates_b
                if base_a != base_b or len(difference) != 1:
                    continue
                word = next(iter(difference))
                anchor = (anchors_a if word in candidates_a else anchors_b).get(word, "")
                if not anchor or anchor == brand_key or anchor in KNOWN_BRANDS:
                    continue
                evidence.setdefault(word, set()).add((brand_key, base_a))
    return evidence


def _word_clears_brand_and_noise(
    word: str,
    word_freq: Counter[str],
    brand_keys: set[str],
) -> bool:
    """Whether a learned candidate is neither a brand nor an abbreviation of noise.

    Shared by every word the catalogue teaches (tiers and colourways alike): a candidate
    is refused when it *is* a brand, or when it is the head of a much more common brand or
    category word - ``INV`` of ``inverter``, ``DAW`` of ``Dawlance`` - because in that case
    it is an abbreviation of something that names no product.
    """
    if normalize_key(word) in brand_keys or word in KNOWN_BRANDS:
        return False
    noise_words = _GENERIC_NAME_WORDS | _DYNAMIC_GENERIC_NAME_WORDS
    return not any(
        longer != word
        and longer.startswith(word)
        and count >= _ABBREVIATION_MIN_COUNT
        and (longer in KNOWN_BRANDS or longer in noise_words)
        for longer, count in word_freq.items()
    )


def _is_tier_word(
    word: str,
    lines: set[tuple[str, frozenset[str]]],
    word_freq: Counter[str],
    brand_keys: set[str],
) -> bool:
    """Whether one candidate word survives the structural guards (see the learner)."""
    if len(lines) < 2 or len(word) < 3:
        return False
    return _word_clears_brand_and_noise(word, word_freq, brand_keys)


def learn_series_differentiators(
    pairs: Iterable[tuple[str | None, str]],
    *,
    extra: Iterable[str] = (),
) -> frozenset[str]:
    """Derive the tier words from the catalogue itself and install them.

    Two same-brand rows whose name words are *identical* but whose tier candidates
    differ by *exactly one* word is sibling evidence that the word names a different
    product: ``Dawlance Avante Freezer-On-Top Refrigerator 9 Cu Ft`` beside
    ``Dawlance AVANTE+ Freezer-On-Top Refrigerator 12 Cu Ft`` evidence ``plus``.
    The structural guards that keep stock-code noise out of the set are split across
    :func:`_tier_pair_evidence` (series anchor) and :func:`_is_tier_word`: at least
    three letters (``GD``/``WB`` columns are noise everywhere, and the never-learned
    short word simply keeps the old lenient behaviour: fail-open), no abbreviation of
    a common brand or category word (``INV`` of ``inverter``), and evidence from
    **two different series lines** - one pair can be a title accident, while a tier
    that repeats across ``Acce Pro``, ``Frost Pro`` and ``Chrome Pro`` is how the
    market spells a variant.

    Anything without that evidence stays noise, and a wrongly learned word can still
    only veto a text that shares the row's series name (see :func:`_tier_mismatch`),
    never an unrelated product.  The installed set replaces the previous one, so runs
    are deterministic.
    """
    set_series_differentiators(())          # the static lists decide the learning
    grouped, word_freq, brand_keys = _tier_pair_rows(pairs)
    learned = {
        word
        for word, lines in _tier_pair_evidence(grouped).items()
        if _is_tier_word(word, lines, word_freq, brand_keys)
    }
    set_series_differentiators(learned | {w.strip().lower() for w in extra if w.strip()})
    return _SERIES_DIFFERENTIATORS


# --------------------------------------------------------------------------- #
# learned colourway (shade) words
# --------------------------------------------------------------------------- #
#: A row can state "Red" and still mean one of three different refrigerators - 9140-WB
#: is sold as Pearl Red, Ruby Red and Diamond Red, and only the shade picks the picture.
#: The words that name a shade are listed nowhere.  The catalogue proves them instead: a
#: word is a colourway word when it sits in front of a colour name repeatedly, does it on
#: more than one series line, and is at home nowhere else.  Measured, not asserted - the
#: same doctrine that keeps ``plus`` and ``pro`` out of a hand table above.
_COLOURWAY_MIN_EVIDENCE = 2
_COLOURWAY_MIN_EXCLUSIVITY = 0.6
_COLOURWAY_MIN_WORD_LENGTH = 4

#: Every word a colour name can end on (``midnight black`` -> ``black``), so the slot in
#: front of a colour is found without maintaining a second vocabulary.
_COLOURWAY_HEADS = frozenset(
    synonym.split()[-1] for synonyms in COLOUR_FAMILIES.values() for synonym in synonyms
)

_COLOURWAY_WORDS: frozenset[str] = frozenset()


def set_colourway_words(words: Iterable[str]) -> None:
    """Install the colourway set for this run (replaces, never unions).

    Same contract as :func:`set_series_differentiators`: cached title keys read their
    colour phrase from this set, so the cache is cleared with it.
    """
    global _COLOURWAY_WORDS
    _COLOURWAY_WORDS = frozenset(w.strip().lower() for w in words if w.strip())
    _build.cache_clear()


def colourway_words() -> frozenset[str]:
    """The colourway words currently in force (for tests and callers that report state)."""
    return _COLOURWAY_WORDS


def _colour_slot_words(title: str) -> tuple[str, ...]:
    """Every unknown word sitting immediately in front of a colour name in one title.

    ``… 9 Cu Ft Pearl Red (9140-WB-GD)``, ``… 12 Cu Ft Hairline Black (9178-WB)`` and
    ``… 20 Ltr Blue`` all fill the slot, and most of what comes out of it names no colour
    - the learner decides.  Words the colour vocabulary already knows (``pearl``,
    ``chrome``) are left out on purpose: they read as shades without any teaching, and
    learning them would let a finish word that makers use as a series name (``Dawlance
    **Chrome**``) pose as a colourway.  Digits are transparent, so ``Frost 20 Pro White``
    still offers ``pro``, exactly as the tier reader does.
    """
    words = [word for word in normalize_text(title).split() if word.isalpha()]
    return tuple(
        words[index - 1]
        for index in range(1, len(words))
        if words[index] in _COLOURWAY_HEADS
        and len(words[index - 1]) >= _COLOURWAY_MIN_WORD_LENGTH
        and not is_colour_word(words[index - 1])
    )


def learn_colourway_words(
    pairs: Iterable[tuple[str | None, str]],
    *,
    extra: Iterable[str] = (),
    min_evidence: int = _COLOURWAY_MIN_EVIDENCE,
    min_exclusivity: float = _COLOURWAY_MIN_EXCLUSIVITY,
) -> frozenset[str]:
    """Derive the shade words the catalogue itself proves and install them.

    Run the tier learner first: the series lines counted here are its name words, so the
    two learners share one reading of every title.

    A slot word is installed when all of these hold.  It names a colour in front of a
    colour head at least ``min_evidence`` times; those uses are at least
    ``min_exclusivity`` of *every* use of the word in the catalogue, which is what tells
    ``hairline`` (27 of 27) from ``grill`` or ``inverter`` (a handful of slots inside
    hundreds of ordinary mentions); it does so on **two different series lines**, because
    one title can be an accident; and it survives the shared brand/abbreviation guards and
    the standing category-noise list, which is the difference from the tier learner -
    ``pro`` is a tier only because two sibling rows prove it, while a slot word has no
    such proof and ``machine`` would otherwise be taught as a colourway.

    Nothing learned is a fail-open, not a risk: an unlearned shade word simply keeps
    today's colour reading, which can cost a protection but can never reject a page.
    """
    set_colourway_words(())
    rows = [(brand, title) for brand, title in pairs if title and title.strip()]
    _grouped, word_freq, brand_keys = _tier_pair_rows(rows)
    slot: Counter[str] = Counter()
    lines: dict[str, set[tuple[str, frozenset[str]]]] = {}
    for brand, title in rows:
        key = normalize_key(brand or "")
        if len(key) < 4:
            continue  # a brand-less row cannot say which line it belongs to
        base = frozenset(_name_segments(title)[0])
        for word in _colour_slot_words(title):
            slot[word] += 1
            lines.setdefault(word, set()).add((key, base))
    noise = _GENERIC_NAME_WORDS | _DYNAMIC_GENERIC_NAME_WORDS
    learned = {
        word
        for word, count in slot.items()
        if count >= min_evidence
        and count >= min_exclusivity * word_freq.get(word, count)
        and len(lines[word]) >= 2
        and word not in noise
        and _word_clears_brand_and_noise(word, word_freq, brand_keys)
    }
    set_colourway_words(learned | {w.strip().lower() for w in extra if w.strip()})
    return _COLOURWAY_WORDS


def _name_words(title: str) -> tuple[str, ...]:
    """The positional "name" words of a title: series lines and unknown colourways.

    Keeps alphabetic tokens of length >= 4 that are not the category/marketing noise,
    not a known colour word and not part of a model code.  The brand stays in here (this
    function does not know it); :func:`_drop_brand_words` removes it before any veto, so
    a brand spelled one way in the sheet and another on the page can never veto.

    A separator-delimited segment that carries a digit is a piece of a part number, so
    its alphabetic runs are code fragments, never line names (``MSAG``, ``HRDN`` of
    ``MSAG2B-12HRDN1-QC0``): a family page that prints the code only in its body can
    never carry those fragments in its own name, so left in they veto the series on
    every variant page (live Midea Eco row, 2026-10-13).  A pure word glued to a code
    by a separator (``(PWD-425-GD)-Ocean``, ``…023WDC(W)-Triple``) is its own segment
    and stays a name word.
    """
    out: list[str] = []
    for token in title.split():
        standalone = not any(c.isdigit() for c in token)
        for segment in re.split(r"[^A-Za-z0-9]+", token):
            if not segment.isalpha():
                continue
            normalized = segment.lower()
            if normalized in out or not _keep_name_segment(normalized, standalone):
                continue
            out.append(normalized)
        anchor = _glued_plus(token)
        # ``AVANTE+`` names the same product as ``Avante Plus``: the ``plus`` half of
        # the spelling is read too (only once learned - otherwise it stays noise).
        if (
            anchor is not None
            and anchor in out
            and _PLUS_WORD not in out
            and _keep_name_segment(_PLUS_WORD)
        ):
            out.append(_PLUS_WORD)
    return tuple(out)


_KEYWORD_EXTRA_NOISE = frozenset(("air", "conditioners", "conditioner", "price", "pakistan"))


def _keyword_tokens(title: str) -> tuple[str, ...]:
    """The significant words of a title that carries *no* model code.

    Owner 2026-10-09: rows like ``Dawlance Split AC Dual Pro T3 Inverter 30`` were
    rejected at 4.1 although "Dawlance Dual Pro 30 Inverter T3" *is* the model -
    some products are sold by keywords, not codes.  Kept: alphabetic words of >= 3
    that are not category/marketing noise or a colour, alphanumeric tags (``t3``)
    and numbers of >= 2 digits (``30``).  A lone digit (``1`` of "1 Ton") says too
    little, because "1.5" normalizes to the same digit.
    """
    out: list[str] = []
    for raw in re.findall(r"[A-Za-z0-9]+", title):
        word = raw.lower()
        if len(word) < 2 or word in out:
            continue
        if word.isdigit():
            if len(word) >= 2:
                out.append(word)
            continue
        if not word.isalpha():
            if len(word) >= 2 and re.search(r"[a-z]", word) and re.search(r"\d", word):
                out.append(word)          # alphanumeric tags like ``t3``
            continue
        if len(word) < 3:
            continue
        if word in _GENERIC_NAME_WORDS or word in _KEYWORD_EXTRA_NOISE:
            continue
        if word in TAIL_STOPWORDS or is_colour_word(word):
            continue
        out.append(word)
    # a year or a bare size ("2024", "55") is not a product name: without at least
    # one alphabetic name word the row still has no identity (stays a 4.1 reject).
    return tuple(out) if any(word.isalpha() for word in out) else ()


@lru_cache(maxsize=8192)
def _build(
    title: str,
    allow_title_only: bool,
    keep_unbalanced: bool,
    bracketed_fallback: bool,
    max_extra_tokens: int,
) -> ModelKeys:
    cleaned = WHITESPACE_RE.sub(
        " ",
        strip_bracketed(
            _INLINE_COLOURWAY_RE.sub(r"\1-\2", title), keep_if_unbalanced=keep_unbalanced
        ),
    ).strip()
    words = cleaned.split()

    def scan(source_words: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        found: list[tuple[int, str, str]] = []
        seen: set[str] = set()

        def remember(position: int, token: str, identity: str) -> None:
            core = normalize_key(token)
            if not core or core in seen:
                return
            seen.add(core)
            found.append((position, token, identity))

        for index, word in enumerate(source_words):
            if model_like_token(word):
                token, identity = _token_and_identity(source_words, index, max_extra_tokens)
                remember(index, token, identity)
            glued = glued_model_token(source_words, index)
            if glued is not None:
                remember(index, glued[0], glued[1])
        found.sort(key=lambda entry: entry[0])
        return tuple(entry[1] for entry in found), tuple(entry[2] for entry in found)

    tokens, identities = scan(words)
    if not tokens and bracketed_fallback:
        # Rescue-only path: all 10 rows of the sample sheet carry the model in brackets.
        kept: list[str] = []
        for match in re.finditer(r"\(([^)]*)\)|\[([^\]]*)\]", title):
            kept.extend(part for part in match.groups() if part)
        if kept:
            tokens, identities = scan(" ".join(kept).split())

    title_key = normalize_key(cleaned) if allow_title_only else ""
    colour_word, colour_shade = colour_phrase(title, _COLOURWAY_WORDS)

    return ModelKeys(
        cleaned_title=cleaned,
        tokens=tokens,
        title_key=title_key,
        identities=identities,
        colours=tuple(sorted(extract_colour_families(title))),
        variant_facts=tuple(sorted(extract_variant_facts(title).items())),
        colour_word=colour_word,
        colour_shade=colour_shade,
        name_words=_name_words(title),
        keywords=() if tokens else _keyword_tokens(title),
        colourway_markers=frozenset(
            hit.group(2).lower() for hit in _INLINE_COLOURWAY_RE.finditer(title)
        ),
    )


def build_model_keys(
    title: str,
    *,
    allow_title_only: bool = False,
    unbalanced_bracket_keeps_text: bool = True,
    bracketed_key_fallback: bool = True,
    brand_match_max_extra_tokens: int = 2,
) -> ModelKeys:
    """Extract model keys, glued identities, colours and variant facts from a title."""
    return _build(
        title,
        allow_title_only,
        unbalanced_bracket_keeps_text,
        bracketed_key_fallback,
        brand_match_max_extra_tokens,
    )


# --------------------------------------------------------------------------- #
# matching
# --------------------------------------------------------------------------- #
def truncation_variants(core: str) -> tuple[str, ...]:
    """Weaker spellings of a key: the core minus 1-2 *trailing letters*.

    ``dw7500c`` -> ``dw7500`` (the sheet says ``DW-7500C``, the site prints
    ``DW-7500``).  Only trailing letters may be dropped, so ``hsu12`` has no variant
    and the ``HSU-12`` vs ``HSU-12HFPAA`` protection is untouched (that rule also
    never *extends* a key with characters the title did not contain).
    """
    variants: list[str] = []
    for cut in (1, 2):
        if len(core) <= cut + 2:
            continue
        suffix = core[-cut:]
        if not suffix.isalpha() or not suffix.isascii():
            continue
        candidate = core[:-cut]
        if re.search(r"[a-z]", candidate) and re.search(r"[0-9]", candidate):
            variants.append(candidate)
    return tuple(variants)


#: A digit run, the ONE letter glued to it, and a tail long enough to be the product's
#: name - the shape of ``12xolive``, where the sheet's ``12X`` meets the brand's ``12S``.
_FUZZY_CODE_RE = re.compile(r"(\d{2,})([a-z])([a-z]{4,})")


def fuzzy_identity_match(text: str, keys: ModelKeys) -> str:
    """Which of the row's identities the given text matches with only its capacity letter
    differing - ``""`` when none does.

    Measured on Orient (owner, 2026-10-10): the sheet title says ``Titanium 12X Olive Black``,
    Orient's own page and file name say ``titanium-12s-olive-black-t3-plus.html``, because the
    letter after the tonnage digits is the brand's series code, not part of what the reseller
    copied.  The row therefore matched *nothing* on the brand site - no URL, no page - and the
    images came from Daraz and Lazada instead.

    This is deliberately not a general typo tolerance and it is not an acceptance: every
    safeguard that separates two products by code still uses the exact keys.  What it does
    allow is *finding* the brand's own page, which is why the tail must be a name of >= 4
    letters and the digit run >= 2 digits, so ``12`` alone can never match anything.  Callers
    treat a hit as a tolerated level (S15-style), never as proof.
    """
    slug = normalize_key(text)
    if not slug:
        return ""
    for identity in keys.identities:
        for match in _FUZZY_CODE_RE.finditer(identity):
            digits, _letter, tail = match.groups()
            if re.search(re.escape(digits) + r"[a-z]?" + re.escape(tail), slug):
                return identity
    return ""


def near_miss_codes(text: str, keys: ModelKeys) -> tuple[str, ...]:
    """Codes on the page that *look like* this product's family but did not match.

    Used only to make a rejection actionable in the log (``page shows H50S80GUX``).
    """
    prefixes = {_alpha_prefix(token) for token in keys.tokens}
    prefixes.discard("")
    if not prefixes:
        return ()
    hints: list[str] = []
    for code in code_like_tokens(text):
        if len(re.findall(r"\d", code)) < 3:
            continue
        if _alpha_prefix(code) in prefixes:
            hints.append(code)
    return tuple(dict.fromkeys(hints))[:3]


def _leading_code_stem(segment: str) -> str:
    """The leading ``letters+digits`` run of a slash segment (``023WDC-OW-T3`` -> ``023wdc``).

    A shop that leaves the factory side of ``HSU-.../023WDC...`` off prints neither
    this stem nor its suffix; a shop that prints *another* colourway shows the same
    stem with a different tail - Haier sells the identical body as ``023WDC-OW-T3``
    (Ocean White) and ``023WDC-DG-T3`` (Dark Grey).  The stem is what tells those
    apart: a page showing it is a page that knows this factory code.
    """
    for chunk in re.split(r"[-_/ ]+", segment):
        core = normalize_key(chunk)
        if len(core) >= 3 and re.search(r"[a-z]", core) and re.search(r"[0-9]", core):
            return core
    return ""


def _slash_segment_hit(
    token: str, core: str, normalized: str, colourways: frozenset[str] = frozenset()
) -> bool:
    """True when one ``/``-separated segment of the token matches on its own.

    Haier sheets print ``HSU-13HFCP/023WDC(W)``: the part before the slash is the
    customer model, the rest is a factory code that shops routinely leave out
    (live 2026-10-09: friendshome.pk lists the very same machine as
    ``HSU-13HFCP-W`` and the row was rejected for "no model key").  A segment
    only counts when it is model-like in its own right (letters + digits, >= 3
    chars) and keeps the boundary-anchored pattern, so ``HSU-12`` still never
    matches inside ``HSU-12HFPAA``.

    A row that states a colourway is held to it (live 2026-10-15: Haier's Dark-Grey
    page was accepted as an Ocean-White row because both print the same customer
    model).  When the page *does* carry the factory code's stem but not our colourway
    it is a different finish, not a shop that left the code out, so the tolerance is
    refused.  Rows that state no colourway keep the original lenient behaviour.
    """
    segments = token.split("/")
    for segment in segments:
        seg_core = normalize_key(segment)
        if len(seg_core) < 3 or seg_core == core:
            continue
        if not re.search(r"[a-z]", seg_core) or not re.search(r"[0-9]", seg_core):
            continue
        if not _compiled(seg_core).search(normalized):
            continue
        if colourways and any(
            (stem := _leading_code_stem(other))
            and _compiled(stem).search(normalized)
            and not _compiled(normalize_key(other)).search(normalized)
            for other in segments
            if other != segment
        ):
            continue  # the page prints that factory code under another colourway
        return True
    return False


def _segment_truncation_hit(
    token: str, core: str, normalized: str, colourways: frozenset[str] = frozenset()
) -> bool:
    """True when the token minus its trailing ``-suffix`` segment matches the page.

    GREE sheets print ``GS-12PITH17G-T3`` while the product page prints the model
    without the series tail (``GS-12PITH17G`` - live 2026-10-09).  The letter-drop
    truncation cannot help because the tail ends in a digit (``T3``).  Dropping a
    whole separator-delimited segment is safe where letter-dropping is not: the
    remainder still has to be model-like (letters *and* digits), so ``9160-WB``
    never degenerates to ``9160`` and ``PWM-1250`` never to ``PWM``.

    The one thing that may never be dropped is a colourway the sheet wrote into the
    code (``(OW)`` names the finish, so a page without it is another product).  It is
    matched against the sheet's own marker, never against the shape of a short tail:
    ``DF-300-ES`` and ``DW-128-G`` are series tails and stay truncatable.
    """
    segments = [part for part in re.split(r"[-_/ ]+", token) if part]
    for drop in (1, 2):
        if len(segments) <= drop:
            continue
        if colourways and any(part.lower() in colourways for part in segments[-drop:]):
            continue
        if segments[-drop].isdigit() or re.search(r"\d{2,}", segments[-drop]):
            continue
        candidate = normalize_key("-".join(segments[:-drop]))
        if len(candidate) < 5 or candidate == core:
            continue
        if not re.search(r"[a-z]", candidate) or not re.search(r"[0-9]", candidate):
            continue
        if _compiled(candidate).search(normalized) and not _page_names_other_short_tail(
            candidate, normalized, frozenset(normalize_key(part) for part in segments[-drop:])
        ):
            return True
    return False


def _page_names_other_short_tail(
    stem: str, normalized: str, our_tails: frozenset[str]
) -> bool:
    """True when the page carries ``stem`` and ends it with a **different** short tail.

    ``9140-WB-GD`` row, ``9140WB-RB`` page: the shop did not leave our tail out, it printed
    another one - and a short pure-letter tail is a colourway code, not noise (owner
    2026-10-15).  This is the contradiction half of the standing rule; omission stays
    innocent, so the page that really drops our tail still matches.

    The glued stem, its last run and its last digit+letter pair are all tried
    (``hsu13hfcp023wdc`` anchors whole, its tail only after ``023wdc``), because a sheet
    glues what a page prints apart and an anchor that misses the spelling misses the fact.
    What the page legitimately puts there is skipped, using vocabulary the pipeline already
    keeps rather than a new list: a colour (``9140WB Red``), a category or marketing word,
    one of our own dropped tails, and anything with a digit (``023WDC-T3`` is a series tail,
    never a colourway).
    """
    if not stem:
        return False
    runs = _MODEL_RUN_RE.findall(stem)
    anchors = {stem} | {"".join(runs[-length:]) for length in (1, 2) if len(runs) >= length}
    tail_re = r"[\s\-_/.]*([a-z]{2,3})(?![a-z0-9])"
    for anchor in sorted(anchors, key=len, reverse=True):
        for hit in re.compile(_compiled(anchor).pattern + tail_re).finditer(normalized):
            tail = hit.group(1)
            if tail in our_tails or tail in _GENERIC_NAME_WORDS or is_colour_word(tail):
                continue
            return True
    return False


_FAM_VARIANT_RE = re.compile(r"^([a-z]*\d+[a-z]+)(\d+)")


def _family_range_hit(core: str, normalized: str) -> bool:
    """True when the page lists our variant inside a hyphen range of the same family.

    Shops sell one page per range: ``GREE GS-12PITH15-16-17-18 T3`` carries the
    ``GS-12PITH17G-T3`` row's model among four (owner 2026-10-09: "ismey sab bech
    raha hay woh").  The family stem (``gs12pith``) must equal ours and our variant
    number (``17``) must be one of the range's members - a ``GS-12AITH24W`` page has
    a different stem and never hits this rule.
    """
    parsed = _FAM_VARIANT_RE.match(core)
    if parsed is None or len(parsed.group(1)) < 5:
        return False
    family, variant = parsed.groups()
    pattern = re.compile(
        r"(?<![a-z0-9])"
        + _SEP.join(re.escape(char) for char in family)
        + r"(\d+(?:[\s\-_/.,]+\d+)+)(?![a-z0-9])"
    )
    return any(
        variant in re.findall(r"\d+", run.group(1)) for run in pattern.finditer(normalized)
    )


@lru_cache(maxsize=16384)
def find_matching_keys(
    text: str,
    keys: ModelKeys,
    include_title_key: bool = True,
    include_identities: bool = True,
    include_truncation: bool = True,
) -> tuple[str, ...]:
    """Keys of ``keys`` that appear in ``text``.

    A token matches when its own pattern matches **or** (when enabled) its glued
    identity pattern matches - so ``HRF-538 IFGA`` matches ``HRF538IFGA``, while
    ``HSU-12`` still never matches inside ``HSU12HFPAA`` because the title carries
    no tail there.
    """
    if not text:
        return ()
    normalized = normalize_text(text)
    if not normalized:
        return ()
    matched: list[str] = []
    tokens = keys.tokens
    for index, token in enumerate(tokens):
        core = normalize_key(token)
        if _compiled(core).search(normalized):
            matched.append(token)
            continue
        if include_identities and index < len(keys.identities):
            identity = keys.identities[index]
            if identity and identity != core and _compiled(identity).search(normalized):
                matched.append(token)
                continue
        if include_identities and "/" in token and _slash_segment_hit(
            token, core, normalized, keys.colourway_markers
        ):
            matched.append(token)
            continue
        if include_identities and (
            _segment_truncation_hit(token, core, normalized, keys.colourway_markers)
            or _family_range_hit(core, normalized)
        ):
            matched.append(token)
            continue
        marker_end = bool(keys.colourway_markers) and core.endswith(tuple(keys.colourway_markers))
        if include_truncation and not marker_end:
            # a colourway the sheet wrote into the code ends the core, and dropping letters
            # off it is how a -DG page was tolerated for an -OW row (owner 2026-10-15)
            for variant in truncation_variants(core):
                if not _compiled(variant).search(normalized):
                    continue
                if _page_names_other_short_tail(
                    variant, normalized, frozenset({core[len(variant):]}) | keys.colourway_markers
                ):
                    continue  # our stem, spelled to another short tail: another product
                matched.append(token)
                break
    title_hit = (
        include_title_key
        and keys.title_key
        and _compiled(keys.title_key).search(normalized)
    )
    if title_hit and keys.title_key not in {normalize_key(t) for t in matched}:
        matched.append(keys.title_key)
    return tuple(matched)


def _alpha_prefix(token: str) -> str:
    match = _PREFIX_RE.match(normalize_key(token))
    return match.group(0) if match else ""


def code_like_tokens(text: str) -> tuple[str, ...]:
    """Code-like tokens of a filename/folder (rule 4.4), noise filtered out.

    Extracts:
    1. Direct alpha-numeric codes (``HSU18HFPAA``, ``DW7500C``).
    2. Consecutive compound runs where a letter prefix is joined to numbers by
       hyphens/underscores/dots/spaces (e.g. ``HRF-47552`` -> ``hrf47552``,
       ``DW-7500`` -> ``dw7500``, ``HRF-47552-GD`` -> ``hrf47552gd``).

    Drops pure noise (``img1234``, ``800x800``, long hex hashes, dates) and single-type
    tokens (pure words like ``refrigerator`` or pure 2-digit numbers like capacity ``15``).
    """
    segments = [s for s in re.split(r"[^A-Za-z0-9]+", text) if s]
    found: list[str] = []

    # 1. Single segments containing both letters and digits
    for s in segments:
        core = normalize_key(s)
        if (
            len(core) >= 3
            and re.search(r"[a-z]", core)
            and re.search(r"[0-9]", core)
            and not any(pattern.match(core) for pattern in _NOISE_TOKEN_RES)
        ):
            found.append(core)

    # 2. Glued pairs: prefix letters (2-5) + numeric model code (2-6 digits, e.g. HSU+18, HRF+47552)
    for i in range(len(segments) - 1):
        s1, s2 = segments[i], segments[i + 1]
        pair = normalize_key(s1 + s2)
        if (
            len(pair) >= 4
            and re.match(r"^[a-z]{2,5}\d{2,6}", pair)
            and not any(pattern.match(pair) for pattern in _NOISE_TOKEN_RES)
        ):
            found.append(pair)

    # 3. Glued triplets: prefix + numbers + suffix letters (e.g. HRF+47552+GD, DW+7500+C)
    for i in range(len(segments) - 2):
        s1, s2, s3 = segments[i], segments[i + 1], segments[i + 2]
        triplet = normalize_key(s1 + s2 + s3)
        if (
            len(triplet) >= 5
            and re.match(r"^[a-z]{2,5}\d{2,6}[a-z]{1,4}$", triplet)
            and not any(pattern.match(triplet) for pattern in _NOISE_TOKEN_RES)
        ):
            found.append(triplet)

    return tuple(dict.fromkeys(found))


def url_belongs_to_other_model(url: str, keys: ModelKeys) -> bool:
    """Safeguard S4: does this image URL clearly belong to *another* model?

    ``True`` -> reject.  Keys **and** glued identities count as "ours", so
    ``HRF538IFGA-1.jpg`` is kept while ``HSU-18HFPAA-front.png`` is rejected.
    """
    if not keys.tokens:
        return False
    decoded = unquote(url)
    if find_matching_keys(decoded, keys, include_title_key=False):
        return False
    parts = urlsplit(decoded)
    path_parts = [segment for segment in parts.path.split("/") if segment]
    filename = path_parts[-1] if path_parts else ""
    folder = path_parts[-2] if len(path_parts) > 1 else ""
    if "." in filename:
        filename = filename.rsplit(".", 1)[0]
    tokens = code_like_tokens(f"{filename} {folder}")
    if not tokens:
        return False
    product_prefixes = {_alpha_prefix(token) for token in keys.tokens}
    product_prefixes.discard("")
    if not product_prefixes:
        return False
    return any(_alpha_prefix(token) in product_prefixes for token in tokens)


#: A capacity-and-series code, the way this catalog's rows and file names identify a
#: split AC: two or three digits plus one lone letter ("18x", "19c", "12x", "18g").
_CAPACITY_CODE_RE = re.compile(r"(?<![a-z0-9])\d{2,3}[a-z](?![a-z0-9])")


def capacity_codes(text: str) -> frozenset[str]:
    """The ``<capacity><series letter>`` codes in a title or a file name.

    Rows like ``Orient Split AC Hyper 18X Super White`` carry no HSU-style part number -
    their whole identity is that pair - and retailers write the same pair into the file
    name (``Orient-Super-18G-EVA-T3-…``).  Bounded on both sides on purpose: a size
    suffix (``-800x800``), an upload folder (``2024/12``) and a random asset id
    (``W020260404458450118371``) all fail the shape, so they cannot invent a code.
    """
    return frozenset(_CAPACITY_CODE_RE.findall(normalize_text(text)))


def url_names_other_capacity_code(url: str, keys: ModelKeys) -> bool:
    """Safeguard S4, second half: the file names *another* capacity code than the row.

    Live on 2026-10-10 (owner's run): the row ``Orient Split AC Hyper 18X Super White``
    published ``…/Orient-Super-18G-EVA-T3-1.5-Ton-DC-Inverter-Air-Conditioner2.jpg`` - a
    silver 18G EVA wearing an orange "1.1 Meter Long" badge, a different product - and
    :func:`url_belongs_to_other_model` let it through.  Its reason is exact: that row's
    only key is ``18X``, whose *alphabetic* prefix is empty, so S4's prefix comparison
    had nothing to compare and gave up (``if not product_prefixes: return False``).
    Comparing the digit-plus-letter code instead names the mismatch directly.

    Both sides must carry a code and they must be disjoint, so a page that also names
    our own code stays accepted (``…-1.5-Ton-Hyper-18X-T3-Super-White-Turbo-Pro.webp``
    is the correct file for this row and is kept by the intersection).
    """
    ours = capacity_codes(" ".join(keys.tokens)) or capacity_codes(keys.cleaned_title)
    if not ours:
        return False
    path = unquote(urlsplit(unquote(url)).path)
    theirs = capacity_codes(path.rsplit("/", 1)[-1])
    if not theirs:
        return False
    return not (ours & theirs)


#: The shortest digit tail that names a product family rather than a size or a view.
#: Two digits is a capacity ("60", "50", "18"); three is where a model number begins,
#: so the three public functions below default to it and ``model_code_tail_min`` can
#: raise it per run.
_FAMILY_TAIL_MIN = 3

#: The largest number a trailing photo counter may carry: ``…-1217-1``, ``…-1217-01`` and
#: ``…-1217-001`` are the first, second and third picture of the drum named just before.
_COUNTER_MAX_VALUE = 99

#: A number of at most this many digits followed by a unit word is a shipping quantity
#: ("9kg", "1200w", "1.5ton").  Longer than that, digits plus letters is a model code
#: wearing a unit lookalike - ``1217GB`` is gold/black on a Haier drum, not 1217 bytes.
_QUANTITY_MAX_DIGITS = 3


def _is_quantity(segment: str) -> bool:
    """Whether a segment states how much of something there is, not which model it is."""
    if not UNIT_QUANTITY_RE.match(segment):
        return False
    digits = re.match(r"\d+", segment)
    return digits is not None and len(digits.group()) <= _QUANTITY_MAX_DIGITS


def _is_code_noise(segment: str) -> bool:
    """Whether a name part says how big, how many or when - never which model.

    The tables S4 already trusts: a pixel size (``800x600``), an upload date
    (``20241201``), an editor counter (``img1234``), a hex asset id, a quantity
    (``9kg``).  Each of them would otherwise pose as part of a model number.
    """
    return _is_quantity(segment) or any(p.match(segment) for p in _NOISE_TOKEN_RES)


def code_digit_clusters(text: str) -> tuple[tuple[str, ...], ...]:
    """The digit groups of every *contiguous* code a text writes, in written order.

    One model code is written as one piece, so a cluster is a run of neighbouring name
    parts that carry digits: ``Haier-HWS-60-50.webp`` -> ``(("60", "50"),)`` and
    ``1301217E`` -> ``(("1301217"),)`` - the letters and separators a shop re-arranges at
    will (``HWM 80-1217``, ``hwm801217``) cannot change the digits.
    A word *between* two numbers means two statements about the product, and that is the
    difference between a code and a spec sheet: ``midea-1400-rpm-w120-front-load`` says
    1400 revolutions and model W120, so it yields ``(1400)`` and ``(120)`` and never the
    invented ``1400120`` that would accuse the row of another capacity (owner review,
    2026-10-17 - the same trap for ``kenwood-13-cu-ft-320gd``).  A name part that is
    commerce noise closes the cluster too.
    """
    done: list[tuple[str, ...]] = []
    current: list[str] = []
    for segment in re.split(r"[^A-Za-z0-9]+", normalize_text(text)):
        runs = () if _is_code_noise(segment) else re.findall(r"\d+", segment)
        if runs:
            current.extend(runs)
        elif current:
            done.append(_complete_code(tuple(current)))
            current = []
    if current:
        done.append(_complete_code(tuple(current)))
    return tuple(done)


def _is_trailing_counter(group: str, rest: tuple[str, ...]) -> bool:
    """Whether the last group of a cluster is *which picture*, not *which machine*.

    A single digit always is (``HWM_80-1217-1``).  A longer one is a counter only while a
    family number is already written beside it, so ``HWS-60-50`` keeps its ``50`` and
    ``W90DG5U34`` keeps its ``34``, while ``…-1217-01`` and ``…-1217-001`` - the shop's
    second and third photo of the same drum - go.
    """
    if len(group) == 1:
        return True
    return int(group) <= _COUNTER_MAX_VALUE and any(
        len(other) >= _FAMILY_TAIL_MIN for other in rest
    )


def _complete_code(runs: tuple[str, ...]) -> tuple[str, ...]:
    """A cluster as the code it means: photo counters off the end, a doubled code once.

    Then a code written twice collapses to one period: these sheets repeat the model in
    the title (``WA21CK6745BVRT-WA21CK6745BVRT``) and S4 reads that as a single key, so
    its digits come out ``21 6745 21 6745`` while every shop writes them once - without
    the collapse the row refuses its own hero (measured on the Samsung rows of
    2026-10-17).
    """
    while len(runs) >= 2 and _is_trailing_counter(runs[-1], runs[:-1]):
        runs = runs[:-1]
    for period in range(1, len(runs) // 2 + 1):
        if len(runs) % period == 0 and runs[:period] * (len(runs) // period) == runs:
            return runs[:period]
    return runs


def _colourway_tail(text: str) -> str:
    """The letters glued after the last digits of a code - the finish inside the model.

    ``1301217E`` and ``1301217GB`` are one number in two colours, and this repository
    treats that suffix as identity (owner 2026-10-07: the colourway is written inside the
    model code), so equal digits must never prove them one product.  Only the letters
    glued to the code itself count - ``…-1217E-white-9kg`` still says ``E`` - and an
    image extension is not a finish.
    """
    body = normalize_text(text)
    dot = body.rfind(".")
    if dot >= 0 and body[dot + 1 :] in IMAGE_EXTENSIONS:
        body = body[:dot]
    for segment in reversed(re.split(r"[^A-Za-z0-9]+", body)):
        if not segment or _is_code_noise(segment):
            continue
        match = re.search(r"\d+([a-z]*)$", segment)
        if match:
            return match.group(1)
    return ""


def _family_tail(
    our_groups: tuple[str, ...], their_groups: tuple[str, ...], *, tail_min: int
) -> str:
    """The number both codes end with, as somebody actually wrote it ("" = no family).

    A group of at least ``tail_min`` digits that both digit strings end in.  Reading
    the *longest common suffix* instead would eat a head digit: ``1301217`` against
    ``801217`` shares the suffix ``01217``, which is neither the row's number nor the
    file's, and the row ``DWF 85-1214`` against its own file ``Dawlance-85214`` shares
    ``214`` - a coincidence created by a moved hyphen, not a family.
    """
    ours, theirs = "".join(our_groups), "".join(their_groups)
    shared = [
        group
        for group in set(our_groups) | set(their_groups)
        if len(group) >= tail_min and ours.endswith(group) and theirs.endswith(group)
    ]
    return max(shared, key=len, default="")


def _codes_are_sibling_models(
    our_groups: tuple[str, ...], their_groups: tuple[str, ...], *, tail_min: int
) -> bool:
    """One pair of codes: same family number, different capacity in front of it."""
    tail = _family_tail(our_groups, their_groups, tail_min=tail_min)
    if not tail:
        return False
    our_digits = "".join(our_groups)
    their_digits = "".join(their_groups)
    our_head = our_digits[: len(our_digits) - len(tail)]
    their_head = their_digits[: len(their_digits) - len(tail)]
    if not our_head or not their_head:
        return False
    # Trailing zeros are the unit, not the machine: a row written "110-688S8" (tenths of
    # a kilogram) and its own file "haier-11-688-top-load" (kilograms) are one product,
    # while "130" against "80" is 13 kg beside 8 kg.
    return our_head.rstrip("0") != their_head.rstrip("0")


def code_names_other_family_code(
    ours: str, theirs: str, *, tail_min: int = _FAMILY_TAIL_MIN
) -> bool:
    """Safeguard S4, digit half: the file names a *sibling* of our number family.

    ``True`` -> reject.  Both codes end in the same family number - a group of at least
    ``tail_min`` digits somebody wrote whole - while the digits before it disagree: the
    owner's 2026-10-17 run published the 8 kg ``hwm80-1217`` and
    ``Haier_Single_Washing_Machine_HWM_80-1217-1`` into the gallery of the 13 kg
    ``1301217E`` row.  S4's prefix half was blind to it (a digit-headed code has no
    alphabetic prefix, so ``if not product_prefixes: return False`` gave up) and the
    capacity half needs the ``\\d{2,3}[a-z]`` shape on both sides, which neither code
    writes.  The digit structure sees it directly: family ``1217`` shared, ``130``
    against ``80``.

    Each further condition is a false positive the live measurement ruled out: a file
    that writes the family and no head (``…/1217-front-view.jpg``) claims nothing about
    the capacity; numbers separated by a word are specs, not one code; a tail nobody
    wrote whole is a coincidence; and a file that carries our own code clears itself.
    """
    if code_confirms_this_model(ours, theirs, tail_min=tail_min):
        return False
    return any(
        _codes_are_sibling_models(our_code, their_code, tail_min=tail_min)
        for our_code in code_digit_clusters(ours)
        for their_code in code_digit_clusters(theirs)
    )


def code_confirms_this_model(
    ours: str, theirs: str, *, tail_min: int = _FAMILY_TAIL_MIN
) -> bool:
    """The proof half of the same reading: both sides write one and the same code.

    ``6050`` confirms ``HWS-60-50`` and ``W90DG5U34ABFA`` confirms ``WW90DG5U34ABFA`` -
    different shops, same machine.  It is a *proof*, so it may only agree or say
    nothing: ``…U34ABFA`` against ``…U36ABFA`` is two products, and ``1301217E`` against
    ``1301217GB`` is one number in two colours - the colourway suffix is identity here,
    so a digit match alone must never claim the two rows are one product.
    """
    if _colourway_tail(ours) != _colourway_tail(theirs):
        return False
    our_codes = ["".join(cluster) for cluster in code_digit_clusters(ours)]
    their_codes = ["".join(cluster) for cluster in code_digit_clusters(theirs)]
    return any(
        len(digits) >= tail_min and digits == other
        for digits in our_codes
        for other in their_codes
    )


def url_names_other_family_code(
    url: str, keys: ModelKeys, *, tail_min: int = _FAMILY_TAIL_MIN
) -> bool:
    """S4's digit half read off an image URL: this row's codes against the file's.

    Only the row's own key tokens speak - a row S4 cannot read a code from stays silent,
    because its digits are then inches, tonnages and BTU ratings, and a file naming
    those would be accused for nothing.  One token writing the file's digits clears the
    file; otherwise a single token that shares the family tail and names a different
    head rejects it.  Only the file name is read - a category folder or an upload date
    is not a statement about the product.
    """
    if not keys.tokens:
        return False
    filename = urlsplit(unquote(url)).path.rsplit("/", 1)[-1]
    if not filename:
        return False
    if any(code_confirms_this_model(code, filename, tail_min=tail_min) for code in keys.tokens):
        return False
    return any(
        code_names_other_family_code(code, filename, tail_min=tail_min)
        for code in keys.tokens
    )


#: The shortest a word may be to name a product line: ``mega``, ``frost``, ``ifga``
#: qualify; ``ac``, ``t3``, ``pro`` are too short to carry that meaning on their own.
_SIBLING_WORD_MIN_LENGTH = 4

#: The hump where a lower-case letter or digit meets an upper-case one: studios glue a
#: line's own name into one camel-case token (``eComfortPro.png`` is ``E-Comfort Pro``),
#: and reading the hump as a separator recovers the words instead of one unknown blob.
_CAMEL_HUMP_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _filename_words(filename: str) -> set[str]:
    """The words of a file name: separators *and* camel-case humps divide words.

    ``15-ton-innova-ultra-white`` yields ``ton``, ``innova``, ``white`` - and
    ``eComfortPro`` yields ``comfort`` (owner run 2026-10-12: the E-Comfort Pro row's
    own hero was refused as "another product line" because the plain split saw one
    glued word ``ecomfortpro`` that the row did not own).  Editor defaults such as
    ``UntitledProject`` split too - and die as commerce noise, not as an accusation.
    """
    camel = _CAMEL_HUMP_RE.sub(" ", filename)
    return {
        segment
        for segment in re.split(r"[^a-z]+", camel.lower())
        if len(segment) >= _SIBLING_WORD_MIN_LENGTH
    }


def _series_words(title: str) -> frozenset[str]:
    """The words a title uses to *name* its product (see ``ModelKeys.name_words``).

    Codes do not count here: rows like ``HRF-538 IFGA`` are identified by their tokens,
    and two variants of one model (``IFGA`` / ``IFPA``) already sit under S4's code
    limbs and the variant-tail flow - a glued identity ending in the word (``hrf538ifga``
    ends in ``ifga``) proves the word is a variant tail, not a series name, and the
    tolerated-variant flow must keep publishing those pages' photos.  Only free words
    stay - the series names a catalogue writes into its file names.
    """
    keys = build_model_keys(title)
    return frozenset(
        word
        for word in keys.name_words
        if word.isalpha()
        and len(word) >= _SIBLING_WORD_MIN_LENGTH
        and not any(identity.endswith(word) for identity in keys.identities)
    )


def _own_anchor_words(own_terms: frozenset[str]) -> set[str]:
    """The own line words that prove a name belongs to *this* row.

    The brand and the catalogue noise never anchor: every Orient asset says ``orient``,
    so the word proves nothing and would let ``orient-blaze-…`` pose as any Orient row.
    Shared by S4's file limb (:func:`url_anchored_to_row`) and the tail-chunk limb's
    foreign-series veto, so the two cannot drift apart on what counts as our own name.
    """
    return {
        word
        for word in own_terms
        if word not in KNOWN_BRANDS and word not in _KEYWORD_EXTRA_NOISE
    }


def sibling_series_terms(titles: Sequence[str]) -> tuple[frozenset[str], ...]:
    """Sheet knowledge for S4: for each row, the series words of the *other* rows.

    The Orient run of 2026-10-10 is the lesson: the Pulse row's tolerated page carried
    the Innova file too, and no limb of S4 could see the difference - neither URL
    prints the ``19C`` key, and ``pulse`` / ``innova`` are words, not codes.  The sheet
    itself names the difference: a word appearing in exactly one row's title is that
    row's name, and from every other row it names *another product*.

    Only such single-row words can count; a word on two or more rows (``ultra``,
    ``orient``) is a shared modifier, never an identity.  One frozenset per input
    title, in order; a single-row sheet yields empty sets and the limb stays silent.
    """
    per_row = [_series_words(title) for title in titles]
    counts: Counter[str] = Counter()
    for words in per_row:
        counts.update(words)
    unique = {word for word, count in counts.items() if count == 1}
    return tuple(
        frozenset(unique - own) for own in per_row
    )


def url_names_sibling_product(url: str, sibling_terms: frozenset[str]) -> bool:
    """Safeguard S4, third limb: the file name names another product of this sheet.

    ``True`` -> reject.  The file name is read as plain words (``15-ton-innova-…`` ->
    ``ton``, ``innova``, …); folders are not evidence, a category path like
    ``/air-conditioners/`` must never speak about the product.  With no sibling terms
    (single-row sheet, code-only catalogue) the limb is silent.
    """
    if not sibling_terms:
        return False
    filename = urlsplit(unquote(url)).path.rsplit("/", 1)[-1]
    filename = filename.rsplit(".", 1)[0]
    return bool(_filename_words(filename) & sibling_terms)


def annotate_sibling_terms(tasks: Sequence[ProductTask]) -> list[ProductTask]:
    """Give every sheet row the series words of the *other* rows (S4's third limb).

    The Orient run of 2026-10-10: the Pulse row published the Innova file from a
    tolerated page because neither URL prints the ``19C`` key.  The sheet itself names
    the difference, so the sheet is threaded into the per-row decision.  A single-row
    sheet (or any code-only catalogue) yields empty sets and the limb stays silent.

    Each row also keeps its *own* series words, so a file that carries one of them is
    recognised as anchored to the row and the sibling limb leaves it alone.
    """
    titles = [task.title for task in tasks]
    terms = sibling_series_terms(titles)
    own = [_series_words(title) for title in titles]
    return [
        replace(task, sibling_terms=sibling, own_terms=mine)
        for task, sibling, mine in zip(tasks, terms, own, strict=True)
    ]


#: The shape of a shared capacity pair ("18x", "19c"): the whole identity of a bare
#: capacity row, and the one code that many models of one maker share.
_CAPACITY_SHAPE_RE = re.compile(r"\d{2,3}[a-z]")


def _strong_anchor_keys(keys: ModelKeys) -> ModelKeys:
    """The keys that prove a file on their own.

    A real part number (``hrf538ifga``, ``dw7500c``, ``msez2c18hrfn1``) names exactly
    one product.  The shared capacity pair (``18x``) and the capacity-glued identity
    (``19cultra``) do not: the pair names every model of the tonnage, and the glued
    shape names the whole product line (owner run 2026-10-11: ``blaze-19c-ultra``
    spells ``19cultra`` too).  Neither may anchor a file that a foreign product word
    accuses, so they are filtered out before the anchor asks the URL anything.
    """
    strong_tokens = tuple(
        token
        for token in keys.tokens
        if token
        and not _CAPACITY_SHAPE_RE.fullmatch(token.lower())
        and not identity_is_capacity_glued(token.lower())
    )
    strong_identities = tuple(
        identity
        for identity in keys.identities
        if identity
        and not _CAPACITY_SHAPE_RE.fullmatch(identity.lower())
        and not identity_is_capacity_glued(identity.lower())
    )
    return replace(keys, tokens=strong_tokens, identities=strong_identities)


def url_anchored_to_row(
    url: str,
    keys: ModelKeys,
    *,
    brand: str | None = None,
    own_terms: frozenset[str] = frozenset(),
) -> bool:
    """True when a file name provably belongs to *this* row.

    A file is anchored when it carries one of the row's *strong* keys - a real part
    number, never the shared capacity pair or a capacity-glued identity - or one of the
    row's own line words.  An anchored file stays even if another product's word
    decorates the same name (live 2026-10-11: the Hyper row's ``…Hyper-18X-T3-Super-
    White-Turbo-Pro.webp`` also says ``turbo``, another row's word, but ``hyper``
    proves it is this row's picture; ``18X`` alone would not, because the sibling file
    of another series can carry the same capacity code).  The brand and the catalogue
    noise never anchor: every Orient asset says ``orient``, so the word proves nothing
    and would let ``orient-blaze-…`` pose as any Orient row.
    """
    strong = _strong_anchor_keys(keys)
    if (strong.tokens or strong.identities) and url_matches_this_product(
        url, strong, brand=brand
    ):
        return True
    usable = _own_anchor_words(own_terms)
    if usable:
        filename = urlsplit(unquote(url)).path.rsplit("/", 1)[-1]
        filename = filename.rsplit(".", 1)[0]
        words = _filename_words(filename)
        if words & usable:
            return True
        # A glued word that still carries an own line word ("pulseultra", no camel
        # humps to split on) proves the file just as well - the anchor errs on the
        # keep side, because refusing the row's own picture is the owner-visible bug.
        if any(own_word in word for word in words for own_word in usable if len(own_word) >= 4):
            return True
    return False


#: File-name words that describe commerce and photography, never a product line.  A
#: foreign word is evidence of *another product* only after these stay silent, so a
#: retailer's ``…-hero-banner-sale.webp`` can never pose as a sibling model.
_URL_COMMERCE_NOISE = frozenset((
    "sale", "offer", "offers", "deal", "deals", "discount", "price", "prices",
    "shop", "store", "online", "buy", "order", "delivery", "shipping", "warranty",
    "install", "installation", "support", "latest", "arrival", "exclusive",
    "genuine", "official", "authentic", "package", "bundle", "combo", "stabilizer",
    "copper", "energy", "saving", "karachi", "lahore", "islamabad",
    "products", "product", "images", "image", "photo", "photos", "gallery",
    "assets", "media", "files", "uploads", "thumbnail", "thumb", "banner", "hero",
    "main", "front", "back", "side", "view", "detail", "zoom", "extra", "large",
    "small", "square",
    # file-name decorations a studio or CDN appends to the very same picture, and the
    # page furniture that names a graphic, never a product (S14 judges emblems itself).
    # Editor-default exports (``UntitledProject-…``) name nothing, so they accuse nothing.
    "lowq", "highq", "hires", "lores", "orig", "copy", "final", "edited",
    "retouch", "render", "mockup", "emblem", "badge", "icon", "watermark",
    "stamp", "seal", "untitled", "project", "artboard", "design",
    # Camera-export artefacts: a phone photo keeps the app's own name
    # (``WhatsAppImage2026-…``), and the camel hump splits it into ``whats`` - a
    # fragment that names no product line but would accuse the row's own picture
    # (live Midea Eco official gallery, 2026-10-13).
    "whatsapp", "whats",
))

_HEX_HASH_RE = re.compile(
    r"^[0-9a-fA-F\-_]{16,}$|^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?:-\d+)?$"
)


#: Longest/shortest noise word - the greedy split starts with the widest step and
#: shrinks to the table's own floor, so no entry is ever unreachable.
_MAX_NOISE_WORD_LEN = max(len(word) for word in _URL_COMMERCE_NOISE)
_MIN_NOISE_WORD_LEN = min(len(word) for word in _URL_COMMERCE_NOISE)


def _decomposes_into_noise(word: str) -> bool:
    """Whether a glued file-name word is only commerce noise re-assembled.

    A merchant who exports ``Untitledproject`` with a lowercase p defeats the camel
    split, and the plain reader sees one word the row does not own - live run
    2026-10-15, rd.com.pk's only two pictures died as "another product line".  If the
    word decomposes entirely into **two or more** existing noise words (greedy,
    longest first) it names no product, exactly like its spaced form, so it accuses
    nothing.  No new vocabulary: the existing table alone decides.  The greedy scan
    never backtracks, so a compound whose widest-first bite overshoots (``imagesale``
    eats ``images`` and strands ``ale``) still accuses - erring toward the guard,
    never past it.
    """
    index = 0
    parts = 0
    while index < len(word):
        for length in range(_MAX_NOISE_WORD_LEN, _MIN_NOISE_WORD_LEN - 1, -1):
            if word[index : index + length] in _URL_COMMERCE_NOISE:
                index += length
                parts += 1
                break
        else:
            return False
    return parts >= 2


def url_names_other_product_line(url: str, keys: ModelKeys) -> bool:
    """Safeguard S4, fourth limb - sheet knowledge not required.

    The owner run of 2026-10-11 downloaded ``15-ton-innova-ultra-white-dc-inverter``
    for the ``Pulse 19C Ultra`` row: the sheet has no Innova row, so the sibling limb
    was silent, and the file carries no code for the code limbs to compare.  But the
    file *names itself*: a product word the row does not own, that is no brand, no
    category, no colour and no commerce word.  A row with a line name (``pulse``)
    refuses such a file; a bare-capacity row has no line to protect and stays with its
    code guards.  The limb only names the danger - a file anchored to this row by its
    own key or its own line word still stays (see :func:`url_anchored_to_row`).
    """
    if not has_series_name(keys):
        return False
    filename = urlsplit(unquote(url)).path.rsplit("/", 1)[-1]
    filename = filename.rsplit(".", 1)[0]
    if _HEX_HASH_RE.match(filename) and any(c.isdigit() for c in filename):
        return False
    words = _filename_words(filename)
    if not words:
        return False
    own = set(keys.name_words)
    foreign = {
        word
        for word in words
        if word not in own
        and not any(
            own_word in word for own_word in own if len(own_word) >= 4 and own_word not in KNOWN_BRANDS
        )
        and word not in KNOWN_BRANDS
        and word not in _GENERIC_NAME_WORDS
        and word not in _KEYWORD_EXTRA_NOISE
        and word not in _URL_COMMERCE_NOISE
        and not is_colour_word(word)
        and not (len(word) <= 4 and set(word).issubset(set("abcdef")))
        and not _decomposes_into_noise(word)
    }
    return bool(foreign)


def url_names_foreign_brand(url: str, brand: str | None) -> str | None:
    """The maker a file's own name prints, when it is not this row's maker.

    The page gate judges the page's visible title, but a direct image URL carries no
    page at all, and a degraded fetch may print no maker - either way a file named
    after another known maker is never this product (owner run 2026-10-12:
    ``panasonic.jpg`` published for the Orient Megatron row).  Judged on the file name
    alone - directory paths host every brand on a retail CDN - and whole-word only, so
    ``oriental-rug.jpg`` does not name ``orient``.  A row without a detected brand has
    nothing to compare against.
    """
    if not brand:
        return None
    filename = urlsplit(unquote(url)).path.rsplit("/", 1)[-1]
    filename = filename.rsplit(".", 1)[0]
    filename = _CAMEL_HUMP_RE.sub(" ", filename)
    shown = page_product_brand(re.sub(r"[^a-z0-9]+", " ", filename.lower()))
    if shown is not None and shown != brand.lower():
        return shown
    return None


def is_display_product(title: str) -> bool:
    """True for TVs, monitors, and screens where display wallpapers contain arbitrary colours.

    Televisions and monitors do not have distinct chromatic body variants (they are black/grey
    bezels) and their product photography intentionally displays vivid colourful scenes
    (nature wallpapers, sunsets, oceans). Safeguard S16 (chromatic variant gating) is therefore
    bypassed for display products so valid TV shots are not rejected.
    """
    t = title.lower()
    words = set(re.findall(r"[a-z0-9]+", t))
    if {"tv", "television", "monitor", "oled", "qled"} & words:
        return True
    return bool("led" in words and any(w in words for w in ("inch", "in", "smart", "fhd", "uhd", "4k", "2k", "8k")))


# --------------------------------------------------------------------------- #
# colour check (plan section 3.6)
# --------------------------------------------------------------------------- #
def extract_colour_families(text: str) -> frozenset[str]:
    """Colour families named in ``text`` (synonyms collapse onto one family)."""
    normalized = WHITESPACE_RE.sub(" ", normalize_text(text))
    if not normalized:
        return frozenset()
    padded = f" {normalized} "
    found: set[str] = set()
    for family, synonyms in COLOUR_FAMILIES.items():
        for synonym in synonyms:
            needle = f" {normalize_text(synonym)} "
            if needle in padded:
                found.add(family)
                break
    return frozenset(found)


# --------------------------------------------------------------------------- #
# variant check (plan section 3.6)
# --------------------------------------------------------------------------- #
def extract_variant_facts(text: str) -> dict[str, str]:
    """Capacity / size / type facts, e.g. ``{"capacity_kg": "10", "load": "top"}``."""
    facts: dict[str, str] = {}
    normalized = WHITESPACE_RE.sub(" ", normalize_text(text))
    for match in _CAPACITY_RE.finditer(normalized):
        raw_number = match.group(1).replace(",", ".")
        try:
            number = float(raw_number)
        except ValueError:                        # pragma: no cover - regex guarantees
            continue
        value = str(int(number)) if number.is_integer() else f"{number:g}"
        unit = WHITESPACE_RE.sub(" ", match.group(2).strip())
        axis = _UNIT_AXIS.get(unit)
        if axis is None:
            continue
        facts.setdefault(axis, value)
    for axis, options in TYPE_AXES.items():
        for value, synonyms in options.items():
            if any(f" {normalize_text(synonym)} " in f" {normalized} " for synonym in synonyms):
                facts.setdefault(axis, value)
                break
    return facts


def variant_conflicts(title_facts: dict[str, str], page_facts: dict[str, str]) -> tuple[str, ...]:
    """Axes where both sides give a value and the values differ (publish + flag)."""
    return tuple(
        sorted(
            axis
            for axis, value in title_facts.items()
            if axis in page_facts and page_facts[axis] != value
        )
    )


# --------------------------------------------------------------------------- #
# brand + code match (simple and generic - "the brand counts")
# --------------------------------------------------------------------------- #
def brand_match_cores(identity: str, brand: str = "") -> tuple[str, ...]:
    """Accepted spellings of a code, longest first.

    ``hrf538ifga`` (Haier) -> ``("hrf538ifga", "rf538ifga", "538ifga")``
    ``55s80eux``   (no brand letters) -> ``("55s80eux",)``

    Both shorter spellings exist because sites print the model without the brand's own
    letters: ``HRF-538 IFGA`` appears as ``RF-538 IFGA`` or plain ``538 IFGA``.  The
    letters are only dropped when the brand name is present in the text (checked by
    :func:`find_brand_code_matches`), which is the customer's "brand counts" rule.
    """
    cores: list[str] = [identity]
    prefix = _leading_letters(identity)
    if prefix and len(identity) > len(prefix):
        normalized_brand = normalize_key(brand) if brand else ""
        if not normalized_brand or prefix.startswith(normalized_brand[:1]):
            cores.append(identity[1:])             # brand initial dropped ("rf538ifga")
            cores.append(identity[len(prefix):])   # whole family prefix dropped ("538ifga")
    return tuple(dict.fromkeys(cores))


#: How many letters may sit directly in front of a code (``HSF-538``).
_CODE_PREFIX_MAX = 4

#: Letters only stick to a code through a hyphen/underscore (``HSF-538``).  A space or
#: a slash separates words ("Haier 538 IFGA", ".../p/538-ifga.jpg"), so those letters
#: are *not* a prefix and can never be mistaken for a conflicting family.
_PREFIX_JOIN = r"[\-_]*"


def _family_letters(identity: str, brand: str) -> str:
    """Letters of ``identity`` that belong to the family, without the brand initial.

    ``hrf538ifga`` (Haier) -> ``"rf"``; ``hsu12hfpaa`` (Haier) -> ``"su"``;
    ``55s80eux`` -> ``""``.
    """
    prefix = _leading_letters(identity)
    brand_initial = normalize_key(brand)[:1] if brand else ""
    if brand_initial and prefix.startswith(brand_initial):
        return prefix[1:]
    return prefix


def _prefix_is_allowed(observed: str, identity: str, brand: str) -> bool:
    """May these letters sit in front of the code?

    ``""`` (nothing), the brand initial (``H`` of ``H55S80EUX``), or the sheet's own
    family letters (``RF`` of ``HRF-538``) are fine.  Anything else is a conflict:
    ``Haier HSF-538 IFGA`` must not satisfy ``Haier HRF-538 IFGA``.
    """
    remainder = observed
    brand_initial = normalize_key(brand)[:1] if brand else ""
    if brand_initial and remainder.startswith(brand_initial):
        remainder = remainder[1:]
    if not remainder:
        return True
    family = _family_letters(identity, brand)
    return bool(family) and family.startswith(remainder)


def code_text(text: str) -> str:
    """Lowercase and collapse whitespace, but keep ``-`` ``_`` ``/`` ``.`` in place.

    The prefix check needs the hyphen: ``HSF-538`` must be recognised as ``hsf`` +
    ``-`` + code, which :func:`normalize_text` (it turns every separator into a space)
    would destroy.
    """
    return WHITESPACE_RE.sub(" ", text.lower())


def _code_search(text: str, core: str, identity: str, brand: str) -> bool:
    """Find ``core`` in ``text``: separator-tolerant, whole-token, prefix-checked.

    ``538ifga`` matches ``538 IFGA`` and ``RF-538 IFGA`` (the sheet's own letters) but
    not ``SF-538 IFGA``.
    """
    body = _SEP.join(re.escape(char) for char in core)
    pattern = re.compile(
        rf"(?<![a-z0-9])([a-z]{{0,{_CODE_PREFIX_MAX}}}){_PREFIX_JOIN}{body}(?![a-z0-9])"
    )
    return any(
        _prefix_is_allowed(match.group(1), identity, brand)
        for match in pattern.finditer(text)
    )


def _usable_core(core: str, *, min_length: int, min_digits: int) -> bool:
    """A code is only used for the brand+code rule when it is distinctive enough."""
    return (
        len(core) >= min_length
        and len(re.findall(r"\d", core)) >= min_digits
        and re.search(r"[a-z]", core) is not None
    )


def find_brand_code_matches(
    text: str,
    keys: ModelKeys,
    *,
    brand: str,
    min_length: int = 5,
    min_digits: int = 3,
    require_brand_named: bool = True,
) -> tuple[str, ...]:
    """Codes of this product found in ``text`` while the brand is named there.

    Covers "the site prints the model without the brand's letters" (``538 IFGA`` for
    ``HRF-538 IFGA``) and the dropped trailing letter (``DW-7500`` for ``DW-7500C``).
    With ``require_brand_named=False`` the caller has already established the brand
    (the S12 fallback anchor, which only looks at a single image URL).
    """
    if not brand or not keys.identities:
        return ()
    if require_brand_named and not brand_token_present(text, brand):
        return ()
    normalized = code_text(text)
    found: list[str] = []
    for identity in keys.identities:
        for core in brand_match_cores(identity, brand):
            matched = None
            for candidate in (core, *truncation_variants(core)):
                if not _usable_core(candidate, min_length=min_length, min_digits=min_digits):
                    continue
                if _code_search(normalized, candidate, identity, brand):
                    if candidate != core and _page_names_other_short_tail(
                        candidate, normalized,
                        keys.colourway_markers | frozenset({core[len(candidate):]}),
                    ):
                        continue  # our stem printed to another short tail is another product
                    matched = candidate
                    break
            if matched is not None:
                found.append(matched)
                break
    return tuple(dict.fromkeys(found))


def fuzzy_series_anchor(keys: ModelKeys) -> frozenset[str]:
    """The row's own line names - the words a page/file must carry before a
    capacity-shaped match may speak for it.

    Owner run 2026-10-11: the row ``Pulse 19C Ultra`` glues its identity ``19cultra``
    out of the capacity code ``19C`` and the tail ``ultra`` - and Orient sells a whole
    *line* called Ultra (Blaze 19C Ultra, Glide 19C Ultra, Innovia 19S Ultra ...).  The
    glued shape therefore names four different products, and the fuzzy letter tolerance
    (``19[a-z]?ultra``, built for Titanium 12X vs 12S) named a fifth.  What separates
    the products is the line name: ``pulse``.  The anchor is the row's name words minus
    the brand (``orient``), minus catalogue noise (``pakistan`` from the pipe tail) and
    minus the glued identity's own tail (``ultra`` is part of ``19cultra`` and shared by
    the whole line, so it can never anchor anything).  A bare capacity row has no anchor
    and no line to protect - its codes guard it alone.
    """
    return frozenset(
        word
        for word in keys.name_words
        if word not in KNOWN_BRANDS
        and word not in _KEYWORD_EXTRA_NOISE
        and not any(identity.endswith(word) for identity in keys.identities)
    )


def has_series_name(keys: ModelKeys) -> bool:
    """True when the row names a series/line beyond its brand (``Hyper``, ``Megatron``).

    A bare capacity row (``Orient 18X White`` -> name words ``('orient',)``) has nothing
    but the code to identify it; a named row has the code *and* the line, and the fuzzy
    route uses that name so the bare-code route must not widen its matches.  The pipe
    tail of marketplace titles (``| Orient Electronics Pakistan``) is not a line name.
    The glued identity's tail counts here (``QG 12X Aura`` is a named row even though
    ``aura`` glues into the identity) - only :func:`fuzzy_series_anchor` drops it.
    """
    return any(
        word not in KNOWN_BRANDS and word not in _KEYWORD_EXTRA_NOISE
        for word in keys.name_words
    )


def identity_is_capacity_glued(identity: str) -> bool:
    """True when a glued identity is a capacity code plus a word tail (``19cultra``).

    That shape is the ambiguous one: the tail is a word, so another product that shares
    the capacity family (``blaze-19c-ultra``) or the tail (``innova-19s-ultra``) spells
    the same shape.  The shape must *start* with the digits - a real part number that
    merely contains digits mid-name (``hsu12hfpaa``) is not ambiguous and stays trusted
    wherever it appears.
    """
    return _FUZZY_CODE_RE.match(identity) is not None


def url_matches_this_product(
    url: str,
    keys: ModelKeys,
    *,
    brand: str | None = None,
    brand_match_enabled: bool = True,
    min_length: int = 5,
    min_digits: int = 3,
) -> bool:
    """Is this image URL anchored to *this* product's model (S4 / S12 anchor)?

    The plain model key is the first answer; a brand+code spelling is accepted as well
    so a page that prints ``538 IFGA`` keeps its own ``538-ifga`` photos.  Used by the
    S12 fallback, where an unanchored URL is never taken.
    """
    if not keys:
        return False
    decoded = unquote(url)
    if find_matching_keys(decoded, keys, include_title_key=False):
        return True
    if brand_match_enabled and brand:
        return bool(
            find_brand_code_matches(
                decoded,
                keys,
                brand=brand,
                min_length=min_length,
                min_digits=min_digits,
                require_brand_named=False,
            )
        )
    return False


def brand_token_present(text: str, brand: str) -> bool:
    """True when the brand name appears as a whole word in ``text``."""
    if not brand:
        return False
    pattern = rf"(?<![a-z0-9]){re.escape(brand.lower())}(?![a-z0-9])"
    return re.search(pattern, normalize_text(text)) is not None


# --------------------------------------------------------------------------- #
# the one decision every stage uses
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ProductMatch:
    """Outcome of the product-recognition step (stage 2 / ``verify_page``)."""

    matched: bool
    rule: str                 # model_key | brand_code | body_model_key | body_brand_code | none
    found: tuple[str, ...]
    in_body_only: bool = False
    #: True when the page only matched after the *dropped-trailing-letter* tolerance
    #: (``DW-7500`` printed where the sheet says ``DW-7500C``).  That is a tolerated
    #: spelling, not proof: the pipeline keeps looking and uses such a page, flagged, only
    #: when nothing prints the model itself (live Dawlance row, 2026-10-06).
    tolerated: bool = False

    @property
    def strength(self) -> int:
        """2 when the model was found in the page's own name/URL, else 1 (body)."""
        if not self.matched:
            return 0
        return 1 if self.in_body_only else 2


def _needs_key_tolerance(text: str, keys: ModelKeys, allow_key_truncation: bool) -> bool:
    """True when the page only matched because a trailing letter was tolerated.

    The one owner-approved spelling tolerance (2026-10-05) is the *dropped trailing
    letter*: the sheet says ``DW-7500C`` and dawlance.com.pk prints ``DW-7500`` on the very
    page the sheet points at.  The page is worth keeping - it may be the only source for
    that machine - but it is not proof the page shows our model, so the pipeline first
    looks for a page that really prints it and falls back to this one, flagged, only after
    the whole chain found nothing better.

    A keyword row always matches exactly (no codes) and is never tolerated here.
    """
    if not allow_key_truncation:
        return False
    if not keys.tokens:
        return False                     # keyword rows match exactly or not at all
    return not find_matching_keys(text, keys, include_title_key=False, include_truncation=False)


def _keyword_page_match(keys: ModelKeys, text: str) -> bool:
    """The page carries the row's keywords - the model identity of a code-less row.

    Every number/tag (``30``, ``t3``) must be there as a whole word, and at least
    80% of the alphabetic keywords; a size/type fact the page contradicts (1 Ton
    row on a 1.5 Ton page) is a different product and never matches.
    """
    words = set(normalize_text(text).split())
    required = [key for key in keys.keywords if re.search(r"\d", key)]
    alpha = [key for key in keys.keywords if key.isalpha()]
    if any(key not in words for key in required):
        return False
    if alpha and sum(key in words for key in alpha) / len(alpha) < 0.8:
        return False
    return not variant_conflicts(keys.variant_dict(), extract_variant_facts(text))


def _recognize(
    *,
    keys: ModelKeys,
    brand: str | None,
    strong_text: str,
    body_text: str,
    brand_match_enabled: bool,
    brand_match_min_length: int,
    brand_match_min_digits: int,
    allow_key_truncation: bool,
) -> ProductMatch:
    """The recognition stages, in one readable function.

    Stage A - the model key itself (title/URL/H1 fields, then body text).
    Stage B - brand + code, for sites that print the model without the brand letters.
    Nothing else: no scores, no thresholds, no weighting.

    Code-less rows (owner 2026-10-09: "Dawlance Dual Pro 30 Inverter T3" *is* the
    model) match by their keywords instead - required numbers whole, >= 80% of the
    alphabetic words, no contradicted size fact.
    """
    if not keys.tokens and keys.keywords:
        for text, rule in ((strong_text, "keyword"), (body_text, "body_keyword")):
            if text and _keyword_page_match(keys, text):
                return ProductMatch(True, rule, keys.keywords)
    found = find_matching_keys(
        strong_text, keys, include_title_key=False, include_truncation=allow_key_truncation
    )
    if found:
        return ProductMatch(
            True,
            "model_key",
            tuple(dict.fromkeys(found)),
            tolerated=_needs_key_tolerance(strong_text, keys, allow_key_truncation),
        )


    if brand_match_enabled and brand:
        found = find_brand_code_matches(
            strong_text,
            keys,
            brand=brand,
            min_length=brand_match_min_length,
            min_digits=brand_match_min_digits,
        )
        if found:
            return ProductMatch(True, "brand_code", found)

    found = find_matching_keys(
        body_text, keys, include_title_key=False, include_truncation=allow_key_truncation
    )
    if found:
        return ProductMatch(
            True,
            "body_model_key",
            tuple(dict.fromkeys(found)),
            in_body_only=True,
            tolerated=_needs_key_tolerance(body_text, keys, allow_key_truncation),
        )


    if brand_match_enabled and brand:
        found = find_brand_code_matches(
            body_text,
            keys,
            brand=brand,
            min_length=brand_match_min_length,
            min_digits=brand_match_min_digits,
        )
        if found:
            return ProductMatch(True, "body_brand_code", found, in_body_only=True)

    return ProductMatch(False, "none", ())


def shade_unstated_rows(tasks: Sequence[ProductTask]) -> tuple[tuple[str, str], ...]:
    """Rows naming a bare colour while their **own** sheet link names a shade.

    A row written ``… 9 Cu Ft Red (9140-WB-GD)`` states no shade, and 9140-WB is sold as
    Pearl, Ruby and Diamond Red at once - so no matcher can know which one was promised, and
    the sheet is the only place that can say it.  Each row's own link is read for the hint
    (``ref-9140wb-avante-pearl-red…`` -> ``pearl red``) and the pair is reported, so the owner
    writes the shade once instead of guessing from the pictures that came back.

    Report-only, by owner decision 2026-10-16: a sheet link is sometimes the wrong page
    (``Gem Black`` beside ``sapphire-purple``), so it is never allowed to decide a match.  A
    hint is offered only when the link's colour *head* is the one the row already states.
    """
    out: list[tuple[str, str]] = []
    for task in tasks:
        keys = build_model_keys(task.title)
        if not keys.colour_word or keys.colour_shade:
            continue
        for url in task.source_urls[:1]:
            slug = unquote(urlsplit(url).path).rsplit("/", 1)[-1]
            head, shade = colour_phrase(slug, _COLOURWAY_WORDS)
            if shade and head == keys.colour_word:
                out.append((task.title, f"{shade} {head}"))
    return tuple(out)


def _page_names_another_variant(keys: ModelKeys, strong_text: str) -> bool:
    """True when the page prints a **different** variant suffix of the same model stem.

    The page's own text goes through the same tokenizer, so no brand, suffix or model
    table is involved.  Only a positively different suffix counts (``ifga`` vs ``ifpa``):
    a page that omits the suffix says nothing and stays with the dropped-trailing-letter
    rule, and a page with no related code has no opinion at all.
    """
    return suffix_agreement(keys.identities, build_model_keys(strong_text).identities) == "different"


def _page_names_different_model(keys: ModelKeys, strong_text: str) -> bool:
    """True when the page's own name prints a *sibling family* code.

    Live 2026-10-09: the row is ``GS-12PITH17G-T3`` and the last-resort chain kept a
    ``GS-12AITH24W`` page - our code only appeared in its related-item links, while
    the page's own title names another GREE family.  A tolerated demotion is for
    pages that *might* show our product; a page that positively names a different
    family (same brand prefix ``gs12``, different stem letters) is another product
    and must never publish (owner: "bilkul different model lelya").  Suffix-only
    differences (``HRF-538 IFGA`` vs ``IFPA``) do not parse as families - they stay
    with the tolerated rule, exactly as before.
    """
    page = build_model_keys(strong_text)
    for page_identity in page.identities:
        page_match = _FAM_VARIANT_RE.match(page_identity)
        if page_match is None or len(page_match.group(1)) < 5:
            continue
        for identity in keys.identities:
            own = _FAM_VARIANT_RE.match(identity)
            if own is None or len(own.group(1)) < 5:
                continue
            family, page_family = own.group(1), page_match.group(1)
            if family == page_family:
                continue
            prefix = 0
            while (
                prefix < min(len(family), len(page_family))
                and family[prefix] == page_family[prefix]
            ):
                prefix += 1
            if prefix >= 4 and family[prefix - 1].isdigit():
                return True
    return False


@dataclass(frozen=True)
class ScoreMatch:
    """The scored matcher's view of (row, page): per-attribute vetoes + a confidence.

    A veto means the page positively contradicts the row on that attribute and may
    never publish; the pipeline demotes it to the fallback / flagged band instead.
    ``confidence`` is the share of the row's name words the page also carries - 1.0 when
    the row names nothing to compare (fail open).
    """

    series_veto: bool
    colour_veto: bool
    size_veto: bool
    confidence: float = 1.0

    @property
    def veto(self) -> bool:
        """Any positive contradiction - the page is not this product."""
        return self.series_veto or self.colour_veto or self.size_veto


_URL_TOKEN_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _page_name_words(strong_text: str, brand: str | None) -> tuple[str, ...]:
    """The page's name words with any URL removed first.

    A strong field is ``url | title | h1`` glued together, and the URL is a slug:
    its words (``https``, ``products``, the shop's domain) are site furniture, not
    the product's name.  Left in, they made every page look like it "replaced" the
    row's name words and vetoed the series - live 2026-10-09, friendshome.pk demoted
    a perfect ``HSU-13HFCP`` page to tolerated for exactly that.  Colour words keep
    reading the full text (a colour in the URL still counts) - see
    :func:`score_match`.
    """
    return _drop_brand_words(build_model_keys(_URL_TOKEN_RE.sub(" ", strong_text)).name_words, brand)


def _name_word_confidence(row_words: tuple[str, ...], page_words: tuple[str, ...]) -> float:
    """Share of the row's name words the page also carries (1.0 when the row names none)."""
    if not row_words:
        return 1.0
    matched = sum(1 for word in row_words if any(similar(word, page) for page in page_words))
    return matched / len(row_words)


def _tier_mismatch(row_words: tuple[str, ...], page_words: tuple[str, ...]) -> bool:
    """True when a learned tier word names one product and not the other.

    ``Avante`` row vs ``AVANTE+`` page (or the reverse) is a different product, not a
    terser title: the sheet itself proved the word separates two sold variants.  The
    guard is the shared series name - the one-sided word only speaks when both texts
    already name the same line (``avante`` on both sides), so a wrongly learned word
    can never veto an unrelated product.
    """
    if not _SERIES_DIFFERENTIATORS:
        return False
    row_set, page_set = set(row_words), set(page_words)
    if not (row_set & page_set - _SERIES_DIFFERENTIATORS):
        return False
    return bool((row_set ^ page_set) & _SERIES_DIFFERENTIATORS)


def _name_word_substitution(row_words: tuple[str, ...], page_words: tuple[str, ...]) -> bool:
    """True when the page *replaces* a row name word instead of merely adding words.

    A veto needs a positive contradiction, exactly like the suffix rule above: the page
    has to name a line/colourway the row does not (``Daimond`` vs ``Cuby``, ``Marine
    Impression`` vs ``Red Blaze``).  A word only one side carries is not enough on its
    own, because both directions are innocent in real pages - a shop title that adds a
    marketing word (``Inverter``) is the same product, and a terse title that prints only
    the code and colour simply omits the line name.  Only a *substitution* - a word on
    each side that the other lacks - says the two texts describe different products.
    A page that carries no name word at all says nothing and never vetoes.  The one
    exception is a learned tier word (``plus`` of ``Avante+``), which the sheet itself
    proved separates two sold variants - see :func:`_tier_mismatch`.
    """
    if not row_words or not page_words:
        return False
    if _tier_mismatch(row_words, page_words):
        return True
    row_only = [word for word in row_words if not any(similar(word, page) for page in page_words)]
    page_only = [page for page in page_words if not any(similar(page, word) for word in row_words)]
    return bool(row_only and page_only)


def _drop_brand_words(words: tuple[str, ...], brand: str | None) -> tuple[str, ...]:
    """Drop name words that are only the brand, however the two sides spell it.

    A brand is already matched by its own rule, so it must never decide a series veto.
    Sheets and pages spell multi-word brands both ways (``Super Asia`` / ``Superasia``),
    so a word counts as brand when it is a piece of the brand key or the whole of it.
    Dropping words can only weaken a veto, never invent one, which is the safe direction.
    """
    key = normalize_key(brand or "")
    if len(key) < 4:
        return words
    return tuple(word for word in words if word not in key and key not in word)


def _names_word(text: str, word: str) -> bool:
    """Whether ``text`` names ``word`` as a whole word."""
    return bool(word) and f" {word} " in f" {normalize_text(text)} "


def _shade_veto(keys: ModelKeys, named_text: str, selected_colourway: str) -> bool:
    """Whether the page names the row's colour under **another shade**.

    Read only from what the page itself says: :func:`colour_phrase` looks at the last word in
    front of a colour name, so a URL is stripped first (a slug is a hint, never evidence -
    owner decision 2026-10-16) and the page's labelled "Colour:" value is read as its own
    field, so a shop path ending in ``…-avante-mist`` cannot lend ``mist`` to a declared
    ``Green`` and invent a colourway the page never printed.

    Like the head rule it mirrors, a page that names the row's shade anywhere is compatible,
    not contradicted: shops list the range (``Pearl Red / Ruby Red``) on one product page.
    """
    if not keys.colour_shade:
        return False
    page_phrase = colour_phrase(named_text, _COLOURWAY_WORDS)
    if not page_phrase[0] and selected_colourway:
        page_phrase = colour_phrase(selected_colourway, _COLOURWAY_WORDS)
    if shade_agreement((keys.colour_word, keys.colour_shade), page_phrase) != "different":
        return False
    return not _names_word(f"{named_text} {selected_colourway}", keys.colour_shade)


def score_match(
    keys: ModelKeys,
    strong_text: str,
    brand: str | None = None,
    selected_colourway: str = "",
) -> ScoreMatch:
    """Score a page's strong text against the row's keys (additive to match_product).

    Three independent vetoes, each fail-open (an absent attribute is never a veto):
    * series - the page names a line/colourway in place of the row's (``Daimond``/``Cuby``);
    * colour - the row's colour conflicts with the page, by head word
      (:func:`colour_word_conflict`) or by shade (:func:`_shade_veto`);
    * size   - a capacity/type fact contradicts (existing ``variant_conflicts``).

    ``selected_colourway`` is the page's own labelled "Colour:" value (read by
    :func:`page_verification.selected_colourway`, never the swatch list).  It is only
    consulted when the page's strong text names no colour of its own - the variant-page
    case - so the page's declared colour then counts exactly as if the title had printed it.
    """
    page = build_model_keys(strong_text)
    page_words = _page_name_words(strong_text, brand)
    page_colour_text = strong_text
    if not page.colour_word and selected_colourway:
        selected = build_model_keys(selected_colourway)
        page_words = page_words + _drop_brand_words(selected.name_words, brand)
        page_colour_text = f"{strong_text} {selected_colourway}"
    row_words = _drop_brand_words(keys.name_words, brand)
    confidence = _name_word_confidence(row_words, page_words)
    return ScoreMatch(
        series_veto=_name_word_substitution(row_words, page_words),
        colour_veto=colour_word_conflict(keys.colour_word, page_colour_text)
        or _shade_veto(keys, _URL_TOKEN_RE.sub(" ", strong_text), selected_colourway),
        size_veto=bool(variant_conflicts(keys.variant_dict(), extract_variant_facts(strong_text))),
        confidence=confidence,
    )

def match_product(
    *,
    keys: ModelKeys,
    brand: str | None,
    strong_text: str,
    body_text: str = "",
    brand_match_enabled: bool = True,
    brand_match_min_length: int = 5,
    brand_match_min_digits: int = 3,
    allow_key_truncation: bool = True,
    variant_separation_enabled: bool = True,
    selected_colourway: str = "",
) -> ProductMatch:
    """The whole recognition rule: recognize the model, then refuse to *prove* a variant.

    Recognition is unchanged (:func:`_recognize`).  On top of it, a page that prints a
    different variant suffix of the same model stops being proof: it becomes *tolerated*,
    i.e. deferred, used only when the row found nothing better, and flagged in the review
    CSV (owner decision 2026-10-07).  It is demoted rather than rejected because a suffix
    is not reliably a variant code - ``9160-WB`` and ``9160LF`` are the same model - while
    a page that positively names another variant is never allowed to pose as an exact
    match.  The colour signal, which *is* about the picture, is the hard reject.
    """
    match = _recognize(
        keys=keys,
        brand=brand,
        strong_text=strong_text,
        body_text=body_text,
        brand_match_enabled=brand_match_enabled,
        brand_match_min_length=brand_match_min_length,
        brand_match_min_digits=brand_match_min_digits,
        allow_key_truncation=allow_key_truncation,
    )
    if not variant_separation_enabled or not match.matched:
        return match
    if brand:
        shown_brand = page_product_brand(_URL_TOKEN_RE.sub(" ", strong_text))
        if shown_brand is not None and shown_brand != brand.lower():
            return ProductMatch(False, "brand_conflict", ())
    score = score_match(keys, strong_text, brand, selected_colourway)
    if score.colour_veto:
        # A different colour is a different picture, so the page is not this product at
        # all - not even as a flagged last resort (owner decision 2026-10-07).  This is
        # the one signal that rejects, and it outranks a tolerated spelling.  The page's
        # own labelled "Colour:" value counts too, so a variant page that declares
        # ``Red Blaze`` is not proof for the ``Maroon Blaze`` row.
        return ProductMatch(False, "colour_conflict", ())
    if match.matched and _page_names_different_model(keys, strong_text):
        return ProductMatch(False, "different_model", ())
    if not match.tolerated and _page_names_another_variant(keys, strong_text):
        return replace(match, tolerated=True)
    if not match.tolerated and score.series_veto:
        # A series/line name the page does not carry (``Daimond`` vs ``Cuby``) names a
        # different product.  Demote rather than reject - the page may simply omit the
        # line name - so it becomes a flagged last resort, like the suffix case.
        return replace(match, tolerated=True)
    return match


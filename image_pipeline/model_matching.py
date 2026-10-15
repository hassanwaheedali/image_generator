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
from url_filters import KNOWN_BRANDS, page_product_brand
from variant_terms import (
    COLOUR_FAMILIES,
    colour_word_conflict,
    is_colour_word,
    primary_colour_word,
    suffix_agreement,
)

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
_PUNCTUATION = ".,;:!?()[]{}\"'`“”‘’–—-_/\\|+*&^%$#@~<>="
_SEP = r"[\s\-_/.]*"

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
    "refrigerator", "fridge", "freezer", "washing", "machine", "washer", "dryer",
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


def _is_tier_word(
    word: str,
    lines: set[tuple[str, frozenset[str]]],
    word_freq: Counter[str],
    brand_keys: set[str],
) -> bool:
    """Whether one candidate word survives the structural guards (see the learner)."""
    if len(lines) < 2 or len(word) < 3:
        return False
    if word in brand_keys or word in KNOWN_BRANDS:
        return False
    noise_words = _GENERIC_NAME_WORDS | _DYNAMIC_GENERIC_NAME_WORDS
    return not any(
        longer != word
        and longer.startswith(word)
        and count >= _ABBREVIATION_MIN_COUNT
        and (longer in KNOWN_BRANDS or longer in noise_words)
        for longer, count in word_freq.items()
    )


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
        " ", strip_bracketed(title, keep_if_unbalanced=keep_unbalanced)
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

    return ModelKeys(
        cleaned_title=cleaned,
        tokens=tokens,
        title_key=title_key,
        identities=identities,
        colours=tuple(sorted(extract_colour_families(title))),
        variant_facts=tuple(sorted(extract_variant_facts(title).items())),
        colour_word=primary_colour_word(title) or "",
        name_words=_name_words(title),
        keywords=() if tokens else _keyword_tokens(title),
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


def _slash_segment_hit(token: str, core: str, normalized: str) -> bool:
    """True when one ``/``-separated segment of the token matches on its own.

    Haier sheets print ``HSU-13HFCP/023WDC(W)``: the part before the slash is the
    customer model, the rest is a factory code that shops routinely leave out
    (live 2026-10-09: friendshome.pk lists the very same machine as
    ``HSU-13HFCP-W`` and the row was rejected for "no model key").  A segment
    only counts when it is model-like in its own right (letters + digits, >= 3
    chars) and keeps the boundary-anchored pattern, so ``HSU-12`` still never
    matches inside ``HSU-12HFPAA``.
    """
    for segment in token.split("/"):
        seg_core = normalize_key(segment)
        if len(seg_core) < 3 or seg_core == core:
            continue
        if not re.search(r"[a-z]", seg_core) or not re.search(r"[0-9]", seg_core):
            continue
        if _compiled(seg_core).search(normalized):
            return True
    return False


def _segment_truncation_hit(token: str, core: str, normalized: str) -> bool:
    """True when the token minus its trailing ``-suffix`` segment matches the page.

    GREE sheets print ``GS-12PITH17G-T3`` while the product page prints the model
    without the series tail (``GS-12PITH17G`` - live 2026-10-09).  The letter-drop
    truncation cannot help because the tail ends in a digit (``T3``).  Dropping a
    whole separator-delimited segment is safe where letter-dropping is not: the
    remainder still has to be model-like (letters *and* digits), so ``9160-WB``
    never degenerates to ``9160`` and ``PWM-1250`` never to ``PWM``.
    """
    segments = [part for part in re.split(r"[-_/ ]+", token) if part]
    for drop in (1, 2):
        if len(segments) <= drop:
            continue
        if segments[-drop].isdigit() or re.search(r"\d{2,}", segments[-drop]):
            continue
        candidate = normalize_key("-".join(segments[:-drop]))
        if len(candidate) < 5 or candidate == core:
            continue
        if not re.search(r"[a-z]", candidate) or not re.search(r"[0-9]", candidate):
            continue
        if _compiled(candidate).search(normalized):
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
        if include_identities and "/" in token and _slash_segment_hit(token, core, normalized):
            matched.append(token)
            continue
        if include_identities and (
            _segment_truncation_hit(token, core, normalized)
            or _family_range_hit(core, normalized)
        ):
            matched.append(token)
            continue
        if include_truncation:
            for variant in truncation_variants(core):
                if _compiled(variant).search(normalized):
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
    usable = {
        word
        for word in own_terms
        if word not in KNOWN_BRANDS and word not in _KEYWORD_EXTRA_NOISE
    }
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


def score_match(
    keys: ModelKeys,
    strong_text: str,
    brand: str | None = None,
    selected_colourway: str = "",
) -> ScoreMatch:
    """Score a page's strong text against the row's keys (additive to match_product).

    Three independent vetoes, each fail-open (an absent attribute is never a veto):
    * series - the page names a line/colourway in place of the row's (``Daimond``/``Cuby``);
    * colour - the row's colour word conflicts with the page (existing primitive);
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
        colour_veto=colour_word_conflict(keys.colour_word, page_colour_text),
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


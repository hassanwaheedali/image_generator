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
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import unquote, urlsplit

from models import ModelKeys

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
_CODE_RUN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]*[A-Za-z0-9]|[A-Za-z0-9]")
NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")
WHITESPACE_RE = re.compile(r"\s+")
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")

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
# --------------------------------------------------------------------------- #
COLOUR_FAMILIES: dict[str, tuple[str, ...]] = {
    "white": ("white", "off white", "off-white", "ivory white", "pure white", "pearl"),
    "black": ("black", "jet black", "midnight black", "matte black"),
    "grey": ("grey", "gray", "graphite", "silver", "stainless steel", "steel", "chrome"),
    "gold": ("gold", "golden", "champagne", "bronze", "copper", "rose gold"),
    "beige": ("beige", "cream", "ivory", "sand"),
    "brown": ("brown", "walnut", "wood", "wooden", "chocolate", "maroon brown"),
    "red": ("red", "maroon", "burgundy", "cherry", "wine"),
    "blue": ("blue", "navy", "sky blue", "azure"),
    "green": ("green", "olive", "mint"),
    "purple": ("purple", "violet", "lavender"),
    "pink": ("pink", "rose"),
    "orange": ("orange", "amber"),
    "yellow": ("yellow",),
}

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
# basic normalisation
# --------------------------------------------------------------------------- #
def normalize_text(text: str) -> str:
    """Lowercase, strip zero-width junk, turn non-alphanumeric runs into spaces."""
    cleaned = _ZERO_WIDTH_RE.sub("", text)
    return NON_ALNUM_RE.sub(" ", cleaned.lower()).strip()


def normalize_key(key: str) -> str:
    """Bare alphanumerics of a key, lowercased."""
    return NON_ALNUM_RE.sub("", key.lower())


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
    """
    if not keys.identities:
        return False, ""
    haystack = f"{page_url} {page_text}".lower()
    for identity in keys.identities:
        pattern = _whole_token_pattern(identity)
        if len(identity) >= 5 and pattern is not None and pattern.search(haystack):
            return True, f"page carries {identity}"
    stems: list[str] = []
    for identity in keys.identities:
        stem = numeric_stem(identity)
        if not stem:
            continue
        stems.append(stem)
        digits = re.match(r"\d+", stem)
        if digits and digits.group(0) not in stems:
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
    if brand and not brand_token_present(f"{page_url} {page_text}", brand):
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

    Both hyphen-joined runs (``HSU-18HFPAA``) and their individual segments are
    considered, and pure noise (``img1234``, ``800x800``, long hex hashes, dates) is
    dropped so it can never look like a model code.
    """
    found: list[str] = []
    for run in _CODE_RUN_RE.findall(text):
        for part in (run, *re.split(r"[^A-Za-z0-9]+", run)):
            core = normalize_key(part)
            if len(core) < 3:
                continue
            if not re.search(r"[a-z]", core) or not re.search(r"[0-9]", core):
                continue
            if any(pattern.match(core) for pattern in _NOISE_TOKEN_RES):
                continue
            found.append(core)
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


def colour_conflict(title_colours: frozenset[str], page_colours: frozenset[str]) -> bool:
    """True when both sides name a colour and they share no family at all."""
    if not title_colours or not page_colours:
        return False
    return not (title_colours & page_colours)


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
    """
    if not allow_key_truncation:
        return False
    return not find_matching_keys(text, keys, include_title_key=False, include_truncation=False)


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
) -> ProductMatch:
    """The whole recognition rule, in one readable function.

    Stage A - the model key itself (title/URL/H1 fields, then body text).
    Stage B - brand + code, for sites that print the model without the brand letters.
    Nothing else: no scores, no thresholds, no weighting.
    """
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

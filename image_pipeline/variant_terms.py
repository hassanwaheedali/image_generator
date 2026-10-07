"""Variant separation: the vocabulary and the pure helpers behind it.

Two rows can carry the same model code and still be different products, and a
wrong-variant picture is the one mistake the owner is penalised for.  Two signals
tell them apart, and both are derived from the text itself - there is no per-brand,
per-suffix or per-colour table anywhere:

*the model-code suffix* - the letters after the last digit of a glued identity
(``hrf538ifga`` -> ``ifga``).  A page that spells a *different* suffix is a
different variant; a page that omits it says nothing.

*the colour word* - the last colour the text names ("Dawlance **Chrome** ...
Hairline **Black** (9178-WB)" sells black; "Chrome" is the series).  Compared as a
**word**, never as a family, because ``burgundy`` and ``red`` share the ``red``
family and must still be told apart - with one exception: two *finish* names inside
one family (``chrome`` / ``silver`` / ``graphite`` / ``grey``) are the same colour,
because makers use those words as series names rather than colourways.

Both signals fail open: a suffix or a colour that cannot be read is *no opinion*,
never a rejection, so an unknown colourway can cost a protection but can never
reject a correct page.  Measured on the owner's 752-title catalogue
(`docs/riper/variant-colour-corpus-report.md`): 280/298 same-code/different-colour
pairs separated, 0 false rejects.

Nothing here imports from the pipeline - it is data plus pure functions, so it can
never take part in an import cycle with :mod:`model_matching`.  Text arguments are
normalized internally (:mod:`textnorm`), so raw titles and page text can be passed
straight in.
"""

from __future__ import annotations

import re
from typing import Literal

from textnorm import normalize_text

# --------------------------------------------------------------------------- #
# colour vocabulary (moved here verbatim from model_matching, plan section 3.6)
# --------------------------------------------------------------------------- #
#: Synonyms collapse onto one family.  Used for *recognition* only: the separation
#: rule compares the words themselves, so a missing entry costs a protection but
#: never causes a wrong rejection (see the module docstring).  Extending coverage
#: is a one-line data change, not a logic change.
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

#: The only spelling variants in the vocabulary.  ``gray`` and ``grey`` are the same
#: colour written two ways, so they must never count as a variant difference.
SPELLING_ALIASES: dict[str, str] = {"gray": "grey"}

#: Achromatic words: they describe the *finish*, not the colourway.  Two of them inside
#: one family are the same colour for our purposes (``chrome`` / ``silver`` / ``graphite``
#: / ``grey``), which matters because makers also use them as series names - "Dawlance
#: **Chrome** FH Freezer-On-Top" names no colour at all.  Two *chromatic* names inside one
#: family are still different products (``burgundy`` vs ``red``, ``copper`` vs ``gold``).
NEUTRAL_COLOURS: frozenset[str] = frozenset(
    {"white", "pearl", "ivory", "cream", "beige", "black", "grey", "silver", "steel", "chrome",
     "graphite"}
)

#: Verdicts of :func:`suffix_agreement`, best first.
SuffixVerdict = Literal["same", "omitted", "different"]

_ALPHA_TAIL_RE = re.compile(r"^(.*\d)([a-z]+)$")

#: Every known colour word, longest first so ``off white`` is found before ``white``.
_COLOUR_SYNONYMS: tuple[str, ...] = tuple(
    sorted(
        {synonym for synonyms in COLOUR_FAMILIES.values() for synonym in synonyms},
        key=len,
        reverse=True,
    )
)

_RANK: dict[str, int] = {"same": 0, "omitted": 1, "different": 2}


def _canonical(word: str) -> str:
    """The vocabulary's own spelling of a colour word (``gray`` -> ``grey``)."""
    return SPELLING_ALIASES.get(word, word)


#: Canonical colour word -> its family, so two names can be recognised as one colour.
_FAMILY_OF: dict[str, str] = {
    _canonical(synonym): family
    for family, synonyms in COLOUR_FAMILIES.items()
    for synonym in synonyms
}


def _is_chromatic(word: str) -> bool:
    """True for a word that names a colour rather than a finish."""
    return word not in NEUTRAL_COLOURS


def _same_colour(one: str, other: str) -> bool:
    """Whether two colour words describe the same picture.

    Equal words always do.  Inside one family, two *chromatic* names do not
    (``burgundy`` vs ``red`` is exactly the pair that must be told apart), while two
    finish names do (``chrome`` vs ``grey``).  Across families, never.
    """
    if one == other:
        return True
    if _FAMILY_OF.get(one) != _FAMILY_OF.get(other):
        return False
    return not (_is_chromatic(one) and _is_chromatic(other))


def _padded(text: str) -> str:
    """Normalize and space-pad, so colour words can be matched exactly.

    Callers may pass raw text: ``(9149-WB)-Pearl Red`` has to reveal ``pearl`` just as
    the normalized spelling does, or a colour is silently invisible.
    """
    return f" {normalize_text(text)} "


# --------------------------------------------------------------------------- #
# colour words
# --------------------------------------------------------------------------- #
def colour_words(text: str) -> frozenset[str]:
    """Every colour word ``text`` names, in canonical spelling.

    A text whose colour is not in the vocabulary yields nothing at all - that is the
    fail-open behaviour the separation rule relies on.
    """
    padded = _padded(text)
    return frozenset(
        _canonical(synonym) for synonym in _COLOUR_SYNONYMS if f" {synonym} " in padded
    )


def primary_colour_word(text: str) -> str | None:
    """The colour ``text`` is selling: the **last** colour word it names.

    Series and finish names come early in a title ("Dawlance **Chrome** Freezer-On-Top
    ..."), the colourway sits next to the model code ("... Hairline **Black**
    (9178-WB)"), so the last word is the one that describes the picture.  A multi-word
    synonym resolves to its head word (``matte black`` -> ``black``) so that a longer
    name for the same colour compares equal.  ``None`` when no known colour appears.
    """
    padded = _padded(text)
    best: tuple[int, int, str] | None = None
    for synonym in _COLOUR_SYNONYMS:
        start = padded.rfind(f" {synonym} ")
        if start < 0:
            continue
        candidate = (start + len(synonym), start, synonym)   # latest end, then latest start
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    return _canonical(best[2]) if best is not None else None


def primary_colour_conflict(title_text: str, page_text: str) -> bool:
    """True when the page names colours and its own colour is not the title's.

    Two steps, both deliberately forgiving:

    1. if the page mentions the title's colour *anywhere*, there is no conflict - a spec
       page listing the whole range ("available in white silver black") is not a
       contradiction, and neither is a page that names no colour at all, because
       omission never penalises;
    2. otherwise the verdict comes from the page's **primary** colour, not from any word
       it happens to contain.  Comparing against every word would let a series name
       ("Dawlance **E Chrome** ... Metallic Gold") pose as compatible with a grey row,
       because ``chrome`` sits in the grey family.

    Whether two words are the same colour is :func:`_same_colour`.
    """
    title_word = primary_colour_word(title_text)
    if title_word is None:
        return False
    page_words = colour_words(page_text)
    if not page_words or title_word in page_words:
        return False
    page_word = primary_colour_word(page_text)
    return page_word is not None and not _same_colour(title_word, page_word)


# --------------------------------------------------------------------------- #
# model-code suffix
# --------------------------------------------------------------------------- #
def identity_alpha_tail(identity: str) -> tuple[str, str]:
    """Split a glued identity at its last digit: ``hrf538ifga`` -> (``hrf538``, ``ifga``).

    The stem is the model family, the tail is whatever the maker appended.  An identity
    that ends in a digit (``dsd4890``) has an empty tail - and optional feature letters
    never reach here at all, because the tokenizer stops gluing at a colour or type word
    (``DSD 4890 Silver SD`` and ``DSD-4890`` both give ``dsd4890``).
    """
    match = _ALPHA_TAIL_RE.match(identity.lower())
    if match is None:
        return identity.lower(), ""
    return match.group(1), match.group(2)


def suffix_agreement(
    title_identities: tuple[str, ...] | list[str],
    page_identities: tuple[str, ...] | list[str],
) -> SuffixVerdict | None:
    """How the page's model-code suffix compares with the title's, or ``None`` if unrelated.

    ``same``      - the tails agree, or one extends the other (``ifga`` / ``ifgainv``);
    ``omitted``   - one side carries no tail, so it says nothing;
    ``different`` - both carry a tail and the tails differ (``ifga`` / ``ifpa``).

    Only identities whose stem matches are compared, and the most permissive pairing
    wins, so a page that prints two spellings of the same model is still an agreement.
    Callers demote on ``different`` and ``omitted``; they never reject on this signal
    alone, because a suffix is not reliably a variant code (``9160-WB`` and ``9160LF``
    are the same model).
    """
    best: SuffixVerdict | None = None
    for title_identity in title_identities:
        title_stem, title_tail = identity_alpha_tail(title_identity)
        for page_identity in page_identities:
            page_stem, page_tail = identity_alpha_tail(page_identity)
            if title_stem != page_stem:
                continue
            if not title_tail or not page_tail:
                verdict: SuffixVerdict = "omitted"
            elif title_tail.startswith(page_tail) or page_tail.startswith(title_tail):
                verdict = "same"
            else:
                verdict = "different"
            if best is None or _RANK[verdict] < _RANK[best]:
                best = verdict
    return best

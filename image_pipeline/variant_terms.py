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
reject a correct page.  Where a maker publishes a colourway name that no plain
colour word covers (``Marine Impression``), the name joins the colour vocabulary as
its own family - one line of data, not logic.  Measured on the owner's 752-title
catalogue (`docs/riper/variant-colour-corpus-report.md`): 269/304 same-code pairs
whose titles both name a colour separated, 0 false rejects.

Nothing here imports from the pipeline - it is data plus pure functions, so it can
never take part in an import cycle with :mod:`model_matching`.  Text arguments are
normalized internally (:mod:`textnorm`), so raw titles and page text can be passed
straight in.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

from similarity import similar
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
    "grey": ("grey", "gray", "graphite", "silver", "stainless steel", "steel", "chrome", "inox", "mirror"),
    "gold": ("gold", "golden", "champagne", "bronze", "copper", "rose gold"),
    "beige": ("beige", "cream", "ivory", "sand"),
    "brown": ("brown", "walnut", "wood", "wooden", "chocolate", "maroon brown"),
    "red": ("red", "maroon", "burgundy", "cherry", "wine"),
    "blue": ("blue", "navy", "sky blue", "azure"),
    "green": ("green", "olive", "mint", "emerald", "emerald green", "teal"),
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
     "graphite", "inox", "mirror"}
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


def same_colour(one: str, other: str) -> bool:
    """Whether two colour words describe the same picture.

    Equal words always do.  Inside one family, two *chromatic* names do not
    (``burgundy`` vs ``red`` is exactly the pair that must be told apart), while two
    finish names do (``chrome`` vs ``grey``).  Across families, never.

    Both sides are read in the vocabulary's own spelling first, because the sheet writes
    ``Gray`` where the table spells it ``grey`` - without that the two look like two
    colours and a row's own colour stops being recognised as its own.
    """
    one, other = _canonical(one), _canonical(other)
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
def is_colour_word(word: str) -> bool:
    """Whether a single token is one of the vocabulary's colour words."""
    return _canonical(normalize_text(word)) in _FAMILY_OF


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

    :func:`colour_phrase` reads the same position and keeps the shade in front of it.
    """
    return colour_phrase(text)[0] or None


#: Verdict of :func:`shade_agreement`, same three-way shape as :class:`SuffixVerdict`.
ShadeVerdict = Literal["same", "omitted", "different"]

#: Every word a colour name can end on, in the vocabulary's own spelling.
_HEADS: dict[str, str] = {
    synonym.split()[-1]: _canonical(synonym.split()[-1])
    for synonyms in COLOUR_FAMILIES.values()
    for synonym in synonyms
}

#: The two-word colour names the vocabulary already holds (``off white``, ``matte black``,
#: ``rose gold``).  Inside one of them the first word *is* part of the colour, not a second
#: attribute about it - which is what keeps ``off`` from posing as a colourway of white.
_COMPOUND_COLOURS: frozenset[str] = frozenset(
    normalize_text(synonym)
    for synonyms in COLOUR_FAMILIES.values()
    for synonym in synonyms
    if len(synonym.split()) > 1
)


def colour_phrase(text: str, shade_words: Iterable[str] = ()) -> tuple[str, str]:
    """The colour ``text`` sells, as ``(head, shade)`` - ``Pearl Red`` -> ``("red", "pearl")``.

    The head is exactly :func:`primary_colour_word`'s answer.  The shade is the single word
    standing in front of it, and only when that word names a colour of its own (``pearl``,
    ``emerald``) or the caller was taught that it does (``ruby``, ``hairline`` - the
    catalogue proves those; no list of them belongs in this file).  An unknown word in that
    slot is a series name or a category noun, not a colour, so ``inverter red`` states no
    shade and can never veto a page.  ``("", "")`` when the text names no colour at all.
    """
    taught = {_canonical(word) for word in shade_words}
    tokens = normalize_text(text).split()
    for index in range(len(tokens) - 1, -1, -1):
        head = _HEADS.get(tokens[index])
        if head is None:
            continue
        shade = ""
        if index:
            previous = _canonical(tokens[index - 1])
            if f"{previous} {head}" not in _COMPOUND_COLOURS and (
                previous in _HEADS or previous in taught
            ):
                shade = previous
        return head, shade
    return "", ""


def shade_agreement(row: tuple[str, str], page: tuple[str, str]) -> ShadeVerdict:
    """Whether a row's colour and a page's colour describe the same picture.

    Three verdicts, and only ``different`` ever speaks:

    * ``same`` - the two shades are one colour: the same word however it is spelled
      (``matt`` ~ ``matte``, because a page that re-types the word is not a different
      colourway), or two *finish* names the vocabulary already knows as one colour
      (``silver`` ~ ``chrome``, both grey) - the same carve-out :func:`same_colour`
      makes for heads, since makers spell a finish several ways and use it as a series name;
    * ``omitted`` - one side states no shade, or the two colours are not the same family.
      The family case is left alone on purpose: :func:`colour_word_conflict` already
      rejects it, so this rule only ever *adds* the same-family-different-shade veto that
      rule could not see, and never changes a verdict that already exists;
    * ``different`` - one colour, two shades.  A different shade of the same colour is a
      different picture (owner 2026-10-16: 9140-WB is sold as Pearl, Ruby and Diamond Red,
      and only the shade says which one the customer was promised).
    """
    row_head, row_shade = row
    page_head, page_shade = page
    if not row_head or not page_head or not same_colour(row_head, page_head):
        return "omitted"
    if not row_shade or not page_shade:
        return "omitted"
    if similar(row_shade, page_shade) or same_colour(row_shade, page_shade):
        return "same"
    return "different"


def colour_word_conflict(title_word: str | None, page_text: str) -> bool:
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

    Whether two words are the same colour is :func:`same_colour`.
    """
    if not title_word:
        return False
    page_words = colour_words(page_text)
    if not page_words or title_word in page_words:
        return False
    page_word = primary_colour_word(page_text)
    return page_word is not None and not same_colour(title_word, page_word)


def primary_colour_conflict(title_text: str, page_text: str) -> bool:
    """:func:`colour_word_conflict` with the title's colour read from its own text."""
    return colour_word_conflict(primary_colour_word(title_text), page_text)


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


# --------------------------------------------------------------------------- #
# spaced variant tail  (``DWT 255 ES``, ``DWT 255 Clear Lid``, ``DWT 1166 ADS+``)
# --------------------------------------------------------------------------- #
# Some makers separate sibling products only by the *words after the model number*
# (Dawlance: ``DWT 255 ES`` and ``DWT 255 Clear Lid`` share the code ``DWT 255``;
# ``DWT 1166 X ADS`` is sold as ``ADS+``).  The tail is read from the text itself -
# the words that follow the number up to the first category / colour / marketing word -
# so there is no per-brand table: the same function reads a sheet title, a page title,
# an h1 or a URL slug.  A trailing ``+`` is kept as the word ``plus`` so it is never
# silently stripped (``ADS+`` -> ``ads plus``).
_CODE_HEAD_RE = re.compile(r"^[a-z]{2,4}$")
_CODE_NUMBER_RE = re.compile(r"^\d{3,5}$")
_TAIL_TOKEN_RE = re.compile(r"[a-z0-9]+\+?")
_TAIL_MAX_WORDS = 3
_PLUS = "plus"


def variant_tails(
    text: str, stop_words: frozenset[str] | set[str] = frozenset()
) -> list[tuple[str, str, tuple[str, ...]]]:
    """Every ``(letters, number, tail words)`` the text prints after a ``<letters> <number>`` code.

    ``stop_words`` ends a tail (category and marketing words are passed in by the caller,
    because this module imports nothing from the pipeline; colour words always end it).
    A tail may be empty.
    """
    tokens = _TAIL_TOKEN_RE.findall(text.lower())
    out: list[tuple[str, str, tuple[str, ...]]] = []
    for index in range(len(tokens) - 1):
        head, number = tokens[index].rstrip("+"), tokens[index + 1]
        if not _CODE_HEAD_RE.match(head) or not _CODE_NUMBER_RE.match(number):
            continue
        tail: list[str] = []
        for raw in tokens[index + 2 :]:
            word = raw.rstrip("+")
            if (
                not word.isalpha()
                or word in stop_words
                or is_colour_word(word)
                or len(tail) >= _TAIL_MAX_WORDS
            ):
                break
            tail.append(word)
            if raw.endswith("+"):
                tail.append(_PLUS)
                break
        out.append((head, number, tuple(tail)))
    return out


def _tail_words_compatible(one: str, other: str) -> bool:
    """Equal, or one is a prefix of the other (``c`` ~ ``clear``, ``ifga`` ~ ``ifgainv``)."""
    return one == other or one.startswith(other) or other.startswith(one)


def _core_tail(tail: tuple[str, ...]) -> list[str]:
    return [word for word in tail if word != _PLUS]


def variant_tails_conflict(row_tail: tuple[str, ...], page_tail: tuple[str, ...]) -> bool:
    """True when two tails name **different** variants of one code.

    A conflict needs *no word in common* (any shared word is one line spelled two ways): ``es`` vs ``clear lid`` conflict, while ``c`` vs ``clear lid`` agree
    (the letter is the initial), ``ads`` vs ``x ads`` agree (the page adds a word) and
    ``ads plus`` vs ``x ads`` agree (``+`` is kept, but never makes a page foreign).  An
    empty tail has no opinion.
    """
    row, page = _core_tail(row_tail), _core_tail(page_tail)
    if not row or not page:
        return False
    # Any shared word means one product line spelled two ways (`bkg tech` / `tech r`);
    # only tails with nothing in common are different variants.
    return not any(_tail_words_compatible(w, p) for w in row for p in page)


def variant_tail_agrees(row_tail: tuple[str, ...], page_tail: tuple[str, ...]) -> bool:
    """True when both tails speak and every word of the row's tail is found in the page's."""
    row, page = _core_tail(row_tail), _core_tail(page_tail)
    if not row or not page:
        return False
    return all(any(_tail_words_compatible(w, p) for p in page) for w in row)


def code_heads_compatible(one: str, other: str) -> bool:
    """Two code letter-prefixes of one model line: equal, or one abbreviates the other.

    A brand's own URLs drop a letter the retailers keep (``dw-255-es`` for ``DWT 255 ES``).
    Only letters of length >= 2 on both sides, so a lone letter never glues two families.
    """
    return len(one) >= 2 and len(other) >= 2 and (one.startswith(other) or other.startswith(one))

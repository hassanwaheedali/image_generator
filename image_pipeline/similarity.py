"""Similarity primitives for the probabilistic matcher (plan rev 1, task 1).

A leaf module - it imports only :mod:`textnorm` and the standard library, so it can
never join an import cycle.  It supplies the *fuzzy* half of the scored matcher: the
deterministic half (model codes, vetoes) lives in :mod:`model_matching`.

Two ideas, both table-free:

* :func:`similar` - two spellings of the same word agree exactly or fuzzily
  (``avanteplus`` ~ ``avante plus``), so a reworded page is not a false veto.
* :func:`same_digit_runs` - the digit skeleton of a code must never change
  (``9160-WB`` ~ ``9160LF``), while the letters may, which is exactly the owner's
  "same model, different tail" correction.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from textnorm import normalize_text

_WORD_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")


def words(text: str) -> list[str]:
    """The normalized letter/number tokens of ``text``, in order."""
    return _WORD_RE.findall(normalize_text(text))


def ratio(one: str, other: str) -> float:
    """Raw sequence similarity in ``[0, 1]`` over the normalized spellings."""
    return SequenceMatcher(None, one, other).ratio()


def similar(one: str, other: str, threshold: float = 0.85) -> bool:
    """Whether two spellings name the same thing (exact, or fuzzy past ``threshold``).

    An absent side is never similar - the matcher treats that as "no opinion", not a
    match, so the rule stays fail-open.  Very short strings are exact-only, because a
    fuzzy ratio on two or three letters is meaningless.
    """
    a = normalize_text(one)
    b = normalize_text(other)
    if not a or not b:
        return False
    if a == b:
        return True
    return len(a) >= 4 and len(b) >= 4 and ratio(a, b) >= threshold


def digit_runs(text: str) -> list[str]:
    """The number sequences of ``text``, in order (``PWD-425-GD`` -> ``["425"]``)."""
    return re.findall(r"\d+", normalize_text(text))


def same_digit_runs(one: str, other: str) -> bool:
    """True when both codes carry digits and their digit skeletons are identical.

    Letters may differ (``9160-WB`` / ``9160LF``); digits may not (``9160`` / ``9178``).
    A side with no digits yields False - no opinion - so a bare word can never be
    declared the same model.
    """
    one_digits = digit_runs(one)
    return bool(one_digits) and one_digits == digit_runs(other)

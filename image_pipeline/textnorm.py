"""Text normalisation shared by the recognition modules.

One leaf module, no project imports, so both :mod:`model_matching` and
:mod:`variant_terms` can use the *same* normaliser without importing each other.
Every comparison in the pipeline is made on text that went through
:func:`normalize_text`, which is what makes ``DW-7500C``, ``DW 7500C`` and
``dw/7500/c`` the same model - and what stops a hyphen from hiding a colour word.
"""

from __future__ import annotations

import re

#: Anything that is not a lowercase letter or digit - separators, punctuation, brackets.
NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")
WHITESPACE_RE = re.compile(r"\s+")
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")


def normalize_text(text: str) -> str:
    """Lowercase, strip zero-width junk, turn non-alphanumeric runs into spaces."""
    cleaned = _ZERO_WIDTH_RE.sub("", text)
    return NON_ALNUM_RE.sub(" ", cleaned.lower()).strip()


def normalize_key(key: str) -> str:
    """Bare alphanumerics of a key, lowercased."""
    return NON_ALNUM_RE.sub("", key.lower())

"""Spaced variant tails: ``DWT 255 ES`` / ``DWT 255 Clear Lid`` / ``DWT 255 C`` / ``DWT 1166 ADS+``.

Regression for the owner's Dawlance run: the ES row published the Clear Lid page's photos
(identical folders), a ``C`` row must reach the Clear Lid page and never the ES one, and
the ``+`` of ``ADS+`` was stripped so the official page was never asked for.
"""

from __future__ import annotations

import pytest

from model_matching import (
    build_model_keys,
    match_product,
    page_variant_conflict,
    row_variant_code,
    url_variant_verdict,
)
from sources import _healed_first, model_tokens_in_url, rank_candidates, search_terms
from models import PageCandidate
from variant_terms import (
    code_heads_compatible,
    variant_tail_agrees,
    variant_tails,
    variant_tails_conflict,
)

BASE = "https://www.dawlance.com.pk/top-load-washing-machine/"
ES_URL = BASE + "dw-255-es-automatic-washing-machines"
CLEAR_URL = BASE + "dwt-255-clear-lid-automatic-washing-machines"
ADS_URL = BASE + "dwt-1166-x-ads-automatic-washing-machines"
OTHER_1166 = BASE + "dwt-1167-flp-top-load-washing-machine-10-kg"

ES = "Dawlance DWT 255 ES Automatic Top Load Washing Machine - White"
CLEAR = "Dawlance DWT 255 Clear Lid Automatic Top Load Washing Machine - White"
C_ROW = "Dawlance DWT 255 C Automatic Top Load Washing Machine - White"
ADS = "Dawlance DWT 1166 ADS+ Automatic Top Load Washing Machine"


def tails(text: str) -> list[tuple[str, str, tuple[str, ...]]]:
    return variant_tails(text, frozenset({"automatic", "washing", "machine", "top", "load"}))


class TestTailReading:
    def test_reads_words_up_to_category_word(self) -> None:
        assert tails(ES) == [("dwt", "255", ("es",))]
        assert tails(CLEAR) == [("dwt", "255", ("clear", "lid"))]

    def test_plus_is_kept_as_a_word(self) -> None:
        assert tails(ADS) == [("dwt", "1166", ("ads", "plus"))]

    def test_slug_reads_like_a_title(self) -> None:
        assert tails("dw-255-es-automatic-washing-machines") == [("dw", "255", ("es",))]
        assert tails("dwt-1166-x-ads-automatic") == [("dwt", "1166", ("x", "ads"))]


class TestTailVerdicts:
    @pytest.mark.parametrize(
        ("row", "page", "conflict"),
        [
            (("es",), ("clear", "lid"), True),
            (("c",), ("clear", "lid"), False),      # the letter is the initial
            (("c",), ("es",), True),
            (("clear", "lid"), ("es",), True),
            (("ads", "plus"), ("x", "ads"), False),  # + never makes a page foreign
            (("ads",), ("x", "ads"), False),
            ((), ("es",), False),
            (("es",), (), False),
        ],
    )
    def test_conflict(self, row: tuple[str, ...], page: tuple[str, ...], conflict: bool) -> None:
        assert variant_tails_conflict(row, page) is conflict

    def test_agreement_needs_both_sides_to_speak(self) -> None:
        assert variant_tail_agrees(("c",), ("clear", "lid"))
        assert not variant_tail_agrees(("c",), ())
        assert not variant_tail_agrees(("es",), ("clear", "lid"))

    def test_head_abbreviation_needs_two_letters(self) -> None:
        assert code_heads_compatible("dw", "dwt")
        assert not code_heads_compatible("d", "dwt")
        assert not code_heads_compatible("dwt", "hrf")


class TestUrlRouting:
    def test_es_row_takes_es_page_not_clear_lid(self) -> None:
        keys = build_model_keys(ES)
        assert model_tokens_in_url(ES_URL, keys)
        assert not model_tokens_in_url(CLEAR_URL, keys)

    def test_clear_lid_row_takes_clear_lid_only(self) -> None:
        keys = build_model_keys(CLEAR)
        assert model_tokens_in_url(CLEAR_URL, keys)
        assert not model_tokens_in_url(ES_URL, keys)

    def test_bare_c_means_clear_lid(self) -> None:
        keys = build_model_keys(C_ROW)
        assert model_tokens_in_url(CLEAR_URL, keys)
        assert not model_tokens_in_url(ES_URL, keys)

    def test_plus_row_reaches_official_page(self) -> None:
        keys = build_model_keys(ADS)
        assert model_tokens_in_url(ADS_URL, keys)
        assert not model_tokens_in_url(OTHER_1166, keys)

    def test_rank_puts_agreeing_variant_first(self) -> None:
        keys = build_model_keys(CLEAR)
        candidates = [
            PageCandidate(url=u, origin="t", method="sitemap", title_hint="")
            for u in (ES_URL, CLEAR_URL)
        ]
        assert rank_candidates(candidates, keys)[0].url == CLEAR_URL

    def test_rows_without_tail_are_unaffected(self) -> None:
        keys = build_model_keys("Dawlance DWT 255 Automatic Washing Machine")
        assert row_variant_code(keys) is None
        assert url_variant_verdict(CLEAR_URL, keys) == "none"
        assert model_tokens_in_url(CLEAR_URL, keys)


class TestPageVerdict:
    def test_es_row_refuses_clear_lid_page(self) -> None:
        keys = build_model_keys(ES)
        text = f"{CLEAR_URL} | Dawlance DWT 255 Clear Lid Automatic Washing Machine"
        assert page_variant_conflict(keys, text)
        assert not match_product(keys=keys, brand="dawlance", strong_text=text).matched

    def test_es_row_accepts_es_page(self) -> None:
        keys = build_model_keys(ES)
        text = f"{ES_URL} | Dawlance DW 255 ES Automatic Washing Machine"
        assert not page_variant_conflict(keys, text)

    def test_page_that_omits_the_tail_is_no_conflict(self) -> None:
        keys = build_model_keys(ES)
        assert not page_variant_conflict(keys, "Dawlance DWT 255 Automatic Washing Machine")

    def test_plus_row_accepts_page_without_plus(self) -> None:
        keys = build_model_keys(ADS)
        assert not page_variant_conflict(keys, "Dawlance DWT 1166 X ADS Automatic Washing Machine")


class TestSearchAndHealing:
    def test_queries_keep_tail_and_plus(self) -> None:
        assert search_terms(build_model_keys(ADS), brand="dawlance", title=ADS)[0] == "Dawlance DWT 1166 ADS+"
        assert search_terms(build_model_keys(ES), brand="dawlance", title=ES)[0] == "Dawlance DWT 255 ES"

    def test_double_hyphen_slug_is_repaired_first(self) -> None:
        stale = BASE + "dwt-1166-x-ads--automatic-washing-machines"
        assert _healed_first([stale]) == [ADS_URL, stale]
        assert _healed_first([ES_URL]) == [ES_URL]


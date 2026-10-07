"""Tests for the similarity primitives (plan: probabilistic-matching, task 1)."""

from __future__ import annotations

from model_matching import build_model_keys, score_match
from similarity import digit_runs, same_digit_runs, similar, words


def test_words_splits_letters_and_numbers() -> None:
    assert words("HRF-538 IFGA") == ["hrf", "538", "ifga"]
    assert words("Dawlance 9191 Daimond RED") == ["dawlance", "9191", "daimond", "red"]


def test_similar_is_exact_or_fuzzy() -> None:
    assert similar("avante plus", "avante plus")
    assert similar("avanteplus", "avante plus")          # fuzzy absorbs the space
    assert not similar("daimond", "cuby")
    assert not similar("", "cuby")                       # absent is never "similar"


def test_same_digit_runs_letters_may_differ_digits_may_not() -> None:
    assert same_digit_runs("9160-WB", "9160LF")           # owner's same-model pair
    assert same_digit_runs("HRF-538 IFGA", "HRF-538 IFPA")
    assert not same_digit_runs("9160-WB", "9178-WB")      # different digits
    assert same_digit_runs("DW-7500", "DW-7500C")         # same digits; the C is a suffix, handled elsewhere
    assert not same_digit_runs("DW-7500", "DW-7600")      # different digits
    assert not same_digit_runs("abcd", "abcd")            # no digits -> no opinion


def test_digit_runs_extracts_number_sequences() -> None:
    assert digit_runs("PWD-425-GD") == ["425"]
    assert digit_runs("DW-7500C") == ["7500"]

def test_series_veto_fires_on_different_series_word() -> None:
    row = build_model_keys("Dawlance 9191 Daimond RED")
    assert score_match(row, "Dawlance 9191 Cuby RED").series_veto
    assert score_match(row, "Dawlance 9191 Ruby Red").series_veto
    assert not score_match(row, "Dawlance 9191 Daimond RED").series_veto


def test_series_veto_absorbs_rewording() -> None:
    row = build_model_keys("Dawlance Avante+ Freezer-On-Top Refrigerator 7 Cu Ft")
    assert not score_match(row, "Dawlance Avante Plus Freezer-On-Top Refrigerator 7 Cu Ft").series_veto


def test_unknown_colourway_is_a_veto_not_a_silent_match() -> None:
    row = build_model_keys("PEL Straight 3 Taps Glass Door Water Dispenser (PWD-425-GD)-Ocean Mist")
    assert score_match(row, "PEL Straight 3 Taps Glass Door Water Dispenser (PWD-425-GD)-Maroon Blaze").veto
    assert not score_match(row, "PEL Straight 3 Taps Glass Door Water Dispenser (PWD-425-GD)-Ocean Mist").veto


def test_same_digits_no_series_is_not_a_veto() -> None:
    row = build_model_keys("Dawlance REF 9160-WB-GD Burgundy")
    assert not score_match(row, "Dawlance REF 9160LF AVANTE PEARL BURGUNDY").series_veto

def test_series_veto_needs_a_substitution_not_an_omission() -> None:
    """A terse page that prints only brand + code + colour omits the line name; it must
    not veto (found in review 2026-10-07: an omission-based veto demoted 75 of 94 rows)."""
    row = build_model_keys("Dawlance AVANTE+ Freezer-On-Top Refrigerator 12 Cu Ft Luxe Brown (9173-WB)")
    assert not score_match(row, "Dawlance 9173-WB Luxe Brown").series_veto
    # a word the page merely *adds* is not a contradiction either
    assert not score_match(row, "Dawlance Avante+ Inverter Freezer-On-Top Refrigerator 12 Cu Ft").series_veto


def test_series_veto_separates_an_unknown_colourway_with_no_table() -> None:
    row = build_model_keys("PEL Straight 3 Taps Glass Door Water Dispenser (PWD-425-GD)-Marine Impression")
    assert score_match(row, "PEL Straight 3 Taps Glass Door Water Dispenser (PWD-425-GD)-Red Blaze").series_veto
    assert not score_match(
        row, "PEL Straight 3 Taps Glass Door Water Dispenser (PWD-425-GD)-Marine Impression"
    ).series_veto


def test_brand_spelling_cannot_veto() -> None:
    """The sheet says ``Superasia``, the title says ``Super Asia`` - same product."""
    row = build_model_keys("Super Asia Smart Wash Twin Tub 7.5KG Washing Machine (SA-241)")
    assert not score_match(row, "Superasia SA-241 Twin Tub Washing Machine", "Superasia").series_veto

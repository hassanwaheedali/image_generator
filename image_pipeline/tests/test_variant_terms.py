"""Variant separation primitives: model-code suffixes and colour words.

Every rule here is pure (text in, verdict out) so it can be pinned without a page,
a config or the network.  The pipeline behaviour they drive is covered in
``test_model_matching.py`` and ``test_pipeline_end_to_end.py``.
"""

from __future__ import annotations

from variant_terms import (
    colour_words,
    identity_alpha_tail,
    primary_colour_conflict,
    primary_colour_word,
    suffix_agreement,
)

# --------------------------------------------------------------------------- #
# identity_alpha_tail
# --------------------------------------------------------------------------- #


def test_identity_alpha_tail_splits_the_variant_suffix() -> None:
    """The letters after the last digit are the variant suffix."""
    assert identity_alpha_tail("hrf538ifga") == ("hrf538", "ifga")
    assert identity_alpha_tail("9160wbgd") == ("9160", "wbgd")
    assert identity_alpha_tail("dw7500c") == ("dw7500", "c")


def test_identity_without_a_suffix_keeps_its_whole_stem() -> None:
    """``DSD 4890`` / ``DSD 4890 Silver SD`` both end in a digit: nothing to split."""
    assert identity_alpha_tail("dsd4890") == ("dsd4890", "")


def test_code_ending_in_letters_splits_at_the_last_digit() -> None:
    """``HSU-18HFT``: the tail is part of the code, and both sides carry it."""
    assert identity_alpha_tail("hsu18hft") == ("hsu18", "hft")


# --------------------------------------------------------------------------- #
# suffix_agreement
# --------------------------------------------------------------------------- #


def test_identical_suffix_is_exact() -> None:
    assert suffix_agreement(("hrf538ifga",), ("hrf538ifga",)) == "same"


def test_a_reformatted_suffix_still_agrees() -> None:
    """``538 IFGA INV`` on the page glues to the same identity - still exact."""
    assert suffix_agreement(("hrf538ifga",), ("hrf538ifgainv",)) == "same"


def test_different_suffix_is_reported() -> None:
    """The owner's case: ``HRF-538 IFGA`` must not silently take an ``IFPA`` page."""
    assert suffix_agreement(("hrf538ifga",), ("hrf538ifpa",)) == "different"


def test_same_model_written_with_two_suffixes_is_reported_not_rejected() -> None:
    """Owner correction: ``9160-WB`` and ``9160LF`` are the same model - report only."""
    assert suffix_agreement(("9160wb",), ("9160lf",)) == "different"


def test_a_suffix_missing_on_either_side_is_an_omission() -> None:
    """An omitted suffix says nothing, so it can never be a contradiction."""
    assert suffix_agreement(("hrf538ifga",), ("hrf538",)) == "omitted"
    assert suffix_agreement(("hrf538",), ("hrf538ifga",)) == "omitted"


def test_unrelated_stems_have_no_opinion() -> None:
    assert suffix_agreement(("dw7500c",), ("hsu18hft",)) is None


def test_the_most_permissive_pairing_wins() -> None:
    """A page printing both spellings agrees with the title on one of them."""
    assert suffix_agreement(("9160wb",), ("9160lf", "9160wb")) == "same"
    assert suffix_agreement(("9160wb",), ("9160lf", "9160")) == "omitted"


# --------------------------------------------------------------------------- #
# colour words
# --------------------------------------------------------------------------- #


def test_the_primary_colour_is_the_last_one_named() -> None:
    """Series names come early; the colourway sits next to the code."""
    text = "dawlance chrome freezer on top refrigerator 12 cu ft hairline black 9178 wb"
    assert primary_colour_word(text) == "black"
    assert primary_colour_word("dawlance e chrome refrigerator metallic gold 9140 wb") == "gold"


def test_the_primary_colour_unifies_the_one_spelling_variant() -> None:
    assert primary_colour_word("haier ac 1 ton gray") == "grey"
    assert primary_colour_word("haier ac 1 ton grey") == "grey"


def test_multi_word_synonyms_resolve_to_their_head_word() -> None:
    """``Matte Black`` and ``Black`` must compare equal, not as two colours."""
    assert primary_colour_word("washer matte black") == "black"
    assert primary_colour_word("fridge off white") == "white"
    assert primary_colour_word("fridge rose gold") == "gold"


def test_text_without_a_colour_has_no_primary_colour() -> None:
    assert primary_colour_word("haier thunder inverter ac 1 5 ton hsu 18hft") is None


def test_an_unknown_colour_name_is_not_a_colour() -> None:
    """Fail open: a colourway outside the vocabulary yields nothing, never a guess."""
    assert primary_colour_word("pel water dispenser pwd 425 gd marine impression") is None
    assert colour_words("pel water dispenser pwd 425 gd marine impression") == frozenset()


def test_colour_words_returns_every_colour_named() -> None:
    assert colour_words("dawlance chrome refrigerator pearl copper") == frozenset(
        {"chrome", "pearl", "copper"}
    )


# --------------------------------------------------------------------------- #
# primary_colour_conflict
# --------------------------------------------------------------------------- #


def test_burgundy_and_red_conflict() -> None:
    """The case the colour-family synonyms hide: both are the ``red`` family."""
    title = "dawlance avante refrigerator 7 cu ft burgundy 9160 wb gd"
    page = "dawlance avante refrigerator 7 cu ft red 9160 wb gd"
    assert primary_colour_conflict(title, page)


def test_the_same_colour_never_conflicts() -> None:
    title = "dawlance avante refrigerator 7 cu ft burgundy 9160 wb gd"
    assert not primary_colour_conflict(title, "dawlance ref 9160lf avante pearl burgundy")


def test_a_longer_colour_name_containing_the_titles_word_does_not_conflict() -> None:
    assert not primary_colour_conflict(
        "haier t3 plus ac 1 5 ton hsu 20hftex grey",
        "haier hsu 20hftex t3 plus ac 1 5 ton hairline grey",
    )


def test_a_page_that_names_several_colours_keeps_the_titles_own() -> None:
    """A spec page listing the whole range is not a contradiction."""
    assert not primary_colour_conflict(
        "haier thunder ac 1 5 ton hsu 18hft silver",
        "haier hsu 18hft available in white silver black",
    )


def test_a_missing_colour_on_either_side_never_conflicts() -> None:
    """Omission never penalises: 360 of the 752 catalogue titles name no colour."""
    assert not primary_colour_conflict(
        "haier inventer t3 air conditioner 2 ton hsu 24hfab",
        "haier t3 inverter ac 2 ton hsu 24hfab white",
    )
    assert not primary_colour_conflict(
        "haier inventer t3 air conditioner 2 ton grey hsu 24hfab",
        "haier t3 inverter ac 2 ton hsu 24hfab",
    )


def test_an_unknown_colour_name_never_conflicts() -> None:
    """Fail open, both directions: an unlisted name cannot reject a correct page."""
    assert not primary_colour_conflict(
        "pel dispenser pwd 425 gd marine impression",
        "pel dispenser pwd 425 gd maroon blaze",
    )
    assert not primary_colour_conflict(
        "pel dispenser pwd 425 gd maroon blaze",
        "pel dispenser pwd 425 gd marine impression",
    )

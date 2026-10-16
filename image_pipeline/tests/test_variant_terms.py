"""Variant separation primitives: model-code suffixes and colour words.

Every rule here is pure (text in, verdict out) so it can be pinned without a page,
a config or the network.  The pipeline behaviour they drive is covered in
``test_model_matching.py`` and ``test_pipeline_end_to_end.py``.
"""

from __future__ import annotations

from variant_terms import (
    colour_phrase,
    colour_word_conflict,
    colour_words,
    identity_alpha_tail,
    primary_colour_conflict,
    primary_colour_word,
    shade_agreement,
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
    assert primary_colour_word("pel water dispenser pwd 425 gd ocean mystery") is None
    assert colour_words("pel water dispenser pwd 425 gd ocean mystery") == frozenset()


def test_colour_words_returns_every_colour_named() -> None:
    assert colour_words("dawlance chrome refrigerator pearl copper") == frozenset(
        {"chrome", "pearl", "copper"}
    )


def test_punctuation_never_hides_a_colour_word() -> None:
    """The catalogue writes ``(9149-WB)-Pearl Red`` - the dash must not swallow ``pearl``.

    Callers should not have to remember to normalize first: a colour word that cannot be
    seen is a protection silently lost.
    """
    raw = "Dawlance Avante Refrigerator 9 Cu Ft (9149-WB)-Pearl Red"
    assert primary_colour_word(raw) == "red"
    assert colour_words(raw) == frozenset({"pearl", "red"})
    assert primary_colour_conflict(
        "Dawlance Avante Refrigerator 9 Cu Ft (9149-WB)-Pearl Burgundy", raw
    )


# --------------------------------------------------------------------------- #
# primary_colour_conflict
# --------------------------------------------------------------------------- #


def test_burgundy_and_red_conflict() -> None:
    """The case the colour-family synonyms hide: both are the ``red`` family."""
    title = "dawlance avante refrigerator 7 cu ft burgundy 9160 wb gd"
    page = "dawlance avante refrigerator 7 cu ft red 9160 wb gd"
    assert primary_colour_conflict(title, page)


def test_finish_names_inside_one_family_are_not_a_variant_difference() -> None:
    """``Chrome``/``silver``/``graphite``/``grey`` are one family of finishes.

    The catalogue uses "Chrome" as a *series* name ("Dawlance **Chrome** FH ..."), so a
    title whose only colour word is ``chrome`` has not named a colourway at all.  Treating
    that as a contradiction would reject correct pages, so within one family only two
    *chromatic* names differ.
    """
    assert not primary_colour_conflict(
        "dawlance chrome fh freezer on top refrigerator 12 cu ft 9178 wb",
        "dawlance e chrome freezer 14 cu ft metallic grey 9178 wb",
    )
    assert not primary_colour_conflict(
        "dawlance refrigerator 12 cu ft 9178 wb platinum silver",
        "dawlance refrigerator 12 cu ft 9178 wb graphite",
    )


def test_two_chromatic_names_inside_one_family_do_conflict() -> None:
    """The pairs the owner must never see mixed: burgundy/red, maroon/red, copper/gold."""
    assert primary_colour_conflict("refrigerator 7 cu ft copper 9140 wb", "refrigerator metallic gold 9140 wb")
    assert primary_colour_conflict("dispenser pwd 425 gd maroon blaze", "dispenser pwd 425 gd red blaze")


def test_neutral_and_chromatic_names_still_conflict() -> None:
    """A different family is a different colour, whatever the finish wording."""
    assert primary_colour_conflict(
        "haier thunder ac 1 5 ton hsu 18hft silver",
        "haier thunder ac 1 5 ton hsu 18hft white",
    )
    assert primary_colour_conflict(
        "dawlance refrigerator 16 cu ft white 9193 wb",
        "dawlance chrome fh refrigerator 16 cu ft hairline black 9193 wb",
    )


def test_the_same_colour_never_conflicts() -> None:
    title = "dawlance avante refrigerator 7 cu ft burgundy 9160 wb gd"
    assert not primary_colour_conflict(title, "dawlance ref 9160lf avante pearl burgundy")


def test_a_longer_colour_name_containing_the_titles_word_does_not_conflict() -> None:
    assert not primary_colour_conflict(
        "haier t3 plus ac 1 5 ton hsu 20hftex grey",
        "haier hsu 20hftex t3 plus ac 1 5 ton hairline grey",
    )


def test_a_series_name_never_masks_a_real_colour_difference() -> None:
    """``Chrome`` sits in the grey family, so it must not make a *gold* page compatible.

    Live shape: "Dawlance **E Chrome** ... Metallic Grey (9173-WB)" against "Dawlance
    **E Chrome** ... Metallic Gold (9173-WB)".  Comparing the title's colour against
    every word the page mentions would find "chrome" and call it a match; the verdict has
    to come from the page's own primary colour.
    """
    assert primary_colour_conflict(
        "dawlance e chrome freezer on top refrigerator 11 5 cu ft metallic grey 9173 wb",
        "dawlance e chrome freezer on top refrigerator 11 5 cu ft metallic gold 9173 wb",
    )
    assert primary_colour_conflict(
        "dawlance avante freezer 12 cu ft 9178 wb platinum silver",
        "dawlance e chrome freezer 14 cu ft metallic gold 9178 wb",
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
        "pel dispenser pwd 425 gd ocean mystery",
        "pel dispenser pwd 425 gd maroon blaze",
    )
    assert not primary_colour_conflict(
        "pel dispenser pwd 425 gd maroon blaze",
        "pel dispenser pwd 425 gd ocean mystery",
    )


def test_the_title_colour_can_be_supplied_directly() -> None:
    """Recognition computes the title's colour once; the verdict must not re-parse it."""
    assert colour_word_conflict("burgundy", "dawlance avante refrigerator 7 cu ft red 9160 wb gd")
    assert not colour_word_conflict("burgundy", "dawlance ref 9160lf avante pearl burgundy")
    assert not colour_word_conflict("burgundy", "dawlance avante refrigerator 7 cu ft 9160 wb gd")
    assert not colour_word_conflict(None, "dawlance avante refrigerator 7 cu ft red 9160 wb gd")
    assert not colour_word_conflict("", "dawlance avante refrigerator 7 cu ft red 9160 wb gd")

def test_an_unknown_colourway_is_separated_dynamically_not_by_a_table() -> None:
    """The marine-impression vocabulary line is gone (2026-10-07): the positional
    name-word rule separates unknown colourways on its own, with no per-name entry."""
    from model_matching import build_model_keys, match_product

    row = "PEL Straight 3 Taps Glass Door Water Dispenser (PWD-425-GD)-Marine Impression"
    keys = build_model_keys(row)
    assert not keys.colour_word                      # not a known colour any more
    for other in ("Red Blaze", "Maroon Blaze"):
        page = "PEL Straight 3 Taps Glass Door Water Dispenser (PWD-425-GD)-" + other
        verdict = match_product(keys=keys, brand="pel", strong_text=page)
        assert not (verdict.matched and not verdict.tolerated), other  # never a clean match
    same = match_product(keys=keys, brand="pel", strong_text=row)
    assert same.matched and not same.tolerated       # identical name words -> exact
    # and the known chromatic pair still conflicts through the colour rule
    assert colour_word_conflict("red", "PEL Water Dispenser Maroon Blaze")


# --------------------------------------------------------------------------- #
# colour_phrase / shade_agreement (owner 2026-10-16: "color red is not enough -
# 9140 ships as Pearl Red, Ruby Red and Diamond Red, and only the shade is the picture")
# --------------------------------------------------------------------------- #
def test_the_shade_in_front_of_a_colour_is_read_as_part_of_the_colour() -> None:
    """A word the vocabulary already knows needs no teaching: ``pearl`` is a colour."""
    assert colour_phrase("dawlance avante refrigerator 9 cu ft pearl red") == ("red", "pearl")
    assert colour_phrase("haier refrigerator 7 cu ft mist green", {"mist"}) == ("green", "mist")
    assert colour_phrase("haier refrigerator 7 cu ft mist green") == ("green", "")


def test_a_two_word_colour_the_vocabulary_already_knows_names_no_second_shade() -> None:
    """``off white`` is one colour name, so ``off`` must not pose as a colourway."""
    assert colour_phrase("dawlance refrigerator off white") == ("white", "")
    assert colour_phrase("washer matte black") == ("black", "")
    assert colour_phrase("haier inverter ac silk white", {"silk"}) == ("white", "silk")


def test_an_unknown_word_names_a_shade_only_once_the_catalogue_taught_it() -> None:
    assert colour_phrase("dawlance avante ruby red", {"ruby"}) == ("red", "ruby")
    assert colour_phrase("dawlance avante ruby red") == ("red", "")


def test_a_text_naming_no_colour_has_no_phrase() -> None:
    assert colour_phrase("haier thunder inverter ac 1.5 ton") == ("", "")


def test_two_pages_stating_the_same_shade_of_the_same_colour_agree() -> None:
    assert shade_agreement(("red", "pearl"), ("red", "pearl")) == "same"
    assert shade_agreement(("grey", "silver"), ("grey", "chrome")) == "same"


def test_a_different_shade_of_the_same_colour_is_the_only_hard_word() -> None:
    assert shade_agreement(("red", "pearl"), ("red", "ruby")) == "different"


def test_an_unstated_shade_is_never_a_rejection() -> None:
    """The standing doctrine: what cannot be read is no opinion, not a contradiction."""
    assert shade_agreement(("red", "pearl"), ("red", "")) == "omitted"
    assert shade_agreement(("red", ""), ("red", "pearl")) == "omitted"
    assert shade_agreement(("", ""), ("red", "pearl")) == "omitted"


def test_another_colour_is_left_to_the_colour_rule_that_already_rejects_it() -> None:
    """red vs burgundy vs copper are told apart by ``colour_word_conflict``, not here."""
    assert shade_agreement(("red", "pearl"), ("copper", "pearl")) == "omitted"
    assert shade_agreement(("red", "pearl"), ("burgundy", "pearl")) == "omitted"


def test_a_shade_spelled_two_ways_is_still_one_shade() -> None:
    assert shade_agreement(("black", "matte"), ("black", "matt")) == "same"
    assert shade_agreement(("green", "meadow"), ("green", "medow")) == "same"

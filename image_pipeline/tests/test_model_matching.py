"""Model keys, identity/glued matching, colour and variant extraction."""

from __future__ import annotations

import pytest

from model_matching import (
    build_model_keys,
    code_like_tokens,
    extract_colour_families,
    extract_variant_facts,
    find_matching_keys,
    glued_model_token,
    key_pattern,
    match_product,
    model_like_token,
    near_miss_codes,
    numeric_stem,
    relaxed_page_match,
    score_match,
    truncation_variants,
    url_belongs_to_other_model,
    variant_conflicts,
)

SAMPLE_TITLES = (
    "Dawlance Semi Automatic Washing Machine 10kg (DW-7500C)",
    "Haier 50 Inch QLED Google LED TV (H50S80EUX)",
    "Haier 55 Inch 4K Smart QLED TV (55S80EUX)",
    "Haier 55 Inch Smart Google LED TV (H55K85FX)",
    "PEL Top Load Fully Automatic Washing Machine (PAWM-1100)",
    "PEL Kitchen Pro Grill Microwave Oven (PMO–23 Kitchen Pro GR",
    "TCL 85 Inch SQD Mini LED TV (85C8L)",
    "TCL 98 Inch SQD Mini LED TV (98C8L)",
    "PEL Top Load Washing Machine White 12.5 Kg (PWM-1250)",
    "Homage Freezer-on-Top Refrigerator 18 Cu Ft Golden (HRF-47662-GD)",
)


# --------------------------------------------------------------------------- #
# keys from a title
# --------------------------------------------------------------------------- #
def test_keys_keep_the_model_and_drop_noise() -> None:
    keys = build_model_keys("Haier HSU-12HFPAA 1.5 Ton Inverter (White)")
    assert "HSU-12HFPAA" in keys.tokens
    joined = " ".join(keys.tokens)
    assert "1.5" not in joined
    assert "Ton" not in joined
    assert "White" not in joined


def test_mixed_letters_digits_are_model_like() -> None:
    assert model_like_token("HSU-12HFPAA")
    assert model_like_token("DWB-300")
    assert model_like_token("55P635")
    assert not model_like_token("2024")
    assert not model_like_token("1.5")
    assert not model_like_token("12000BTU")
    assert not model_like_token("9KG")
    assert not model_like_token("55inch")
    assert not model_like_token("Ton")
    assert not model_like_token("White")


def test_title_without_model_token_has_no_keys() -> None:
    keys = build_model_keys("Premium LED TV 55 inch 2024")
    assert keys.tokens == ()
    assert keys.all_keys == ()
    assert not keys.variant_dict().get("model")


def test_key_pattern_boundaries() -> None:
    keys = build_model_keys("Haier HSU-12 Cool")
    assert find_matching_keys("hsu12", keys)
    assert find_matching_keys("HSU 12", keys)
    assert find_matching_keys("hsu_12", keys)
    assert not find_matching_keys("HSU-12HFPAA", keys)
    assert not find_matching_keys("AHSU-12", keys)
    assert key_pattern("HSU-12").search("hsu12") is not None


@pytest.mark.parametrize("title", SAMPLE_TITLES)
def test_bracketed_model_keys_are_rescued(title: str) -> None:
    """All ten sample rows carry the model inside brackets - every one must yield a key."""
    keys = build_model_keys(title)
    assert keys.tokens, f"no model key found for {title!r}"


def test_unbalanced_bracket_keeps_tail() -> None:
    keys = build_model_keys("PEL Kitchen Pro Grill Microwave Oven (PMO–23 Kitchen Pro GR")
    assert any("PMO" in token for token in keys.tokens)


def test_strict_mode_drops_bracketed_models() -> None:
    keys = build_model_keys(
        "Haier 50 Inch QLED Google LED TV (H50S80EUX)",
        bracketed_key_fallback=False,
        unbalanced_bracket_keeps_text=False,
    )
    assert keys.tokens == ()


# --------------------------------------------------------------------------- #
# glued identities (plan section 3.5.4)
# --------------------------------------------------------------------------- #
def test_glued_identity_matches() -> None:
    keys = build_model_keys("Haier HRF-538 IFGA")
    for spelling in ("HRF538IFGA", "HRF-538-IFGA", "hrf 538 ifga", "HRF_538_IFGA", "HRF-538 IFGA"):
        assert find_matching_keys(spelling, keys), spelling


def test_glued_prefix_extension_still_rejected() -> None:
    """A key extended with characters the title never had is a different variant."""
    keys = build_model_keys("Haier HSU-12 1.5 Ton")
    assert not find_matching_keys("HSU12HFPAA", keys)
    assert not find_matching_keys("HSU-12HFPAA", keys)


def test_glued_unknown_extension_rejected() -> None:
    keys = build_model_keys("Haier HRF-538 IFGA")
    assert not find_matching_keys("HRF538XYZ", keys)


def test_key_truncation_matches_sheet_suffix() -> None:
    """Sheet says DW-7500C, the real Dawlance page prints DW-7500."""
    keys = build_model_keys("Dawlance Semi Automatic Washing Machine 10kg (DW-7500C)")
    assert find_matching_keys("DW-7500 TWIN TUB GLASS LID BLACK/GREY", keys)
    assert find_matching_keys("dw 7500 twin tub", keys)
    assert not find_matching_keys("DW-8000 TWIN TUB", keys)


def test_truncation_never_extends_a_key() -> None:
    """The HSU-12 vs HSU-12HFPAA protection is untouched by the truncation rule."""
    keys = build_model_keys("Haier HSU-12 1.5 Ton")
    assert find_matching_keys("HSU12HFPAA", keys) == ()
    assert find_matching_keys("HSU-12HFPAA", keys) == ()
    assert find_matching_keys("HSU-12", keys)


def test_truncation_can_be_disabled() -> None:
    keys = build_model_keys("Dawlance Washing Machine (DW-7500C)")
    assert find_matching_keys("DW-7500 page", keys, include_truncation=False) == ()


def test_near_miss_codes_report_the_page_model() -> None:
    keys = build_model_keys("Haier 50 Inch QLED Google LED TV (H50S80EUX)")
    assert "h50s80gux" in near_miss_codes('Haier H50S80GUX -50" QLED Google TV', keys)


def test_glued_identity_can_be_disabled() -> None:
    keys = build_model_keys("Haier HRF-538 IFGA")
    assert find_matching_keys("HRF538IFGA", keys, include_identities=True)
    assert not find_matching_keys("HRF538IFGA", keys, include_identities=False)


# --------------------------------------------------------------------------- #
# S4 - other model
# --------------------------------------------------------------------------- #
def test_other_model_url_rejected() -> None:
    keys = build_model_keys("Haier HSU-12HFPAA")
    assert url_belongs_to_other_model("https://cdn.haier.com/p/HSU-18HFPAA-front.png", keys)


def test_neutral_and_own_model_urls_not_rejected() -> None:
    keys = build_model_keys("Haier HSU-12HFPAA")
    assert not url_belongs_to_other_model("https://cdn.haier.com/p/img123.png", keys)
    assert not url_belongs_to_other_model("https://cdn.haier.com/p/hsu-12hfpaa-1.png", keys)


def test_glued_image_url_not_other_model() -> None:
    keys = build_model_keys("Haier HRF-538 IFGA")
    assert not url_belongs_to_other_model("https://cdn.haier.com/p/HRF538IFGA-1.jpg", keys)


def test_truncation_variants_only_drop_trailing_letters() -> None:
    assert truncation_variants("dw7500c") == ("dw7500",)
    assert truncation_variants("hsu12") == ()
    assert truncation_variants("hrf47662gd") == ("hrf47662g", "hrf47662")


def test_code_like_tokens_ignore_noise() -> None:
    tokens = code_like_tokens("IMG_1234-800x800-hrf538ifga-a1b2c3d4e5f6a7b8")
    assert "img1234" not in tokens
    assert "800x800" not in tokens
    assert "hrf538ifga" in tokens


# --------------------------------------------------------------------------- #
# colour (plan section 3.6)
# --------------------------------------------------------------------------- #
def test_colour_families_still_collapse_synonyms() -> None:
    """This feeds the gallery swatch filter; the variant rule lives in variant_terms."""
    assert extract_colour_families("Homage Refrigerator Golden") == frozenset({"gold"})
    assert extract_colour_families("Haier AC White") == frozenset({"white"})
    # silver counts as grey, champagne as gold
    assert extract_colour_families("Champagne Gold") == frozenset({"gold"})
    assert extract_colour_families("Dawlance DW-7500 Black Grey") == frozenset({"black", "grey"})


def test_title_colours_come_from_brackets() -> None:
    keys = build_model_keys("Homage Freezer Refrigerator 18 Cu Ft Golden (HRF-47662-GD)")
    assert "gold" in keys.colours


# --------------------------------------------------------------------------- #
# variant facts (plan section 3.6)
# --------------------------------------------------------------------------- #
def test_variant_facts_and_conflicts() -> None:
    title = extract_variant_facts("Dawlance 10 kg Top Load Semi Automatic Washing Machine")
    assert title["capacity_kg"] == "10"
    assert title["load"] == "top"
    assert title["automation"] == "semi"
    page = extract_variant_facts("Dawlance 12 kg Top Load Washer")
    assert variant_conflicts(title, page) == ("capacity_kg",)
    same = extract_variant_facts("Dawlance 10kg top-load washing machine")
    assert variant_conflicts(title, same) == ()
    assert variant_conflicts(title, {}) == ()


def test_display_technology_is_not_an_axis() -> None:
    assert "display" not in extract_variant_facts("Haier 55 Inch QLED TV")
    assert extract_variant_facts("PEL Microwave 23 L")["capacity_l"] == "23"


# --------------------------------------------------------------------------- #
# spaced model spellings: "DW 7500" / "DW 7500 C" (live case 2026-10-06)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("title", "token"),
    (
        ("DW 7500 TWIN TUB CLEAR LID (WHITE)", "DW 7500"),
        ("Dawlance DW 7500 TWIN TUB CLEAR LID (WHITE)", "DW 7500"),
        ("Dawlance Twin Tub Washing Machine DW 7500 White", "DW 7500"),
        ("DW 7500 C", "DW 7500 C"),
        ("Dawlance DW 2500 C Twin Tub", "DW 2500 C"),
        ("DW 7500C", "DW 7500C"),
        ("PEL 1250 Top Load Washing Machine", "PEL 1250"),
    ),
)
def test_a_spaced_model_spelling_is_still_a_model_key(title: str, token: str) -> None:
    """The sheet writes what the brand prints - `DW 7500 TWIN TUB CLEAR LID (WHITE)` is the
    official product name, and it used to produce **no** model key at all, so the row was
    skipped before a single page was fetched."""
    keys = build_model_keys(title)
    assert token in keys.tokens, f"{title!r} -> {keys.tokens}"
    assert keys.all_keys


def test_the_spaced_key_matches_the_pages_own_spellings() -> None:
    keys = build_model_keys("DW 7500 C TWIN TUB CLEAR LID (WHITE)")
    assert "dw7500c" in keys.identities
    # the page may print DW-7500C, DW 7500C or just DW-7500 (the truncation rule)
    for page in ("DW-7500C TWIN TUB", "DW 7500C Twin Tub", "DW-7500 TWIN TUB"):
        assert find_matching_keys(page, keys, include_title_key=False), page


def test_generic_words_and_two_digit_sizes_never_glue() -> None:
    """`TCL 85` is a size and `TV 55` is a category - neither may become a model key, or a
    row would accept any 85-inch / any 55-inch page."""
    assert glued_model_token(["TV", "55"], 0) is None
    assert glued_model_token(["kg", "7500"], 0) is None
    assert glued_model_token(["DW", "75"], 0) is None                 # 2-digit: not a model
    assert build_model_keys("TV 55 Inch Smart").tokens == ()
    assert build_model_keys("TCL 85 Inch SQD Mini LED TV (85C8L)").tokens == ("85C8L",)
    # the HSU-12 vs HSU-12HFPAA protection is untouched: a glued key never extends
    hsu = build_model_keys("Haier HSU-12 1.5 Ton")
    assert not find_matching_keys("HSU-12HFPAA Inverter", hsu, include_title_key=False)


def test_relaxed_matching_never_swallows_a_sibling_model_code() -> None:
    """``dw7500`` must not match inside ``dw7500c`` - that is the black/glass machine.

    The relaxed rule is deliberately loose, and the first version tested `identity in
    haystack` on normalized text; normalization drops the separators, so ``dw7500`` was
    found inside ``dw7500c`` and the brand's *other* product supplied the pictures.  Today's
    acceptance run caught it (DW-7500 row, DW-7500C page accepted).  Matching through the
    page's own separators, as a whole token, keeps the loose rule from inventing a variant.
    """
    white = build_model_keys("DW 7500 TWIN TUB CLEAR LID (WHITE)")
    black = build_model_keys("DW-7500C TWIN TUB GLASS LID BLACK/GREY")
    assert relaxed_page_match(
        "https://www.dawlance.com.pk/twin-tub-washing-machine/"
        "dw-7500c-twin-tub-glass-lid-black-grey",
        "DW-7500C TWIN TUB GLASS LID BLACK/GREY",
        white,
        "Dawlance",
    ) == (False, "")
    # a *separated* letter is the one spelling the strict matcher already treats as "the
    # site dropped the trailing letter" (owner decision, 2026-10-05); relaxed agrees with
    # strict instead of inventing a rule of its own - and the row is flagged anyway.
    spaced = "DW-7500 C"
    assert find_matching_keys(spaced, white) == ("DW 7500",)
    assert relaxed_page_match("https://example.pk/products/x", spaced, white, "Dawlance") == (
        True,
        "page carries dw7500",
    )
    # and the code we *do* mean is still found through whatever separator the page uses
    for spelling in ("DW-7500 - 10Kg", "dw 7500 twin tub", "Dawlance DW/7500 Clear Lid"):
        accepted, reason = relaxed_page_match(
            "https://www.surmawala.pk/products/dawlance-washing-machine-dw-6550g-white",
            spelling,
            white,
            "Dawlance",
        )
        assert accepted, spelling
        assert reason == "page carries dw7500"
    # the C row keeps its own code, glued or spaced, and is never matched to the plain one
    assert relaxed_page_match("https://x.pk/dw-7500c", "DW-7500C", black, "Dawlance") == (
        True,
        "page carries dw7500c",
    )


def test_numeric_stem_and_relaxed_fallback_match() -> None:
    keys = build_model_keys("Dawlance DW 2500 C Twin Tub")
    assert numeric_stem(keys.identities[0]) == "2500c"
    ok, reason = relaxed_page_match(
        "https://shop.pk/product/dawlance-washer",
        "Dawlance Twin Tub Washing Machine 2500 - Special Edition",
        keys,
        "dawlance",
    )
    assert ok and "2500" in reason
    # the brand must be on the page: the same text without it is not accepted
    ok, _ = relaxed_page_match(
        "https://shop.pk/product/other-brand-washer", "Twin Tub Washing Machine 2500", keys, "dawlance"
    )
    assert not ok
    # an unrelated number is not accepted either
    ok, _ = relaxed_page_match(
        "https://shop.pk/product/dawlance", "Dawlance Twin Tub Washing Machine 9999", keys, "dawlance"
    )
    assert not ok


def test_a_tolerated_spelling_is_not_an_exact_match() -> None:
    """The sheet says DW-7500C; the page that link points at prints DW-7500 (live, 2026-10-06).

    Matching it is fine - it may be the only page for that machine - but the pipeline has to
    *know* it only matched by tolerance, because that is what lets it keep looking for the
    page that really prints the model (and flag the row if none is found).
    """
    keys = build_model_keys("Dawlance Semi Automatic Washing Machine 10kg (DW-7500C)")
    page = "DW-7500 TWIN TUB GLASS LID BLACK/GREY"

    loose = match_product(keys=keys, brand="dawlance", strong_text=page)
    assert loose.matched and loose.tolerated

    exact = match_product(keys=keys, brand="dawlance", strong_text="DW-7500C TWIN TUB")
    assert exact.matched and not exact.tolerated
    # the spaced spelling of the same model is exact, not tolerated
    assert not match_product(keys=keys, brand="dawlance", strong_text="DW 7500 C").tolerated

    # S1 with the tolerance switched off keeps the wrong page out entirely
    strict = match_product(keys=keys, brand="dawlance", strong_text=page, allow_key_truncation=False)
    assert not strict.matched


# --------------------------------------------------------------------------- #
# variant separation: model-code suffix (plan: variant-separation, task 2)
# --------------------------------------------------------------------------- #
IFGA_TITLE = "Haier No Frost Inverter Refrigerator 18 Cu Ft (HRF-538 IFGA)"


def test_a_page_showing_a_different_variant_suffix_is_never_exact_proof() -> None:
    """Owner rule: a different suffix is a different product, so the page is demoted.

    Live shape of the bug: the title's matchable token is the bare ``HRF-538``, so an
    ``IFPA`` page matched at full strength and its pictures were published as if they
    proved the ``IFGA`` model.  The page is now *tolerated* - deferred, used only when
    the row found nothing else, and flagged for review.
    """
    keys = build_model_keys(IFGA_TITLE)
    verdict = match_product(keys=keys, brand="haier", strong_text="Haier HRF-538 IFPA refrigerator")
    assert verdict.matched
    assert verdict.tolerated


def test_a_page_showing_the_same_suffix_stays_exact() -> None:
    keys = build_model_keys(IFGA_TITLE)
    verdict = match_product(keys=keys, brand="haier", strong_text="Haier HRF-538 IFGA refrigerator")
    assert verdict.matched
    assert not verdict.tolerated


def test_a_longer_spelling_of_the_same_suffix_is_still_exact() -> None:
    """``538 IFGA INV`` is the same variant spelled longer, not a different one."""
    keys = build_model_keys(IFGA_TITLE)
    verdict = match_product(keys=keys, brand="haier", strong_text="Haier 538 IFGA INV")
    assert verdict.matched
    assert not verdict.tolerated


def test_a_page_that_omits_the_suffix_is_judged_as_before() -> None:
    """Omission says nothing: the existing dropped-trailing-letter rule still owns it."""
    keys = build_model_keys("Dawlance Semi Automatic Washing Machine 10kg (DW-7500C)")
    verdict = match_product(keys=keys, brand="dawlance", strong_text="Dawlance DW-7500 twin tub")
    assert verdict.matched
    assert verdict.tolerated                       # unchanged: tolerated, as before this rule


def test_a_glued_code_carrying_another_suffix_never_matched_anyway() -> None:
    """Dawlance glues the suffix into the code, so S1 already keeps those variants apart.

    ``9160-WB`` and ``9160LF`` are the same model (owner correction), but a page printing
    the other spelling does not match this row at all - the matchable token is the whole
    ``9160-WB``.  There is nothing for the suffix rule to demote, and it must not change
    that.  The rule only bites where the code is spelled with a space (``HRF-538 IFGA``),
    because there the bare ``HRF-538`` is the matchable token.
    """
    keys = build_model_keys("Dawlance Chrome Freezer-On-Top Refrigerator 10 Cu Ft (9160-WB)")
    assert not match_product(keys=keys, brand="dawlance", strong_text="Dawlance 9160LF fridge").matched
    assert match_product(keys=keys, brand="dawlance", strong_text="Dawlance 9160-WB fridge").matched


def test_variant_separation_can_be_switched_off() -> None:
    """One config field rolls the whole rule back to the previous behaviour."""
    keys = build_model_keys(IFGA_TITLE)
    verdict = match_product(
        keys=keys,
        brand="haier",
        strong_text="Haier HRF-538 IFPA refrigerator",
        variant_separation_enabled=False,
    )
    assert verdict.matched
    assert not verdict.tolerated


# --------------------------------------------------------------------------- #
# variant separation: colour (plan: variant-separation, task 3)
# --------------------------------------------------------------------------- #
BURGUNDY_TITLE = "Dawlance Avante Freezer-On-Top Refrigerator 7 Cu Ft Burgundy (9160-WB-GD)"
RED_PAGE = "Dawlance Avante Freezer-On-Top Refrigerator 7 Cu Ft Red (9160-WB-GD)"


def test_the_title_carries_its_own_colour_word() -> None:
    """Computed once with the keys, so the verdict never re-parses the title."""
    assert build_model_keys(BURGUNDY_TITLE).colour_word == "burgundy"
    assert build_model_keys("Haier Thunder Inverter AC 1.5 Ton (HSU-18HFT)-Silver").colour_word == "silver"
    assert build_model_keys("Haier Inventer T3 Air Conitioner 2 Ton (HSU-24HFAB)").colour_word == ""


def test_a_page_showing_a_different_colour_is_not_this_product() -> None:
    """Owner rule: a different colour is a different picture, so the page is rejected.

    Both titles carry the *same* code (9160-WB-GD) and ``burgundy``/``red`` even share a
    colour family, so nothing else in the pipeline could tell these two rows apart.
    """
    keys = build_model_keys(BURGUNDY_TITLE)
    verdict = match_product(keys=keys, brand="dawlance", strong_text=RED_PAGE)
    assert not verdict.matched
    assert verdict.strength == 0
    assert verdict.rule == "colour_conflict"


def test_a_longer_name_for_the_same_colour_still_matches() -> None:
    """"Pearl Burgundy" is the row's own colour spelled longer, not another variant."""
    keys = build_model_keys(BURGUNDY_TITLE)
    verdict = match_product(
        keys=keys,
        brand="dawlance",
        strong_text="Dawlance Avante Freezer-On-Top Refrigerator 7 Cu Ft Pearl Burgundy (9160-WB-GD)",
    )
    assert verdict.matched
    assert not verdict.tolerated


def test_a_page_that_omits_the_colour_still_matches() -> None:
    """Omission never penalises: most pages do not repeat the colourway."""
    keys = build_model_keys(BURGUNDY_TITLE)
    verdict = match_product(
        keys=keys, brand="dawlance", strong_text="Dawlance Avante Freezer-On-Top Refrigerator 9160-WB-GD"
    )
    assert verdict.matched


def test_a_title_without_a_colour_still_matches() -> None:
    """360 of the 752 catalogue titles name no colour; those rows must be untouched."""
    keys = build_model_keys("Haier Inventer T3 Air Conitioner 2 Ton (HSU-24HFAB)")
    verdict = match_product(keys=keys, brand="haier", strong_text="Haier T3 Inverter AC 2 Ton HSU-24HFAB White")
    assert verdict.matched


def test_a_colour_conflict_outranks_a_tolerated_spelling() -> None:
    """The colour is about the picture, so it rejects even where the code was tolerated."""
    keys = build_model_keys("Dawlance Semi Automatic Washing Machine 10kg Black (DW-7500C)")
    verdict = match_product(keys=keys, brand="dawlance", strong_text="Dawlance DW-7500 twin tub white")
    assert not verdict.matched
    assert verdict.rule == "colour_conflict"


def test_colour_separation_can_be_switched_off() -> None:
    keys = build_model_keys(BURGUNDY_TITLE)
    verdict = match_product(
        keys=keys, brand="dawlance", strong_text=RED_PAGE, variant_separation_enabled=False
    )
    assert verdict.matched
    assert not verdict.tolerated

def test_name_words_keep_series_and_unknown_colourway_but_drop_noise() -> None:
    """Task 2: the positional "name" words - series lines and unknown colourways -
    survive, while brand-agnostic noise (type words, colours, codes) is dropped."""
    daimond = build_model_keys("Dawlance 9191 Daimond RED")
    assert "daimond" in daimond.name_words
    assert "red" not in daimond.name_words
    assert "9191" not in daimond.name_words

    fridge = build_model_keys("Dawlance Avante Freezer-On-Top Refrigerator 7 Cu Ft Burgundy")
    assert "avante" in fridge.name_words
    assert "refrigerator" not in fridge.name_words
    assert "burgundy" not in fridge.name_words

    ocean = build_model_keys("PEL Straight 3 Taps Glass Door Water Dispenser (PWD-425-GD)-Ocean Mist")
    assert "ocean" in ocean.name_words and "mist" in ocean.name_words


def test_category_words_are_noise_not_series_names() -> None:
    """Owner 2026-10-08: every word in the catalogue's category paths is generic.

    A category word describes the appliance, so it must never take part in a series
    veto - otherwise `Microwave Oven` on one page and `Microwave` on another would look
    like two different product lines.
    """
    assert "geyser" not in build_model_keys("Homage Instant Gas Water Geyser HG-10 Silver").name_words
    oven = build_model_keys("Kenwood Microwave Oven KMW-300 Black")
    assert oven.name_words == ("kenwood",)
    assert not score_match(oven, "Kenwood KMW-300 Microwave", "kenwood").series_veto

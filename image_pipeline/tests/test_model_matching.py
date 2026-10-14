"""Model keys, identity/glued matching, colour and variant extraction."""

from __future__ import annotations

import pytest

from model_matching import (
    build_model_keys,
    capacity_codes,
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
    url_names_foreign_brand,
    url_names_other_capacity_code,
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


def test_other_model_url_with_numeric_mismatch_rejected() -> None:
    keys = build_model_keys("Homage Freezer-on-Top Refrigerator 18 Cu Ft Golden (HRF-47662-GD)")
    # HRF-47552 is a different model number in the same HRF family
    assert url_belongs_to_other_model(
        "https://hadielectronics.com.pk/wp-content/uploads/2024/01/Homage-HRF-47552-GD-15-Cuft-Refrigerator3.jpg",
        keys,
    )


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


def test_fallback_match_ignores_the_search_query_in_the_url() -> None:
    """A search-result URL echoes the searched number in ``?_psq=``, so the query must
    not be evidence - otherwise a wrong product's result "matches" itself (live bug
    2026-10-08: PWM-1250 row tolerated a PWMS-1050 page purely via the query string)."""
    keys = build_model_keys("PEL Top Load Washing Machine White 12.5 Kg (PWM-1250)")
    wrong = "https://shop.pk/products/pel-pwms-1050-twin-tub?_psq=PWM+1250"
    right = "https://shop.pk/products/pel-pwm-1250-top-load?_psq=PWM+1250"
    text_wrong = "PEL PWMS-1050 Semi Auto Twin Tub Washing Machine 10kg"
    text_right = "PEL PWM-1250 Top Load Washing Machine White 12.5kg"
    assert not relaxed_page_match(wrong, text_wrong, keys, "pel")[0]
    assert relaxed_page_match(right, text_right, keys, "pel")[0]


def test_relaxed_rescue_never_publishes_another_line_s_page_for_a_named_row() -> None:
    """Owner run 2026-10-11: the rescue tier is loose by design (publish + flag), but a
    URL that *names another product line* is not loose, it is wrong.  The official
    sitemap handed the ``Pulse 19C Ultra`` row an ``Innova 19S Ultra`` page - same maker,
    same capacity family, wrong product - and a bare number in the body text must not
    publish that gallery under the rescue flag.  The same number on a page whose URL
    names no foreign line still rescues, so the guard is precise, not blanket."""
    pulse = build_model_keys("Pulse 19C Ultra White T3 | Orient Electronics Pakistan")
    innova_page = (
        "https://www.orient.com.pk/air-conditioners/split/innova-19s-ultra-white-dc-inverter.html"
    )
    innova_text = "Orient Innova 19S Ultra White DC Inverter AC, cooling capacity 19000 BTU"
    assert not relaxed_page_match(innova_page, innova_text, pulse, "orient")[0]

    white = build_model_keys("DW 7500 TWIN TUB CLEAR LID (WHITE)")
    foreign_url = "https://www.dawlance.com.pk/twin-tub/avanto-7500-twin-tub-white"
    plain_url = "https://example.pk/products/x"
    matching_text = "Dawlance 7500 Twin Tub, model DW-7500 C"
    # the number alone would rescue - the foreign word in the URL forbids it.
    assert relaxed_page_match(plain_url, matching_text, white, "Dawlance")[0]
    assert not relaxed_page_match(foreign_url, matching_text, white, "Dawlance")[0]


# --------------------------------------------------------------------------- #
# slash-variant codes: HSU-13HFCP/023WDC(W) - the part before the slash is the model
# --------------------------------------------------------------------------- #
HAIER_SLASH_TITLE = "Haier HSU-13HFCP/023WDC(W)-Triple Inverter-Haier Pakistan"


def test_a_slash_variant_title_matches_a_page_printing_only_the_base_model() -> None:
    """Live bug 2026-10-09: the sheet prints ``HSU-13HFCP/023WDC(W)`` - the part after
    the slash is a factory code shops routinely leave out (owner: "023WDC likha zarori
    nahi").  friendshome.pk lists the very same machine as ``HSU-13HFCP-W`` and S1
    rejected the row for "no model key".  The base segment is model-like in its own
    right, so it must match - exactly, not tolerated."""
    keys = build_model_keys(HAIER_SLASH_TITLE)
    verdict = match_product(
        keys=keys,
        brand="haier",
        strong_text="Haier 1.0 Ton Triple Inverter Split AC Model HSU-13HFCP-W",
    )
    assert verdict.matched
    assert verdict.rule == "model_key"
    assert not verdict.tolerated


def test_a_slash_variant_still_keeps_the_glued_extension_guard() -> None:
    """A segment never extends into extra characters: HSU-12 never matches inside
    HSU-12HFPAA, slash code or not."""
    keys = build_model_keys("Haier HSU-12/023ABC Inverter")
    assert find_matching_keys("HSU-12HFPAA", keys) == ()
    assert find_matching_keys("HSU12HFPAA", keys) == ()
    assert find_matching_keys("HSU-12", keys)


def test_a_slash_variant_does_not_match_a_different_base_model() -> None:
    keys = build_model_keys("Haier HSU-18HFCP/023WDC(W)")
    assert find_matching_keys("Haier HSU-13HFCP-W price", keys) == ()


def test_url_words_are_not_the_page_series_name() -> None:
    """Live bug 2026-10-09 (same row): the strong text glues the URL in, so the page's
    name words were ``https``/``friendshome``/``products`` - site furniture that
    "replaced" the row's words and vetoed the series, demoting a perfect page to
    tolerated.  URL words must never take part in the series veto."""
    keys = build_model_keys(HAIER_SLASH_TITLE)
    strong = (
        "https://friendshome.pk/products/haier-1-0-ton-triple-inverter-split-ac-model-hsu-13hfcp-w"
        " \n Haier 1.0 Ton Triple Inverter Split AC Model HSU-13HFCP-W"
    )
    score = score_match(keys, strong, "haier")
    assert not score.series_veto


# --------------------------------------------------------------------------- #
# series-tail truncation + family ranges (owner bugs 2026-10-09)
# --------------------------------------------------------------------------- #
GREE_T3_TITLE = "GREE Pular T3 1 TON Split AC - GS-12PITH17G-T3"


def test_a_page_printing_the_model_without_its_series_tail_matches() -> None:
    """Live 2026-10-09: the sheet prints ``GS-12PITH17G-T3`` and the shop page prints
    ``GS-12PITH17G``.  The letter-drop truncation cannot help (the tail ``T3`` ends in
    a digit), but dropping the whole separator-delimited segment is the same model -
    exact, not tolerated."""
    keys = build_model_keys(GREE_T3_TITLE)
    verdict = match_product(
        keys=keys,
        brand="gree",
        strong_text="GREE Pular T3 Series GS-12PITH17G 1 Ton Split AC",
    )
    assert verdict.matched and verdict.rule == "model_key" and not verdict.tolerated


def test_a_range_page_listing_our_variant_among_siblings_matches() -> None:
    """Live 2026-10-09: one page sells ``GS-12PITH15-16-17-18`` - our 17 is among the
    variants, so its pictures are usable (owner: "ismey sab bech raha hay woh")."""
    keys = build_model_keys(GREE_T3_TITLE)
    verdict = match_product(
        keys=keys,
        brand="gree",
        strong_text="GREE GS-12PITH15-16-17-18 - T3 White 1.0 Ton Inverter Split AC",
    )
    assert verdict.matched and not verdict.tolerated


def test_a_sibling_family_page_is_rejected_even_when_our_code_appears_in_links() -> None:
    """Live 2026-10-09: the last-resort chain kept a ``GS-12AITH24W`` page because the
    row's code appeared in its related-item links.  The page's own name is a different
    family - that page must never publish, not even as a flagged last resort."""
    keys = build_model_keys(GREE_T3_TITLE)
    verdict = match_product(
        keys=keys,
        brand="gree",
        strong_text="GREE GS-12AITH24W-T3 White Silver 1.0 Ton Air Conditioner",
        body_text="more models: GS-12PITH17G-T3",
    )
    assert not verdict.matched
    assert verdict.rule == "different_model"


def test_keyword_rows_match_pages_that_carry_the_name() -> None:
    """Owner 2026-10-09: "istarah key product mey token nahi hota bas keywords hotey
    hay".  The row carries no model code, so S1 matches by keywords instead."""
    keys = build_model_keys("Dawlance Split AC Dual Pro T3 Inverter 30")
    assert keys.keywords and not keys.tokens
    verdict = match_product(
        keys=keys,
        brand="dawlance",
        strong_text="Dawlance Split AC Dual Pro T3 Inverter 30",
    )
    assert verdict.matched and verdict.rule == "keyword" and not verdict.tolerated
    # a sibling size lacks the required number and never matches
    wrong = match_product(
        keys=keys, brand="dawlance", strong_text="Dawlance Split AC Dual Pro T3 Inverter 24"
    )
    assert not wrong.matched


def test_a_keyword_row_never_matches_a_conflicting_size() -> None:
    keys = build_model_keys("Inspire Pro T3 Inverter 1 Ton")
    verdict = match_product(
        keys=keys, brand="dawlance", strong_text="Dawlance Inspire Pro T3 Inverter 1.5 Ton"
    )
    assert not verdict.matched


# --------------------------------------------------------------------------- #
# S4 second half - the capacity code the file name sells (owner 2026-10-10)
# --------------------------------------------------------------------------- #
_HYPER_18X = "Orient Split AC Hyper 18X Super White"
_WRONG = (
    "https://selectronics.com.pk/media/catalog/product/O/r/"
    "Orient-Super-18G-EVA-T3-1.5-Ton-DC-Inverter-Air-Conditioner2.jpg"
)
_RIGHT = (
    "https://bijlibazar.com/wp-content/uploads/2025/07/"
    "Orient-Split-AC-1.5-Ton-Hyper-18X-T3-Super-White-Turbo-Pro.webp"
)


def test_capacity_codes_are_read_from_both_sides() -> None:
    keys = build_model_keys(_HYPER_18X)
    assert capacity_codes(" ".join(keys.tokens)) == frozenset({"18x"})
    assert capacity_codes(_WRONG.rsplit("/", 1)[-1]) == frozenset({"18g"})


def test_s4_rejects_a_file_selling_another_capacity_code() -> None:
    """The live hole: the owner's "Hyper 18X" row published a silver 18G EVA with an
    orange "1.1 Meter Long" badge, and S4 gave up because ``18X`` has no alphabetic
    prefix to compare (``if not product_prefixes: return False``)."""
    keys = build_model_keys(_HYPER_18X)
    assert not url_belongs_to_other_model(_WRONG, keys)  # the old half cannot see it
    assert url_names_other_capacity_code(_WRONG, keys)


def test_s4_keeps_the_url_that_names_our_own_capacity_code() -> None:
    keys = build_model_keys(_HYPER_18X)
    assert not url_names_other_capacity_code(_RIGHT, keys)


def test_s4_capacity_code_stays_quiet_when_no_code_is_comparable() -> None:
    """Size suffixes, upload folders, random asset ids and code-less rows must not be
    read as model statements - otherwise this rule would eat the catalog."""
    keys = build_model_keys(_HYPER_18X)
    for url in (
        "https://cdn.x.com/a/orient-hyper-product-name-800x800.jpg",
        "https://cdn.x.com/wp-content/uploads/2024/12/Ariston-Electric-Water-Heater.jpg",
        "https://haier.com/pk/media/image/product/31365/lg/W020260404458450118371_1200.png",
    ):
        assert not url_names_other_capacity_code(url, keys), url
    blank = build_model_keys("Orient Split AC Super Dc Inverter")
    assert not url_names_other_capacity_code(_WRONG, blank)


# --------------------------------------------------------------------------- #
# S4, third limb - a sibling product's series word in the file name
# --------------------------------------------------------------------------- #
_PULSE_ROW = "Orient Inverter AC Pulse 19C Ultra White T3 1.5 Ton"
_INNOVA_ROW = "Orient DC Inverter AC Innova 19C Ultra White T3 1.5 Ton"
_PULSE_PNG = (
    "https://orient.com.pk/assets/products-images-2026/air-conditioners/"
    "15-ton-pulse-ultra-white-dc-inverter.png"
)
_INNOVA_PNG = (
    "https://orient.com.pk/assets/products-images-2026/air-conditioners/"
    "15-ton-innova-ultra-white-dc-inverter.png"
)


def test_sibling_terms_name_the_other_rows_series_word() -> None:
    """The Orient run of 2026-10-10: the Pulse row published the Innova file because
    neither URL carries the ``19C`` key and the page hosted both series.  The sheet
    itself names the difference - ``innova`` appears on exactly one other row."""
    from model_matching import sibling_series_terms

    pulse_terms, innova_terms = sibling_series_terms([_PULSE_ROW, _INNOVA_ROW])
    assert pulse_terms == frozenset({"innova"})
    assert innova_terms == frozenset({"pulse"})


def test_sibling_terms_drop_shared_modifiers_and_brands() -> None:
    """``ultra`` and ``orient`` sit on both rows, so they can never name *another*
    product; a single-row sheet has no siblings at all."""
    from model_matching import sibling_series_terms

    pulse_terms, _ = sibling_series_terms([_PULSE_ROW, _INNOVA_ROW])
    assert "ultra" not in pulse_terms and "orient" not in pulse_terms
    (alone,) = sibling_series_terms([_PULSE_ROW])
    assert alone == frozenset()


def test_sibling_terms_ignore_code_only_rows() -> None:
    """Code rows (HRF-538 IFGA) identify by token, not by a series word: they neither
    give nor take sibling terms, so the variant-tail flow is untouched."""
    from model_matching import sibling_series_terms

    titles = ["Haier Refrigerator HRF-538 IFGA Golden", "Haier Refrigerator HRF-538 IFPA Golden"]
    a, b = sibling_series_terms(titles)
    assert a == frozenset() and b == frozenset()


def test_s4_flags_a_url_naming_a_sibling_product() -> None:
    from model_matching import url_names_sibling_product

    assert url_names_sibling_product(_INNOVA_PNG, frozenset({"innova"}))
    assert not url_names_sibling_product(_PULSE_PNG, frozenset({"innova"}))
    assert not url_names_sibling_product(_INNOVA_PNG, frozenset())  # no siblings, no verdict


def test_s4_sibling_check_reads_the_file_name_not_the_folders() -> None:
    """A category folder (``air-conditioners``) is not a product statement."""
    from model_matching import url_names_sibling_product

    url = "https://cdn.x.com/assets/air-conditioners/generic-white-unit.png"
    assert not url_names_sibling_product(url, frozenset({"conditioners"}))


# --------------------------------------------------------------------------- #
# The sheet-independent product-line guard (owner run 2026-10-11, Innovia case)
# --------------------------------------------------------------------------- #
def test_the_series_anchor_is_the_rows_own_line_name() -> None:
    """The fuzzy/identity routes may only speak for a page that names the row's own
    line.  The anchor drops the brand, the catalogue noise (``pakistan`` from the pipe
    tail) and the glued identity's tail - ``ultra`` is part of ``19cultra``, and it is
    shared by the whole Orient Ultra line, so it can never anchor anything."""
    from model_matching import fuzzy_series_anchor, has_series_name

    pulse = build_model_keys("Pulse 19C Ultra White T3 | Orient Electronics Pakistan")
    assert fuzzy_series_anchor(pulse) == frozenset({"pulse"})
    assert has_series_name(pulse)

    bare = build_model_keys("Orient 18X White")
    assert fuzzy_series_anchor(bare) == frozenset()
    assert not has_series_name(bare)

    noisy_bare = build_model_keys("Orient 18X White | Orient Electronics Pakistan")
    assert fuzzy_series_anchor(noisy_bare) == frozenset()
    assert not has_series_name(noisy_bare)


def test_an_image_url_naming_another_product_line_is_rejected() -> None:
    """The Innovia download of the owner run 2026-10-11: the file names a line the row
    does not own, carries none of the row's codes and none of its own line words.  The
    sheet did not contain an Innova row, so the sheet-knowledge limb was silent; this
    guard does not need the sheet.  The row's own files stay: the one named by its line
    word, the one anchored by its key, and a name that carries no product words at all."""
    from model_matching import url_names_other_product_line

    pulse = build_model_keys("Pulse 19C Ultra White T3 | Orient Electronics Pakistan")
    innova = (
        "https://www.orient.com.pk/assets/products-images-2026/air-conditioners/"
        "15-ton-innova-ultra-white-dc-inverter-4742e7e9.png"
    )
    own = (
        "https://www.orient.com.pk/assets/products-images-2026/air-conditioners/"
        "15-ton-pulse-ultra-white-dc-inverter.png"
    )
    blaze = "https://newglacier.pk/media/orient-blaze-19c-ultra-golden-t3.webp"
    assert url_names_other_product_line(innova, pulse)
    assert url_names_other_product_line(blaze, pulse)
    assert not url_names_other_product_line(own, pulse)
    # A bare-capacity row has no line name to protect; its codes guard it instead.
    bare = build_model_keys("Orient 18X White")
    assert not url_names_other_product_line(innova, bare)

def test_an_image_named_after_another_maker_is_foreign() -> None:
    """Owner run 2026-10-12: ``panasonic.jpg`` renders published for the Orient Megatron
    row.  The page gate judges the page's title, but a direct image URL carries no page
    and a degraded fetch may print no maker - the file's own name is the last word, and
    a file named after another known maker is never this product.  Whole-word only, on
    the file name alone: the row's own maker is fine, ``oriental`` is not ``orient``, a
    retail CDN directory hosting every brand is never evidence, and a row without a
    detected brand has nothing to compare against."""
    panasonic = "https://cdn.shopify.com/s/files/1/x/files/panasonic.jpg"
    assert url_names_foreign_brand(panasonic, "orient") == "panasonic"
    assert url_names_foreign_brand(panasonic, "Orient") == "panasonic"
    own = "https://www.orient.com.pk/assets/air-conditioners/orient-megatron-18x-white.png"
    assert url_names_foreign_brand(own, "orient") is None
    nameless = "https://cdn.shopify.com/files/UntitledProject-2024-04-29T104202.450.webp"
    assert url_names_foreign_brand(nameless, "orient") is None
    assert url_names_foreign_brand(panasonic, None) is None
    rug = "https://x.pk/cdn/panasonic/oriental-rug.jpg"
    assert url_names_foreign_brand(rug, "orient") is None
    # camel humps are separators here too; the row's own maker in the name is fine.
    camel = "https://cdn.shopify.com/s/files/1/x/files/panasonicRender.jpg"
    assert url_names_foreign_brand(camel, "orient") == "panasonic"
    own_camel = "https://www.orient.com.pk/assets/orientMegatron18x.png"
    assert url_names_foreign_brand(own_camel, "orient") is None


def test_a_camel_case_own_file_is_not_another_product_line() -> None:
    """Owner run 2026-10-12 (false positive): the ``E-Comfort Pro KEC-1267S`` row's own
    hero ``eComfortPro.png`` was refused as "image URL names another product line",
    because the plain split read the camel-case name as one glued word ``ecomfortpro``
    that the row did not own.  Camel humps are word separators - the file spells the
    row's own line word ``comfort`` - so it stays.  A truly foreign camel-case name is
    still refused, and an editor-default export (``UntitledProject``) accuses nothing."""
    from model_matching import url_names_other_product_line

    ecomfort = build_model_keys("E-Comfort Pro KEC-1267S")
    own_camel = "https://www.kenwoodpakistan.pk/cdn/shop/files/eComfortPro.png"
    own_glued = "https://www.kenwoodpakistan.pk/cdn/shop/files/ecomfortpro.png"
    assert not url_names_other_product_line(own_camel, ecomfort)
    assert not url_names_other_product_line(own_glued, ecomfort)

    pulse = build_model_keys("Pulse 19C Ultra White T3 | Orient Electronics Pakistan")
    foreign_camel = "https://x.pk/media/InnovaUltra.png"
    nameless = "https://cdn.shopify.com/files/UntitledProject-2024-04-29T104202.450.webp"
    assert url_names_other_product_line(foreign_camel, pulse)
    assert not url_names_other_product_line(nameless, pulse)


def test_super_asia_ecm_4700_and_gh_406_di_clean_matches() -> None:
    """Super Asia products on official store must match cleanly without being deferred."""
    # 1. ECM-4700
    t1 = "Super Asia ECM-4700 Plus Air Cooler 50 Litters"
    k1 = build_model_keys(t1)
    st1 = (
        "https://superasiastore.com/products/ecm-4700-plus-dc-inverter-cool-master \n "
        "ECM-4700 Plus DC Inverter Cool Master"
    )
    m1 = match_product(keys=k1, brand="superasia", strong_text=st1)
    assert m1.matched
    assert m1.rule == "model_key"
    assert not m1.tolerated

    # 2. GH-406-Di
    t2 = "Super Asia Instant Gas Water Heater 6Ltr (GH-406-Di)"
    k2 = build_model_keys(t2)
    st2 = "https://superasiastore.com/products/gh-406-di \n GH-406DI CRYSTAL SERIES (NG/LPG)"
    m2 = match_product(keys=k2, brand="superasia", strong_text=st2)
    assert m2.matched
    assert m2.rule == "model_key"
    assert not m2.tolerated


def test_tcl_tv_exact_match_not_demoted_to_tolerated() -> None:
    """TCL 65 Inch 4K UHD QLED TV (65P7L) must match ShandaarBuy page cleanly without being deferred."""
    title = "TCL 65 Inch 4K UHD QLED TV (65P7L)"
    keys = build_model_keys(title)
    strong_text = (
        "https://shandaarbuy.pk/products/tcl-65-p7l-4k-qled-google-tv \n "
        "TCL 65\" P7L 4K QLED Google TV Price in Pakistan | ShandaarBuy"
    )
    verdict = match_product(keys=keys, brand="tcl", strong_text=strong_text)
    assert verdict.matched
    assert verdict.rule == "model_key"
    assert not verdict.tolerated



def test_code_fragments_are_not_series_names() -> None:
    """Midea Eco row (owner 2026-10-13): the part number ``MSAG2B-12HRDN1-QC0`` splits
    into the alphabetic runs ``msag`` / ``hrdn``, which are code fragments, not line
    names.  Left as name words they vetoed the series on the brand's own family page -
    a page whose name carries no code at all can never print them, so the veto fired
    on every variant page and demoted a perfect body match to tolerated."""
    keys = build_model_keys(
        "MIDEA ECO - WALL MOUNTED DC INVERTER MSAG2B-12HRDN1-QC0 1.0 Ton"
    )
    assert "msag" not in keys.name_words
    assert "hrdn" not in keys.name_words
    assert {"midea", "wall", "mounted"} <= set(keys.name_words)


def test_family_page_body_match_is_not_demoted_to_tolerated() -> None:
    """The official Midea Eco page names the product only in its body (the variant
    selector) while title/H1/URL carry the family name.  A body match on a page whose
    own name adds only shop furniture words is proof, not a last resort."""
    keys = build_model_keys(
        "MIDEA ECO - WALL MOUNTED DC INVERTER MSAG2B-12HRDN1-QC0 1.0 Ton"
    )
    strong_text = (
        "https://mideapakistan.store/products/midea-wall-mounted-eco \n "
        "MIDEA ECO - WALL MOUNTED DC INVERTER | Midea Pakistan | Official Online Store"
    )
    body_text = (
        "Capacity Not sure? Size my room MSAG2B-12HRDN1-QC0 Rs.140,000.00 "
        "MSAG2C-18HRDN1-QC2 Rs.172,000.00 MSAG2D-24HRDN1-QC0W Rs.230,000.00 "
        "In stock - installed within 3 working days Add to cart"
    )
    verdict = match_product(keys=keys, brand="Midea", strong_text=strong_text, body_text=body_text)
    assert verdict.matched
    assert verdict.rule == "body_model_key"
    assert not verdict.tolerated


def test_a_whatsapp_export_filename_accuses_no_product_line() -> None:
    """Midea Eco official gallery (owner 2026-10-13): the brand's own pictures are
    phone exports named ``WhatsAppImage2026-…``; the camel hump split read ``whats`` as
    a foreign product word and S4 refused all four of the row's own photos.  A camera
    export names no product line.  A file that really names another line still accuses."""
    from model_matching import url_names_other_product_line

    keys = build_model_keys(
        "MIDEA ECO - WALL MOUNTED DC INVERTER MSAG2B-12HRDN1-QC0 1.0 Ton"
    )
    whatsapp = (
        "https://mideapakistan.store/cdn/shop/files/"
        "WhatsAppImage2026-04-10at11.05.41AM_2.jpg?v=1777325128&width=1200"
    )
    assert not url_names_other_product_line(whatsapp, keys)
    innova = (
        "https://www.orient.com.pk/assets/products-images-2026/air-conditioners/"
        "15-ton-innova-ultra-white-dc-inverter-4742e7e9.png"
    )
    pulse = build_model_keys("Pulse 19C Ultra White T3 | Orient Electronics Pakistan")
    assert url_names_other_product_line(innova, pulse)


# --------------------------------------------------------------------------- #
# learned series differentiators (owner 2026-10-14: Avante vs Avante+, Acce vs Acce Pro)
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _no_learned_differentiators():
    """The learned set is run state: every test starts and ends with none in force."""
    from model_matching import set_series_differentiators

    set_series_differentiators(())
    yield
    set_series_differentiators(())


#: Two *series lines* that each show the plain/tier pair - the learner needs evidence
#: from two lines before a word becomes a differentiator (one line can be a title
#: accident; the owner's master stock file is exactly this shape: Avante + Avante+,
#: Extreme + Extreme Plus).
TIER_SIBLINGS = (
    ("Dawlance", "Dawlance Avante Freezer-On-Top Refrigerator 9 Cu Ft (9149-WB)"),
    ("Dawlance", "Dawlance AVANTE+ Freezer-On-Top Refrigerator 12 Cu Ft (9193-WB)"),
    ("Haier", "Haier Extreme Freezer-On-Top Refrigerator 5 Cu Ft"),
    ("Haier", "Haier Extreme Plus Freezer-On-Top Refrigerator 7 Cu Ft"),
)


def test_two_sibling_lines_teach_that_avante_and_avante_plus_differ() -> None:
    """Sibling rows prove the tier: identical name words, one tier candidate apart."""
    from model_matching import learn_series_differentiators

    learned = learn_series_differentiators(TIER_SIBLINGS)
    assert "plus" in learned
    plus_row = build_model_keys(TIER_SIBLINGS[1][1])
    # a plain-Avante page is not proof for the Avante+ row, nor the reverse
    assert score_match(plus_row, "Dawlance Avante Refrigerator", "dawlance").series_veto
    plain_row = build_model_keys(TIER_SIBLINGS[0][1])
    assert score_match(plain_row, "Dawlance AVANTE+ Refrigerator", "dawlance").series_veto
    # the same tier on both sides is a match, not a contradiction
    assert not score_match(plus_row, "Dawlance AVANTE+ Refrigerator", "dawlance").series_veto


def test_plus_symbol_and_the_word_plus_extract_identically() -> None:
    from model_matching import learn_series_differentiators

    learn_series_differentiators(TIER_SIBLINGS)
    glued = build_model_keys("Dawlance AVANTE+ Freezer-On-Top Refrigerator").name_words
    word = build_model_keys("Dawlance Avante Plus Freezer-On-Top Refrigerator").name_words
    assert "plus" in glued and "plus" in word
    assert set(glued) == set(word)


def test_pro_after_a_series_word_is_learned_even_with_capacity_between() -> None:
    from model_matching import learn_series_differentiators, series_differentiators

    learn_series_differentiators((
        ("Dawlance", "Dawlance Frost Inverter Air Conditioner"),
        ("Dawlance", "Dawlance Frost 20 Pro Inverter Air Conditioner"),
        ("Haier", "Haier Acce Inverter Air Conditioner"),
        ("Haier", "Haier Acce Pro Inverter Air Conditioner"),
    ))
    assert "pro" in series_differentiators()  # the digit is transparent to adjacency
    row = build_model_keys("Dawlance Frost 20 Pro Inverter Air Conditioner")
    assert "pro" in row.name_words
    assert score_match(row, "Dawlance Frost Inverter 20 Air Conditioner", "dawlance").series_veto


def test_unlearned_tier_word_changes_nothing() -> None:
    """Fail open: without sibling evidence the standing noise rules decide."""
    keys = build_model_keys("Dawlance Frost Pro Inverter Air Conditioner")
    assert "pro" not in keys.name_words
    assert not score_match(keys, "Dawlance Frost Inverter", "dawlance").series_veto


def test_page_naming_no_line_still_never_vetoes_after_learning() -> None:
    from model_matching import learn_series_differentiators

    learn_series_differentiators(TIER_SIBLINGS)
    row = build_model_keys("Dawlance AVANTE+ Freezer-On-Top Refrigerator 12 Cu Ft")
    # a terse code-and-category page says nothing about the line (Midea family route)
    assert not score_match(row, "Dawlance 9193-WB Refrigerator", "dawlance").series_veto


def test_one_line_or_one_row_teaches_nothing() -> None:
    from model_matching import learn_series_differentiators

    # a single sibling pair is one series line: not enough corroboration
    assert not learn_series_differentiators(TIER_SIBLINGS[:2])
    assert not learn_series_differentiators((
        ("Dawlance", "Dawlance AVANTE+ Freezer-On-Top Refrigerator 12 Cu Ft"),
    ))
    assert not learn_series_differentiators((  # brand-less rows cannot pair
        (None, "Avante Freezer-On-Top Refrigerator"),
        (None, "AVANTE+ Freezer-On-Top Refrigerator"),
    ))


def test_learned_word_without_a_shared_series_never_tiers_apart() -> None:
    """The wrong-promotion guard: one-sided tier words speak only inside one series."""
    from model_matching import _tier_mismatch, learn_series_differentiators

    learn_series_differentiators(TIER_SIBLINGS)
    assert not _tier_mismatch(("cryo",), ("extreme", "plus"))  # nothing shared
    assert _tier_mismatch(("avante", "plus"), ("avante",))     # same line, one tier up


def test_learning_is_deterministic_and_model_codes_are_not_tier_evidence() -> None:
    from model_matching import learn_series_differentiators

    first = learn_series_differentiators(TIER_SIBLINGS)
    assert learn_series_differentiators(TIER_SIBLINGS) == first  # idempotent
    assert "wb" not in first  # stock columns are code, never tier (9193-WB)
    assert "daw" not in first  # abbreviations of brands/words are no tier either

    code_rows = (
        ("Dawlance", "Dawlance Semi Automatic Washing Machine 10kg (DW-7500C)"),
        ("Dawlance", "Dawlance Semi Automatic Washing Machine 12kg (DW-7500C)"),
    )
    before = build_model_keys(code_rows[0][1]).name_words
    assert not learn_series_differentiators(code_rows)  # a kg difference names no tier
    assert build_model_keys(code_rows[0][1]).name_words == before

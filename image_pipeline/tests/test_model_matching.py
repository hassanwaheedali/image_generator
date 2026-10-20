"""Model keys, identity/glued matching, colour and variant extraction."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from model_matching import (
    build_model_keys,
    capacity_codes,
    code_confirms_this_model,
    code_digit_clusters,
    code_like_tokens,
    code_names_other_family_code,
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
    set_colourway_words,
    shade_unstated_rows,
    tail_chunk_match,
    truncation_variants,
    url_belongs_to_other_model,
    url_names_foreign_brand,
    url_names_other_capacity_code,
    url_names_other_family_code,
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
# S4, fourth limb - the digit structure of the model code (sibling capacity)
# --------------------------------------------------------------------------- #

#: The live hole (owner run 2026-10-17, row "Haier 1301217E" - a 13 kg machine): the
#: gallery that published them named the 8 kg sibling of the same number family.
_SIBLING_PLAIN = (
    "https://lahorelectronics.com/wp-content/uploads/2025/09/"
    "haier-hwm80-1217-washing-machine.webp"
)
_SIBLING_INDEXED = (
    "https://www.alfatah.com.pk/wp-content/uploads/2026/07/"
    "Haier_Single_Washing_Machine_HWM_80-1217-1.webp"
)
_OWN_1301217E = "https://friendshome.pk/cdn/shop/files/130-1217E.jpg"


def test_code_digit_clusters_read_only_contiguous_codes() -> None:
    """Letters and separators are spelling; a *word* between numbers is a new statement."""
    assert code_digit_clusters("Haier-HWS-60-50.webp") == (("60", "50"),)
    assert code_digit_clusters("1301217E") == (("1301217",),)
    assert code_digit_clusters("hwm80-1217") == (("80", "1217"),)
    # A spec word breaks the cluster, so a speed never becomes part of a model number.
    assert code_digit_clusters("midea-1400-rpm-w120-front-load") == (("1400",), ("120",))
    assert code_digit_clusters("kenwood-13-cu-ft-freezer-320gd") == (("13",), ("320",))
    # Commerce noise: a pixel size, an upload date, a quantity, an editor counter.
    assert code_digit_clusters("product-hero-800x600.jpg") == ()
    assert code_digit_clusters("Haier 1301217E 20241201 9kg") == (("1301217",),)
    assert code_digit_clusters("photo-1217-front-view.jpg") == (("1217",),)


def test_a_doubled_title_code_collapses_to_one_and_counters_go() -> None:
    assert code_digit_clusters("WA21CK6745BVRT-WA21CK6745BVRT") == (("21", "6745"),)
    assert code_digit_clusters("HWM_80-1217-1") == (("80", "1217"),)
    assert code_digit_clusters("Haier-Single-HWM-80-1217-01") == (("80", "1217"),)
    # A short trailer that completes the code itself stays: 60-50 is the model, not photo 50.
    assert code_digit_clusters("Haier-HWS-60-50-2") == (("60", "50"),)


def test_a_shared_family_tail_with_a_different_head_names_another_model() -> None:
    assert code_names_other_family_code("1301217E", "hwm80-1217")
    assert code_names_other_family_code("1301217E", "HWM_80-1217-1")
    assert code_names_other_family_code("1301217E", "Haier_Single_HWM_80-1217-01")
    assert code_names_other_family_code("110-688S8", "haier-hwm80-688")


def test_the_same_digits_written_differently_is_the_same_model() -> None:
    """Hyphenation, a glued brand, a view counter: the digits decide, not the spelling."""
    for ours, theirs in (
        ("1301217E", "130-1217E"),
        ("1301217E", "haier-130-1217E-white-9kg"),
        ("6050", "HWS-60-50"),
        ("6050", "HWS-60-50-2"),
        ("W90DG5U34ABFA", "WW90DG5U34ABFA"),
        ("DW-7500C", "7500C"),
    ):
        assert code_confirms_this_model(ours, theirs), (ours, theirs)
        assert not code_names_other_family_code(ours, theirs), (ours, theirs)


def test_a_capacity_written_in_kilograms_is_the_one_written_in_tenths() -> None:
    """Live near-miss: the row ``HAIER 110-688S8`` (tenths of a kilo) and its own shop
    file ``haier-11-688-top-load`` (kilograms) - the head differs only by the unit."""
    for ours, theirs in (
        ("110-688S8", "haier-11-688-top-load"),
        ("1301217E", "haier-13-kg-1217e-front"),
        ("1301217E", "haier-1301217e-2"),
    ):
        assert not code_names_other_family_code(ours, theirs), (ours, theirs)


def test_a_spec_number_never_becomes_the_capacity_head() -> None:
    """The false rejects a plain "compare the digits" rule commits (owner review
    2026-10-17, all of them the row's own picture): a rpm rating, a cubic-foot rating
    and a spaced kilogram all sit *before* a word, so they are not the code's head."""
    for ours, theirs in (
        ("MF200-W120", "midea-1400-rpm-w120-front-load"),
        ("KRF24457-320GD", "kenwood-13-cu-ft-freezer-320gd"),
        ("MF200-W90-G", "midea-8-5-kg-w90-front-load"),
    ):
        assert not code_names_other_family_code(ours, theirs), (ours, theirs)


def test_the_digit_limb_stays_quiet_without_a_family_tail_to_share() -> None:
    """No shared 3+ digit group, or a file that writes only the tail, is no opinion -
    otherwise every bare-number folder would accuse every other model."""
    for ours, theirs in (
        ("1301217E", "1217-front-view"),  # names the family, names no capacity
        ("1301217E", "WhatsApp-Image-2026-02-21-at-12.38.31-PM"),
        ("1301217E", "130-1217-12"),  # our number without its finish: no claim, no proof
        ("18X", "orient-super-18g-eva"),  # capacity pair: the other limb owns this
        ("HSU-18HFPAA", "hsu-12hfpaa"),  # no group reaches the bar on either side
    ):
        assert not code_names_other_family_code(ours, theirs), (ours, theirs)
        assert not code_confirms_this_model(ours, theirs), (ours, theirs)


def test_a_sibling_two_digits_away_is_not_confirmed_as_ours() -> None:
    """The other half of the same coin: ``…U34ABFA`` and ``…U36ABFA`` are different
    machines, and a rule that reads them as one would be a false *proof* for T5."""
    assert not code_confirms_this_model("W90DG5U34ABFA", "WW90DG5U36ABFA")
    assert not code_names_other_family_code("W90DG5U34ABFA", "WW90DG5U36ABFA")


def test_a_tail_nobody_wrote_whole_names_no_family() -> None:
    """Live near-miss, row 26 of the same run: the sheet writes ``DWF 85-1214`` and the
    shop writes its own file ``Dawlance-85214``, so the two happen to end in "214" while
    a moved hyphen - not a different machine - is the whole difference.  A shared
    fragment that neither side wrote as a number is no opinion."""
    assert not code_names_other_family_code("DWF 85-1214 MG INV", "Dawlance-85214")
    assert not code_names_other_family_code("1301217E", "hwm-801217")


def test_the_colourway_inside_the_code_is_identity_not_decoration() -> None:
    """``1301217E`` and ``1301217GB`` are one number in two finishes (the colourway-in-the-
    code law of 2026-10-07), so a digit match must never *prove* them one product - a
    false proof is what would let the GB picture into the E gallery at T5.  And ``1217GB``
    is gold/black, not 1217 gigabytes: read as a unit it would leave the GB row with no
    number at all and its 8 kg sibling free to join."""
    assert not code_confirms_this_model("1301217E", "1301217GB")
    assert code_confirms_this_model("1301217E", "130-1217E")
    assert code_digit_clusters("1301217GB") == (("1301217",),)
    keys = build_model_keys("HAIER 1301217GB-HAIER 1301217GB")
    assert url_names_other_family_code(_SIBLING_INDEXED, keys)
    assert not url_names_other_family_code(
        "https://friendshome.pk/cdn/shop/files/130-1217GB.jpg", keys
    )


def test_a_code_written_twice_in_the_row_is_still_one_code() -> None:
    """Live false positive, the Samsung rows of the same run: the sheet title repeats the
    model ("WA21CK6745BVRT-WA21CK6745BVRT"), S4 reads that as ONE key, and gluing its
    digits gives ``216745216745`` - which no shop ever writes.  Against the file that
    names the same machine once, the repeated half then looks like a different head and
    the row's own hero is refused."""
    keys = build_model_keys("WA21CK6745BVRT-WA21CK6745BVRT")
    assert keys.tokens == ("WA21CK6745BVRT-WA21CK6745BVRT",)
    url = (
        "https://samsung.com.pk/p/samsung-21kg-automatic-top-load-washing-machine-"
        "model-wa21ck6745bvrt.jpg"
    )
    assert not url_names_other_family_code(url, keys)
    assert url_names_other_family_code(
        "https://samsung.com.pk/p/samsung-17kg-automatic-top-load-washing-machine-"
        "model-wa17ck6745bdrt.jpg",
        keys,
    )


def test_s4_code_rejects_both_files_of_the_wrong_product_run() -> None:
    r"""Measured 2026-10-17: S4's prefix limb is blind to a digit-headed code (``1301217E``
    has no alphabetic prefix, so ``if not product_prefixes: return False``), and its
    capacity limb needs the ``\d{2,3}[a-z]`` shape on both sides, which neither code has."""
    keys = build_model_keys("HAIER 1301217E-HAIER 1301217E")
    assert not url_belongs_to_other_model(_SIBLING_PLAIN, keys)  # the old half cannot see it
    assert not url_names_other_capacity_code(_SIBLING_PLAIN, keys)
    assert url_names_other_family_code(_SIBLING_PLAIN, keys)
    assert url_names_other_family_code(_SIBLING_INDEXED, keys)


@pytest.mark.parametrize(
    "url",
    (
        _OWN_1301217E,
        # The must-not-die cases, all taken from the live galleries of this run.
        "https://cdn.shopify.com/files/Haier-HWS-60-50.webp",
        "https://www.lg.com/pk/images/WW90DG5U34ABFA_001_front.jpg",
        "https://dawlance.com.pk/media/img1234.png",
        "https://x.pk/wp-content/uploads/2024/12/DW-7500C-hero-800x600.jpg",
    ),
)
def test_s4_code_keeps_the_row_s_own_spelling_and_the_unreadable_file(url: str) -> None:
    """Each row here carries a real key token, so a pass means the limb read the digits
    and stayed quiet - never that it had nothing to compare."""
    for title in (
        "HAIER 1301217E-HAIER 1301217E",
        "HAIER 6050-HAIER 6050",
        "LG W90DG5U34ABFA",
        "Dawlance DW-7500C",
    ):
        keys = build_model_keys(title)
        assert keys.tokens, title
        assert not url_names_other_family_code(url, keys), (title, url)


def test_s4_code_is_silent_for_a_row_without_a_model_code() -> None:
    keys = build_model_keys("Haier Top Load Washing Machine")
    assert keys.tokens == ()
    assert not url_names_other_family_code(_SIBLING_PLAIN, keys)
    assert not url_names_other_family_code(_OWN_1301217E, keys)


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


def test_a_placeholder_word_glued_without_a_hump_accuses_nothing() -> None:
    """Live run website_ac_clean 2026-10-15: rd.com.pk exported its gallery as
    ``Untitledproject-2025-02-17T110657.024_<uuid>.jpg`` - a lowercase p, so the camel
    split never fires and the plain reader saw one glued word the row did not own.  The
    row's only two pictures died as "another product line".  A word that decomposes
    entirely into the commerce-noise vocabulary (``untitled`` + ``project``) names no
    product and must accuse nothing, however the merchant capitalised it - while a
    glued word that is *not* made of noise words (``grandvcm``) is still the
    accusation it always was."""
    from model_matching import url_names_other_product_line

    haier = build_model_keys("Haier 1.5 Ton Ocean White DC Inverter Air Conditioner HSU-20HFT-T3+")
    live = (
        "https://rd.com.pk/cdn/shop/files/Untitledproject-2025-02-17T110657.024_"
        "73800caf-b874-4a88-be46-9546d7f05580.jpg"
    )
    assert not url_names_other_product_line(live, haier)

    pulse = build_model_keys("Pulse 19C Ultra White T3 | Orient Electronics Pakistan")
    glued_foreign = "https://x.pk/media/grandvcm.png"  # decomposes into nothing
    assert url_names_other_product_line(glued_foreign, pulse)


# -----------------------------------------------------------------------------
# a colourway written inside the code (live run website_ac_clean 2026-10-15:
# Haier sells the SAME body as HSU-20HFTEX/023WDC(OW)-T3 and -DG-; the (OW) is the
# colourway, and dropping it made the dark-grey page look like "our model minus a
# trailing letter" and demoted the brand's own Ocean-White page to merely tolerated)
# -----------------------------------------------------------------------------


def test_a_series_tail_is_still_truncatable_when_no_colourway_is_stated() -> None:
    """The guard belongs to the colourway, never to the shape of a short tail.  ``ES``
    in ``DF-300-ES`` and ``G`` in ``DW-128-G`` are series/feature letters a shop drops
    routinely; treating them as finishes would cost ~14% of the catalogue its tolerance
    (measured 2026-10-15 on 598 corpus codes), so they must keep truncating."""
    assert find_matching_keys(
        "Daewoo DF-300 Microwave", build_model_keys("Daewoo DF-300-ES Microwave")
    )
    assert find_matching_keys(
        "Dawlance DW-128 Half Load Dishwasher", build_model_keys("Dawlance DW-128-G Dishwasher")
    )


def test_a_slash_code_without_a_colourway_keeps_the_old_leniency() -> None:
    """A row that states no finish is unaffected: a shop printing only part of the
    factory side still matches, exactly as before the colourway guard existed."""
    keys = build_model_keys("Haier HSU-13HFCP/023WDC-Triple Inverter")
    assert not keys.colourway_markers
    assert find_matching_keys("Haier HSU-13HFCP 023WDC Inverter AC", keys)


def test_a_glued_colourway_parenthetical_stays_in_the_model_code() -> None:
    """`023WDC(OW)` is one label, not a code plus a removable note: the colourway
    marker belongs to the identity so a different-colour page cannot match it."""
    keys = build_model_keys(
        "Haier 1.5 Ton Ocean White DC Inverter Air Conditioner HSU-20HFTEX/023WDC(OW)-T3 Plus"
    )
    assert keys.tokens[0] == "HSU-20HFTEX/023WDC-OW-T3"
    assert keys.identities[0] == "hsu20hftex023wdcowt3"


def test_the_colourway_letter_tells_the_two_haier_pages_apart() -> None:
    """With (OW) kept, the brand's Ocean-White page matches our row and the Dark-Grey
    page does not - before, both looked like the same model minus a trailing letter."""
    keys = build_model_keys("Haier Ocean White Inverter HSU-20HFTEX/023WDC(OW)-T3 Plus")
    assert find_matching_keys("Haier HSU-20HFTEX/023WDC-OW-T3 Plus", keys)
    assert not find_matching_keys("Haier HSU-20HFTEX/023WDC-DG-T3 Plus", keys)


def test_a_spaced_or_noncolour_parenthetical_is_still_stripped() -> None:
    """Only a code glued to 1-4 letters folds. A spaced note, a multi-word phrase and
    digits inside brackets keep being removed exactly as before - nothing invented."""
    spaced = build_model_keys("Orient Pulse 19C Ultra (2024 edition) White")
    assert all("2024" not in t and "edition" not in t for t in spaced.tokens)
    phrase = build_model_keys("Haier HRF-538 IFGA (Dark Grey) Split AC")
    assert all("grey" not in t.lower() and "dark" not in t.lower() for t in phrase.tokens)


# -----------------------------------------------------------------------------
# tail-chunk match (live run website_ac_clean 2026-10-15: retailers print the sheet
# code's tail - MSEF1C-18HRFN1-QC6 sold as "18HRFN1 QC6" - and every sheet page died
# at strength 0 while the Xtreme/Titan siblings share the same middle chunk)
# -----------------------------------------------------------------------------

_GO_EASY_15 = "Midea 1.5 Ton DC Inverter Air Conditioner MSEF1C-18HRFN1-QC6 Go Easy"
_GO_EASY_24 = "Midea 2 Ton T3 Inverter Air Conditioner MSEF1D-24HRFN1-QC6 Go Easy"


def _tail(
    page: str,
    title: str = _GO_EASY_15,
    *,
    owners: int = 1,
    brand: str | None = "midea",
    siblings: frozenset[str] = frozenset(),
    own: frozenset[str] = frozenset(),
) -> str | None:
    from model_matching import tail_chunk_match

    return tail_chunk_match(
        page,
        title=title,
        keys=build_model_keys(title),
        brand=brand,
        tail_owner_count=owners,
        sibling_terms=siblings,
        own_terms=own,
    )


def test_a_page_printing_the_complete_tail_and_series_words_arms() -> None:
    """mabdullah.pk sells the row as ``18HRFN1 QC6 ... GO EASY`` - the complete tail
    of our code (every chunk after the first), the row's series word and its capacity.
    That is the sheet's own model written shortly, so the limb arms with evidence."""
    page = (
        "Midea 18HRFN1 QC6 1.5 Ton GO EASY T3 Inverter Split Air Conditioner "
        "midea-18hrfn1-qc6-1-5-ton-go-easy-t3-inverter-ac"
    )
    evidence = _tail(page)
    assert evidence is not None
    assert "18hrfn1" in evidence.lower()


def test_a_hyphen_written_capacity_still_counts_as_stated() -> None:
    """Retail slugs spell 1.5 Ton as ``1-5-ton`` - the same number, a different
    separator.  Digit-digit hyphens are dots to the tonnage reader."""
    page = "midea-18hrfn1-qc6-1-5-ton-go-easy-t3-inverter-ac 1-5 ton capacity"
    assert _tail(page) is not None


def test_a_different_tonnage_on_the_page_is_not_corroboration() -> None:
    """A 2 Ton page for a 1.5 Ton row shares the series word but not the size -
    capacity corroboration must actually match the number."""
    page = "Midea 18HRFN1 QC6 2 Ton GO EASY T3 Inverter"
    assert _tail(page) is None


def test_a_page_without_the_row_capacity_never_arms() -> None:
    page = "Midea 18HRFN1 QC6 GO EASY T3 Inverter Split AC"
    assert _tail(page) is None


def test_a_shortened_or_partial_tail_never_arms() -> None:
    """Today's live rejects: alizaina prints ``18HRFNQC6`` (dropped the 1), alfamall
    bare ``12HRFN``, and a Titan page ends in ``QC2``.  A tail with a letter or a
    chunk missing is a *different* code - only the complete run arms."""
    assert _tail("Midea 18HRFNQC6 1.5 Ton GO EASY") is None
    assert _tail("Midea 18HRFN1 1.5 Ton GO EASY") is None
    assert _tail("Midea 18HRFN1 QC2 1.5 Ton GO EASY") is None


def test_a_page_printing_another_head_code_is_refused() -> None:
    """The proven false-accept: surmawala's Xtreme Plus page prints
    ``MSEZ2B-24HRFN1-QC6`` - our row's exact tail under a different head.  A
    code-like chunk sitting directly before our tail run names another series and
    vetoes the limb, however the rest of the page reads."""
    page = "Midea 2 Ton MSEZ2B-24HRFN1-QC6 24HRFN1 QC6 GO EASY Xtreme Plus"
    assert _tail(page, title=_GO_EASY_24) is None
    # our own head before the run is the exact match S1 already handles - never reached,
    # and certainly not a veto; a plain word (brand, capacity) before it is neutral.
    assert _tail("Midea msez1c-18hrfn1-qc6 18HRFN1 QC6 1.5 Ton GO EASY") is None
    assert _tail("midea 18HRFN1 QC6 1.5 Ton GO EASY") is not None


def test_a_letter_only_head_on_the_tail_is_refused() -> None:
    """A pure-letter series head (``MSUS`` of ``MSUS-18HRFN1-QC6``) has no digit, so a
    digit-only head test misses it and another Midea line arms on our Go Easy tail.
    Any opaque token glued before our tail names another series and vetoes."""
    assert _tail("Midea MSUS-18HRFN1-QC6 18HRFN1 QC6 1.5 Ton GO EASY") is None


_T3_PLUS_15 = "Midea 1.5 Ton DC Inverter Air Conditioner MSEF1C-18HRFN1-QC6 T3 Plus"
_DIAMOND_EDGE_15 = "Midea 1.5 Ton DC Inverter Air Conditioner MSEF1C-18HRFN1-QC6 Diamond Edge"


def test_a_page_naming_a_sibling_series_is_refused() -> None:
    """Live-run shape: a row whose only free word after its code is a spec name
    (``T3 Plus``) arms on an Xtreme page, because that page prints ``T3 Plus`` too -
    so the head veto and the tonnage both hold and nothing separates the two.  The
    sheet already knows ``xtreme`` names another row's product (S4's sibling terms)
    and the page's own name says it: the colour guard's veto, aimed at the series."""
    def arms(page: str) -> str | None:
        # this row's own terms are only the brand, and a brand never anchors a page to it
        return _tail(page, _T3_PLUS_15, siblings=frozenset({"xtreme", "titan"}), own=frozenset({"midea"}))

    assert arms("Midea 18HRFN1 QC6 1.5 Ton T3 Plus Xtreme Inverter AC") is None
    assert arms("Midea 18HRFN1 QC6 1.5 Ton T3 Plus Titan Inverter AC") is None
    assert arms("Midea 18HRFN1 QC6 1.5 Ton T3 Plus Inverter AC") is not None


def test_a_page_named_by_our_own_line_word_keeps_its_decorating_sibling() -> None:
    """The live 2026-10-11 lesson S4 already honours (``…Hyper-18X-T3-Super-White-
    Turbo-Pro.webp``): a name that carries *this* row's line word is anchored to the
    row, and another row's word beside it is decoration - a cross-sell in a title.  A
    page printed with our own Diamond Edge and the word ``turbo`` is still our picture;
    refusing it is the owner-visible bug of losing a row's own page."""
    own = frozenset({"midea", "diamond"})
    arms = _tail(
        "Midea 18HRFN1 QC6 1.5 Ton Diamond Edge Turbo Split Inverter AC",
        _DIAMOND_EDGE_15,
        siblings=frozenset({"turbo"}),
        own=own,
    )
    assert arms is not None


def test_a_sibling_code_written_inside_the_page_name_is_still_foreign() -> None:
    """The refusal reads the page's name as whole words, not only the words that survive
    a model-key parse: ``MSE2C-INNOVA`` is another row's code, and the retailer that
    prints it beside our tail is listing two products - the word ``innova`` inside that
    code is evidence of the other one, however the parser segments it."""
    own = frozenset({"midea"})
    page = "Midea 18HRFN1 QC6 1.5 Ton T3 Plus MSE2C-INNOVA 2 Ton Split AC"
    assert _tail(page, _T3_PLUS_15, siblings=frozenset({"innova"}), own=own) is None


def test_the_word_the_limb_demands_on_the_page_never_vetoes() -> None:
    """``easy`` is the word this limb *requires* the page to print, so it can never be
    the foreign word that refuses it: a row's own corroboration outranks a sibling term
    carrying the same letters, or the limb could never arm at all."""
    assert _tail("Midea 18HRFN1 QC6 1.5 Ton GO EASY", siblings=frozenset({"easy"})) is not None


def test_a_sheet_that_names_no_siblings_leaves_the_limb_alone() -> None:
    """A single-row sheet (or a code-only catalogue) hands over nothing, and nothing is
    an opinion: the limb arms exactly as it did before the series veto existed."""
    assert _tail("Midea 18HRFN1 QC6 1.5 Ton T3 Plus Xtreme Inverter AC", title=_T3_PLUS_15) is not None


def test_a_row_whose_only_free_words_are_category_never_arms() -> None:
    """A row that writes the appliance type after its code (``…-QC6 Split Inverter Air
    Conditioner``) states no series word: those words sit on *every* page of the
    category, so they corroborate nothing and a sibling (Xtreme) page sharing the tail
    would arm.  The limb refuses rather than trust a category noun."""
    title = "Midea MSEF1C-18HRFN1-QC6 Split Inverter Air Conditioner 1.5 Ton"
    page = "Midea 18HRFN1 QC6 Split Inverter Air Conditioner 1.5 Ton Xtreme"
    assert _tail(page, title=title) is None


def test_a_slug_hyphen_capacity_does_not_double_count_as_two() -> None:
    """``1-5 ton`` is one stated size (1.5), but a naive reader also sees ``5 ton``
    inside it - so a 5 Ton row would arm on a 1.5 Ton page.  The 5 Ton row must refuse."""
    five_ton = "Midea 5 Ton DC Inverter Air Conditioner MSEF1C-60HRFN1-QC6 Go Easy"
    assert _tail("Midea 60HRFN1 QC6 1-5 ton GO EASY", title=five_ton) is None


def test_a_row_without_series_words_never_arms() -> None:
    """The Haier ``HSU-20HFT-T3+`` row states no free word after its code - there is
    nothing to corroborate with, so the limb refuses to fire at all (the owner fixes
    that sheet row; no guessing rescue)."""
    title = "Haier 1.5 Ton Ocean White DC Inverter Air Conditioner HSU-20HFT-T3+"
    assert _tail("Haier HSU-20HFT-T3 ocean white 1.5 ton", title=title) is None


def test_an_ambiguous_tail_never_arms() -> None:
    """A tail naming two rows of the sheet (MSEF1C-24 and MSEF1D-24 both Go Easy)
    cannot pick a winner - both rows stay dead and loud."""
    page = "Midea 18HRFN1 QC6 1.5 Ton GO EASY T3 Inverter"
    assert _tail(page, owners=2) is None
    assert _tail(page, owners=0) is None


def test_a_different_colourway_page_is_refused() -> None:
    """A row that names a colour arms the limb only for pages that do not contradict
    it - the Dark-Grey twin stays another product (owner rule 2026-10-07)."""
    title = "Midea 1.5 Ton White DC Inverter Air Conditioner MSEF1C-18HRFN1-QC6 Go Easy"
    assert _tail("Midea 18HRFN1 QC6 1.5 Ton GO EASY T3 Dark Grey edition", title=title) is None
    assert _tail("Midea 18HRFN1 QC6 1.5 Ton GO EASY T3 White", title=title) is not None


def test_a_listing_page_never_arms() -> None:
    """orient.com.pk/air-conditioners.html prints every model of the catalogue -
    tails, capacities and series words all present at once.  A page crowded with
    distinct codes is a listing, not a product, and the limb refuses it."""
    codes = " ".join(f"MSX{i}ZZ{i}00" for i in range(1, 12))
    page = f"Midea 18HRFN1 QC6 1.5 Ton GO EASY {codes}"
    assert _tail(page) is None


def test_annotate_tail_owners_counts_a_tail_across_the_sheet() -> None:
    """The sheet's verdict on ambiguity: a tail one row owns arms (count 1), a tail two
    rows share never arms (count 2), and a row with no armable tail (no code, or no
    series word after it) is marked 0 so the limb stays out."""
    from model_matching import annotate_tail_owners
    from models import ProductTask

    tasks = [
        ProductTask(index=0, title=_GO_EASY_15, source_urls=()),
        ProductTask(index=1, title=_GO_EASY_15, source_urls=()),  # same tail, two owners
        ProductTask(
            index=2,
            title="Midea 2 Ton T3 Inverter Air Conditioner MSEF1D-24HRFN1-QC6 Go Easy",
            source_urls=(),
        ),  # its own tail
        ProductTask(
            index=3, title="Haier 1.5 Ton Ocean White DC Inverter HSU-20HFT-T3+", source_urls=()
        ),  # code ends the title: no series word
    ]
    counts = [t.tail_owner_count for t in annotate_tail_owners(tasks)]
    assert counts == [2, 2, 1, 0]


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
def _no_learned_differentiators() -> Iterator[None]:
    """The learned sets are run state: every test starts and ends with none in force."""
    from model_matching import set_colourway_words, set_series_differentiators

    set_series_differentiators(())
    set_colourway_words(())
    yield
    set_series_differentiators(())
    set_colourway_words(())


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


# --------------------------------------------------------------------------- #
# a product line whose name is also a colour word (owner 2026-10-20: the Dawlance
# 9173WB is sold as the `Chrome` line, beside its `Avante` and its `Digital Pro`;
# the colourway the row sells is written elsewhere in the same title)
# --------------------------------------------------------------------------- #
def test_a_colour_word_in_the_line_slot_names_the_line() -> None:
    keys = build_model_keys(
        "Dawlance 9173WB Chrome 12 Cu Ft Steel Door Direct Cool Non-Inverter Refrigerator "
        "- Pearl Copper"
    )
    assert "chrome" in keys.name_words
    assert keys.colour_word == "copper"          # the row still sells Pearl Copper


def test_the_line_slot_word_vetoes_a_page_naming_a_sibling_line() -> None:
    """The point of the rule: two lines of one number must not share pictures."""
    keys = build_model_keys(
        "Dawlance 9173WB Chrome 12 Cu Ft Steel Door Direct Cool Non-Inverter Refrigerator "
        "- Pearl Copper"
    )
    assert score_match(keys, "Dawlance 9173WB Avante Refrigerator Pearl Copper", "dawlance").series_veto
    assert not score_match(keys, "Dawlance 9173WB Chrome Refrigerator Pearl Copper", "dawlance").series_veto


def test_a_colour_word_stays_a_colour_when_the_row_names_no_other_colour() -> None:
    """The guard: nothing is promoted while the slot word IS the colourway being sold."""
    keys = build_model_keys("Dawlance 9173WB Chrome 12 Cu Ft Direct Cool Refrigerator")
    assert "chrome" not in keys.name_words
    assert keys.colour_word == "chrome"


def test_a_colour_word_before_the_code_is_never_promoted() -> None:
    """Position is the evidence, so the slot behind the code is the only one that counts."""
    keys = build_model_keys("Haier Chrome Freezer-On-Top Refrigerator 7 Cu Ft Pearl Blue (HRF-520)")
    assert "chrome" not in keys.name_words


def test_the_row_own_colour_in_another_spelling_is_never_promoted() -> None:
    """``11kg Gray`` reads as a code-plus-colour, and the row sells that grey - so the word
    in the slot is the colourway, not a line (found while measuring the catalogue)."""
    title = "Dawlance Top Load Fully Automatic Washing Machine 11kg Gray (DWT-11671-FLT)"
    keys = build_model_keys(title)
    assert keys.colour_word == "grey"
    assert "gray" not in keys.name_words


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


# --------------------------------------------------------------------------- #
# learned colourway (shade) words (owner 2026-10-16: "Red" is not enough - 9140
# ships as Pearl Red, Ruby Red and Diamond Red, and only the shade picks the picture)
# --------------------------------------------------------------------------- #
#: The catalogue proves a shade word the same way it proves a tier: the word is used
#: in front of a colour name, on more than one series line, and nowhere else.
COLOURWAY_ROWS = (
    ("Dawlance", "Dawlance Avante Freezer-On-Top Refrigerator 9 Cu Ft Pearl Red (9140-WB-GD)"),
    ("Dawlance", "Dawlance Avante Freezer-On-Top Refrigerator 9 Cu Ft Ruby Red (9140-WB-RB)"),
    ("Dawlance", "Dawlance Acce Glass Door Freezer-On-Top Refrigerator Pearl Red (9173)"),
    ("Dawlance", "Dawlance Acce Glass Door Freezer-On-Top Refrigerator Ruby Red (9173)"),
    ("Haier", "Haier Chrome Freezer-On-Top Refrigerator 7 Cu Ft Pearl Blue (HRF-520)"),
    ("Dawlance", "Dawlance Chrome FH Freezer-On-Top Refrigerator 12 Cu Ft Hairline Black (9178-WB)"),
    ("Haier", "Haier Inverter Freezer Refrigerator Hairline Black (HRF-60)"),
    # category nouns in the same slot - a word that names the appliance names no colourway
    ("Haier", "Haier Top Load Washing Machine White"),
    ("Dawlance", "Dawlance Top Load Washing Machine Grey"),
    # a unit abbreviation in the same slot - too short to be a shade
    ("Dawlance", "Dawlance Water Dispenser 20 Ltr Blue"),
    ("Haier", "Haier Water Dispenser 25 Ltr Black"),
    # a series word that also lives outside the colour slot - not a shade name
    ("Haier", "Haier Grill Red Gas Stove"),
    ("Flare", "Flare Grill Red Gas Stove"),
    ("Haier", "Haier Barbecue Grill Stand"),
    ("Flare", "Flare Heavy Duty Grill Stand"),
    ("Haier", "Haier Grill With Lid"),
    ("Flare", "Flare Grill With Lid"),
)


def test_the_catalogue_teaches_which_words_name_a_shade() -> None:
    from model_matching import colourway_words, learn_colourway_words

    learned = learn_colourway_words(COLOURWAY_ROWS)
    assert learned == colourway_words()
    assert {"ruby", "hairline"} <= learned
    # a word the colour vocabulary already knows needs no teaching to be a shade
    assert "pearl" not in learned


def test_a_word_used_outside_the_colour_slot_names_no_shade() -> None:
    """Exclusivity is the classifier: a word at home elsewhere is not a colourway."""
    from model_matching import learn_colourway_words

    assert "grill" not in learn_colourway_words(COLOURWAY_ROWS)


def test_a_category_word_in_the_colour_slot_is_never_a_shade() -> None:
    """"… Washing Machine White" teaches no colourway, even with perfect evidence."""
    from model_matching import learn_colourway_words

    assert not {"machine", "ltr"} & learn_colourway_words(COLOURWAY_ROWS)


def test_nothing_learned_leaves_the_colour_reading_exactly_as_today() -> None:
    """Fail open: an unlearned shade word can only cost a protection, never reject."""
    from model_matching import colourway_words, learn_colourway_words

    title = "Dawlance Avante Freezer-On-Top Refrigerator 9 Cu Ft Pearl Red (9140-WB-GD)"
    before = build_model_keys(title)
    assert not learn_colourway_words(())
    assert not colourway_words()
    after = build_model_keys(title)
    assert after.colour_word == before.colour_word == "red"
    assert after.name_words == before.name_words


# --------------------------------------------------------------------------- #
# full-shade colourway match (owner 2026-10-16: 9140-WB is Pearl, Ruby and Diamond
# Red at once, so "red" proves nothing and only the shade is the promised picture)
# --------------------------------------------------------------------------- #
PEARL_RED_ROW = "Dawlance Avante Freezer-On-Top Refrigerator 9 Cu Ft Pearl Red (9140-WB-GD)"
PEARL_RED_PAGE = "Dawlance REF 9140WB Avante Pearl Red Refrigerator"
RUBY_RED_PAGE = "Dawlance REF 9140WB Avante Ruby Red Refrigerator"


def test_the_row_reads_its_own_shade() -> None:
    keys = build_model_keys(PEARL_RED_ROW)
    assert (keys.colour_word, keys.colour_shade) == ("red", "pearl")
    assert build_model_keys(RUBY_RED_PAGE).colour_shade == ""   # untaught: no opinion


def test_a_page_naming_another_shade_of_the_same_colour_is_not_this_product() -> None:
    from model_matching import learn_colourway_words

    learn_colourway_words(COLOURWAY_ROWS)          # the catalogue teaches "ruby"
    keys = build_model_keys(PEARL_RED_ROW)
    assert match_product(keys=keys, brand="Dawlance", strong_text=RUBY_RED_PAGE).rule == (
        "colour_conflict"
    )
    assert match_product(keys=keys, brand="Dawlance", strong_text=PEARL_RED_PAGE).rule == (
        "model_key"
    )


def test_an_unlearned_shade_leaves_the_page_exactly_as_today() -> None:
    """Fail open: without catalogue evidence nothing is refused on a word alone."""
    keys = build_model_keys(PEARL_RED_ROW)
    match = match_product(keys=keys, brand="Dawlance", strong_text=RUBY_RED_PAGE)
    assert match.matched and match.rule == "model_key"


def test_a_page_that_states_no_shade_is_still_no_opinion() -> None:
    from model_matching import learn_colourway_words

    learn_colourway_words(COLOURWAY_ROWS)
    keys = build_model_keys(PEARL_RED_ROW)
    match = match_product(keys=keys, brand="Dawlance",
                          strong_text="Dawlance REF 9140WB Avante Refrigerator")
    assert match.matched


# --------------------------------------------------------------------------- #
# S20.shade-unstated: the sheet is the only place a shade can be written
# (owner 2026-10-16: warn, never match on the URL)
# --------------------------------------------------------------------------- #
def test_a_row_naming_no_shade_is_reported_when_its_own_link_names_one() -> None:
    from models import ProductTask

    tasks = [
        ProductTask(
            index=0,
            title="Dawlance Avante Freezer-On-Top Refrigerator 9 Cu Ft Red (9140-WB-GD)",
            source_urls=(
                (
                    "https://www.dawlance.com.pk/double-door-refrigerator/"
                    "ref-9140wb-avante-pearl-red-refrigerators"
                ),
            ),
        ),
    ]
    assert shade_unstated_rows(tasks) == ((tasks[0].title, "pearl red"),)


def test_a_row_that_already_states_its_shade_is_never_reported() -> None:
    from models import ProductTask

    tasks = [
        ProductTask(
            index=0,
            title="Dawlance Avante Freezer-On-Top Refrigerator 9 Cu Ft Pearl Red (9140-WB-GD)",
            source_urls=("https://www.dawlance.com.pk/ref-9140wb-avante-pearl-red-refrigerators",),
        ),
    ]
    assert not shade_unstated_rows(tasks)


def test_a_link_naming_a_different_colour_gives_no_hint() -> None:
    """The sheet link is sometimes the wrong page: Gem Black beside sapphire-purple."""
    from models import ProductTask

    tasks = [
        ProductTask(
            index=0,
            title="Dawlance Refrigerator 12 Cu Ft Gem Black (9173)",
            source_urls=("https://www.dawlance.com.pk/ref-9173wb-avante-sapphire-purple",),
        ),
    ]
    assert not shade_unstated_rows(tasks)


# --- the shade is read from what the page itself says, never from a slug or a join ---
NO_SHADE_PAGE = "Dawlance REF 9140WB Avante Refrigerator"


def test_a_shade_only_the_page_url_knows_never_rejects() -> None:
    """Owner decision 2: a sheet/URL slug is a hint, not evidence the page states a shade."""
    set_colourway_words({"diamond", "ruby"})
    keys = build_model_keys(PEARL_RED_ROW)
    strong = f"https://www.dawlance.com.pk/ref-9140wb-avante-diamond-red-refrigerators\n{NO_SHADE_PAGE}"
    assert match_product(keys=keys, brand="Dawlance", strong_text=strong).rule == "model_key"


def test_a_page_listing_our_shade_beside_another_is_not_a_clash() -> None:
    """A shop that lists the range ("Pearl Red / Ruby Red") names our colourway too."""
    set_colourway_words({"ruby"})
    keys = build_model_keys(PEARL_RED_ROW)
    assert match_product(
        keys=keys, brand="Dawlance",
        strong_text="Dawlance Avante 9140WB Refrigerator Pearl Red / Ruby Red",
    ).matched


def test_a_labelled_colour_never_borrows_a_shade_from_the_field_before_it() -> None:
    """The page's labelled "Colour:" value is read on its own, so a slug cannot leak into it."""
    set_colourway_words({"mist", "hairline"})
    keys = build_model_keys(
        "Dawlance Avante+ Freezer-On-Top Refrigerator 16 Cu Ft Hairline Green (9178-WB)"
    )
    assert match_product(
        keys=keys, brand="Dawlance",
        strong_text="https://www.dawlance.com.pk/ref-9178wb-avante-mist",
        selected_colourway="Green",
    ).rule == "model_key"


def test_the_tail_last_resort_never_arms_for_another_shade() -> None:
    """S15.tail is a last resort for THIS picture, not for any page sharing the short code."""
    set_colourway_words({"ruby"})
    row = "Midea 1.5 Ton Pearl Red DC Inverter Air Conditioner MSEF1C-18HRFN1-QC6 Go Easy"
    keys = build_model_keys(row)
    def arms(page: str) -> bool:
        return tail_chunk_match(page, title=row, keys=keys, brand="Midea",
                                tail_owner_count=1) is not None
    assert arms("Midea 18HRFN1 QC6 Go Easy 1.5 Ton Pearl Red Split AC")
    assert not arms("Midea 18HRFN1 QC6 Go Easy 1.5 Ton Ruby Red Split AC")


def test_a_marker_is_not_droppable_through_the_letter_tolerance() -> None:
    """(OW) survived the slash and segment paths but fell to the plain letter drop."""
    keys = build_model_keys("Haier HSU-13HFCP/023WDC(OW) Inverter AC")
    assert keys.colourway_markers == frozenset({"ow"})
    assert not match_product(
        keys=keys, brand="Haier", strong_text="Haier HSU-13HFCP 023WDC-DG Inverter AC"
    ).matched


def test_a_page_printing_our_stem_under_another_short_tail_is_not_our_model() -> None:
    """Omission says nothing; a different tail on the same stem says another product."""
    keys = build_model_keys("Dawlance Avante Freezer-On-Top Refrigerator 9 Cu Ft Red (9140-WB-GD)")
    assert not match_product(
        keys=keys, brand="Dawlance", strong_text="Dawlance REF 9140WB-RB Avante Refrigerator"
    ).matched
    # and the innocent readings stay exactly as they were
    assert match_product(
        keys=keys, brand="Dawlance", strong_text="Dawlance REF 9140WB Avante Refrigerator"
    ).matched
    assert match_product(
        keys=keys, brand="Dawlance", strong_text="Dawlance 9140WB Red refrigerator"
    ).matched

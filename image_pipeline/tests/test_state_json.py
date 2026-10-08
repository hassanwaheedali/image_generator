"""Universal embedded-state extraction (TCL pageData-style hidden-input JSON)."""

from __future__ import annotations

import json

from bs4 import BeautifulSoup

from gallery_extractors import ExtractionContext, state_json
from model_matching import build_model_keys

PAGE_URL = "https://www.tcl.com/au/en/tvs/85c8l"


def _context(title: str) -> ExtractionContext:
    products = [
        {"productInfo": {"customerModel": "85C8L", "productPictures": [
            {"pictureUrl": f"//aws.tcl.com/c8l/id/65-85/{i}.jpg"} for i in range(2, 6)]}},
        {"productInfo": {"customerModel": "98C8L", "productPictures": [
            {"pictureUrl": "//aws.tcl.com/c8l/id/98/1.jpg"}]}},
    ]
    html = (
        "<html><body><input id='pageData' value='"
        + json.dumps({"allproducts": products})
        + "'></body></html>"
    )
    soup = BeautifulSoup(html, "lxml")
    return ExtractionContext(
        page_url=PAGE_URL, soup=soup, keys=build_model_keys(title),
        strength=2, brand="TCL", raw_html=html,
    )


def test_state_json_reads_hidden_input_and_picks_only_the_matching_model() -> None:
    candidates = state_json.extract(_context("TCL 85 Inch SQD Mini LED TV (85C8L)"))
    urls = [c.url for c in candidates]
    assert len(urls) == 4
    assert all("65-85" in u for u in urls)          # the 85C8L cluster
    assert not any("/98/" in u for u in urls)       # the sibling model is excluded
    assert candidates[0].source == "state_json"


def test_state_json_returns_full_cluster_for_the_exact_size() -> None:
    """Real reported shape: allproducts[65/75/85], HERO + 2..8.jpg == 8 per model."""
    def pics(model: str) -> list[dict[str, str]]:
        return [{"pictureUrl": "//aws.tcl.com/c8l/id/65_75_85_C8L_FRONT_HERO.png"}] + [
            {"pictureUrl": f"//aws.tcl.com/c8l/id/{model}/{i}.jpg"} for i in range(2, 9)
        ]

    products = [
        {"productInfo": {"customerModel": m, "productPictures": pics(m)}}
        for m in ("65C8L", "75C8L", "85C8L")
    ]
    html = (
        "<html><body><input id='pageData' value='"
        + json.dumps({"allproducts": products})
        + "'></body></html>"
    )
    context = ExtractionContext(
        page_url=PAGE_URL, soup=BeautifulSoup(html, "lxml"),
        keys=build_model_keys("TCL 85 Inch SQD Mini LED TV (85C8L)"),
        strength=2, brand="TCL", raw_html=html,
    )
    urls = [c.url for c in state_json.extract(context)]
    assert len(urls) == 8                            # full 85C8L gallery, not 1
    assert any("FRONT_HERO" in u for u in urls)
    assert not any("/65C8L/" in u or "/75C8L/" in u for u in urls)


def test_state_json_is_silent_below_strength_two() -> None:
    context = _context("TCL 85 Inch SQD Mini LED TV (85C8L)")
    context = ExtractionContext(**{**context.__dict__, "strength": 1})
    assert state_json.extract(context) == []


def test_state_json_ignores_unrelated_state() -> None:
    context = _context("TCL 85 Inch SQD Mini LED TV (85C8L)")
    html = "<html><body><input value='{\"cart\": [\"//x/a.jpg\"]}'></body></html>"
    context = ExtractionContext(
        **{**context.__dict__, "soup": BeautifulSoup(html, "lxml"), "raw_html": html}
    )
    assert state_json.extract(context) == []

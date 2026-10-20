"""Which sheet rows the run may process, and which it must report as unusable.

`main.py` partitions the sheet before the batch starts.  The partition is the whole rule, so it
lives in one named function with its own tests here.
"""

from __future__ import annotations

from models import ProductTask


def task(index: int, title: str, urls: tuple[str, ...]) -> ProductTask:
    return ProductTask(index=index, title=title, source_urls=urls)


def test_a_row_without_a_url_is_still_searchable_in_the_scrape_mode() -> None:
    """Step-1 may not have filled the column yet (owner 2026-10-20: 50 refrigerator rows
    written from the shops, titles only) - so the title alone must go to the search chain
    instead of being reported as `no source url`."""
    from main import split_sheet_rows

    rows = [task(0, "Dawlance 9178LF Avante 14 Cu Ft Refrigerator - Cloud White", ())]
    good, bad = split_sheet_rows(rows, urls_required=False)
    assert good == rows
    assert bad == []


def test_the_url_column_is_the_picture_when_options_2_and_3_run() -> None:
    """`--direct` reads image URLs and `--images-dir` reads local folders, so an empty cell
    genuinely leaves nothing to fetch - those rows keep failing loudly."""
    from main import split_sheet_rows

    rows = [task(0, "Haier HSU-12HFPAA Inverter AC", ())]
    good, bad = split_sheet_rows(rows, urls_required=True)
    assert good == []
    assert bad == rows


def test_an_empty_title_is_a_bad_row_in_every_mode() -> None:
    from main import split_sheet_rows

    rows = [task(0, "", ("https://www.dawlance.com.pk/ref-9178lf",))]
    for urls_required in (False, True):
        good, bad = split_sheet_rows(rows, urls_required=urls_required)
        assert good == [] and bad == rows

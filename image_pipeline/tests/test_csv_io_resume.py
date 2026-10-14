"""Sheet reading, atomic writes, slugs and the checkpoint store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import clean_product_image
from PIL import Image

from csv_io import (
    append_checkpoint,
    append_jsonl,
    atomic_write_csv,
    atomic_write_text,
    filename_prefix,
    find_column,
    hash_source_urls,
    load_banner_cache,
    load_checkpoint,
    merge_duplicate_titles,
    normalize_header,
    read_input,
    safe_slug,
    save_banner_cache,
    save_image_atomic,
)
from models import (
    CHECKPOINT_LAYOUT,
    CheckpointEntry,
    ModelKeys,
    ProductResult,
    ProductTask,
    Status,
)


def _keys(*tokens: str, keywords: tuple[str, ...] = ()) -> ModelKeys:
    return ModelKeys(cleaned_title="t", tokens=tokens, title_key="t", keywords=keywords)


#: The platforms a run publishes, so resume can be judged against a plan the way the
#: pipeline does it.
_PLAN = ("alfa", "digi")


def write_sheet(path: Path, header: str, rows: list[list[str]]) -> None:
    path.write_text(
        "\ufeff" + header + "\n" + "\n".join(",".join(row) for row in rows) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# headers and reading
# --------------------------------------------------------------------------- #
def test_normalize_header_and_find_column() -> None:
    assert normalize_header("Source URL") == "sourceurl"
    assert normalize_header("source_url") == "sourceurl"
    assert find_column(["Title", "Images"], ("sourceurl", "images")) == "Images"
    assert find_column(["Title", "X"], ("sourceurl", "images")) is None


@pytest.mark.parametrize(
    "header",
    ["Title,Source URL", "title,source_url", "TITLE,SourceURL", "Title,Images", "Title,Image"],
)
def test_read_input_accepts_header_variants(tmp_path: Path, header: str) -> None:
    path = tmp_path / "sheet.csv"
    write_sheet(path, header, [["Haier HSU-12HFPAA", "https://a.example/p|https://b.example/p"]])
    tasks = read_input(path)
    assert len(tasks) == 1
    assert tasks[0].title == "Haier HSU-12HFPAA"
    assert tasks[0].source_urls == ("https://a.example/p", "https://b.example/p")


def test_read_input_accepts_products_csv_fixture() -> None:
    fixture_path = Path(__file__).resolve().parents[0] / "fixtures" / "test_files" / "Products.csv"
    if fixture_path.exists():
        tasks = read_input(fixture_path)
        assert len(tasks) > 0
        assert tasks[0].title != ""
        assert len(tasks[0].source_urls) > 0


def test_read_input_rejects_unknown_headers(tmp_path: Path) -> None:
    path = tmp_path / "sheet.csv"
    write_sheet(path, "Model,Linkz", [["x", "y"]])
    with pytest.raises(ValueError):
        read_input(path)


def test_read_input_blank_cells(tmp_path: Path) -> None:
    path = tmp_path / "sheet.csv"
    write_sheet(path, "Title,Images", [["", "https://a.example/p"], ["Haier HSU-12HFPAA", ""]])
    tasks = read_input(path)
    assert tasks[0].title == "" and tasks[1].source_urls == ()


def test_read_xlsx(tmp_path: Path) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Title", "Source URL"])
    sheet.append(["Haier HSU-12HFPAA", "https://a.example/p"])
    path = tmp_path / "sheet.xlsx"
    workbook.save(path)
    tasks = read_input(path)
    assert tasks[0].title == "Haier HSU-12HFPAA"
    assert tasks[0].source_urls == ("https://a.example/p",)


def test_merge_duplicate_titles_keeps_first_order() -> None:
    tasks = [
        ProductTask(0, "A", ("u1",)),
        ProductTask(1, "B", ("u2",)),
        ProductTask(2, "a", ("u1", "u3")),
    ]
    merged, warnings = merge_duplicate_titles(tasks)
    assert [item.title for item in merged] == ["A", "B"]
    assert merged[0].source_urls == ("u1", "u3")
    assert merged[0].index == 0
    assert len(warnings) == 1


# --------------------------------------------------------------------------- #
# atomicity
# --------------------------------------------------------------------------- #
def test_atomic_writers_leave_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "out" / "x.csv"
    atomic_write_csv(target, ["A", "B"], [["1", "2"]])
    atomic_write_text(tmp_path / "out" / "y.txt", "hello")
    leftovers = [path.name for path in (tmp_path / "out").iterdir() if path.name.startswith(".")]
    assert leftovers == []
    assert target.read_text(encoding="utf-8") == "A,B\n1,2\n"


def test_atomic_csv_replaces_previous_content(tmp_path: Path) -> None:
    target = tmp_path / "x.csv"
    atomic_write_csv(target, ["A"], [["1"], ["2"]])
    atomic_write_csv(target, ["A"], [["9"]])
    assert target.read_text(encoding="utf-8") == "A\n9\n"


# --------------------------------------------------------------------------- #
# checkpoint + banner cache
# --------------------------------------------------------------------------- #
def test_checkpoint_round_trip_keeps_latest(tmp_path: Path) -> None:
    path = tmp_path / "state" / "checkpoint.jsonl"
    result = ProductResult(
        task_index=0,
        title="Haier HSU-12HFPAA",
        status=Status.OK,
        platform_urls=(
            ("digi", ("https://img.example/a.jpg",)),
            ("alfa", ("https://img.example/a.jpg",)),
        ),
        source_url_hash="abc123",
    )
    append_checkpoint(path, result, timestamp="2026-01-01T00:00:00Z")
    append_checkpoint(path, result, timestamp="2026-01-02T00:00:00Z")
    entries = load_checkpoint(path)
    assert len(entries) == 1
    entry = entries["haier hsu-12hfpaa"]
    assert entry.status is Status.OK
    assert entry.timestamp == "2026-01-02T00:00:00Z"
    assert entry.is_resume_done("abc123", _PLAN) and not entry.is_resume_done("other", _PLAN)


def test_checkpoint_ignores_garbage_lines(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.jsonl"
    append_jsonl(
        path,
        {
            "title": "A",
            "status": "ok",
            "layout": 2,
            "platform_urls": [["digi", ["u"]]],
            "source_url_hash": "h",
        },
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
    entries = load_checkpoint(path)
    assert list(entries) == ["a"]


def test_banner_cache_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state" / "banner_urls.json"
    save_banner_cache(path, {"hash1": "https://img.example/banner.jpg"})
    assert load_banner_cache(path) == {"hash1": "https://img.example/banner.jpg"}
    path.write_text("{not json", encoding="utf-8")
    assert load_banner_cache(path) == {}


def test_hash_source_urls_is_order_sensitive() -> None:
    assert hash_source_urls(("a", "b")) != hash_source_urls(("b", "a"))
    assert hash_source_urls(("a",)) == hash_source_urls(("a",))


def test_safe_slug_fallbacks() -> None:
    assert safe_slug("***") .startswith("product-")
    assert safe_slug("Haier HSU-12HFPAA") == "haier-hsu-12hfpaa"
    assert len(safe_slug("x" * 300)) <= 80


# --------------------------------------------------------------------------- #
# Windows reserved device names: a folder called "con" cannot be deleted
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("title", ["CON", "nul", "PRN", "aux", "Com1", "LPT9"])
def test_safe_slug_neutralises_windows_device_names(title: str) -> None:
    assert safe_slug(title) == f"_{title.lower()}"


def test_safe_slug_leaves_a_word_that_merely_starts_with_a_device_name() -> None:
    """`con` is reserved; `console` is not.  The guard must match the whole name."""
    assert safe_slug("Console Table") == "console-table"
    assert safe_slug("Auxiliary Pump") == "auxiliary-pump"
    assert safe_slug("Nulled") == "nulled"


# --------------------------------------------------------------------------- #
# filename_prefix - the stem shared by every platform's copy of a product
# --------------------------------------------------------------------------- #
def test_filename_prefix_uses_every_model_token_in_order() -> None:
    """D7: the owner chose all tokens, so the Excel-visible prefix names the variant."""
    keys = _keys("12HRFN1", "QC6")
    assert filename_prefix(keys, "Midea 12HRFN1 QC6 1 Ton GO EASY T3 Inverter AC") == "12HRFN1_QC6"


def test_filename_prefix_keeps_case_and_in_token_separators() -> None:
    assert filename_prefix(_keys("DW-7500C"), "DW-7500C") == "DW-7500C"
    assert filename_prefix(_keys("HSU.12HFPAA"), "HSU.12HFPAA") == "HSU.12HFPAA"


def test_filename_prefix_strips_characters_that_are_illegal_in_a_filename() -> None:
    assert filename_prefix(_keys("AB/CD:EF"), "AB/CD:EF") == "ABCDEF"
    assert filename_prefix(_keys("A B"), "A B") == "AB"


def test_filename_prefix_is_a_single_token_when_the_title_has_one() -> None:
    assert filename_prefix(_keys("55S80EUX"), "Haier 55S80EUX") == "55S80EUX"


def test_filename_prefix_truncates_at_a_token_boundary_never_mid_token() -> None:
    """A half-printed model code is worse than a shorter whole one."""
    tokens = tuple(f"TOK{i:02d}XXXX" for i in range(10))          # 9 chars each
    prefix = filename_prefix(_keys(*tokens), "t")
    assert len(prefix) <= 48
    assert prefix.split("_") == list(tokens)[: len(prefix.split("_"))]
    assert not prefix.endswith("_")


def test_filename_prefix_hard_slices_a_first_token_that_is_alone_over_the_cap() -> None:
    assert filename_prefix(_keys("X" * 90), "t") == "X" * 48


def test_filename_prefix_guards_a_reserved_joined_result() -> None:
    assert filename_prefix(_keys("CON"), "CON") == "_CON"
    assert filename_prefix(_keys("COM1"), "COM1") == "_COM1"


def test_filename_prefix_does_not_guard_a_reserved_word_inside_a_longer_stem() -> None:
    """Only the final stem is a filesystem name, so `AUX_1` is legal and must stay readable.

    Guarding every token would rename real model codes - "AUX" is a legitimate series word.
    """
    assert filename_prefix(_keys("AUX", "1"), "AUX 1") == "AUX_1"
    assert filename_prefix(_keys("CONSOLE"), "CONSOLE") == "CONSOLE"


def test_filename_prefix_never_ends_with_a_dot() -> None:
    """Windows refuses a trailing dot, whatever precedes it."""
    assert filename_prefix(_keys("ABC."), "ABC.") == "ABC"


def test_filename_prefix_falls_back_to_keywords_plus_a_hash() -> None:
    """A row that passed rule 4.1 with no model code still needs a stable, unique stem."""
    keys = _keys(keywords=("dawlance", "dual", "pro", "30", "inverter"))
    prefix = filename_prefix(keys, "Dawlance Dual Pro 30 Inverter T3")
    assert prefix.startswith("dawlance-dual-pro-30-inverter-")
    assert len(prefix) <= 48
    other = filename_prefix(keys, "Dawlance Dual Pro 30 Inverter T4")
    assert other != prefix                      # the hash suffix separates equal keywords
    assert filename_prefix(keys, "Dawlance Dual Pro 30 Inverter T3") == prefix


def test_filename_prefix_falls_back_to_a_hash_when_there_is_nothing_to_use() -> None:
    prefix = filename_prefix(_keys(), "???")
    assert prefix.startswith("product-")
    assert len(prefix) == len("product-") + 8


def test_filename_prefix_ignores_tokens_that_sanitise_to_nothing() -> None:
    assert filename_prefix(_keys("::", "12X", "//"), "12X") == "12X"


def test_filename_prefix_is_deterministic() -> None:
    keys = _keys("12HRFN1", "QC6")
    assert filename_prefix(keys, "Midea 12HRFN1 QC6") == filename_prefix(keys, "Midea 12HRFN1 QC6")


# --------------------------------------------------------------------------- #
# save_image_atomic - the suffix decides the format (F4)
# --------------------------------------------------------------------------- #
def test_save_image_atomic_writes_jpeg_for_a_jpg_suffix(tmp_path: Path) -> None:
    path = tmp_path / "img" / "one.jpg"
    save_image_atomic(clean_product_image(200, 200), path, jpeg_quality=90)
    opened = Image.open(path)
    assert opened.format == "JPEG"
    assert opened.size == (200, 200)
    assert [item.name for item in path.parent.iterdir()] == ["one.jpg"]


def test_save_image_atomic_never_puts_jpeg_bytes_in_a_png_file(tmp_path: Path) -> None:
    """The bug: a quality argument used to win over the suffix, mislabelling the file."""
    path = tmp_path / "one.png"
    save_image_atomic(clean_product_image(120, 120), path, jpeg_quality=90)
    assert Image.open(path).format == "PNG"


def test_save_image_atomic_defaults_to_jpeg_for_a_jpg_suffix(tmp_path: Path) -> None:
    path = tmp_path / "one.jpg"
    save_image_atomic(clean_product_image(80, 80), path)
    assert Image.open(path).format == "JPEG"


def test_save_image_atomic_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    path = tmp_path / "one.png"
    save_image_atomic(clean_product_image(60, 60), path, jpeg_quality=90)
    assert sorted(item.name for item in tmp_path.iterdir()) == ["one.png"]


def test_decisions_log_is_jsonl(tmp_path: Path) -> None:
    from decisions import DecisionLog

    log = DecisionLog(tmp_path / "decisions.jsonl")
    log.keep(title="T", url="https://img.example/a.jpg", rule_id="accept", detail="source=json_ld")
    log.reject(title="T", url="https://img.example/b.jpg", rule_id="S5", detail="bad keyword")
    lines = (tmp_path / "decisions.jsonl").read_text(encoding="utf-8").strip().splitlines()
    payloads = [json.loads(line) for line in lines]
    assert [payload["decision"] for payload in payloads] == ["keep", "reject"]
    assert payloads[1]["rule_id"] == "S5"


def test_dry_run_checkpoint_entries_are_never_resumed() -> None:
    """Owner run 2026-10-08: a dry-run wrote file:// URLs into the checkpoint and the
    next (live) run skipped the row as done.  Dry entries must always re-process."""
    dry = CheckpointEntry(
        title="Homage HRF-47662-GD",
        source_url_hash="abc",
        status=Status.PARTIAL,
        platform_urls=(("digi", ("file:///tmp/output/images/x.jpg",)),),
        reason="",
        timestamp="2026-10-08T19:21:00",
        layout=CHECKPOINT_LAYOUT,
    )
    assert dry.is_resume_done("abc", ("digi",)) is False

    live = CheckpointEntry(
        title="Homage HRF-47662-GD",
        source_url_hash="abc",
        status=Status.PARTIAL,
        platform_urls=(("digi", ("https://iili.io/some-upload.jpg",)),),
        reason="",
        timestamp="2026-10-08T19:21:00",
        layout=CHECKPOINT_LAYOUT,
    )
    assert live.is_resume_done("abc", ("digi",)) is True
    assert live.is_resume_done("different-hash", ("digi",)) is False


def test_a_row_published_on_part_of_the_plan_is_not_done(tmp_path: Path) -> None:
    """A digi-only run's checkpoint cannot satisfy a run that also publishes Alfamall.

    Resuming it as "done" would leave ``alfa/alfamall_product_images.csv`` silently short
    by that row, which is the one failure mode a shop cannot spot.
    """
    entry = CheckpointEntry(
        title="Haier HSU-12HFPAA",
        source_url_hash="abc",
        status=Status.OK,
        platform_urls=(("digi", ("https://iili.io/1.jpg",)),),
        reason="",
        timestamp="2026-10-14T00:00:00",
        layout=CHECKPOINT_LAYOUT,
    )
    assert entry.is_resume_done("abc", ("digi",)) is True
    assert entry.is_resume_done("abc", _PLAN) is False
    assert entry.is_resume_done("abc", ()) is False


def test_a_pre_platform_checkpoint_is_never_resumed() -> None:
    """An entry written before URLs were stored per platform cannot be trusted.

    It carries two fixed lists, so a run that now publishes a third platform would mark
    the row done and then write a CSV column it has no URLs for.
    """
    old = CheckpointEntry.from_dict(
        {
            "title": "Dawlance DW-7500C",
            "status": "ok",
            "digi_urls": ["https://iili.io/1.jpg"],
            "alfa_urls": ["https://iili.io/2.jpg"],
            "source_url_hash": "abc",
        }
    )
    assert old.layout != CHECKPOINT_LAYOUT
    assert old.platform_urls == ()
    assert old.is_resume_done("abc", _PLAN) is False


def test_checkpoint_round_trip_carries_the_layout_and_every_platform(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.jsonl"
    result = ProductResult(
        task_index=0,
        title="Midea 12HRFN1 QC6",
        status=Status.OK,
        platform_urls=(
            ("alfa", ("https://i.example/a1.jpg", "https://i.example/banner.jpg")),
            ("digi", ("https://i.example/d1.jpg", "https://i.example/banner.jpg")),
        ),
        source_url_hash="h1",
    )
    append_checkpoint(path, result, timestamp="2026-10-14T00:00:00Z")
    entry = load_checkpoint(path)["midea 12hrfn1 qc6"]
    assert entry.layout == CHECKPOINT_LAYOUT
    assert entry.platform_urls == result.platform_urls   # order preserved, per platform
    assert entry.is_resume_done("h1", _PLAN) is True

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
from models import ProductResult, ProductTask, Status


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
    ["Title,Source URL", "title,source_url", "TITLE,SourceURL", "Title,Images"],
)
def test_read_input_accepts_header_variants(tmp_path: Path, header: str) -> None:
    path = tmp_path / "sheet.csv"
    write_sheet(path, header, [["Haier HSU-12HFPAA", "https://a.example/p|https://b.example/p"]])
    tasks = read_input(path)
    assert len(tasks) == 1
    assert tasks[0].title == "Haier HSU-12HFPAA"
    assert tasks[0].source_urls == ("https://a.example/p", "https://b.example/p")


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


def test_save_image_atomic(tmp_path: Path) -> None:
    path = tmp_path / "img" / "one.jpg"
    save_image_atomic(clean_product_image(200, 200), path, jpeg_quality=90)
    assert path.exists()
    assert Image.open(path).size == (200, 200)
    assert [item.name for item in path.parent.iterdir()] == ["one.jpg"]


# --------------------------------------------------------------------------- #
# checkpoint + banner cache
# --------------------------------------------------------------------------- #
def test_checkpoint_round_trip_keeps_latest(tmp_path: Path) -> None:
    path = tmp_path / "state" / "checkpoint.jsonl"
    result = ProductResult(
        task_index=0,
        title="Haier HSU-12HFPAA",
        status=Status.OK,
        digi_urls=("https://img.example/a.jpg",),
        alfa_urls=("https://img.example/a.jpg",),
        source_url_hash="abc123",
    )
    append_checkpoint(path, result, timestamp="2026-01-01T00:00:00Z")
    append_checkpoint(path, result, timestamp="2026-01-02T00:00:00Z")
    entries = load_checkpoint(path)
    assert len(entries) == 1
    entry = entries["haier hsu-12hfpaa"]
    assert entry.status is Status.OK
    assert entry.timestamp == "2026-01-02T00:00:00Z"
    assert entry.is_resume_done("abc123") and not entry.is_resume_done("other")


def test_checkpoint_ignores_garbage_lines(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.jsonl"
    append_jsonl(path, {"title": "A", "status": "ok", "digi_urls": ["u"], "source_url_hash": "h"})
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


def test_decisions_log_is_jsonl(tmp_path: Path) -> None:
    from decisions import DecisionLog

    log = DecisionLog(tmp_path / "decisions.jsonl")
    log.keep(title="T", url="https://img.example/a.jpg", rule_id="accept", detail="source=json_ld")
    log.reject(title="T", url="https://img.example/b.jpg", rule_id="S5", detail="bad keyword")
    lines = (tmp_path / "decisions.jsonl").read_text(encoding="utf-8").strip().splitlines()
    payloads = [json.loads(line) for line in lines]
    assert [payload["decision"] for payload in payloads] == ["keep", "reject"]
    assert payloads[1]["rule_id"] == "S5"

"""Tests for enterprise input directory auto-discovery and resolution."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from config import ConfigError
from main import (
    _discover_sheets_in_dir,
    build_config,
    build_parser,
    main,
    resolve_input_path,
)


def test_discover_sheets_in_dir_newest_first(tmp_path: Path) -> None:
    older_csv = tmp_path / "older.csv"
    older_csv.write_text("Title,Images\n", encoding="utf-8")

    # Ensure different mtime
    time.sleep(0.05)
    newer_xlsx = tmp_path / "newer.xlsx"
    newer_xlsx.write_text("Title,Images\n", encoding="utf-8")

    # Temporary/hidden files should be ignored
    temp_file = tmp_path / "~$temp.xlsx"
    temp_file.write_text("Title,Images\n", encoding="utf-8")
    hidden_file = tmp_path / ".hidden.csv"
    hidden_file.write_text("Title,Images\n", encoding="utf-8")

    discovered = _discover_sheets_in_dir(tmp_path)
    assert len(discovered) == 2
    assert discovered[0].name == "newer.xlsx"
    assert discovered[1].name == "older.csv"


def test_resolve_input_path_direct_file(tmp_path: Path) -> None:
    csv_file = tmp_path / "custom.csv"
    csv_file.write_text("Title,Images\n", encoding="utf-8")
    input_dir = tmp_path / "input"
    input_dir.mkdir()

    resolved = resolve_input_path(str(csv_file), input_dir)
    assert resolved == csv_file


def test_resolve_input_path_relative_in_input_dir(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    sheet = input_dir / "catalog.csv"
    sheet.write_text("Title,Images\n", encoding="utf-8")

    resolved = resolve_input_path("catalog.csv", input_dir)
    assert resolved.resolve() == sheet.resolve()


def test_resolve_input_path_auto_discovery_when_omitted(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    sheet = input_dir / "latest_products.csv"
    sheet.write_text("Title,Images\n", encoding="utf-8")

    resolved = resolve_input_path(None, input_dir)
    assert resolved.resolve() == sheet.resolve()


def test_resolve_input_path_directory_target(tmp_path: Path) -> None:
    batch_dir = tmp_path / "batch_202610"
    batch_dir.mkdir()
    sheet = batch_dir / "items.xlsx"
    sheet.write_text("data", encoding="utf-8")

    resolved = resolve_input_path(str(batch_dir), tmp_path / "input")
    assert resolved.resolve() == sheet.resolve()


def test_resolve_input_path_missing_raises_config_error(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty_input"
    empty_dir.mkdir()

    with pytest.raises(ConfigError) as exc_info:
        resolve_input_path(None, empty_dir)
    assert "no input sheet specified" in str(exc_info.value).lower()


def test_cli_parser_input_dir_configuration() -> None:
    parser = build_parser()
    args = parser.parse_args(["--input-dir", "my_custom_inputs", "--dry-run"])
    config = build_config(args)
    assert config.input_dir == Path("my_custom_inputs")


def _studio_shot_with_pasted_panel(path: Path) -> None:
    """A unit on a light plate with a flat black marketing band glued under it."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1000, 600), (246, 247, 249))
    draw = ImageDraw.Draw(image)
    draw.rectangle((62, 30, 937, 300), fill=(232, 233, 236))
    draw.rectangle((0, 330, 1000, 600), fill=(8, 9, 12))
    image.save(path)


def _full_bleed_marketing_poster(path: Path) -> None:
    """A render painted past all four edges - what S20 exists to stop."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (800, 800), (238, 244, 251))
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 100, 700, 200), fill=(30, 60, 160))
    draw.rectangle((100, 400, 600, 600), fill=(250, 250, 250))
    image.save(path)


def test_check_image_cuts_the_panel_and_publishes_the_product(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--check-image must agree with the run: the pasted panel is cut, the product alone
    is judged, and a file that survives every gate reports PUBLISH on both bars."""
    panel_file = tmp_path / "panel.png"
    _studio_shot_with_pasted_panel(panel_file)

    assert main(["--check-image", str(panel_file)]) == 0
    out = capsys.readouterr().out
    assert "cutting the bottom marketing panel" in out
    assert "raw 1000x330" in out
    assert out.count("PUBLISH") == 2
    assert "STOP" not in out


def test_check_image_names_the_gate_that_stops_a_poster(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other half of the promise: a marketing render is stopped on the brand page too,
    and the printout says which rule and why, so no one has to read decisions.jsonl."""
    poster_file = tmp_path / "poster.png"
    _full_bleed_marketing_poster(poster_file)

    assert main(["--check-image", str(poster_file)]) == 1
    out = capsys.readouterr().out
    assert "STOP S20" in out
    assert "poster/banner, not a product photo" in out
    assert "PUBLISH" not in out


def test_check_image_reports_an_unreadable_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    not_image = tmp_path / "sheet.txt"
    not_image.write_text("Title,Images\n", encoding="utf-8")

    assert main(["--check-image", str(not_image)]) == 2
    assert "cannot open" in capsys.readouterr().out


def test_check_image_needs_no_api_key_or_sheet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The mode is meant for "why did this file stop?" on a laptop with no secrets set."""
    monkeypatch.delenv("FREEIMAGE_API_KEY", raising=False)
    panel_file = tmp_path / "panel.png"
    _studio_shot_with_pasted_panel(panel_file)

    assert main(["--check-image", str(panel_file)]) == 0

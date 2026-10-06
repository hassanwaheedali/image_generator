"""Tests for enterprise input directory auto-discovery and resolution."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from config import ConfigError
from main import _discover_sheets_in_dir, build_config, build_parser, resolve_input_path


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

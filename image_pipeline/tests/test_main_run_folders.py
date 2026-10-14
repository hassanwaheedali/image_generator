"""The run folder a sheet gets: its stem plus a short hash of the file it came from.

Owner decision Q1 (2026-10-13): ``output/website_ac-3f9a2c``.  Two sheets with the same
name in different folders used to write into one folder and share one ``checkpoint.jsonl``,
which made a fresh sheet resume an old one's rows - the hash is there to stop that, so it
has to be stable for the same file and different for a different one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from config import DEFAULT_OUTPUT_ROOT, DEFAULT_STATE_ROOT, PipelineConfig
from main import build_config, build_parser, name_run_folders, run_folder

_HEX6 = re.compile(r"^[0-9a-f]{6}$")


def _sheet(tmp_path: Path, name: str = "website_ac.csv", *, in_: str = "input") -> Path:
    folder = tmp_path / in_
    folder.mkdir(parents=True, exist_ok=True)
    sheet = folder / name
    sheet.write_text("Title,Url\n", encoding="utf-8")
    return sheet


# --------------------------------------------------------------------------- #
# the name itself
# --------------------------------------------------------------------------- #
def test_the_folder_is_the_stem_plus_six_hex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    name = run_folder(_sheet(tmp_path))
    stem, _, suffix = name.partition("-")
    assert stem == "website_ac"
    assert _HEX6.match(suffix)


def test_the_same_sheet_always_gets_the_same_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic or the checkpoint is unreadable the next day."""
    monkeypatch.chdir(tmp_path)
    sheet = _sheet(tmp_path)
    assert run_folder(sheet) == run_folder(sheet.resolve())


def test_the_way_the_path_was_typed_does_not_change_the_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    sheet = _sheet(tmp_path)
    assert run_folder(Path("input") / sheet.name) == run_folder(sheet)


def test_two_same_named_sheets_get_two_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The collision this whole step exists for."""
    monkeypatch.chdir(tmp_path)
    left = run_folder(_sheet(tmp_path, in_="ac"))
    right = run_folder(_sheet(tmp_path, in_="old"))
    assert left != right
    assert left.split("-")[0] == right.split("-")[0] == "website_ac"


# --------------------------------------------------------------------------- #
# applying it to the config
# --------------------------------------------------------------------------- #
def test_a_default_run_names_both_folders_after_the_sheet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    sheet = _sheet(tmp_path)
    config = name_run_folders(PipelineConfig(dry_run=True), build_parser().parse_args([]), sheet)
    folder = run_folder(sheet)
    assert config.output_dir == DEFAULT_OUTPUT_ROOT / folder
    assert config.state_dir == DEFAULT_STATE_ROOT / folder
    # the shared files still sit directly under the run folder
    assert config.checkpoint_path.parent == config.state_dir


@pytest.mark.parametrize(
    ("argv", "pinned", "moved"),
    [
        (["--output-dir", "custom/out"], "output_dir", "state_dir"),
        (["--state-dir", "custom/state"], "state_dir", "output_dir"),
    ],
)
def test_a_pinned_folder_is_used_exactly_as_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    pinned: str,
    moved: str,
) -> None:
    """--output-dir is an instruction, not a suggestion; the other one still gets named."""
    monkeypatch.chdir(tmp_path)
    sheet = _sheet(tmp_path)
    args = build_parser().parse_args(["--dry-run", *argv])
    config = name_run_folders(build_config(args), args, sheet)
    assert getattr(config, pinned) == Path(argv[1])
    root = DEFAULT_OUTPUT_ROOT if moved == "output_dir" else DEFAULT_STATE_ROOT
    assert getattr(config, moved) == root / run_folder(sheet)


def test_an_earlier_checkpoint_folder_is_reported_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The rename is the owner's call, but silence here means 1800 re-uploads."""
    monkeypatch.chdir(tmp_path)
    sheet = _sheet(tmp_path)
    legacy = DEFAULT_STATE_ROOT / sheet.stem
    legacy.mkdir(parents=True)
    (legacy / "checkpoint.jsonl").write_text("", encoding="utf-8")
    with caplog.at_level("WARNING"):
        config = name_run_folders(
            PipelineConfig(dry_run=True), build_parser().parse_args([]), sheet
        )
    assert str(legacy) in caplog.text
    assert str(config.state_dir) in caplog.text


def test_a_clean_start_says_nothing_about_old_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.chdir(tmp_path)
    sheet = _sheet(tmp_path)
    with caplog.at_level("WARNING"):
        name_run_folders(PipelineConfig(dry_run=True), build_parser().parse_args([]), sheet)
    assert caplog.text == ""

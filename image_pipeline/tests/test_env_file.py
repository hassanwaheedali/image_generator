"""`.env` handling: parsing rules, precedence and the missing-key failure."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import config as config_module
from config import (
    ENV_FILE_NAMES,
    ConfigError,
    PipelineConfig,
    load_env_file,
    parse_env_text,
)


def test_parse_env_text_basic_rules() -> None:
    text = """
    # a comment
    FREEIMAGE_API_KEY=abc123

    export OTHER_KEY="quoted value"
    SPACED = value
    TRAILING=value # inline comment
    EMPTY=
    not_a_line
    """
    values = parse_env_text(text)
    assert values["FREEIMAGE_API_KEY"] == "abc123"
    assert values["OTHER_KEY"] == "quoted value"
    assert values["SPACED"] == "value"
    assert values["TRAILING"] == "value"
    assert "EMPTY" not in values          # a blank value means "not set"
    assert "not_a_line" not in values


def test_load_env_file_sets_missing_vars_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FREEIMAGE_API_KEY", raising=False)
    monkeypatch.setenv("OTHER_KEY", "from-shell")
    env = tmp_path / ".env"
    env.write_text("FREEIMAGE_API_KEY=from-file\nOTHER_KEY=from-file\n", encoding="utf-8")

    used = load_env_file(env)
    assert used == env
    assert os.environ["FREEIMAGE_API_KEY"] == "from-file"
    assert os.environ["OTHER_KEY"] == "from-shell"          # the real env var wins


def test_visible_secrets_file_is_found_when_dotenv_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.env` is hidden in most file browsers, so `secrets.env` works too."""
    monkeypatch.delenv("FREEIMAGE_API_KEY", raising=False)
    (tmp_path / "secrets.env").write_text("FREEIMAGE_API_KEY=from-visible-file\n", encoding="utf-8")

    used = load_env_file(search_dirs=[tmp_path])          # no .env here at all
    assert used is not None and used.name == "secrets.env"
    assert os.environ["FREEIMAGE_API_KEY"] == "from-visible-file"


def test_dotenv_wins_over_the_visible_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FREEIMAGE_API_KEY", raising=False)
    (tmp_path / "secrets.env").write_text("FREEIMAGE_API_KEY=visible\n", encoding="utf-8")
    (tmp_path / ".env").write_text("FREEIMAGE_API_KEY=hidden\n", encoding="utf-8")

    used = load_env_file(search_dirs=[tmp_path])
    assert used is not None and used.name == ".env"
    assert os.environ["FREEIMAGE_API_KEY"] == "hidden"


def test_all_searched_names_are_git_ignored() -> None:
    """Every file the loader may read must be listed in .gitignore."""
    root = Path(__file__).resolve().parent.parent
    ignore = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    listed = {line.strip() for line in ignore if line.strip() and not line.startswith("#")}
    for name in ENV_FILE_NAMES:
        assert name in listed, f"{name} is not git-ignored"


def test_empty_template_does_not_block_a_filled_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shipped-but-unfilled `.env` must not shadow `secrets.env`."""
    monkeypatch.delenv("FREEIMAGE_API_KEY", raising=False)
    (tmp_path / ".env").write_text("FREEIMAGE_API_KEY=\n", encoding="utf-8")          # empty template
    (tmp_path / "secrets.env").write_text("FREEIMAGE_API_KEY=real\n", encoding="utf-8")

    used = load_env_file(search_dirs=[tmp_path])
    assert used is not None and used.name == "secrets.env"
    assert os.environ["FREEIMAGE_API_KEY"] == "real"


def test_default_search_looks_in_the_project_and_above_it() -> None:
    """Typing the key in the folder that *contains* the project must work too."""
    dirs = config_module.default_search_dirs()
    project = Path(config_module.__file__).resolve().parent
    assert project in dirs
    assert project.parent in dirs                    # the folder above the project
    assert dirs[0] == Path.cwd()                     # the folder you ran the command from
    assert len(dirs) == len(set(dirs))               # no duplicates


def test_the_folder_above_the_project_is_really_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FREEIMAGE_API_KEY", raising=False)
    project = tmp_path / "image_pipeline"
    project.mkdir()
    (tmp_path / "secrets.env").write_text("FREEIMAGE_API_KEY=from-parent\n", encoding="utf-8")

    used = load_env_file(search_dirs=[project, tmp_path])   # project first, parent second
    assert used is not None and used.parent == tmp_path
    assert os.environ["FREEIMAGE_API_KEY"] == "from-parent"


def test_missing_env_file_is_not_an_error(tmp_path: Path) -> None:
    assert load_env_file(tmp_path / "nope.env") is None


def test_config_reads_the_key_at_construction_time(tmp_path: Path) -> None:
    from unittest import mock

    with mock.patch.dict(os.environ, {"FREEIMAGE_API_KEY": "k-123"}, clear=False):
        config = PipelineConfig(
            assets_dir=tmp_path / "assets",
            output_dir=tmp_path / "out",
            state_dir=tmp_path / "state",
        )
    assert config.api_key == "k-123"


def test_real_run_without_a_key_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as error:
        PipelineConfig(
            assets_dir=tmp_path / "assets",
            output_dir=tmp_path / "out",
            state_dir=tmp_path / "state",
            dry_run=False,
            api_key="",                       # explicitly empty
        )
    assert "FREEIMAGE_API_KEY" in str(error.value)

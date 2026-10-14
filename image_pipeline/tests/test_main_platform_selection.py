"""The startup platform prompt: who gets asked, and what an answer means.

Owner decision D6: the terminal asks which platform(s) to run - all of them, any one,
or any pair - and ``--platforms`` is the non-interactive escape.  A prompt that can
silently narrow a run is worse than no prompt, so every dud answer means *everything*.
"""

from __future__ import annotations

import pytest

from config import ConfigError
from main import platform_groups, select_platforms
from platforms import PLATFORM_KEYS


def _ask(
    answer: str | None = None, *, eof: bool = False, interactive: bool = True
) -> tuple[tuple[str, ...] | None, list[str]]:
    """Run the prompt with a canned answer; return its verdict and the lines it printed."""
    printed: list[str] = []

    def read() -> str:
        if eof:
            raise EOFError
        assert answer is not None
        return answer

    verdict = select_platforms(
        None, interactive=interactive, emit=printed.append, read=read
    )
    return verdict, printed


# --------------------------------------------------------------------------- #
# --platforms wins outright
# --------------------------------------------------------------------------- #
def test_the_flag_answers_for_the_prompt() -> None:
    def never_read() -> str:  # pragma: no cover - the prompt must not run
        raise AssertionError("--platforms must skip the prompt")

    assert (
        select_platforms("digi", interactive=True, emit=print, read=never_read) == ("digi",)
    )
    assert (
        select_platforms("alfa,digi", interactive=False, emit=print, read=never_read)
        == ("alfa", "digi")
    )


def test_an_unknown_or_empty_flag_is_an_error_not_a_default() -> None:
    """``--platforms dgii`` quietly publishing everything is the failure mode to avoid."""
    with pytest.raises(ConfigError, match="unknown platform"):
        select_platforms("dgii", interactive=False, emit=print, read=input)
    with pytest.raises(ConfigError, match="at least one"):
        select_platforms(" , ", interactive=False, emit=print, read=input)


# --------------------------------------------------------------------------- #
# who gets asked at all
# --------------------------------------------------------------------------- #
def test_no_terminal_no_question() -> None:
    """Piped or cron-run: nothing is printed and the config default stands."""
    verdict, printed = _ask(interactive=False)
    assert verdict is None
    assert printed == []


def test_no_input_at_all_still_publishes_everything() -> None:
    """An EOF (no stdin attached) must not be a reason to run nothing."""
    verdict, printed = _ask(eof=True)
    assert verdict is None
    assert printed[0] == "Run for which platform(s)?"


# --------------------------------------------------------------------------- #
# the menu
# --------------------------------------------------------------------------- #
def test_the_menu_offers_every_and_then_each_group() -> None:
    _, printed = _ask(eof=True)
    assert printed[0] == "Run for which platform(s)?"
    assert printed[1] == "  1 = all (Website, Alfamall, Digimall)   [Enter]"
    assert printed[2] == "  2 = Website only"
    assert printed[3] == "  3 = Alfamall only"
    assert printed[4] == "  4 = Digimall only"
    assert printed[5] == "  5 = Website + Alfamall"
    assert len(printed) == len(platform_groups()) + 1


def test_the_groups_cover_the_whole_table() -> None:
    """All, each single, each pair - and nothing else, in that order."""
    assert platform_groups() == (
        ("website", "alfa", "digi"),
        ("website",),
        ("alfa",),
        ("digi",),
        ("website", "alfa"),
        ("website", "digi"),
        ("alfa", "digi"),
    )


# --------------------------------------------------------------------------- #
# what an answer means
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("1", PLATFORM_KEYS),
        ("2", ("website",)),
        ("4", ("digi",)),
        ("5", ("website", "alfa")),
        ("  3  ", ("alfa",)),
    ],
)
def test_a_number_picks_its_group(answer: str, expected: tuple[str, ...]) -> None:
    verdict, _ = _ask(answer)
    assert verdict == expected


@pytest.mark.parametrize("answer", ["", "junk", "0", "99", "-1"])
def test_a_dud_answer_means_everything(answer: str) -> None:
    """Enter alone is the owner's normal path; nothing else may shrink the run."""
    verdict, _ = _ask(answer)
    assert verdict is None

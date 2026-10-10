"""Option 2 (direct image URLs) — TDD suite.

Covers, in order of the run: config surface -> the S19 branded-template guard ->
the direct per-row processor -> duplicate download protection -> colour rule reuse.
All tests are offline: fakes from ``conftest.py`` and synthetic images only.
"""

from __future__ import annotations

import pytest

from config import ConfigError, PipelineConfig


# --------------------------------------------------------------------------- #
# Task O2-1: config surface
# --------------------------------------------------------------------------- #
def test_direct_mode_defaults_off() -> None:
    """Option 2 is opt-in; the default run stays the Option 1 scraper flow."""
    assert PipelineConfig(dry_run=True).direct_mode is False


def test_direct_mode_switches_on() -> None:
    assert PipelineConfig(dry_run=True, direct_mode=True).direct_mode is True


def test_branded_template_guard_defaults() -> None:
    """S19 ships enabled with lenient thresholds (white studios never fire it)."""
    config = PipelineConfig(dry_run=True)
    assert config.branded_template_guard_enabled is True
    assert config.branded_corner_min_saturation == pytest.approx(0.30)
    assert config.branded_min_saturated_corners == 3


@pytest.mark.parametrize(
    ("kwargs", "message_part"),
    [
        ({"branded_corner_min_saturation": -0.1}, "branded_corner_min_saturation"),
        ({"branded_corner_min_saturation": 1.4}, "branded_corner_min_saturation"),
        ({"branded_min_saturated_corners": -1}, "branded_min_saturated_corners"),
        ({"branded_min_saturated_corners": 5}, "branded_min_saturated_corners"),
    ],
)
def test_branded_template_guard_validation(kwargs: dict[str, object], message_part: str) -> None:
    with pytest.raises(ConfigError, match=message_part):
        PipelineConfig(dry_run=True, **kwargs)  # type: ignore[arg-type]

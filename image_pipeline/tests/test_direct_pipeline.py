"""Option 2 (direct image URLs) — TDD suite.

Covers, in order of the run: config surface -> the S19 branded-template guard ->
the direct per-row processor -> duplicate download protection -> colour rule reuse.
All tests are offline: fakes from ``conftest.py`` and synthetic images only.
"""

from __future__ import annotations

from pathlib import Path

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


# --------------------------------------------------------------------------- #
# Task O2-2: the S19 branded-template guard (background-only evidence)
# --------------------------------------------------------------------------- #
from PIL import Image, ImageDraw  # noqa: E402

from image_quality import branded_template_reason  # noqa: E402


def _white_product_photo(corner_colour: tuple[int, int, int] = (255, 255, 255)) -> Image.Image:
    """A clean studio shot: bright background, dark product body in the centre."""
    image = Image.new("RGB", (600, 600), corner_colour)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([180, 160, 420, 480], radius=18, fill=(60, 64, 70))
    draw.rectangle([200, 200, 400, 240], fill=(90, 94, 100))  # panel detail
    return image


def _themed_template(background: tuple[int, int, int]) -> Image.Image:
    """A store theme frame: saturated branded background + logo block + product."""
    image = Image.new("RGB", (600, 600), background)
    draw = ImageDraw.Draw(image)
    draw.rectangle([20, 20, 150, 70], fill=(20, 20, 20))          # the shop's logo
    draw.rounded_rectangle([200, 180, 440, 500], radius=14, fill=(240, 240, 240))
    return image


def test_s19_accepts_a_white_background_product_photo() -> None:
    assert branded_template_reason(_white_product_photo(), PipelineConfig(dry_run=True)) is None


def test_s19_accepts_warm_white_and_light_grey_studios() -> None:
    config = PipelineConfig(dry_run=True)
    assert branded_template_reason(_white_product_photo((250, 246, 236)), config) is None
    assert branded_template_reason(_white_product_photo((232, 232, 232)), config) is None


def test_s19_rejects_the_yellow_theme_template() -> None:
    reason = branded_template_reason(_themed_template((248, 198, 0)), PipelineConfig(dry_run=True))
    assert reason is not None and "template" in reason.lower()


def test_s19_rejects_dark_brand_themes_too() -> None:
    """A navy promo frame is still a theme even though it is dark, not bright."""
    config = PipelineConfig(dry_run=True)
    assert branded_template_reason(_themed_template((15, 35, 95)), config) is not None
    assert branded_template_reason(_themed_template((190, 30, 45)), config) is not None


def test_s19_needs_three_corners_one_coloured_corner_passes() -> None:
    """A cable/shadow/price tag in one corner must never fire the guard."""
    image = _white_product_photo()
    ImageDraw.Draw(image).rectangle([0, 0, 120, 120], fill=(200, 40, 40))
    assert branded_template_reason(image, PipelineConfig(dry_run=True)) is None


def test_s19_can_be_switched_off() -> None:
    config = PipelineConfig(dry_run=True, branded_template_guard_enabled=False)
    assert branded_template_reason(_themed_template((248, 198, 0)), config) is None


def test_s19_ignores_the_product_body_itself() -> None:
    """A colourful product on a white background is not a template."""
    image = Image.new("RGB", (600, 600), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([150, 140, 450, 500], radius=20, fill=(210, 30, 30))
    draw.rectangle([180, 180, 420, 260], fill=(30, 90, 200))
    assert branded_template_reason(image, PipelineConfig(dry_run=True)) is None


# --------------------------------------------------------------------------- #
# Task O2-3: BatchRunner processor seam (Option 1 stays the default)
# --------------------------------------------------------------------------- #
import threading  # noqa: E402

from models import (  # noqa: E402
    AcceptedImage,
    ProductResult,
    ProductTask,
    Status,
)
from pipeline import BatchRunner, PipelineDeps, process_product  # noqa: E402


def test_batch_runner_defaults_to_the_option_1_processor() -> None:
    config = PipelineConfig(dry_run=True)
    runner = BatchRunner(config, None, [], {})  # type: ignore[arg-type]
    assert runner._processor is process_product


def test_batch_runner_uses_the_injected_processor(tmp_path: Path) -> None:
    """The seam: Option 2 swaps the per-row processor without touching Option 1."""
    from conftest import FakeImageFetcher, FakeImageHost, FakePageFetcher

    from decisions import DecisionLog

    config = PipelineConfig(
        dry_run=True,
        assets_dir=Path("assets"),
        output_dir=tmp_path / "out",
        state_dir=tmp_path / "state",
    )
    deps = PipelineDeps(
        page_fetcher=FakePageFetcher({}),
        image_fetcher=FakeImageFetcher({}),
        image_host=FakeImageHost(),
        decisions=DecisionLog(tmp_path / "decisions.jsonl"),
        banner_cache={},
        banner_cache_lock=threading.Lock(),
        source_searcher=None,
    )
    calls: list[int] = []

    def fake_processor(
        task: ProductTask,
        deps_: PipelineDeps,
        config_: PipelineConfig,
        cancel_event: threading.Event | None = None,
    ) -> tuple[ProductResult, list[AcceptedImage]]:
        calls.append(task.index)
        return (
            ProductResult(task_index=task.index, title=task.title, status=Status.OK),
            [],
        )

    tasks = [ProductTask(index=0, title="Direct AC 1", source_urls=("http://x/a.jpg",))]
    runner = BatchRunner(config, deps, tasks, {}, processor=fake_processor)
    results = runner.run()
    assert calls == [0]
    assert results[0].status is Status.OK

"""Tests for early stop, limited gallery iteration, and dynamic config file."""

from pathlib import Path

from config import PipelineConfig
from main import build_config, build_parser
from model_matching import _name_words, register_generic_name_words
from models import AcceptedImage, ImageCandidate
from pipeline import _page_publish_goal, _row_is_done


def test_early_stop_when_image_found_flag() -> None:
    """_row_is_done returns True as soon as 1 image is accepted if early_stop is True."""
    cfg_normal = PipelineConfig(dry_run=True, early_stop_when_image_found=False, min_required_images=2)
    cfg_early = PipelineConfig(dry_run=True, early_stop_when_image_found=True, min_required_images=2)

    fake_img = AcceptedImage(
        source_url="https://example.com/hero.jpg",
        dedup_key="hero",
        raw_path=Path("/tmp/raw.jpg"),
        square_path=Path("/tmp/square.jpg"),
        final_paths=(("digi", Path("/tmp/digi.jpg")), ("alfa", Path("/tmp/alfa.jpg"))),
    )

    assert not _row_is_done([], cfg_early)
    assert _row_is_done([fake_img], cfg_early)
    assert not _row_is_done([fake_img], cfg_normal)


def test_limit_gallery_iteration_caps_publish_goal() -> None:
    """When limit_gallery_iteration is True, curated gallery does not expand publish goal to max."""
    candidates = [
        ImageCandidate(
            url=f"https://example.com/img{i}.jpg",
            page_url="https://example.com/p",
            source="gallery_container",
            order=i,
            matches_model=True,
        )
        for i in range(10)
    ]


    cfg_unlimited = PipelineConfig(
        dry_run=True,
        target_images_per_product=1,
        max_images_per_product=10,
        gallery_publishes_all=True,
        limit_gallery_iteration=False,
    )
    # Default without limit_gallery_iteration expands to max (10)
    assert _page_publish_goal(candidates, cfg_unlimited) == 10

    cfg_limited = PipelineConfig(
        dry_run=True,
        target_images_per_product=1,
        max_images_per_product=10,
        gallery_publishes_all=True,
        limit_gallery_iteration=True,
    )
    # With limit_gallery_iteration, stops at target (1)
    assert _page_publish_goal(candidates, cfg_limited) == 1


def test_build_config_inherits_the_code_defaults() -> None:
    """No JSON overlay any more (owner 2026-10-16): a bare run takes its numbers straight
    from ``PipelineConfig``, and only a command-line flag can move them."""
    args = build_parser().parse_args(["--dry-run"])
    pipeline_cfg = build_config(args)

    assert pipeline_cfg.min_required_images == PipelineConfig.min_required_images
    assert pipeline_cfg.target_images_per_product == PipelineConfig.target_images_per_product
    assert pipeline_cfg.max_images_per_product == PipelineConfig.max_images_per_product


def test_extra_generic_name_words_registration() -> None:
    """Extra generic words are registered and ignored during name_words extraction."""
    register_generic_name_words(["supercustomcoolerword"])
    title = "Super Brand 5000 Supercustomcoolerword Air Cooler"
    words = _name_words(title)
    assert "supercustomcoolerword" not in words

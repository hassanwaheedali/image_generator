"""Tests for early stop, limited gallery iteration, and dynamic config file."""

from pathlib import Path

from config import PipelineConfig, load_config_file
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


def test_load_config_file_and_build_config(tmp_path: Path) -> None:
    """Test loading config from JSON file and merging into build_config."""
    cfg_file = tmp_path / "custom_config.json"
    cfg_file.write_text(
        '{"early_stop_when_image_found": true, "limit_gallery_iteration": true, '
        '"min_required_images": 1, "target_images_per_product": 1, "extra_generic_name_words": ["customterm"]}',
        encoding="utf-8",
    )

    data = load_config_file(cfg_file)
    assert data["early_stop_when_image_found"] is True
    assert data["target_images_per_product"] == 1

    args = build_parser().parse_args(["--config", str(cfg_file), "--dry-run"])
    pipeline_cfg = build_config(args)

    assert pipeline_cfg.early_stop_when_image_found is True
    assert pipeline_cfg.limit_gallery_iteration is True
    assert pipeline_cfg.min_required_images == 1
    assert pipeline_cfg.target_images_per_product == 1
    assert "customterm" in pipeline_cfg.extra_generic_name_words


def test_extra_generic_name_words_registration() -> None:
    """Extra generic words are registered and ignored during name_words extraction."""
    register_generic_name_words(["supercustomcoolerword"])
    title = "Super Brand 5000 Supercustomcoolerword Air Cooler"
    words = _name_words(title)
    assert "supercustomcoolerword" not in words

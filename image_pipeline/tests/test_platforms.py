"""The platform table: which platforms run, and what each does with its banner."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from config import ConfigError, PipelineConfig
from platforms import (
    ASSET_EXTENSIONS,
    BANNER_STEM,
    DEFAULT_SPECS,
    PLATFORM_KEYS,
    TEMPLATE_STEM,
    BannerMode,
    BannerPolicy,
    build_platform_specs,
    parse_platform_keys,
    resolve_asset,
    resolve_platforms,
)


def _config(tmp_path: Path, **overrides: object) -> PipelineConfig:
    return PipelineConfig(
        assets_dir=tmp_path / "assets",
        output_dir=tmp_path / "out",
        state_dir=tmp_path / "state",
        dry_run=True,
        **overrides,  # type: ignore[arg-type]
    )


def _write_platform_assets(
    assets: Path, keys: tuple[str, ...], *, banners: tuple[str, ...] | None = None
) -> None:
    """A minimal per-platform asset tree: a 4x4 PNG template, and banners where asked."""
    banner_keys = keys if banners is None else banners
    for key in keys:
        folder = assets / key
        folder.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 4), (255, 255, 255)).save(folder / f"{TEMPLATE_STEM}.png")
        if key in banner_keys:
            Image.new("RGB", (4, 4), (10, 10, 10)).save(folder / f"{BANNER_STEM}.png")


# --------------------------------------------------------------------------- #
# the table itself
# --------------------------------------------------------------------------- #
def test_table_lists_the_three_platforms_in_publish_order() -> None:
    assert PLATFORM_KEYS == ("website", "alfa", "digi")
    assert [spec.display_name for spec in DEFAULT_SPECS] == ["Website", "Alfamall", "Digimall"]


def test_csv_filenames_are_carried_not_derived_from_the_key() -> None:
    """``alfa/`` holds ``alfamall_product_images.csv`` - the folder name cannot produce it."""
    by_key = {spec.key: spec.csv_filename for spec in DEFAULT_SPECS}
    assert by_key == {
        "website": "website_product_images.csv",
        "alfa": "alfamall_product_images.csv",
        "digi": "digimall_product_images.csv",
    }


def test_website_drops_the_banner_and_the_others_keep_it(tmp_path: Path) -> None:
    policies = {spec.key: spec.banner_policy for spec in build_platform_specs(_config(tmp_path))}
    assert policies == {
        "website": BannerPolicy.DROP,
        "alfa": BannerPolicy.KEEP,
        "digi": BannerPolicy.KEEP,
    }


def test_website_keep_banner_is_the_one_switchable_policy(tmp_path: Path) -> None:
    specs = build_platform_specs(_config(tmp_path, website_keep_banner=True))
    assert {spec.key: spec.banner_policy for spec in specs}["website"] is BannerPolicy.KEEP
    # ... and it moves nothing else.
    assert [spec.key for spec in specs] == list(PLATFORM_KEYS)


def test_image_counts_are_inherited_globally_for_now(tmp_path: Path) -> None:
    """Owner decision 2026-10-13: no per-platform quotas yet, so every spec says None."""
    assert all(spec.max_final_images is None for spec in build_platform_specs(_config(tmp_path)))


def test_config_default_matches_the_table() -> None:
    """Guards the one duplication: the config default string vs PLATFORM_KEYS."""
    assert parse_platform_keys(PipelineConfig(dry_run=True).enabled_platforms) == PLATFORM_KEYS


# --------------------------------------------------------------------------- #
# parse_platform_keys
# --------------------------------------------------------------------------- #
def test_parse_keeps_order_and_drops_duplicates_and_blanks() -> None:
    assert parse_platform_keys(" digi ,, alfa ,digi ") == ("digi", "alfa")


def test_parse_is_case_insensitive() -> None:
    assert parse_platform_keys("DIGI,Alfa") == ("digi", "alfa")


def test_parse_accepts_the_display_names_as_aliases() -> None:
    assert parse_platform_keys("digimall,alfamall") == ("digi", "alfa")


def test_parse_rejects_an_unknown_key_instead_of_running_everything() -> None:
    """A typo must not silently publish all three platforms."""
    with pytest.raises(ConfigError, match="unknown platform 'dgii'"):
        parse_platform_keys("dgii")


def test_parse_of_an_empty_string_is_empty() -> None:
    assert parse_platform_keys("") == ()


# --------------------------------------------------------------------------- #
# resolve_asset
# --------------------------------------------------------------------------- #
def test_resolve_asset_finds_the_png(tmp_path: Path) -> None:
    folder = tmp_path / "digi"
    folder.mkdir()
    png = folder / f"{TEMPLATE_STEM}.png"
    png.write_bytes(b"not really a png")
    assert resolve_asset(folder, TEMPLATE_STEM) == png


def test_resolve_asset_falls_back_to_jpg(tmp_path: Path) -> None:
    folder = tmp_path / "digi"
    folder.mkdir()
    jpg = folder / f"{TEMPLATE_STEM}.jpg"
    jpg.write_bytes(b"jpeg export")
    assert resolve_asset(folder, TEMPLATE_STEM) == jpg


def test_resolve_asset_prefers_png_when_both_exist(tmp_path: Path) -> None:
    """Two candidates in a fixed order, so 'which file did it pick?' has one answer."""
    folder = tmp_path / "digi"
    folder.mkdir()
    (folder / f"{TEMPLATE_STEM}.png").write_bytes(b"png")
    (folder / f"{TEMPLATE_STEM}.jpg").write_bytes(b"jpg")
    assert resolve_asset(folder, TEMPLATE_STEM) == folder / f"{TEMPLATE_STEM}.png"


def test_resolve_asset_returns_none_when_absent(tmp_path: Path) -> None:
    folder = tmp_path / "website"
    folder.mkdir()
    assert resolve_asset(folder, TEMPLATE_STEM) is None
    assert resolve_asset(tmp_path / "does-not-exist", TEMPLATE_STEM) is None


def test_resolve_asset_ignores_a_directory_sharing_the_name(tmp_path: Path) -> None:
    (tmp_path / f"{TEMPLATE_STEM}.png").mkdir()
    assert resolve_asset(tmp_path, TEMPLATE_STEM) is None


def test_only_two_extensions_are_considered() -> None:
    """No globbing: a stray ``template (final) v2.webp`` must never win silently."""
    assert ASSET_EXTENSIONS == (".png", ".jpg")


# --------------------------------------------------------------------------- #
# resolve_platforms
# --------------------------------------------------------------------------- #
def test_all_assets_present_resolves_all_three(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_platform_assets(config.assets_dir, PLATFORM_KEYS)
    plan = resolve_platforms(config)
    assert [p.key for p in plan.platforms] == list(PLATFORM_KEYS)
    assert [p.banner_mode for p in plan.platforms] == [
        BannerMode.SKIP_BY_POLICY,   # website drops it by rule
        BannerMode.APPEND,
        BannerMode.APPEND,
    ]
    assert all(p.appends_banner is (p.key != "website") for p in plan.platforms)


def test_missing_template_drops_only_that_platform(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_platform_assets(config.assets_dir, ("alfa", "digi"))     # no website folder
    plan = resolve_platforms(config)
    assert [p.key for p in plan.platforms] == ["alfa", "digi"]
    assert plan.by_key("website") is None


def test_the_skipped_platform_notice_names_the_absolute_path_it_searched(tmp_path: Path) -> None:
    """Two assets folders have existed in this repo; a relative path hides which one lost."""
    config = _config(tmp_path)
    _write_platform_assets(config.assets_dir, ("alfa", "digi"))
    notice = next(n for n in resolve_platforms(config).notices if "Website" in n)
    assert "SKIPPED" in notice
    assert str((config.assets_dir / "website" / f"{TEMPLATE_STEM}.png").resolve()) in notice


def test_missing_banner_keeps_the_platform_running_and_says_so_once(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_platform_assets(config.assets_dir, PLATFORM_KEYS, banners=("alfa",))
    plan = resolve_platforms(config)
    digi = plan.by_key("digi")
    assert digi is not None
    assert digi.banner_mode is BannerMode.SKIP_MISSING_ASSET
    assert digi.banner_path is None
    assert not digi.appends_banner
    assert sum("Digimall" in n and "closing banner SKIPPED" in n for n in plan.notices) == 1


def test_a_banner_path_is_held_exactly_when_it_will_be_used(tmp_path: Path) -> None:
    """One invariant for callers: ``banner_path`` is set iff the banner is appended.

    Website's banner file may well exist - the platform drops it by rule, so it must not
    be resolved, stored or reachable by accident.
    """
    config = _config(tmp_path)
    _write_platform_assets(config.assets_dir, PLATFORM_KEYS)        # every banner present
    (config.assets_dir / "alfa" / f"{BANNER_STEM}.png").unlink()    # ... except alfa's

    plan = resolve_platforms(config)
    for platform in plan.platforms:
        assert (platform.banner_path is not None) is platform.appends_banner

    website, alfa, digi = (plan.by_key(key) for key in ("website", "alfa", "digi"))
    assert website is not None and alfa is not None and digi is not None
    assert website.banner_mode is BannerMode.SKIP_BY_POLICY
    assert alfa.banner_mode is BannerMode.SKIP_MISSING_ASSET
    assert digi.banner_mode is BannerMode.APPEND


def test_a_platform_switched_on_by_config_appends_its_banner(tmp_path: Path) -> None:
    config = _config(tmp_path, website_keep_banner=True)
    _write_platform_assets(config.assets_dir, PLATFORM_KEYS)
    website = resolve_platforms(config).by_key("website")
    assert website is not None
    assert website.banner_mode is BannerMode.APPEND


def test_unselected_platforms_still_get_a_line(tmp_path: Path) -> None:
    """A platform that silently produced nothing is how a whole sheet goes missing."""
    config = _config(tmp_path)
    _write_platform_assets(config.assets_dir, PLATFORM_KEYS)
    plan = resolve_platforms(config, requested=("digi",))
    assert [p.key for p in plan.platforms] == ["digi"]
    assert len(plan.notices) == 3
    assert any("Website" in n and "not selected" in n for n in plan.notices)
    assert any("Alfamall" in n and "not selected" in n for n in plan.notices)


def test_nothing_available_resolves_to_an_empty_plan(tmp_path: Path) -> None:
    """The caller turns this into a SystemExit; the resolver only reports it."""
    plan = resolve_platforms(_config(tmp_path))
    assert plan.platforms == ()
    assert len(plan.notices) == 3


def test_notices_are_one_line_per_platform_never_per_product(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_platform_assets(config.assets_dir, PLATFORM_KEYS)
    assert len(resolve_platforms(config).notices) == len(DEFAULT_SPECS)


def test_by_key_returns_none_for_an_unknown_key(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_platform_assets(config.assets_dir, PLATFORM_KEYS)
    assert resolve_platforms(config).by_key("ebay") is None


# --------------------------------------------------------------------------- #
# config validation of the new fields
# --------------------------------------------------------------------------- #
def test_empty_enabled_platforms_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="at least one"):
        _config(tmp_path, enabled_platforms="")


def test_unknown_enabled_platform_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown platform"):
        _config(tmp_path, enabled_platforms="digi,ebay")


def test_a_path_budget_below_the_floor_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="max_output_path_chars"):
        _config(tmp_path, max_output_path_chars=119)


def test_the_path_budget_default_leaves_room_under_max_path(tmp_path: Path) -> None:
    assert _config(tmp_path).max_output_path_chars == 220 < 260

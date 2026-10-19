"""Option 3 (``--images-dir``): products are local folders, not sheets or URLs."""

from __future__ import annotations

from pathlib import Path

import pytest

from config import ConfigError, PipelineConfig


def _config(tmp_path: Path, **overrides: object) -> PipelineConfig:
    return PipelineConfig(
        dry_run=True,
        assets_dir=tmp_path / "assets",
        output_dir=tmp_path / "out",
        state_dir=tmp_path / "state",
        **overrides,  # type: ignore[arg-type]
    )


class TestLocalImagesModeConfig:
    def test_off_by_default(self, tmp_path: Path) -> None:
        assert _config(tmp_path).local_images_mode is False

    def test_requires_direct_mode(self, tmp_path: Path) -> None:
        # The flag says where Option 2's bytes come from, so it is meaningless
        # without direct_mode; programmatic construction must fail loudly.
        with pytest.raises(ConfigError, match="local_images_mode"):
            _config(tmp_path, local_images_mode=True)

    def test_constructs_with_direct_mode(self, tmp_path: Path) -> None:
        config = _config(tmp_path, direct_mode=True, local_images_mode=True)
        assert config.local_images_mode and config.direct_mode

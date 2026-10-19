"""Option 3 (``--images-dir``): products are local folders, not sheets or URLs."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import clean_product_image, jpeg_bytes, png_bytes

from config import ConfigError, PipelineConfig
from fetch import FetchError, LocalImageFetcher
from protocols import ImageFetcher


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


class TestLocalImageFetcher:
    """The Option 3 seam: every "download" is a local file read."""

    def test_satisfies_the_protocol(self) -> None:
        assert isinstance(LocalImageFetcher(max_bytes=1024), ImageFetcher)

    def test_reads_bytes_and_guesses_the_content_type(self, tmp_path: Path) -> None:
        body = jpeg_bytes(clean_product_image())
        path = tmp_path / "photo.jpg"
        path.write_bytes(body)
        response = LocalImageFetcher(max_bytes=1_048_576).fetch_image(path.as_posix(), referer="")
        assert response.content == body
        assert response.content_type == "image/jpeg"
        assert response.status_code == 200
        assert response.url == path.as_posix()

    def test_unknown_extension_falls_back_to_empty_content_type(self, tmp_path: Path) -> None:
        # validate_image_bytes then identifies the format from the magic bytes.
        path = tmp_path / "photo.zzz"
        path.write_bytes(png_bytes(clean_product_image()))
        response = LocalImageFetcher(max_bytes=1_048_576).fetch_image(str(path), referer="")
        assert response.content_type == ""

    def test_missing_file_raises_fetch_error(self, tmp_path: Path) -> None:
        with pytest.raises(FetchError):
            LocalImageFetcher(max_bytes=1024).fetch_image(str(tmp_path / "gone.jpg"), referer="")

    def test_oversize_file_raises_before_reading(self, tmp_path: Path) -> None:
        path = tmp_path / "huge.jpg"
        path.write_bytes(b"\xff" * 64)
        with pytest.raises(FetchError):
            LocalImageFetcher(max_bytes=32).fetch_image(str(path), referer="")

    def test_probe_is_not_supported(self, tmp_path: Path) -> None:
        # The protocol allows None: the pipeline simply reads the whole file.
        assert LocalImageFetcher(max_bytes=1024).probe_image("whatever", "", 10) is None

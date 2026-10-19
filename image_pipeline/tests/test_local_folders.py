"""Option 3 (``--images-dir``): products are local folders, not sheets or URLs."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import clean_product_image, jpeg_bytes, png_bytes

from config import ConfigError, PipelineConfig
from csv_io import MAX_PREFIX_CHARS, filename_prefix, local_filename_prefix
from fetch import FetchError, LocalImageFetcher
from model_matching import build_model_keys
from models import ProductTask
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


class TestLocalFilenamePrefix:
    """``[code]_[REST]_[N].jpg``: colourways of one code must not share a file name."""

    def test_colourways_get_distinct_stems(self) -> None:
        title = "MSAG2B-12HRDN1-QC0 White"
        keys = build_model_keys(title)
        assert local_filename_prefix(keys, title) == "MSAG2B-12HRDN1-QC0_WHITE"
        black = local_filename_prefix(build_model_keys("MSAG2B-12HRDN1-QC0 Black"), "MSAG2B-12HRDN1-QC0 Black")
        assert black == "MSAG2B-12HRDN1-QC0_BLACK"

    def test_multi_word_rest_joins_with_underscores(self) -> None:
        title = "MSAG2B-12HRDN1-QC0 Ultra Black"
        assert local_filename_prefix(build_model_keys(title), title) == "MSAG2B-12HRDN1-QC0_ULTRA_BLACK"

    def test_code_only_title_is_unchanged(self) -> None:
        title = "MSAG2B-12HRDN1-QC0"
        keys = build_model_keys(title)
        assert local_filename_prefix(keys, title) == filename_prefix(keys, title)

    def test_punctuation_in_the_title_escapes_to_a_digest_stem(self) -> None:
        # "(OW)" carries characters a stem cannot keep, so an exact, unambiguous
        # name is safer than a fold that two titles could share.
        title = "MSAG2B-12HRDN1-QC0 White (OW)"
        stem = local_filename_prefix(build_model_keys(title), title)
        assert stem.startswith("MSAG2B-12HRDN1-QC0")
        assert stem != "MSAG2B-12HRDN1-QC0_WHITE"  # must not masquerade as the plain White folder

    def test_over_budget_title_escapes_to_a_digest_stem(self) -> None:
        # A truncated stem would merge two long titles; the digest keeps them apart.
        title = "MSAG2B-12HRDN1-QC0 " + " ".join(f"word{n}" for n in range(20))
        stem = local_filename_prefix(build_model_keys(title), title)
        assert len(stem) <= MAX_PREFIX_CHARS
        assert stem.startswith("MSAG2B-12HRDN1-QC0")

    def test_titles_differing_only_by_punctuation_stay_distinct(self) -> None:
        # "Black/White", "Black-White" and "Black White" must not merge onto one
        # stem: three folders, three products, three names - WordPress tells them
        # apart by filename and nothing else.
        titles = [
            "MSAG2B-12HRDN1-QC0 Black/White",
            "MSAG2B-12HRDN1-QC0 Black-White",
            "MSAG2B-12HRDN1-QC0 Black White",
        ]
        stems = {local_filename_prefix(build_model_keys(t), t) for t in titles}
        assert len(stems) == 3

    def test_truncated_titles_stay_distinct(self) -> None:
        # Two long titles agreeing on their first words must not collapse onto one
        # stem when the budget truncates - that is the collision the rule exists for.
        common = "MSAG2B-12HRDN1-QC0 " + " ".join(f"word{n}" for n in range(20))
        keys = build_model_keys(common)
        first = local_filename_prefix(keys, common)
        second = local_filename_prefix(keys, common + " extra")
        assert first != second

    def test_no_code_title_keeps_the_hash_fallback(self) -> None:
        title = "Split Inverter AC 1 Ton"
        keys = build_model_keys(title)
        assert local_filename_prefix(keys, title) == filename_prefix(keys, title)

    def test_product_task_defaults_to_no_override(self) -> None:
        task = ProductTask(index=0, title="anything", source_urls=("a",))
        assert task.name_prefix_override is None

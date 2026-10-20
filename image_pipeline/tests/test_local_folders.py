"""Option 3 (``--images-dir``): products are local folders, not sheets or URLs."""

from __future__ import annotations

import argparse
import io
import re
import threading
from pathlib import Path

import pytest
from conftest import clean_product_image, jpeg_bytes, png_bytes
from PIL import Image, ImageFilter

from config import ConfigError, PipelineConfig
from csv_io import (
    _TITLE_WORD_RE,
    MAX_PREFIX_CHARS,
    assign_filename_prefixes,
    filename_prefix,
    hash_source_urls,
    worded_filename_prefix,
)
from fetch import FetchError, LocalImageFetcher
from main import _build_tasks, _product_path, build_config, build_parser, discover_product_folders
from model_matching import build_model_keys
from models import ProductTask
from platforms import resolve_platforms
from protocols import ImageFetcher
from url_filters import IMAGE_EXTENSIONS

#: The 6-hex-character hash tail `csv_io` adds when words alone cannot tell two rows apart.
_DIGEST_RE = re.compile(r"[0-9a-f]{6}")


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


class TestWordedFilenamePrefix:
    """``[code]_[SERIES]_[COLOUR]_[N].jpg``: a name says which product it belongs to."""

    def test_colourways_get_distinct_stems(self) -> None:
        title = "MSAG2B-12HRDN1-QC0 White"
        keys = build_model_keys(title)
        assert worded_filename_prefix(keys, title) == "MSAG2B-12HRDN1-QC0_WHITE"
        black = worded_filename_prefix(build_model_keys("MSAG2B-12HRDN1-QC0 Black"), "MSAG2B-12HRDN1-QC0 Black")
        assert black == "MSAG2B-12HRDN1-QC0_BLACK"

    def test_multi_word_rest_joins_with_underscores(self) -> None:
        title = "MSAG2B-12HRDN1-QC0 Ultra Black"
        assert worded_filename_prefix(build_model_keys(title), title) == "MSAG2B-12HRDN1-QC0_ULTRA_BLACK"

    def test_code_only_title_is_unchanged(self) -> None:
        title = "MSAG2B-12HRDN1-QC0"
        keys = build_model_keys(title)
        assert worded_filename_prefix(keys, title) == filename_prefix(keys, title)

    def test_punctuation_that_is_not_an_identity_word_is_not_named(self) -> None:
        # "(OW)" is warranty noise, not a series and not a colour, so it stays out of the
        # name.  Two rows differing only by it are still named apart - by the run-wide pass.
        title = "MSAG2B-12HRDN1-QC0 White (OW)"
        stem = worded_filename_prefix(build_model_keys(title), title)
        assert stem == "MSAG2B-12HRDN1-QC0_WHITE"
        pair = _stems(_tasks(title, "MSAG2B-12HRDN1-QC0 White"))
        assert len(pair) == 2 and pair[0].casefold() != pair[1].casefold()

    def test_over_budget_title_escapes_to_a_digest_stem(self) -> None:
        # A truncated stem would merge two long titles; the digest keeps them apart.
        title = "MSAG2B-12HRDN1-QC0 " + " ".join(f"word{n}" for n in range(20))
        stem = worded_filename_prefix(build_model_keys(title), title)
        assert len(stem) <= MAX_PREFIX_CHARS
        assert stem.startswith("MSAG2B-12HRDN1-QC0")

    def test_over_budget_stem_cuts_words_whole_and_keeps_the_colour(self) -> None:
        # Owner 2026-10-20 on the refrigerator sheet: the long Samsung rows printed
        # "RT53DG7B10B1FA_RT7300D_SAMSUNG_COOLING_FR_1bc112" - half a word cut by the
        # 48-character budget, and the colour cut off with it.
        title = (
            "Samsung RT53DG7B10B1FA RT7300D Twin Cooling 19 Cu Ft Steel Door "
            "No-Frost Inverter Refrigerator Black"
        )
        keys = build_model_keys(title)
        stem = worded_filename_prefix(keys, title)
        assert len(stem) <= MAX_PREFIX_CHARS
        assert "__" not in stem
        assert "BLACK" in stem  # the colour is identity: it survives the cut
        whole = {word.upper() for word in _TITLE_WORD_RE.split(title) if word} | set(keys.tokens)
        pieces = [p for p in stem.split("_") if not _DIGEST_RE.fullmatch(p)]
        assert all(piece.upper() in whole for piece in pieces), stem

    def test_the_run_wide_hash_also_cuts_words_whole(self) -> None:
        # Two rows whose stems already fit the budget still collide, and the hash tail
        # is added by cutting - which must not slice a word any more than the budget may.
        title = "MSAG2B-12HRDN1-QC0 Dawlance IceMax Platinum White"
        keys = build_model_keys(title)
        pair = _stems(_tasks(title, title.replace("Platinum White", "Platinum-White")))
        assert len(pair) == 2 and pair[0] != pair[1]
        assert all("__" not in stem for stem in pair), pair
        assert all(len(stem) <= MAX_PREFIX_CHARS for stem in pair), pair
        whole = {word.upper() for word in _TITLE_WORD_RE.split(title) if word} | set(keys.tokens)
        for stem in pair:
            pieces = [p for p in stem.split("_") if not _DIGEST_RE.fullmatch(p)]
            assert all(p in whole for p in pieces), stem

    def test_titles_differing_only_by_punctuation_are_named_apart(self) -> None:
        # "Black/White", "Black-White" and "Black White" fold to the same words; the
        # run-wide pass is what tells the three folders apart.
        tasks = assign_filename_prefixes(
            _tasks(
                "MSAG2B-12HRDN1-QC0 Black/White",
                "MSAG2B-12HRDN1-QC0 Black-White",
                "MSAG2B-12HRDN1-QC0 Black White",
            )
        )
        assert len({t.name_prefix_override for t in tasks}) == 3

    def test_truncated_titles_stay_distinct(self) -> None:
        # Two long titles agreeing on their first words collapse onto one 48-character
        # stem; the run-wide pass gives each the hash of its own exact title.
        common = "MSAG2B-12HRDN1-QC0 " + " ".join(f"word{n}" for n in range(20))
        longer = common + " word20"
        assert worded_filename_prefix(build_model_keys(common), common) == (
            worded_filename_prefix(build_model_keys(longer), longer)
        )
        first, second = _stems(_tasks(common, longer))
        assert first != second
        assert all(len(stem) <= MAX_PREFIX_CHARS for stem in (first, second))

    def test_no_code_title_keeps_the_hash_fallback(self) -> None:
        title = "Split Inverter AC 1 Ton"
        keys = build_model_keys(title)
        assert worded_filename_prefix(keys, title) == filename_prefix(keys, title)

    def test_a_no_code_stem_never_repeats_its_own_words(self) -> None:
        # Owner 2026-10-20 on the refrigerator sheet: a code-less row came out as
        # "orient-grand-205-c3351f_ORIENT_GRAND_GOLDEN" - the keywords+hash fallback
        # already carries those words, and appending them again is garbage.
        title = "Orient Grand 205 Freezer-on-Top Refrigerator 7 Cu Ft Golden"
        stem = worded_filename_prefix(build_model_keys(title), title)
        assert "__" not in stem
        assert not any(w in stem for w in ("_ORIENT", "_GRAND", "_GOLDEN"))
        assert stem == stem.lower()  # one stem shape, not a lowercase hash plus caps words

    def test_product_task_defaults_to_no_override(self) -> None:
        task = ProductTask(index=0, title="anything", source_urls=("a",))
        assert task.name_prefix_override is None


def _parent(tmp_path: Path) -> Path:
    root = tmp_path / "products"
    root.mkdir(exist_ok=True)
    return root


def _folder(root: Path, name: str, images: int = 3) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    for n in range(images):
        (folder / f"{n + 1}.jpg").write_bytes(jpeg_bytes(clean_product_image()))
    return folder


class TestDiscoverProductFolders:
    def test_flat_folder_title_is_exact_and_paths_sort_by_name(self, tmp_path: Path) -> None:
        root = _parent(tmp_path)
        folder = _folder(root, "MSAG2B-12HRDN1-QC0 White")
        (folder / "10.jpg").write_bytes(jpeg_bytes(clean_product_image()))
        tasks = discover_product_folders(root)
        assert len(tasks) == 1
        task = tasks[0]
        assert task.title == "MSAG2B-12HRDN1-QC0 White"
        # Plain name sort, like the sheet's URL order: 1, 10, 2, 3.
        assert task.source_urls == (
            _product_path(folder / "1.jpg"),
            _product_path(folder / "10.jpg"),
            _product_path(folder / "2.jpg"),
            _product_path(folder / "3.jpg"),
        )

    def test_nested_folder_joins_the_title_with_one_space(self, tmp_path: Path) -> None:
        root = _parent(tmp_path)
        outer = root / "MSAG2B-12HRDN1-QC0"
        (outer / "Black").mkdir(parents=True)
        (outer / "White").mkdir(parents=True)
        (outer / "Black" / "1.jpg").write_bytes(jpeg_bytes(clean_product_image()))
        (outer / "White" / "1.png").write_bytes(png_bytes(clean_product_image()))
        tasks = discover_product_folders(root)
        assert [t.title for t in tasks] == ["MSAG2B-12HRDN1-QC0 Black", "MSAG2B-12HRDN1-QC0 White"]

    def test_flat_and_nested_mix_in_one_parent(self, tmp_path: Path) -> None:
        root = _parent(tmp_path)
        _folder(root, "Plain Product")
        nested = root / "Model X"
        (nested / "Golden").mkdir(parents=True)
        (nested / "Golden" / "a.jpg").write_bytes(jpeg_bytes(clean_product_image()))
        titles = [t.title for t in discover_product_folders(root)]
        assert titles == ["Model X Golden", "Plain Product"]

    def test_junk_and_hidden_entries_are_invisible(self, tmp_path: Path) -> None:
        root = _parent(tmp_path)
        folder = _folder(root, "Product")
        (folder / "notes.txt").write_text("hi", encoding="utf-8")
        (folder / ".DS_Store").write_bytes(b"junk")
        hidden = root / ".hidden"
        hidden.mkdir()
        (hidden / "1.jpg").write_bytes(jpeg_bytes(clean_product_image()))
        tasks = discover_product_folders(root)
        assert len(tasks) == 1
        assert len(tasks[0].source_urls) == 3  # notes.txt and .DS_Store are not pictures

    def test_empty_product_folder_survives_as_a_short_row(self, tmp_path: Path) -> None:
        root = _parent(tmp_path)
        (root / "Empty One").mkdir()
        tasks = discover_product_folders(root)
        assert [t.source_urls for t in tasks] == [()]

    def test_loose_images_in_the_parent_are_an_error(self, tmp_path: Path) -> None:
        root = _parent(tmp_path)
        (root / "stray.jpg").write_bytes(jpeg_bytes(clean_product_image()))
        _folder(root, "Product")
        with pytest.raises(ValueError, match="loose"):
            discover_product_folders(root)

    def test_folder_with_images_and_subfolders_is_ambiguous(self, tmp_path: Path) -> None:
        root = _parent(tmp_path)
        folder = _folder(root, "Model Y")
        (folder / "Black").mkdir()
        with pytest.raises(ValueError, match="Model Y"):
            discover_product_folders(root)

    def test_folders_inside_a_colourway_are_not_silently_dropped(self, tmp_path: Path) -> None:
        # Model Z/Black/{1.jpg, Zoom/2.jpg}: the same shape, one level deeper, must
        # raise like it does at the flat level - not lose Zoom's picture quietly.
        root = _parent(tmp_path)
        black = root / "Model Z" / "Black"
        black.mkdir(parents=True)
        (black / "1.jpg").write_bytes(jpeg_bytes(clean_product_image()))
        (black / "Zoom").mkdir()
        (black / "Zoom" / "2.jpg").write_bytes(jpeg_bytes(clean_product_image()))
        with pytest.raises(ValueError, match="Black"):
            discover_product_folders(root)

    def test_parent_without_products_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="subfolder"):
            discover_product_folders(_parent(tmp_path))

    def test_extensions_come_from_the_shared_list(self, tmp_path: Path) -> None:
        root = _parent(tmp_path)
        folder = _folder(root, "Product", images=0)
        for suffix in sorted(IMAGE_EXTENSIONS):
            (folder / f"pic.{suffix}").write_bytes(b"x")
        (folder / "movie.mp4").write_bytes(b"x")
        tasks = discover_product_folders(root)
        assert len(tasks[0].source_urls) == len(IMAGE_EXTENSIONS)


class TestImagesDirCli:
    def _args(self, *argv: str) -> argparse.Namespace:
        return build_parser().parse_args(list(argv))

    def test_images_dir_implies_direct_mode(self, tmp_path: Path) -> None:
        config = build_config(self._args("--images-dir", str(tmp_path), "--dry-run"))
        assert config.local_images_mode and config.direct_mode

    def test_plain_run_stays_option_one(self, tmp_path: Path) -> None:
        config = build_config(self._args("--input", "sheet.csv", "--dry-run"))
        assert not config.local_images_mode and not config.direct_mode

    def test_conflicts_with_input_and_direct(self, tmp_path: Path) -> None:
        from main import resolve_run_input

        config = build_config(self._args("--images-dir", str(tmp_path), "--dry-run"))
        with pytest.raises(ConfigError, match="images-dir"):
            resolve_run_input(self._args("--images-dir", str(tmp_path), "--input", "s.csv"), config)
        with pytest.raises(ConfigError, match="images-dir"):
            resolve_run_input(self._args("--images-dir", str(tmp_path), "--direct"), config)

    def test_build_tasks_words_the_name_in_every_run(self, tmp_path: Path) -> None:
        from main import _build_tasks

        root = _parent(tmp_path)
        _folder(root, "MSAG2B-12HRDN1-QC0 Black")
        config = _config(tmp_path, direct_mode=True, local_images_mode=True)
        tasks, _ = _build_tasks(config, root)
        # One product, nothing to collide with: the colour is still in the name, because
        # a media library that shows only "MSAG2B-12HRDN1-QC0_1.jpg" cannot say which
        # colourway the picture is (owner, 2026-10-20).
        assert tasks[0].name_prefix_override == "MSAG2B-12HRDN1-QC0_BLACK"

        _folder(root, "MSAG2B-12HRDN1-QC0 White")
        black, white = _build_tasks(config, root)[0]
        assert black.name_prefix_override == "MSAG2B-12HRDN1-QC0_BLACK"
        assert white.name_prefix_override == "MSAG2B-12HRDN1-QC0_WHITE"

    def test_local_tasks_carry_paths_and_hash_like_urls(self, tmp_path: Path) -> None:
        from main import _build_tasks

        root = _parent(tmp_path)
        _folder(root, "Product One")
        config = _config(tmp_path, direct_mode=True, local_images_mode=True)
        tasks, _ = _build_tasks(config, root)
        assert len(tasks[0].source_urls) == 3
        assert hash_source_urls(tasks[0].source_urls)  # checkpoint keying works on paths

    def test_resolve_run_input_wants_a_real_folder(self, tmp_path: Path) -> None:
        from main import resolve_run_input

        root = _parent(tmp_path)
        config = build_config(self._args("--images-dir", str(root), "--dry-run"))
        assert resolve_run_input(self._args("--images-dir", str(root)), config) == root.resolve()
        with pytest.raises(ConfigError, match="folder"):
            resolve_run_input(self._args("--images-dir", str(tmp_path / "nope")), config)


class TestBuildDepsLocalFetcher:
    def test_local_mode_swaps_the_image_fetcher(self, tmp_path: Path) -> None:
        from decisions import DecisionLog
        from main import build_deps

        config = _config(tmp_path, direct_mode=True, local_images_mode=True)
        deps = build_deps(config, DecisionLog(config.decisions_path), resolve_platforms(config, "website"))
        assert isinstance(deps.image_fetcher, LocalImageFetcher)


# --------------------------------------------------------------------------- #
# The Option 3 processor: Option 2's flow over files read from disk
# --------------------------------------------------------------------------- #
from conftest import (  # noqa: E402
    FakeImageHost,
    FakePageFetcher,
    make_assets,
    synthetic_platforms,
)
from test_direct_pipeline import _themed_template, _white_product_photo, _white_product_photo_alt  # noqa: E402

from decisions import DecisionLog  # noqa: E402
from direct_pipeline import process_direct_product  # noqa: E402
from models import Status  # noqa: E402
from pipeline import PipelineDeps  # noqa: E402


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _local_assets(tmp_path: Path) -> Path:
    folder = tmp_path / "assets"
    folder.mkdir(parents=True, exist_ok=True)
    make_assets(folder)
    return folder


def _local_config(tmp_path: Path, **overrides: object) -> PipelineConfig:
    return PipelineConfig(
        dry_run=True,
        direct_mode=True,
        local_images_mode=True,
        assets_dir=_local_assets(tmp_path),
        output_dir=tmp_path / "out",
        state_dir=tmp_path / "state",
        **overrides,  # type: ignore[arg-type]
    )


def _local_deps(tmp_path: Path) -> PipelineDeps:
    return PipelineDeps(
        page_fetcher=FakePageFetcher({}),
        image_fetcher=LocalImageFetcher(max_bytes=15 * 1024 * 1024),
        image_host=FakeImageHost(),
        decisions=DecisionLog(tmp_path / "decisions.jsonl"),
        banner_cache={},
        banner_cache_lock=threading.Lock(),
        platforms=synthetic_platforms(_local_assets(tmp_path)),
        source_searcher=None,
    )


def _local_task(tmp_path: Path, images: list[Image.Image], title: str = "Cool Breeze 12X AC") -> ProductTask:
    folder = tmp_path / "products" / title
    folder.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for n, image in enumerate(images):
        path = folder / f"{n + 1}.png"
        path.write_bytes(_png_bytes(image))
        paths.append(_product_path(path))
    keys = build_model_keys(title)
    return ProductTask(
        index=0, title=title, source_urls=tuple(paths),
        name_prefix_override=worded_filename_prefix(keys, title),
    )


def _decisions_text(tmp_path: Path) -> str:
    path = tmp_path / "decisions.jsonl"
    return path.read_text(encoding="utf-8") if path.exists() else ""


class TestLocalProcessor:
    def test_publishes_from_files_and_writes_named_finals(self, tmp_path: Path) -> None:
        task = _local_task(tmp_path, [_white_product_photo(), _white_product_photo_alt()])
        result, accepted = process_direct_product(task, _local_deps(tmp_path), _local_config(tmp_path))
        assert result.status is Status.OK
        assert len(accepted) == 2
        assert len(result.urls_for("digi")) == 3  # two photos + the closing banner
        stem = task.name_prefix_override
        assert stem  # the folder title's stem, computed once by the same helper
        finals = sorted(p.name for p in (tmp_path / "out" / "images" / "digi").rglob("*.jpg"))
        assert finals == [f"{stem}_1.jpg", f"{stem}_2.jpg", f"{stem}_3.jpg"]
        # The decision line names the file, not a URL: the audit trail points at the folder.
        text = _decisions_text(tmp_path)
        assert '"rule_id": "accept"' in text
        assert task.source_urls[0] in text

    def test_colourway_stem_names_the_finals_across_platforms(self, tmp_path: Path) -> None:
        task = _local_task(tmp_path, [_white_product_photo(), _white_product_photo_alt()],
                           title="MSAG2B-12HRDN1-QC0 White")
        assert task.name_prefix_override == "MSAG2B-12HRDN1-QC0_WHITE"
        result, accepted = process_direct_product(task, _local_deps(tmp_path), _local_config(tmp_path))
        assert result.status is Status.OK
        for platform in ("digi", "alfa"):
            folder = next((tmp_path / "out" / "images" / platform).iterdir())
            names = sorted(p.name for p in folder.iterdir())
            assert names[:2] == ["MSAG2B-12HRDN1-QC0_WHITE_1.jpg", "MSAG2B-12HRDN1-QC0_WHITE_2.jpg"]

    def test_blurry_picture_dies_at_s13_blur(self, tmp_path: Path) -> None:
        crisp = _white_product_photo()
        blurred = crisp.filter(ImageFilter.GaussianBlur(radius=8))
        task = _local_task(tmp_path, [blurred, crisp, blurred])
        result, accepted = process_direct_product(task, _local_deps(tmp_path), _local_config(tmp_path))
        assert len(accepted) == 1
        assert result.status is Status.PARTIAL
        assert "S13.blur" in _decisions_text(tmp_path)

    def test_branded_store_template_dies_at_s19(self, tmp_path: Path) -> None:
        task = _local_task(tmp_path, [_themed_template((248, 198, 0))])
        result, accepted = process_direct_product(task, _local_deps(tmp_path), _local_config(tmp_path))
        assert accepted == []
        assert result.status is Status.FAILED
        assert "S19" in _decisions_text(tmp_path)

    def test_missing_file_dies_at_s13_fetch(self, tmp_path: Path) -> None:
        task = _local_task(tmp_path, [_white_product_photo(), _white_product_photo_alt()])
        Path(task.source_urls[0]).unlink()  # the folder changed after discovery
        result, accepted = process_direct_product(task, _local_deps(tmp_path), _local_config(tmp_path))
        assert len(accepted) == 1
        assert result.status is Status.PARTIAL
        assert "S13.fetch" in _decisions_text(tmp_path)

    def test_the_same_picture_twice_is_queued_once(self, tmp_path: Path) -> None:
        photo = _white_product_photo()
        task = _local_task(tmp_path, [photo, photo])
        result, accepted = process_direct_product(task, _local_deps(tmp_path), _local_config(tmp_path))
        assert len(accepted) == 1
        assert "re-served copy" in _decisions_text(tmp_path)  # S10 pixel dedup fired


# --------------------------------------------------------------------------- #
# main() end to end: folders in, platform CSVs out, no network
# --------------------------------------------------------------------------- #
import csv  # noqa: E402

from main import main  # noqa: E402


def _photo_folder(root: Path, name: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "1.jpg").write_bytes(jpeg_bytes(_white_product_photo()))
    (folder / "2.jpg").write_bytes(jpeg_bytes(_white_product_photo_alt()))
    return folder


def _read_platform_csv(out: Path, platform: str, filename: str) -> list[dict[str, str]]:
    with (out / platform / filename).open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _run_main(root: Path, assets: Path, out: Path, tmp_path: Path) -> int:
    return main(
        [
            "--images-dir", str(root), "--dry-run", "--platforms", "website,alfa,digi",
            "--assets-dir", str(assets), "--output-dir", str(out),
            "--state-dir", str(tmp_path / "state"), "--workers", "1",
        ]
    )


class TestImagesDirEndToEnd:
    def test_dry_run_produces_exact_titles_and_unique_names(self, tmp_path: Path) -> None:
        root = _parent(tmp_path)
        _photo_folder(root, "MSAG2B-12HRDN1-QC0 Black")
        _photo_folder(root, "MSAG2B-12HRDN1-QC0 White")
        _photo_folder(root, "Wireless Headset, Pro 900")  # comma proves CSV quoting
        (root / "Empty One").mkdir()  # no pictures: fails loudly, the run continues

        assets = tmp_path / "assets"
        make_assets(assets)  # deliberately has no website artwork
        blank = Image.new("RGBA", (800, 800), (255, 255, 255, 255))
        (assets / "website").mkdir()
        (assets / "website" / "template.png").write_bytes(png_bytes(blank))
        out = tmp_path / "out"
        assert _run_main(root, assets, out, tmp_path) == 0

        rows = {r["Title"]: r for r in _read_platform_csv(out, "website", "website_product_images.csv")}
        assert set(rows) == {"MSAG2B-12HRDN1-QC0 Black", "MSAG2B-12HRDN1-QC0 White", "Wireless Headset, Pro 900"}
        assert all(r["Image_Urls"] == "" for r in rows.values())  # dry-run lists no URLs
        assert rows["MSAG2B-12HRDN1-QC0 Black"]["images"].startswith("MSAG2B-12HRDN1-QC0_BLACK_1.jpg")
        assert rows["MSAG2B-12HRDN1-QC0 White"]["images"].startswith("MSAG2B-12HRDN1-QC0_WHITE_1.jpg")

        # The names in the digi CSV are the names on disk, per platform folder, banner last.
        digi = next(r for r in _read_platform_csv(out, "digi", "digimall_product_images.csv")
                    if r["Title"] == "MSAG2B-12HRDN1-QC0 Black")
        stem = "MSAG2B-12HRDN1-QC0_BLACK"
        listed = [n.strip() for n in digi["images"].split(",")]
        assert listed == [f"{stem}_{n}.jpg" for n in (1, 2, 3)]
        on_disk = sorted(p.name for p in (out / "images" / "digi" / "msag2b-12hrdn1-qc0-black").iterdir())
        assert on_disk == listed

        failed_text = (out / "failed_products.csv").read_text(encoding="utf-8-sig")
        assert "Empty One" in failed_text and "no source url" in failed_text
        assert (out / "run_report.csv").exists() and (out / "decisions.jsonl").exists()
        assert (out / "README.md").exists()

    def test_missing_website_artwork_skips_the_platform_not_the_run(self, tmp_path: Path) -> None:
        root = _parent(tmp_path)
        _photo_folder(root, "MSAG2B-12HRDN1-QC0 Black")
        assets = tmp_path / "assets"
        make_assets(assets)  # website absent by design
        out = tmp_path / "out"
        assert _run_main(root, assets, out, tmp_path) == 0
        assert not (out / "website").exists()
        assert (out / "digi" / "digimall_product_images.csv").exists()
        log = (out / "pipeline_run.log").read_text(encoding="utf-8")
        assert "website" in log.lower()  # the skip was announced, not silent


# --------------------------------------------------------------------------- #
# Cross-mode uniqueness: sheet, --direct and --images-dir all share this rule
# --------------------------------------------------------------------------- #
def _tasks(*titles: str) -> list[ProductTask]:
    return [ProductTask(index=n, title=t, source_urls=(f"http://x/{n}.jpg",)) for n, t in enumerate(titles)]


def _stems(tasks: list[ProductTask]) -> list[str]:
    """The stems the run-wide rule assigned; every row here is expected to be named."""
    return [t.name_prefix_override for t in assign_filename_prefixes(tasks) if t.name_prefix_override]


class TestAssignFilenamePrefixes:
    def test_series_and_colour_are_in_the_name_even_without_a_collision(self) -> None:
        # Owner 2026-10-20, refrigerator sheet: every code was unique, so every file was
        # named with the code alone - nothing told 9178LF IceMax Gold from any other.
        assigned = assign_filename_prefixes(
            _tasks("Dawlance 9178LF IceMax 14 Cu Ft Glass Door Water Dispenser Gold")
        )
        assert assigned[0].name_prefix_override == "9178LF_DAWLANCE_ICEMAX_GOLD"

    def test_size_and_tech_noise_stays_out_of_the_name(self) -> None:
        # Capacity, tonnage and "inverter" are not identity; the series and the colour are.
        assert _stems(_tasks("Haier 9160LF Grey 1.5 Ton")) == ["9160LF_HAIER_GREY"]

    def test_colourways_of_one_code_get_their_distinguishing_words(self) -> None:
        first, second, third = _stems(
            _tasks(
                "Haier 9160LF Grey 1.5 Ton",
                "Haier 9160LF White 1.5 Ton",
                "Haier 9160LF Diamond Grey 1.5 Ton",
            )
        )
        assert {first, second} == {"9160LF_HAIER_GREY", "9160LF_HAIER_WHITE"}
        assert len({first.casefold(), second.casefold(), third.casefold()}) == 3
        assert not any(any(c in p for c in "/()") for p in (first, second, third))

    def test_titles_that_fold_identically_get_a_hash_tail_not_a_name_clash(self) -> None:
        first, second = _stems(_tasks("Haier 9160LF (Grey)", "Haier 9160LF Grey"))
        assert first != second and None not in (first, second)
        assert all(len(p) <= MAX_PREFIX_CHARS for p in (first, second))

    def test_it_is_not_limited_to_local_folders(self) -> None:
        # Sheet rows of one code collide the same way; the rule is mode-agnostic.
        assert all(p is not None for p in _stems(_tasks("Haier 9160LF Grey", "Haier 9160LF White")))

    def test_case_only_stems_are_one_name_on_windows_and_wordpress(self) -> None:
        # "HSU9160LF" and "hsu9160lf" are the SAME file to a case-insensitive media
        # library, so a case difference never counts as uniqueness.
        first, second = _stems(_tasks("Haier HSU9160LF", "Haier hsu9160lf Grey"))
        assert first.casefold() != second.casefold()

    def test_titles_the_merge_left_apart_are_never_given_one_name(self) -> None:
        # "Straße" and "Strasse" are one word folded, but two rows: the merge keyed on
        # lower() kept them apart, so the naming must treat them as two products.
        first, second = _stems(_tasks("Haier 9160LF Straße", "Haier 9160LF Strasse"))
        assert first != second


class TestWordedPrefixStaysReadable:
    def test_punctuation_titles_keep_readable_words(self) -> None:
        title = "Haier HSU-9160LF Grey 1.5 Ton"
        stem = worded_filename_prefix(build_model_keys(title), title)
        assert stem == "HSU-9160LF_HAIER_GREY"  # code, series, colour - and nothing else
        assert "_" in stem  # a name, not a bare hash


def test_folder_mode_names_come_from_the_shared_rule(tmp_path: Path) -> None:
    root = _parent(tmp_path)
    _photo_folder(root, "Haier 9160LF Grey")
    _photo_folder(root, "Haier 9160LF White")
    config = _config(tmp_path, direct_mode=True, local_images_mode=True)
    tasks, _ = _build_tasks(config, root)
    prefixes = {t.name_prefix_override for t in tasks}
    assert len(prefixes) == 2 and None not in prefixes


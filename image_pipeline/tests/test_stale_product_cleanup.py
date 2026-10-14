"""Stale pictures from an earlier run must not outlive the row that made them.

The import sheet lists what *this* run published.  If the product's folder still holds
the pictures of the run before, the two disagree - and the owner's zip-and-import mode
ships files no sheet claims.  Cleaning happens at row start, by the worker that owns the
row, and only for the folders that row's own title names.
"""

from __future__ import annotations

import threading
from pathlib import Path

from conftest import FakeImageFetcher, FakeImageHost, FakePageFetcher, synthetic_platforms

from config import PipelineConfig
from csv_io import hash_source_urls, safe_slug
from decisions import DecisionLog
from models import (
    CHECKPOINT_LAYOUT,
    AcceptedImage,
    CheckpointEntry,
    ProductResult,
    ProductTask,
    Status,
)
from pipeline import BatchRunner, PipelineDeps, clear_stale_product_files
from platforms import PlatformPlan, resolve_platforms

TITLE_A = "Haier HSU-12HFPAA Inverter AC"
TITLE_B = "Haier HSU-24HQNA01U1 Inverter AC"
PAGE = "https://www.haier.com/pk/hsu-12hfpaa.shtml"


def _config(tmp_path: Path, *, workers: int = 4) -> PipelineConfig:
    return PipelineConfig(
        assets_dir=tmp_path / "assets",
        output_dir=tmp_path / "out",
        state_dir=tmp_path / "state",
        dry_run=True,
        workers=workers,
    )


def _deps(config: PipelineConfig, plan: PlatformPlan) -> PipelineDeps:
    return PipelineDeps(
        page_fetcher=FakePageFetcher({}),
        image_fetcher=FakeImageFetcher({}),
        image_host=FakeImageHost(),
        decisions=DecisionLog(config.output_dir / "decisions.jsonl"),
        banner_cache={},
        banner_cache_lock=threading.Lock(),
        platforms=plan,
        source_searcher=None,
    )


def _final_dir(config: PipelineConfig, title: str, key: str) -> Path:
    return config.images_dir / key / safe_slug(title)


def _plant(config: PipelineConfig, title: str, key: str, *names: str) -> Path:
    """One product's folder, full of files from a run that has since changed its mind."""
    folder = _final_dir(config, title, key)
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_text("stale", encoding="utf-8")
    return folder


def _plant_audit(config: PipelineConfig, title: str, *names: str) -> Path:
    folder = config.audit_dir / safe_slug(title) / "raw"
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_text("stale", encoding="utf-8")
    return folder


# --------------------------------------------------------------------------- #
# what one clean removes
# --------------------------------------------------------------------------- #
def test_a_reprocessed_row_starts_from_an_empty_folder(tmp_path: Path) -> None:
    """Five published pictures last time, three now: the two leftovers have to go."""
    config = _config(tmp_path)
    deps = _deps(config, synthetic_platforms(config.assets_dir))
    digi = _plant(config, TITLE_A, "digi", "HSU-12HFPAA_1.jpg", "HSU-12HFPAA_5.jpg")
    alfa = _plant(config, TITLE_A, "alfa", "HSU-12HFPAA_1.jpg")
    audit = _plant_audit(config, TITLE_A, "01-raw.jpg")

    clear_stale_product_files(config, deps, TITLE_A)

    assert not digi.exists() and not alfa.exists() and not audit.exists()
    assert config.images_dir.is_dir() and config.audit_dir.is_dir()
    assert config.output_dir.is_dir()


def test_another_products_pictures_are_not_touched(tmp_path: Path) -> None:
    """One row's cleanup must never reach a sibling product's delivered files."""
    config = _config(tmp_path)
    deps = _deps(config, synthetic_platforms(config.assets_dir))
    mine = _plant(config, TITLE_A, "digi", "HSU-12HFPAA_1.jpg")
    theirs = _plant(config, TITLE_B, "digi", "HSU-24HQNA01U1_1.jpg")
    their_audit = _plant_audit(config, TITLE_B, "01-raw.jpg")

    clear_stale_product_files(config, deps, TITLE_A)

    assert not mine.exists()
    assert theirs.is_dir() and their_audit.is_dir()


def test_a_platform_that_is_not_running_keeps_its_pictures(tmp_path: Path) -> None:
    """--platforms digi re-runs Digimall; Alfamall's delivered set belongs to that sheet."""
    config = _config(tmp_path)
    synthetic_platforms(config.assets_dir)          # writes the per-platform asset tree
    plan = resolve_platforms(config, ("digi",))
    digi = _plant(config, TITLE_A, "digi", "HSU-12HFPAA_1.jpg")
    alfa = _plant(config, TITLE_A, "alfa", "HSU-12HFPAA_1.jpg")

    clear_stale_product_files(config, _deps(config, plan), TITLE_A)

    assert not digi.exists()
    assert alfa.is_dir()


def test_a_state_folder_is_never_a_cleanup_target(tmp_path: Path) -> None:
    """The checkpoint is the resume cache; deleting it would re-upload the whole sheet."""
    config = _config(tmp_path)
    deps = _deps(config, synthetic_platforms(config.assets_dir))
    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / "checkpoint.jsonl").write_text("", encoding="utf-8")

    clear_stale_product_files(config, deps, TITLE_A)
    clear_stale_product_files(config, deps, "")

    assert (config.state_dir / "checkpoint.jsonl").is_file()


# --------------------------------------------------------------------------- #
# the runner seam
# --------------------------------------------------------------------------- #
def _task(index: int, title: str) -> ProductTask:
    return ProductTask(index=index, title=title, source_urls=(PAGE,))


def test_a_resumed_row_keeps_the_pictures_its_done_run_published(
    tmp_path: Path,
) -> None:
    """A row the checkpoint already covers is never cleaned - there is nothing to replace."""
    config = _config(tmp_path)
    plan = synthetic_platforms(config.assets_dir)
    digi = _plant(config, TITLE_A, "digi", "HSU-12HFPAA_1.jpg")

    def never(
        task: ProductTask,
        deps: PipelineDeps,
        cfg: PipelineConfig,
        cancel: threading.Event | None = None,
    ) -> tuple[ProductResult, list[AcceptedImage]]:  # pragma: no cover
        raise AssertionError("a done row must not be re-processed")

    checkpoint = {
        TITLE_A.lower(): CheckpointEntry(
            title=TITLE_A,
            source_url_hash=hash_source_urls((PAGE,)),
            status=Status.OK,
            platform_urls=(("alfa", ("https://i.example/a1.jpg",)),
                           ("digi", ("https://i.example/d1.jpg",))),
            reason="",
            timestamp="2026-10-14T00:00:00",
            layout=CHECKPOINT_LAYOUT,
        )
    }
    results = BatchRunner(
        config, _deps(config, plan), [_task(0, TITLE_A)], checkpoint, processor=never
    ).run()

    assert results[0].note == "resumed from checkpoint"
    assert digi.is_dir()


def test_two_workers_do_not_erase_each_other(tmp_path: Path) -> None:
    """36 workers clean 36 folders at once: each may only see its own product."""
    config = _config(tmp_path, workers=2)
    deps = _deps(config, synthetic_platforms(config.assets_dir))
    both_rows = threading.Barrier(2)
    complaints: list[str] = []

    def processor(
        task: ProductTask,
        deps_: PipelineDeps,
        config_: PipelineConfig,
        cancel: threading.Event | None = None,
    ) -> tuple[ProductResult, list[AcceptedImage]]:
        mine = _plant(config_, task.title, "digi", "01.jpg")
        both_rows.wait(timeout=10)
        # past the barrier both rows have written, so anything missing now was erased
        # by the *other* worker's cleanup
        if not (mine / "01.jpg").is_file():
            complaints.append(f"{task.title}: own picture vanished")
        other = TITLE_B if task.index == 0 else TITLE_A
        if not (_final_dir(config_, other, "digi") / "01.jpg").is_file():
            complaints.append(f"{task.title} wiped {other}")
        return ProductResult(task_index=task.index, title=task.title, status=Status.OK), []

    tasks = [_task(0, TITLE_A), _task(1, TITLE_B)]
    BatchRunner(config, deps, tasks, {}, processor=processor).run()

    assert complaints == []


def test_a_fresh_row_is_cleaned_before_the_processor_writes(tmp_path: Path) -> None:
    """The seam is row start, not row end: the processor cannot be told to overwrite."""
    config = _config(tmp_path)
    deps = _deps(config, synthetic_platforms(config.assets_dir))
    stale = _plant(config, TITLE_A, "digi", "HSU-12HFPAA_9.jpg")
    seen: list[bool] = []

    def processor(
        task: ProductTask,
        deps_: PipelineDeps,
        config_: PipelineConfig,
        cancel: threading.Event | None = None,
    ) -> tuple[ProductResult, list[AcceptedImage]]:
        seen.append(stale.exists())
        return ProductResult(task_index=task.index, title=task.title, status=Status.OK), []

    BatchRunner(config, deps, [_task(0, TITLE_A)], {}, processor=processor).run()
    assert seen == [False]

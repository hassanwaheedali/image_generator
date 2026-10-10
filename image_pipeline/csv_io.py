"""Sheet reading, atomic writers, slugs, checkpoint and banner cache.

All disk writes are atomic: write a temp file in the destination folder, ``fsync``
it, then ``os.replace`` onto the target.  A crash can therefore never leave a
half-written CSV.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import logging
import os
import re
import tempfile
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from models import CheckpointEntry, ProductResult, ProductTask

LOG = logging.getLogger("pipeline.csv_io")

TITLE_ALIASES: tuple[str, ...] = ("title", "producttitle", "name", "productname")
URL_ALIASES: tuple[str, ...] = (
    "sourceurl", "sourcelink", "images", "image", "imageurls", "imageurl", "sourceurls",
    "url", "urls", "link", "pagelink",
)
#: Option 2 (--direct) reads the sheet's direct-image column, never the Source URL
#: column (that one belongs to Option 1's scraping).  Falls back to URL_ALIASES only
#: when the sheet carries no image column at all.
IMAGE_URL_ALIASES: tuple[str, ...] = (
    "images", "image", "imageurls", "imageurl", "directimageurls", "directimages",
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


# --------------------------------------------------------------------------- #
# headers
# --------------------------------------------------------------------------- #
def normalize_header(name: str) -> str:
    """Lowercase, remove spaces/underscores/dashes (``Source URL`` -> ``sourceurl``)."""
    return re.sub(r"[\s_\-]+", "", str(name).strip().lower())


def find_column(fieldnames: Sequence[str], aliases: Sequence[str]) -> str | None:
    """First column whose normalised name is in ``aliases``."""
    for field in fieldnames:
        if normalize_header(field) in aliases:
            return field
    return None


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
def _read_rows(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    if suffix in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        workbook = load_workbook(path, read_only=True, data_only=True)
        sheet = workbook.active
        rows = list(sheet.iter_rows(values_only=True))
        workbook.close()
        if not rows:
            return []
        header = [str(cell) if cell is not None else "" for cell in rows[0]]
        return [
            {header[index]: row[index] for index in range(len(header))}
            for row in rows[1:]
            if row
        ]
    raise ValueError(f"unsupported input format: {path.suffix!r} (use .csv or .xlsx)")


def read_input(path: Path, *, direct: bool = False) -> list[ProductTask]:
    """Read ``Title`` + source-URL rows into ordered ``ProductTask`` objects.

    Header names are matched case-insensitively and ignoring spaces/underscores, so
    ``Source URL``, ``source_url``, ``SourceURL`` and ``Images`` all work.
    With ``direct`` (Option 2) the image-URL column (``Images`` / ``image_urls``) is
    read and a ``Source URL`` column on the same sheet is ignored - page links belong
    to Option 1's scraping, direct mode only downloads the images themselves.

    Blank titles / URLs are returned as tasks with empty fields; ``build_tasks``
    turns them into failed rows in ``main``.
    """
    rows = _read_rows(path)
    if not rows:
        return []
    fieldnames = list(rows[0].keys())
    title_field = find_column(fieldnames, TITLE_ALIASES)
    url_field = find_column(fieldnames, IMAGE_URL_ALIASES) if direct else None
    if url_field is None:
        url_field = find_column(fieldnames, URL_ALIASES)
    if title_field is None or url_field is None:
        raise ValueError(
            "input must have a Title column and a source-URL column "
            f"(accepted URL headers: {', '.join(URL_ALIASES)}); found: {fieldnames}"
        )
    tasks: list[ProductTask] = []
    for index, row in enumerate(rows):
        title = str(row.get(title_field) or "").strip()
        raw_urls = str(row.get(url_field) or "")
        urls = tuple(part.strip() for part in raw_urls.split("|") if part.strip())
        tasks.append(ProductTask(index=index, title=title, source_urls=urls))
    return tasks


def merge_duplicate_titles(
    tasks: Sequence[ProductTask],
) -> tuple[list[ProductTask], list[str]]:
    """Merge rows with the same title; URLs concatenated in order and de-duplicated."""
    merged: dict[str, ProductTask] = {}
    order: list[str] = []
    warnings: list[str] = []
    for task in tasks:
        key = task.title.strip().lower()
        if key not in merged:
            merged[key] = ProductTask(task.index, task.title, task.source_urls)
            order.append(key)
            continue
        existing = merged[key]
        combined = list(existing.source_urls)
        for url in task.source_urls:
            if url not in combined:
                combined.append(url)
        merged[key] = ProductTask(existing.index, existing.title, tuple(combined))
        warnings.append(
            f"duplicate title merged: {task.title!r} (row {task.index} into row {existing.index})"
        )
    return [merged[key] for key in order], warnings


# --------------------------------------------------------------------------- #
# hashing / slugs
# --------------------------------------------------------------------------- #
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_source_urls(urls: Sequence[str]) -> str:
    """Stable hash of a product's source URL list (drives resume eligibility)."""
    return sha256_bytes("\n".join(urls).encode("utf-8"))[:16]


def safe_slug(title: str, *, max_length: int = 80) -> str:
    """Filesystem-safe slug; falls back to a short hash for unusable titles."""
    slug = _SLUG_RE.sub("-", title.strip().lower()).strip("-")
    slug = slug[:max_length].strip("-")
    if not slug:
        slug = f"product-{sha256_bytes(title.encode('utf-8'))[:8]}"
    return slug


# --------------------------------------------------------------------------- #
# atomic writers
# --------------------------------------------------------------------------- #
def _atomic_write(path: Path, text: str) -> None:
    """Write through a temporary file, flush to disk, then swap it in.

    A reader (or a crash) never sees a half-written CSV.  The temporary file is removed if
    anything goes wrong, so a failed run leaves no debris behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    os.close(descriptor)
    try:
        with open(temp_name, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    """Write text so readers see either the old file or the complete new one."""
    _atomic_write(path, text)


def atomic_write_csv(
    path: Path, header: Sequence[str], rows: Iterable[Sequence[str]]
) -> None:
    """Write a CSV atomically with a fixed header."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(list(header))
    for row in rows:
        writer.writerow(list(row))
    _atomic_write(path, buffer.getvalue())


def append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    """Append one JSON line (checkpoint entries).  Append-only, never rewritten."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(payload), ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def save_image_atomic(image: Image.Image, path: Path, *, jpeg_quality: int | None = None) -> None:
    """Save an image atomically (tmp file + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower() or ".png"
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp{suffix}"
    try:
        if jpeg_quality is not None or suffix in (".jpg", ".jpeg"):
            from compositing import to_jpeg_bytes

            tmp.write_bytes(
                to_jpeg_bytes(image, quality=jpeg_quality or 95)
            )
        else:
            image.save(tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):        # pragma: no cover - defensive
                tmp.unlink()


# --------------------------------------------------------------------------- #
# checkpoint
# --------------------------------------------------------------------------- #
def load_checkpoint(path: Path) -> dict[str, CheckpointEntry]:
    """Latest checkpoint entry per title (later lines win)."""
    if not path.exists():
        return {}
    entries: dict[str, CheckpointEntry] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                LOG.warning("checkpoint line ignored (not JSON): %s", line[:120])
                continue
            if not isinstance(payload, dict):
                continue
            entry = CheckpointEntry.from_dict(payload)
            if entry.title:
                entries[entry.title.strip().lower()] = entry
    return entries


def append_checkpoint(path: Path, result: ProductResult, *, timestamp: str | None = None) -> None:
    """Append one product result to the checkpoint."""
    payload = result.to_checkpoint_dict()
    payload["timestamp"] = timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds")
    append_jsonl(path, payload)


def timestamp_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# banner cache
# --------------------------------------------------------------------------- #
def load_banner_cache(path: Path) -> dict[str, str]:
    """Content-hash -> uploaded URL for the closing banners."""
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        LOG.warning("banner cache is not valid JSON; starting empty")
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value) for key, value in payload.items()}


def save_banner_cache(path: Path, cache: Mapping[str, str]) -> None:
    atomic_write_text(path, json.dumps(dict(cache), indent=2, ensure_ascii=False))



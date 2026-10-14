"""Sheet reading, atomic writers, slugs and filename prefixes, checkpoint, banner cache.

All disk writes are atomic: write a temp file in the destination folder, ``fsync``
it, then ``os.replace`` onto the target.  A crash can therefore never leave a
half-written CSV.

This module also owns every rule for turning a product title into a name on disk
(``safe_slug`` for folders, ``filename_prefix`` for files), because a name that is
illegal or merely ambiguous on Windows is a run-stopping bug, not a cosmetic one.
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

from models import CheckpointEntry, ModelKeys, ProductResult, ProductTask

LOG = logging.getLogger("pipeline.csv_io")

TITLE_ALIASES: tuple[str, ...] = ("title", "producttitle", "name", "productname")
URL_ALIASES: tuple[str, ...] = (
    "sourceurl", "sourcelink", "brandurl", "pageurl", "producturl", "images", "image",
    "imageurls", "imageurl", "sourceurls", "url", "urls", "link", "pagelink",
)
#: Option 2 (--direct) reads the sheet's direct-image column, never the Source URL
#: column (that one belongs to Option 1's scraping).  Falls back to URL_ALIASES only
#: when the sheet carries no image column at all.
IMAGE_URL_ALIASES: tuple[str, ...] = (
    "images", "image", "imageurls", "imageurl", "directimageurls", "directimages",
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
#: Characters a model-code prefix may keep.  Dots and dashes stay because real codes carry
#: them (``DW-7500C``, ``HSU.12HFPAA``) and the owner reads this prefix in Excel.
_PREFIX_ILLEGAL_RE = re.compile(r"[^A-Za-z0-9._-]+")
#: Longest filename prefix.  With the run folder, platform folder, 80-char slug and
#: ``_NN.jpg`` this keeps the worst-case Windows path inside MAX_PATH (see plan §2.2.1).
MAX_PREFIX_CHARS = 48
_FALLBACK_JPEG_QUALITY = 95

#: Windows reserves these device names for *any* extension, and a folder called ``con``
#: cannot be created, listed or deleted normally - so it must never reach the disk.
WINDOWS_RESERVED_NAMES = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
})


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


def guard_windows_name(name: str) -> str:
    """Make ``name`` usable as a Windows folder name or file stem.

    Device names are reserved whatever the extension (``CON``, ``CON.jpg`` and ``CON.tar.gz``
    all mean the console), and a trailing dot is rejected outright.  Both are neutralised
    here rather than at each call site, because every name the pipeline invents ends up on
    disk under one of these two roles.
    """
    if name.split(".", 1)[0].strip().lower() in WINDOWS_RESERVED_NAMES:
        return f"_{name}"
    return name.rstrip(".")


def safe_slug(title: str, *, max_length: int = 80) -> str:
    """Filesystem-safe slug; falls back to a short hash for unusable titles."""
    slug = _SLUG_RE.sub("-", title.strip().lower()).strip("-")
    slug = slug[:max_length].strip("-")
    if not slug:
        return f"product-{sha256_bytes(title.encode('utf-8'))[:8]}"
    return guard_windows_name(slug)


def _prefix_token(token: str) -> str:
    """One model token reduced to characters a filename may carry."""
    return _PREFIX_ILLEGAL_RE.sub("", token)


def _join_within_budget(tokens: Sequence[str], limit: int) -> str:
    """Join ``tokens`` with ``_``, dropping whole trailing tokens past ``limit``.

    Never cut inside a token: a truncated model code prints as a *different* model, while a
    shorter whole one is still findable.  The first token alone over the limit is hard-sliced
    - a 48-character code is already pathological and losing it entirely is worse.
    """
    if len(tokens[0]) > limit:
        return tokens[0][:limit]
    joined = tokens[0]
    for token in tokens[1:]:
        candidate = f"{joined}_{token}"
        if len(candidate) > limit:
            break
        joined = candidate
    return joined


def filename_prefix(keys: ModelKeys, title: str) -> str:
    """The filename stem every platform shares for one product's images.

    All model tokens in title order (owner decision D7), so ``Midea 12HRFN1 QC6 1 Ton
    GO EASY T3 Inverter AC`` -> ``12HRFN1_QC6`` and the finals read ``12HRFN1_QC6_1.jpg``.
    Using the same stem on all platforms is deliberate: it makes the same photograph
    comparable across ``digi/``, ``alfa/`` and ``website/`` by filename alone.

    Rows with no model code fall back to their keywords plus a hash of the title, so two
    keyword-only rows can never collide on the same stem.
    """
    tokens = tuple(clean for clean in (_prefix_token(token) for token in keys.tokens) if clean)
    if tokens:
        return guard_windows_name(_join_within_budget(tokens, MAX_PREFIX_CHARS))
    digest = sha256_bytes(title.encode("utf-8"))
    if keys.keywords:
        words = safe_slug("-".join(keys.keywords), max_length=40)
        return guard_windows_name(f"{words}-{digest[:6]}")
    return f"product-{digest[:8]}"


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
    """Save an image atomically (tmp file + ``os.replace``).

    The **suffix chooses the format**; ``jpeg_quality`` only applies to ``.jpg``/``.jpeg``.
    A quality argument that won over the suffix used to write JPEG bytes into a file named
    ``.png`` - which Pillow, Excel and every image viewer still open happily, so nothing
    downstream would ever report it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower() or ".png"
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp{suffix}"
    try:
        if suffix in (".jpg", ".jpeg"):
            from compositing import to_jpeg_bytes

            tmp.write_bytes(to_jpeg_bytes(image, quality=jpeg_quality or _FALLBACK_JPEG_QUALITY))
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



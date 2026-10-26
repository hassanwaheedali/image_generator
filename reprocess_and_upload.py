"""Reprocess 750 local images with fit_product_to_canvas (95% full VIP canvas),
overwrite local image files in output/1800_products-929850/images/alfa,
upload to FreeImage, and update alfamall_product_images.csv automatically.
"""

from __future__ import annotations

import csv
import io
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Add image_pipeline to path
sys.path.insert(0, str(Path(__file__).parent / "image_pipeline"))

from PIL import Image
from compositing import fit_product_to_canvas, to_jpeg_bytes
from config import PipelineConfig, load_env_file, parse_env_text
from image_host import FreeImageHost
from pipeline import _product_folder

OUTPUT_DIR = Path(r"d:\image_generator\output\1800_products-929850")
ALFA_CSV = OUTPUT_DIR / "alfa" / "alfamall_product_images.csv"
BACKUP_CSV = OUTPUT_DIR / "alfa" / "alfamall_product_images_backup.csv"
AUDIT_BASE = OUTPUT_DIR / "audit"
IMAGES_BASE = OUTPUT_DIR / "images" / "alfa"


def main() -> None:
    print("=" * 70)
    print("REPROCESS & UPLOAD 750 IMAGES (NO SOURCE DOWNLOADS)")
    print("=" * 70)

    # 1. Backup CSV
    if not BACKUP_CSV.exists():
        shutil.copy2(ALFA_CSV, BACKUP_CSV)
        print(f"Backed up original CSV to: {BACKUP_CSV.name}")
    else:
        print(f"Backup CSV already exists: {BACKUP_CSV.name}")

    # 2. Setup config & freeimage host
    env_file = load_env_file()
    if env_file is None:
        raise RuntimeError("secrets.env not found!")
    env_vars = parse_env_text(env_file.read_text(encoding="utf-8"))
    api_key = env_vars.get("FREEIMAGE_API_KEY")
    if not api_key:
        raise RuntimeError("FREEIMAGE_API_KEY is missing from secrets.env")

    config = PipelineConfig(dry_run=True, output_dir=OUTPUT_DIR)
    host = FreeImageHost(api_key=api_key, min_interval_s=0.05, max_retries=5)

    # 3. Read CSV rows
    with open(ALFA_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or ["Title", "Brand", "Image_Urls", "images"])
        rows = list(reader)

    total_rows = len(rows)
    print(f"Total products to process: {total_rows}")

    lock = threading.Lock()
    completed_count = 0
    start_time = time.perf_counter()

    def process_row(idx: int, row: dict[str, str]) -> tuple[int, dict[str, str]]:
        title = row["Title"]
        folder = _product_folder(config, title)
        img_names = [x.strip() for x in row["images"].split(",") if x.strip()]

        new_urls: list[str] = []
        for img_name in img_names:
            # Parse image index from filename (e.g. _1.jpg -> 1)
            m = re.search(r"_(\d+)\.jpg$", img_name)
            img_idx = int(m.group(1)) if m else 1

            iso_path = AUDIT_BASE / folder / "isolated" / f"{img_idx:02d}-square.jpg"
            final_path = IMAGES_BASE / folder / img_name

            if not iso_path.exists():
                raise FileNotFoundError(f"Missing isolated image: {iso_path}")

            # Open local isolated product
            with Image.open(iso_path) as square:
                # 95% full VIP canvas scaling
                final_image = fit_product_to_canvas(square, config.canonical_side, config)

            # Overwrite local final image
            final_path.parent.mkdir(parents=True, exist_ok=True)
            jpeg_bytes = to_jpeg_bytes(final_image, quality=config.output_jpeg_quality)
            final_path.write_bytes(jpeg_bytes)

            # Upload to FreeImage
            url = host.upload(jpeg_bytes, img_name)
            new_urls.append(url)

        # Update row with pipe-separated URLs
        updated_row = dict(row)
        updated_row["Image_Urls"] = "|".join(new_urls)
        return idx, updated_row

    updated_rows: list[dict[str, str]] = list(rows)

    # Parallel processing with 20 workers
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(process_row, i, r): i for i, r in enumerate(rows)}
        for future in as_completed(futures):
            i, updated_row = future.result()
            updated_rows[i] = updated_row
            with lock:
                completed_count += 1
                if completed_count % 25 == 0 or completed_count == total_rows:
                    elapsed = time.perf_counter() - start_time
                    rate = completed_count / max(1e-3, elapsed)
                    remaining = (total_rows - completed_count) / max(1e-3, rate)
                    print(
                        f"[{completed_count:3d}/{total_rows}] "
                        f"Rate: {rate:.1f} imgs/s | "
                        f"Elapsed: {elapsed:.0f}s | "
                        f"ETA: {remaining:.0f}s"
                    )

    # 4. Atomically write updated CSV
    temp_csv = ALFA_CSV.with_suffix(".csv.tmp")
    with open(temp_csv, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(updated_rows)

    shutil.move(str(temp_csv), str(ALFA_CSV))
    total_time = time.perf_counter() - start_time
    print("=" * 70)
    print(f"SUCCESS: All {total_rows} products reprocessed, scaled to 95% VIP, uploaded, and CSV updated in {total_time:.1f}s!")
    print(f"Updated CSV: {ALFA_CSV}")
    print("=" * 70)


if __name__ == "__main__":
    main()

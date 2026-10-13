"""Blur Detection and Filtering Tool for Generated Output Images.

Scans generated product images, identifies true blurry/low-resolution products
(upscaled low-res thumbnails, muddy text/details, and suppressed high frequencies),
and generates a targeted CSV for reprocessing with Option 1.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "image_pipeline"))
from csv_io import safe_slug


def compute_blur_metrics(image_path: str | Path) -> dict[str, Any]:
    """Calculate mathematically grounded blur & spectral frequency metrics."""
    try:
        img = Image.open(image_path).convert("L")
    except Exception:
        return {"hf_energy": 1.0, "edge_sharpness": 100.0, "lap_var": 1000.0, "is_blurry": False}

    arr = np.array(img, dtype=float)
    h, w = arr.shape

    # 1. Product mask (ignore white studio template background > 250)
    prod_mask = arr < 250
    if prod_mask.sum() < 500:
        return {"hf_energy": 1.0, "edge_sharpness": 100.0, "lap_var": 1000.0, "is_blurry": False}

    # 2. FFT High-Frequency Energy Ratio (Nyquist cutoff test)
    # Scaled/blurred thumbnails lack high-frequency spatial harmonics
    f = np.fft.fftshift(np.abs(np.fft.fft2(arr)))
    cy, cx = h // 2, w // 2
    r_high = min(h, w) * 0.35
    y, x = np.ogrid[:h, :w]
    high_freq_mask = (x - cx) ** 2 + (y - cy) ** 2 > r_high ** 2
    hf_energy = float(f[high_freq_mask].sum() / max(1e-6, f.sum()))

    # 3. Product Edge Sharpness
    gy, gx = np.gradient(arr)
    grad = np.sqrt(gx ** 2 + gy ** 2)
    edge_pixels = prod_mask & (grad > 20)
    edge_sharpness = float(grad[edge_pixels].mean()) if edge_pixels.sum() > 50 else 0.0

    # 4. Laplacian Variance on Product Area
    lap = ndimage.laplace(arr)
    lap_var = float(lap[prod_mask].var())

    # Decision criteria:
    # A true blurry image has depressed high-frequency spectral ratio (< 0.102)
    # and lower edge sharpness (< 48.0)
    is_blurry = bool(hf_energy < 0.102 and edge_sharpness < 48.0)

    return {
        "hf_energy": round(hf_energy, 5),
        "edge_sharpness": round(edge_sharpness, 1),
        "lap_var": round(lap_var, 1),
        "is_blurry": is_blurry,
    }


def slugify(title: str) -> str:
    """Matches the pipeline's directory slug naming."""
    return safe_slug(title)


def scan_and_filter_blurry(
    input_csv_path: str = "image_pipeline/input/1800_products.csv",
    output_dir: str = "output",
    hf_threshold: float = 0.102,
    edge_threshold: float = 48.0,
) -> tuple[list[dict[str, Any]], Path]:
    """Scan all generated images, detect blurry photos, and export CSV for Option 1."""
    out_dir = Path(output_dir)
    images_dir = out_dir / "images"

    # Read the 1800_products.csv to get Title, Brand URL, and Direct URLs
    products = []
    with open(input_csv_path, encoding="utf-8") as f:
        reader = csv.reader(f)
        _header = next(reader, None)
        for row in reader:
            if not row:
                continue
            title = row[0].strip()
            source_url = row[1].strip() if len(row) > 1 else ""
            direct_urls = row[2].strip() if len(row) > 2 else ""
            products.append({
                "title": title,
                "source_url": source_url,
                "direct_urls": direct_urls,
                "slug": slugify(title),
            })

    blurry_list = []
    all_reports = []

    for prod in products:
        slug = prod["slug"]
        img_path = images_dir / slug / "final_digimall" / "01-digimall.jpg"
        if not img_path.exists():
            img_path = images_dir / slug / "final_alfamall" / "01-alfamall.jpg"

        if not img_path.exists():
            continue

        raw_path = images_dir / slug / "raw" / "01-raw.jpg"
        raw_size = ""
        if raw_path.exists():
            try:
                with Image.open(raw_path) as r:
                    raw_size = f"{r.width}x{r.height}"
            except Exception:
                pass

        metrics = compute_blur_metrics(img_path)
        is_blurry = metrics["hf_energy"] < hf_threshold and metrics["edge_sharpness"] < edge_threshold

        report_entry = {
            "title": prod["title"],
            "source_url": prod["source_url"],
            "raw_size": raw_size,
            "hf_energy": metrics["hf_energy"],
            "edge_sharpness": metrics["edge_sharpness"],
            "lap_var": metrics["lap_var"],
            "status": "BLURRY" if is_blurry else "CLEAR",
            "image_path": str(img_path),
        }
        all_reports.append(report_entry)

        if is_blurry:
            blurry_list.append({
                "title": prod["title"],
                "source_url": prod["source_url"],
                "direct_urls": prod["direct_urls"],
                "hf_energy": metrics["hf_energy"],
                "raw_size": raw_size,
            })

    # Save full report
    report_csv = out_dir / "blurry_images_full_report.csv"
    with open(report_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "title", "status", "hf_energy", "edge_sharpness", "lap_var", "raw_size", "source_url", "image_path"
        ])
        writer.writeheader()
        writer.writerows(all_reports)

    # Save Option 1 Input CSV (only blurry images)
    option1_csv = out_dir / "blurry_products_for_option1.csv"
    with open(option1_csv, "w", newline="", encoding="utf-8") as f:
        csv_writer = csv.writer(f)
        csv_writer.writerow(["Title", "source_url", "Direct_Urls"])
        for b in blurry_list:
            csv_writer.writerow([b["title"], b["source_url"], b["direct_urls"]])

    print(f"Scanned {len(all_reports)} products.")
    print(f"Detected {len(blurry_list)} blurry products.")
    print(f"Full report saved to: {report_csv}")
    print(f"Option 1 target CSV saved to: {option1_csv}")

    return blurry_list, option1_csv


if __name__ == "__main__":
    scan_and_filter_blurry()

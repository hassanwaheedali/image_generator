"""Rebuild the visual check sheets for the current output/ run.

Run:  python3 /home/user/verification/build_sheets.py
Writes: verification/sheet_digimall.jpg, sheet_alfamall.jpg, verification/sizing.txt
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent / "image_pipeline"
OUT = Path(__file__).resolve().parent
TEMPLATES = {"final_digimall": ROOT / "assets" / "DigiMall.png",
             "final_alfamall": ROOT / "assets" / "AlfaMall.png"}


def paste_bbox(final: Image.Image, template: Image.Image) -> tuple[int, int, int, int] | None:
    """Bounding box of the pixels that differ from the bare template (the product)."""
    import numpy as np
    a = np.asarray(final.convert("RGB"), dtype=np.int16)
    b = np.asarray(template.convert("RGB"), dtype=np.int16)
    mask = (np.abs(a - b).sum(axis=2) > 24)
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def sheet(folder: str, target: Path, max_cols: int = 6) -> None:
    thumbs: list[tuple[str, Image.Image]] = []
    for product in sorted((ROOT / "output" / "images").iterdir()):
        files = sorted((product / folder).glob("*.jpg"))
        for path in files:
            thumb = Image.open(path).convert("RGB").resize((180, 180))
            thumbs.append((f"{product.name[:22]} #{path.stem[:2]}", thumb))
    if not thumbs:
        print("no images for", folder)
        return
    rows = (len(thumbs) + max_cols - 1) // max_cols
    canvas = Image.new("RGB", (max_cols * 190, rows * 190), (255, 255, 255))
    for index, (_label, thumb) in enumerate(thumbs):
        canvas.paste(thumb, ((index % max_cols) * 190 + 5, (index // max_cols) * 190 + 5))
    canvas.save(target, quality=88)
    print(f"{target.name}: {len(thumbs)} final pictures")


def sizing() -> None:
    lines: list[str] = []
    for role, template_path in TEMPLATES.items():
        template = Image.open(template_path)
        for product in sorted((ROOT / "output" / "images").iterdir()):
            for path in sorted((product / role).glob("*.jpg")):
                box = paste_bbox(Image.open(path), template)
                if box is None:
                    continue
                x0, y0, x1, y1 = box
                w, h = template.size
                lines.append(
                    f"{role:15s} {product.name[:34]:34s} {path.name} "
                    f"fill_h={100 * (y1 - y0) / h:5.1f}% fill_w={100 * (x1 - x0) / w:5.1f}% "
                    f"top={y0:4d} bottom_gap={h - y1:4d} x_centre_off={abs((x0 + x1) / 2 - w / 2):4.1f}"
                )
    (OUT / "sizing.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"sizing.txt: {len(lines)} pictures measured")
    print("\n".join(lines[:6]))


if __name__ == "__main__":
    sheet("final_digimall", OUT / "sheet_digimall.jpg")
    sheet("final_alfamall", OUT / "sheet_alfamall.jpg")
    sizing()
    sys.exit(0)

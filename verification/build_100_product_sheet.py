#!/usr/bin/env python3
"""Write ``sheet_100.csv``: 100 unique products, stratified over every variant angle.

Run this, then run the pipeline live on the result:

    python3 verification/build_100_product_sheet.py
    cd image_pipeline && python3 main.py --input ../verification/sheet_100.csv --workers 4

then audit the run with ``audit_100.py``.  The 100 rows mix same-code multi-colour
families (colour rule), same-stem multi-tail families (suffix rule), colour-less titles
(fail-open) and plain titles, so a live pass exercises the whole variant machinery.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "image_pipeline"))

from smoke_test_100_matching import load_records, stratified_select  # noqa: E402

OUT = HERE / "sheet_100.csv"


def main() -> None:
    selected = stratified_select(load_records())
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Brand", "Title"])
        for record in selected:
            writer.writerow([record["brand"], record["title"]])
    print(f"wrote {len(selected)} rows to {OUT}")


if __name__ == "__main__":
    main()

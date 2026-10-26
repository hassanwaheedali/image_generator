"""Feed the owner's 50 refrigerator titles into the kiachahiye title maker.

Writes the 7 columns their `main.py` requires; Description repeats the Title because their
extractors read title + description as one text and this sheet has no description.  Nothing in
their project is written - the run's outputs are redirected to a scratch folder here.
"""

import csv
from pathlib import Path

HERE = Path(r"D:\image_generator")
OUT = HERE / "_scratch_kia"
OUT.mkdir(exist_ok=True)

titles = [line.strip() for line in open(HERE / "_titles_in.txt", encoding="utf-8") if line.strip()]
FREEZER = "Electronics / Home appliances / Deep Freezer"
REFRIGERATOR = "Electronics / Home appliances / Refrigerators"

with open(OUT / "refrigerators_50.csv", "w", newline="", encoding="utf-8") as handle:
    writer = csv.writer(handle)
    writer.writerow(
        ["Title", "Description", "Sell Price", "Original Price", "Categories", "Brand", "Image"]
    )
    for title in titles:
        brand = title.split(" ", 1)[0]
        category = FREEZER if "Freezer" in title else REFRIGERATOR
        writer.writerow([title, title, "99999", "119999", category, brand, ""])
print(f"wrote {OUT / 'refrigerators_50.csv'} ({len(titles)} rows)")

# Enterprise Input Folder (`input/`)

This directory is the designated workspace for incoming product catalogue sheets (`.csv` and `.xlsx`).

---

## How It Works

### 1. Automatic Discovery (Zero Configuration)
If you run the pipeline without passing an explicit `--input` flag:
```bash
python main.py
```
The pipeline automatically scans this `input/` folder and selects the **newest** `.csv` or `.xlsx` file by modification timestamp.

### 2. Relative Sheet Selection
You can target a specific sheet in `input/` by providing just its filename:
```bash
python main.py --input batch_20261006.csv
```
The CLI automatically resolves the path inside `input/` without requiring full absolute paths.

### 3. Custom Input Directory
If you maintain separate input folders for different teams or batch runs, use `--input-dir`:
```bash
python main.py --input-dir /path/to/custom_input
```

---

## Required Column Formats

Every input sheet must include at least:
- **Product Title**: Column named `Title`, `Product`, or `Name` (contains brand and model number, e.g. `Dawlance DW-7500C`).
- **Source URL**: Column named `Images`, `URL`, `Source URL`, `Link`, or `Website` (initial target URL for verification and extraction).

Additional columns (e.g. SKU, Category, Price) are safely ignored or preserved in output reporting.

---

## Enterprise Recommended File Naming Convention

- `YYYY-MM-DD_<brand_or_vendor>_catalog.csv` (e.g., `2026-10-06_dawlance_catalog.csv`)
- `batch_<batch_number>_<description>.xlsx` (e.g., `batch_001_refrigerators.xlsx`)

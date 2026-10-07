#!/usr/bin/env python3
"""Audit a *live* pipeline run (default ``image_pipeline/output``) after running on
``sheet_100.csv``.  It is a report, not a gate: it prints how the variant rules behaved
on real pages so a human can eyeball the new rejections and flags.

    python3 verification/audit_100.py [path/to/out]

What it surfaces:

  * the status mix (ok / partial / failed) from ``run_report.csv``;
  * every S1 rejection whose detail says the page shows **another colourway** - the new
    colour rule firing on real pages;
  * every ``S15.tolerated`` (variant tail / tolerated spelling) and ``S15.relaxed`` flag;
  * any row that ended up FAILED so it can be reviewed.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path

DEFAULT = Path(__file__).resolve().parent.parent / "image_pipeline" / "output"


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else DEFAULT
    decisions = out / "decisions.jsonl"
    report = out / "run_report.csv"
    if not decisions.exists():
        print(f"no {decisions} - run the pipeline on sheet_100.csv first")
        return 1

    rules: Counter[str] = Counter()
    colour_rejects: list[str] = []
    tolerated: list[str] = []
    relaxed: list[str] = []
    for line in decisions.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        rule_id = row.get("rule_id", "?")
        detail = row.get("detail", "")
        rules[rule_id] += 1
        if rule_id == "S1" and "another colourway" in detail:
            colour_rejects.append(f"{row.get('title', '?')[:55]}  <-  {detail[:60]}")
        if rule_id == "S15.tolerated":
            tolerated.append(f"{row.get('title', '?')[:55]}  <-  {detail[:60]}")
        if rule_id == "S15.relaxed":
            relaxed.append(f"{row.get('title', '?')[:55]}  <-  {detail[:60]}")

    print("=" * 74)
    print(f"audit of live run: {out}")
    print("=" * 74)
    print("  decision rule counts:")
    for rule, count in rules.most_common():
        print(f"    {rule:16} {count}")
    print(f"\n  colour-rule rejections on real pages   {len(colour_rejects)}")
    for line in colour_rejects[:15]:
        print("    -", line)
    print(f"\n  tolerated (variant tail / spelling)     {len(tolerated)}")
    for line in tolerated[:15]:
        print("    -", line)
    print(f"\n  relaxed fallback (brand+number) flags   {len(relaxed)}")
    for line in relaxed[:15]:
        print("    -", line)

    if report.exists():
        statuses: Counter[str] = Counter()
        failed: list[str] = []
        with report.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                status = (row.get("Status") or "").strip().lower()
                statuses[status] += 1
                if status == "failed":
                    failed.append(f"{(row.get('Title') or '?')[:55]}  <-  {(row.get('Reason') or '')[:50]}")
        print("\n  run_report status mix:", dict(statuses))
        print(f"  failed rows for review  {len(failed)}")
        for line in failed[:15]:
            print("    -", line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

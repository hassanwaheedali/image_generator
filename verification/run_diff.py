#!/usr/bin/env python3
"""Compare which pictures two pipeline runs published, so a gate change is provable.

This diffs *selection*, not pixels: it answers "which published pictures appeared or disappeared,
which rule changed its rejection count, and which rows changed the sources they lean on".  A
compositing or placement change moves none of those, so it proves nothing there - compare the
rendered rows for that (see ``tests/test_image_quality.py``).

Exit codes: 0 the published set is identical, 1 it moved (review every line), 2 bad input - so a
missing or empty log can never look like a green run.

    python3 verification/run_diff.py output/run-before output/run-after
    python3 verification/run_diff.py old/decisions.jsonl new/decisions.jsonl --allow-changes
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

Record = dict[str, Any]
Key = tuple[str, str]


class InputError(Exception):
    """A run folder or decision log that cannot be compared - reported as exit code 2."""


def decisions_path(arg: str) -> Path:
    """Accept either a run folder or a decisions.jsonl file."""
    path = Path(arg)
    if path.is_dir():
        candidate = path / "decisions.jsonl"
        if not candidate.is_file():
            raise InputError(f"no decisions.jsonl inside {path}")
        return candidate
    if path.is_file():
        return path
    raise InputError(f"not a run folder or decision log: {arg}")


def load_records(path: Path) -> list[Record]:
    """Read the JSON lines, skipping anything the writer left half-finished."""
    records: list[Record] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
    return records


def is_accept(record: Record) -> bool:
    return record.get("decision") == "keep" and record.get("rule_id") == "accept"


def key_of(record: Record) -> Key:
    """What identifies one published picture: the row it belongs to and the file it came from."""
    return (str(record.get("title", "")), str(record.get("url", "")))


def accepted_counts(records: list[Record]) -> collections.Counter[Key]:
    """How many times each picture was published - the same URL can be accepted twice."""
    return collections.Counter(key_of(record) for record in records if is_accept(record))


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def sources_per_row(records: list[Record]) -> dict[str, list[str]]:
    """Which hosts each row's published pictures came from, in accept order."""
    grouped: dict[str, list[str]] = collections.defaultdict(list)
    for record in records:
        if is_accept(record):
            grouped[str(record.get("title", ""))].append(host_of(str(record.get("url", ""))))
    return dict(grouped)


def rule_histogram(records: list[Record]) -> collections.Counter[str]:
    return collections.Counter(
        str(record.get("rule_id")) for record in records if record.get("decision") == "reject"
    )


def describe(title: str, url: str, record: Record) -> str:
    detail = str(record.get("detail", ""))[:52]
    return f"{title}  <-  {url}  [{detail}]"


def moved_keys(
    before: collections.Counter[Key], after: collections.Counter[Key], *, lost: bool
) -> list[Key]:
    """Keys whose published count changed, listed as many times as the count moved."""
    keys: list[Key] = []
    for key in sorted(set(before) | set(after)):
        delta = before[key] - after[key]
        if (delta > 0) == lost and delta != 0:
            keys.extend([key] * abs(delta))
    return keys


def compare(old: list[Record], new: list[Record]) -> int:
    """Print the delta and return the number of published pictures that moved."""
    before_counts, after_counts = accepted_counts(old), accepted_counts(new)
    before_record = {key_of(record): record for record in old if is_accept(record)}
    after_record = {key_of(record): record for record in new if is_accept(record)}
    if not before_counts or not after_counts:
        side = "old" if not before_counts else "new"
        raise InputError(f"the {side} log published nothing - there is nothing to compare")
    old_sources, new_sources = sources_per_row(old), sources_per_row(new)

    print(f"old: {sum(before_counts.values())} published pictures across {len(old_sources)} rows")
    print(f"new: {sum(after_counts.values())} published pictures across {len(new_sources)} rows")
    print()

    lost = moved_keys(before_counts, after_counts, lost=True)
    gained = moved_keys(before_counts, after_counts, lost=False)
    print(f"PICTURES LOST ({len(lost)})")
    for key in lost:
        print(f"  - {describe(key[0], key[1], before_record[key])}")
    print(f"PICTURES GAINED ({len(gained)})")
    for key in gained:
        print(f"  + {describe(key[0], key[1], after_record[key])}")
    print()

    old_rules, new_rules = rule_histogram(old), rule_histogram(new)
    changed = sorted({*old_rules, *new_rules}, key=lambda rule: -abs(new_rules[rule] - old_rules[rule]))
    print("REJECTIONS BY RULE (old -> new)")
    quiet = True
    for rule in changed:
        if old_rules[rule] != new_rules[rule]:
            print(f"  {rule:16} {old_rules[rule]:>5} -> {new_rules[rule]:>5}")
            quiet = False
    if quiet:
        print("  (no rule changed its count)")
    print()

    print("ROWS CHANGING SOURCE")
    quiet = True
    for title in sorted({*old_sources, *new_sources}):
        hosts_before, hosts_after = set(old_sources.get(title, ())), set(new_sources.get(title, ()))
        if hosts_before != hosts_after:
            quiet = False
            print(
                f"  {title[:58]:58} {len(old_sources.get(title, ()))} from {len(hosts_before)} "
                f"source(s) -> {len(new_sources.get(title, ()))} from {len(hosts_after)} source(s)"
            )
    if quiet:
        print("  (every row keeps the same sources)")

    print()
    if not lost and not gained:
        print("VERDICT: identical published set")
        return 0
    print(f"VERDICT: {len(lost)} lost, {len(gained)} gained - review every line above")
    return len(lost) + len(gained)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare two pipeline runs' published pictures.")
    parser.add_argument("old", help="run folder or decisions.jsonl of the before run")
    parser.add_argument("new", help="run folder or decisions.jsonl of the after run")
    parser.add_argument(
        "--allow-changes",
        action="store_true",
        help="exit 0 even when the published set moved (default: exit 1, so a commit can gate on it)",
    )
    args = parser.parse_args(argv)

    try:
        changed = compare(load_records(decisions_path(args.old)), load_records(decisions_path(args.new)))
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0 if changed == 0 or args.allow_changes else 1


if __name__ == "__main__":
    sys.exit(main())

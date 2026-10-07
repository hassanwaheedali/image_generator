#!/usr/bin/env python3
"""Offline smoke test of variant matching on 100 unique catalogue products.

Read-only and fully offline: it drives the *shipped* ``build_model_keys`` /
``match_product`` / ``variant_terms`` code, never the network.  It selects 100
distinct titles stratified over every angle that the variant rules touch, then
checks, for each title, the matching verdict against:

  * its OWN page (the title used as the page name)  -> must match, never a colour
    conflict (a page that prints the row's own words can never be "another colour");
  * a SAME-COLOUR sibling (same model code, same primary colour)  -> must never be
    rejected for colour (the no-false-reject control);
  * a DIFF-COLOUR sibling (same model code, different primary colour, both named)  ->
    must never be a clean match (a wrong colour may not publish unflagged).

It asserts the three invariants and prints a per-angle coverage summary.
"""

from __future__ import annotations

import csv
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "image_pipeline"))

from model_matching import build_model_keys, match_product  # noqa: E402
from variant_terms import colour_word_conflict, primary_colour_word  # noqa: E402

CSV = Path(__file__).resolve().parent.parent / "image_pipeline" / "tests" / "fixtures" / "test_files" / "Products.csv"
TARGET = 100
SEED = 7


def load_records() -> list[dict]:
    by_title: dict[str, dict] = {}
    with CSV.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            title = (row["Title"] or "").strip()
            if not title or title in by_title:
                continue
            keys = build_model_keys(title)
            by_title[title] = {
                "title": title,
                "brand": (row["Brand"] or "").strip(),
                "keys": keys,
                "ids": tuple(dict.fromkeys(x.lower() for x in (keys.identities or keys.tokens))),
                "word": keys.colour_word,
            }
    return list(by_title.values())


def family_of(record: dict, by_code: dict) -> list[dict] | None:
    for group in by_code.values():
        if record in group and len(group) > 1:
            return group
    return None


def stratified_select(records: list[dict]) -> list[dict]:
    """Spread the 100 picks over every angle so each rule gets exercised."""
    by_code: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for record in records:
        for identity in record["ids"]:
            by_code[(record["brand"].lower(), identity)].append(record)

    multi_colour: list[dict] = []
    multi_tail: list[dict] = []
    no_colour: list[dict] = []
    plain: list[dict] = []
    for record in records:
        family = family_of(record, by_code)
        words = {x["word"] for x in (family or [record]) if x["word"]}
        tails = {
            identity_alpha_tail_safe(identity)[1]
            for x in (family or [record])
            for identity in x["ids"]
            if identity_alpha_tail_safe(identity)[1]
        }
        if family and len(words) > 1:
            multi_colour.append(record)
        elif family and len(tails) > 1:
            multi_tail.append(record)
        elif not record["word"]:
            no_colour.append(record)
        else:
            plain.append(record)

    rng = random.Random(SEED)
    for bucket in (multi_colour, multi_tail, no_colour, plain):
        rng.shuffle(bucket)
    picked: list[dict] = []
    for bucket, quota in ((multi_colour, 40), (multi_tail, 25), (no_colour, 20), (plain, 15)):
        picked.extend(bucket[:quota])
    seen = {r["title"] for r in picked}
    for bucket in (multi_colour, multi_tail, plain, no_colour):
        for record in bucket:
            if len(picked) >= TARGET:
                break
            if record["title"] not in seen:
                seen.add(record["title"])
                picked.append(record)
    return picked[:TARGET]


def identity_alpha_tail_safe(identity: str) -> tuple[str, str]:
    import re
    match = re.match(r"^(.*\d)([a-z]+)$", identity.lower())
    return match.groups() if match else (identity.lower(), "")


def main() -> int:
    records = load_records()
    selected = stratified_select(records)
    by_code: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for record in records:
        for identity in record["ids"]:
            by_code[(record["brand"].lower(), identity)].append(record)

    checks = 0
    failures: list[str] = []
    own_match = own_conflict = 0
    same_colour_control = same_colour_false_reject = 0
    diff_colour_pairs = diff_colour_clean_match = 0

    for record in selected:
        keys, brand, word = record["keys"], record["brand"].lower(), record["word"]

        # Angle 1: the row's own page.
        verdict = match_product(keys=keys, brand=brand, strong_text=record["title"])
        checks += 1
        if record["ids"] and not verdict.matched:
            failures.append(f"OWN page did not match: {record['title'][:70]}")
        if verdict.rule == "colour_conflict":
            own_conflict += 1
            failures.append(f"OWN page flagged colour conflict: {record['title'][:70]}")
        if verdict.matched:
            own_match += 1

        family = family_of(record, by_code)
        if not family:
            continue
        for sibling in family:
            if sibling is record:
                continue
            if word and sibling["word"] == word:
                # Angle 2: same colour sibling - never a false reject.
                same_colour_control += 1
                if match_product(keys=keys, brand=brand,
                                 strong_text=sibling["title"]).rule == "colour_conflict":
                    same_colour_false_reject += 1
                    failures.append(f"FALSE REJECT same colour: {record['title'][:50]} vs {sibling['title'][:50]}")
            elif word and sibling["word"] and colour_word_conflict(word, sibling["title"]):
                # Angle 3: the rule judges these two colours a conflict - the match must
                # not publish them together (it may reject or demote, never clean-match).
                diff_colour_pairs += 1
                v = match_product(keys=keys, brand=brand, strong_text=sibling["title"])
                if v.matched and not v.tolerated and v.rule != "colour_conflict":
                    diff_colour_clean_match += 1
                    failures.append(f"DIFF colour clean match: {record['title'][:50]} vs {sibling['title'][:50]}")

    print("=" * 74)
    print("100-product matching smoke test (offline, shipped code)")
    print("=" * 74)
    print(f"  unique products selected          {len(selected)}")
    print(f"  angle 1 own-page verdicts checked {checks} (matched {own_match})")
    print(f"  angle 2 same-colour controls      {same_colour_control}  false rejects {same_colour_false_reject}")
    print(f"  angle 3 diff-colour pairs         {diff_colour_pairs}  clean matches {diff_colour_clean_match}")
    print(f"  own-page colour conflicts         {own_conflict} (must be 0)")
    print()
    if failures:
        print("FAILURES:")
        for line in failures[:20]:
            print("  -", line)
        print(f"RESULT: {len(failures)} failures - NOT GREEN")
        return 1
    print("RESULT: ALL INVARIANTS HOLD - GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

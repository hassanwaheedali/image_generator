"""Rewrite the owner's marketing titles into the shape this pipeline reads best.

Nothing is invented: the model code, series words and capacity are kept verbatim, the warranty
tail is dropped, and the row's own colourway phrase is moved to the LAST position - because
`build_model_keys` takes the last colour word it sees, and a series name like `9173WB Chrome`
or `9140WB E-Chrome` is finish identity, not the colourway.

Every result is then measured with the real extractor; a row whose extracted colour or code
disagrees with what we expect is listed, never silently adjusted.
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "image_pipeline"))

from model_matching import build_model_keys  # noqa: E402
from sources import search_terms  # noqa: E402
from url_filters import detect_brand  # noqa: E402

INPUT = r"D:\image_generator\_titles_in.txt"
OUT_CSV = Path(r"D:\image_generator\input\refrigerators_50_titles.csv")
OUT_REPORT = Path(r"D:\image_generator\input\refrigerators_50_before_after.csv")

BRANDS = ("dawlance", "orient", "haier", "homage", "kenwood", "lg", "pel", "samsung", "midea")
#: where the colourway phrase stops and the door/type description starts
DOOR_WORDS = (
    "glass door", "steel door", "vcm door", "single door", "french door", "side-by-side",
    "4-door", "multi-door", "no-frost", "texture", "door",
)
WARRANTY_RE = re.compile(r"\d+\s*Years Compressor Warranty|Lifetime Compressor Warranty", re.I)
MARKETING = {"new"}


def split_description(head: str) -> tuple[str, str, str, str]:
    """`brand`, `capacity`, `colourway`, `type words` from the marketing head."""
    brand = head.split(" ", 1)[0]
    match = re.search(r"(\d+(?:\.\d+)? Cu Ft)\s+", head)
    capacity = match.group(1) if match else ""
    rest = head[match.end():] if match else head.split(" ", 1)[1]
    lowered = rest.lower()
    stop = min((lowered.find(w) for w in DOOR_WORDS if w in lowered), default=-1)
    colour = rest[:stop].strip() if stop >= 0 else ""
    kind = rest[stop:].strip() if stop >= 0 else ""
    return brand, capacity, colour, kind


def code_segment(tail: str) -> str:
    """The model code + its series words, verbatim, minus the warranty and pure filler."""
    parts = [p.strip() for p in tail.split(" - ") if p.strip()]
    kept: list[str] = []
    for part in parts:
        if WARRANTY_RE.search(part):
            continue
        kept.append(" ".join(w for w in part.split() if w.lower() not in MARKETING))
    return " ".join(kept)


def rewrite(title: str) -> tuple[str, str, str]:
    head, _, tail = title.partition(" - ")
    brand, capacity, colour, kind = split_description(head.strip())
    code = code_segment(tail)
    type_words = kind if ("Refrigerator" in kind or "Freezer" in kind) else f"{kind} Refrigerator"
    clean = " ".join(p for p in [brand, code, capacity, type_words] if p)
    return (f"{clean} - {colour}" if colour else clean), colour, code


def main() -> int:
    titles = [line.strip() for line in open(INPUT, encoding="utf-8") if line.strip()]
    rows, report, flags, codeless = [], [], [], []
    seen: dict[str, list[str]] = {}
    for title in titles:
        new, colour, code = rewrite(title)
        keys = build_model_keys(new)
        brand = detect_brand("", new)
        queries = search_terms(keys, brand=brand, title=new)
        expected_last = colour.split()[-1].lower() if colour else ""
        got = keys.colour_word.lower()
        rows.append((new, ""))
        report.append(
            (
                title,
                new,
                brand or "?",
                "|".join(keys.tokens),
                got or "-",
                "|".join(keys.name_words),
                "|".join(keys.keywords),
                "|".join(queries),
            )
        )
        kept_series = {w.lower().strip("+") for w in keys.name_words} | {w.lower() for w in keys.keywords}
        lost = [
            w
            for w in code.split()[1:]
            if w.lower().strip("+") not in kept_series and not any(c.isdigit() for c in w)
        ]
        if lost:
            flags.append(f"words the parser did NOT keep as a name word: {lost} <- {new}")
        if not keys.tokens:
            codeless.append((new, "|".join(keys.keywords)))
        if expected_last and expected_last not in got:
            flags.append(f"colour read '{got or '-'}', title ends with '{expected_last}': {new}")
        if code and code.split()[0].lower().replace("-", "") not in "".join(keys.tokens).lower().replace("-", ""):
            flags.append(f"code '{code.split()[0]}' not among tokens {keys.tokens}: {new}")
        if keys.tokens:
            seen.setdefault(keys.tokens[0].lower(), []).append(colour or "(none)")
    OUT_CSV.parent.mkdir(exist_ok=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Title", "Source_url"])
        writer.writerows(rows)
    with open(OUT_REPORT, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["original", "pipeline_title", "brand", "codes", "colour",
             "name_words", "keywords", "search_terms"]
        )
        writer.writerows(report)
    print(f"{len(rows)} rows -> {OUT_CSV.name}\n")
    print("rows with NO model code (searched by keywords instead):")
    for title, keywords in codeless or [("(none)", "")]:
        print(f"  {keywords:38s} {title[:70]}")
    print("\nname words the matcher keeps, per row:")
    for row in report:
        print(f"  {row[4]:14s} | {row[5]:34s} | {row[2][:3]} | {row[1][:62]}")
    shared = {code: c for code, c in seen.items() if len(c) > 1}
    print("rows sharing one code (colour must carry the difference):")
    for code, colours in sorted(shared.items()):
        print(f"  {code}: {', '.join(colours)}")
    print("\nflags:")
    for flag in flags or ["none - every row read its own colour and code"]:
        print("  " + flag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

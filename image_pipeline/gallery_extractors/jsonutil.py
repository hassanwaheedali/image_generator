"""Tolerant JSON scanning used by the JSON-ish extractors.

Deterministic, dependency-free, and auditable: no heuristics beyond balanced
bracket matching and ``json.loads``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

_SCRIPT_RE = re.compile(
    r"<script[^>]*>(?P<body>.*?)</script>", re.IGNORECASE | re.DOTALL
)
_LD_JSON_RE = re.compile(
    r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(?P<body>.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


def safe_json_loads(text: str) -> Any | None:
    """``json.loads`` that returns ``None`` instead of raising on garbage."""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        try:
            return json.loads(text, strict=False)
        except (ValueError, TypeError):
            return None


def iter_script_bodies(html: str) -> Iterator[str]:
    """Every ``<script>`` body, in document order."""
    for match in _SCRIPT_RE.finditer(html):
        body = match.group("body")
        if body and body.strip():
            yield body


def iter_ld_json_blocks(soup: Any) -> Iterator[str]:
    """Raw text of every ``application/ld+json`` block found in the document."""
    for script in soup.find_all("script"):
        script_type = (script.get("type") or "").strip().lower()
        if script_type == "application/ld+json":
            text = script.string or script.get_text() or ""
            if text.strip():
                yield text


def iter_json_objects(text: str) -> Iterator[dict[str, Any]]:
    """Every JSON object that can be decoded from arbitrary text.

    Scans for ``{`` and tries to decode the balanced substring, so a JSON blob
    embedded in JavaScript (Magento ``x-magento-init``, Shopify variants, ...) can
    still be read.  Bounded work: only the first 400 candidates are tried.
    """
    tried = 0
    for start, char in enumerate(text):
        if char != "{":
            continue
        tried += 1
        if tried > 400:
            return
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            current = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : index + 1]
                    if len(candidate) > 40:
                        payload = safe_json_loads(candidate)
                        if isinstance(payload, dict):
                            yield payload
                    break


def walk_json(node: Any) -> Iterator[Any]:
    """Depth-first walk over dicts/lists, yielding every node once."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk_json(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk_json(item)


def string_values(node: Any) -> Iterator[str]:
    """Every string in a JSON subtree (used to read ``image`` fields)."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from string_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from string_values(item)

"""Model-level guarantees: the containers every stage passes results through.

Behaviour that belongs to a dataclass itself (not to a stage that happens to use it) is
tested here, so a generalisation like "two platforms -> N platforms" is pinned by a test
that does not also depend on the whole pipeline running.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from models import AcceptedImage


def _accepted(*finals: tuple[str, str]) -> AcceptedImage:
    return AcceptedImage(
        source_url="https://cdn.test/a.jpg",
        dedup_key="deadbeef",
        raw_path=Path("raw/01-raw.jpg"),
        square_path=Path("isolated/01-square.jpg"),
        final_paths=tuple((key, Path(name)) for key, name in finals),
    )


def test_final_path_finds_each_platform_by_key() -> None:
    image = _accepted(("digi", "d/1.jpg"), ("alfa", "a/1.jpg"), ("website", "w/1.jpg"))
    assert image.final_path("digi") == Path("d/1.jpg")
    assert image.final_path("alfa") == Path("a/1.jpg")
    assert image.final_path("website") == Path("w/1.jpg")


def test_final_path_keeps_the_order_the_platforms_were_resolved_in() -> None:
    """Callers zip a product's images against the platform list, so order is the contract."""
    image = _accepted(("website", "w/1.jpg"), ("alfa", "a/1.jpg"), ("digi", "d/1.jpg"))
    assert [key for key, _ in image.final_paths] == ["website", "alfa", "digi"]


def test_final_path_refuses_a_platform_the_run_did_not_produce() -> None:
    """Silently returning None here would publish a half-filled row instead of failing it."""
    image = _accepted(("digi", "d/1.jpg"))
    with pytest.raises(KeyError, match="website"):
        image.final_path("website")


def test_an_image_can_carry_a_single_platform() -> None:
    """A run filtered to one platform (or a Website-only sheet) is a normal shape, not an error."""
    image = _accepted(("digi", "d/1.jpg"))
    assert image.final_paths == (("digi", Path("d/1.jpg")),)
    assert image.final_path("digi") == Path("d/1.jpg")


def test_acceptedImage_is_hashable_and_comparable() -> None:
    """Frozen and immutable: results get compared, logged and de-duplicated."""
    first = _accepted(("digi", "d/1.jpg"))
    second = _accepted(("digi", "d/1.jpg"))
    assert first == second
    assert len({first, second}) == 1

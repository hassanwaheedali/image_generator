"""Tolerant JSON scanning and HTTP/retry helpers (pure logic, no network)."""

from __future__ import annotations

import contextlib

from fetch import (
    FetchError,
    PermanentAPIError,
    classify_http_status,
    retry_with_backoff,
)
from gallery_extractors.jsonutil import (
    iter_json_objects,
    safe_json_loads,
    string_values,
    walk_json,
)
from image_host import classify_upload_response


# --------------------------------------------------------------------------- #
# JSON helpers
# --------------------------------------------------------------------------- #
def test_safe_json_loads_refuses_garbage() -> None:
    assert safe_json_loads("not json") is None
    assert safe_json_loads('{"a": 1}') == {"a": 1}
    assert safe_json_loads("") is None


def test_iter_json_objects_in_javascript() -> None:
    blob = 'var x = {"title": "Haier HSU-12HFPAA", "images": ["https://cdn.shop/a.jpg"]}; done();'
    found = list(iter_json_objects(blob))
    assert found and found[0]["title"] == "Haier HSU-12HFPAA"


def test_iter_json_objects_ignores_balanced_nonsense() -> None:
    assert list(iter_json_objects("if (a) { return; }")) == []
    assert list(iter_json_objects('{"broken": ')) == []


def test_walk_and_string_values() -> None:
    payload = {"@graph": [{"@type": "Product", "image": {"url": "https://cdn.shop/a.jpg"}}]}
    assert any(isinstance(node, dict) and node.get("@type") == "Product" for node in walk_json(payload))
    assert "https://cdn.shop/a.jpg" in list(string_values(payload))


# --------------------------------------------------------------------------- #
# HTTP classification + retry
# --------------------------------------------------------------------------- #
def test_http_status_classification() -> None:
    assert classify_http_status(200) == "ok"
    assert classify_http_status(301) == "transient"   # redirects are followed by curl_cffi
    assert classify_http_status(404) == "permanent"
    assert classify_http_status(410) == "permanent"
    assert classify_http_status(403) == "permanent"
    assert classify_http_status(429) == "transient"
    assert classify_http_status(500) == "transient"
    assert classify_http_status(503) == "transient"
    assert classify_http_status(418) == "permanent"


def test_retry_backoff_then_success() -> None:
    attempts = {"count": 0}
    slept: list[float] = []

    def flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise FetchError("temporary")
        return "ok"

    result = retry_with_backoff(
        flaky, max_retries=3, base_delay_s=1.0, max_delay_s=8.0,
        description="test", sleeper=slept.append, random_fn=lambda: 0.5,
    )
    assert result == "ok"
    assert attempts["count"] == 3
    assert slept == [1.0, 2.0]


def test_retry_gives_up_and_permanent_is_not_retried() -> None:
    def always_fails() -> str:
        raise FetchError("nope")

    try:
        retry_with_backoff(
            always_fails, max_retries=2, base_delay_s=0.0, max_delay_s=0.0,
            description="test", sleeper=lambda _: None, random_fn=lambda: 0.0,
        )
    except FetchError as exc:
        assert "giving up after 3 attempts" in str(exc)
    else:                                                  # pragma: no cover
        raise AssertionError("expected FetchError")

    calls = {"count": 0}

    def permanent() -> str:
        calls["count"] += 1
        raise PermanentAPIError("invalid API key")

    with contextlib.suppress(PermanentAPIError):     # the retry must re-raise at once
        retry_with_backoff(
            permanent, max_retries=5, base_delay_s=0.0, max_delay_s=0.0,
            description="test", sleeper=lambda _: None, random_fn=lambda: 0.0,
        )
    assert calls["count"] == 1


# --------------------------------------------------------------------------- #
# image host response classification
# --------------------------------------------------------------------------- #
def test_classify_upload_response() -> None:
    ok = {"status_code": 200, "image": {"url": "https://img.example/a.jpg"}}
    assert classify_upload_response(ok) == ("ok", "https://img.example/a.jpg")
    assert classify_upload_response(
        {"status_code": 401, "error": {"message": "Invalid API key"}}
    )[0] == "permanent"
    assert classify_upload_response(
        {"status_code": 400, "error": {"message": "Upload limit reached (quota)"}}
    )[0] == "permanent"
    assert classify_upload_response(
        {"status_code": 503, "error": {"message": "Service temporarily unavailable"}}
    )[0] == "transient"
    assert classify_upload_response({"status_code": 200})[0] == "transient"

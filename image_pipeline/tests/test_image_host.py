"""Unit tests for FreeImageHost, ImgBBHost, APILayerHost, and FallbackImageHost."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from config import PipelineConfig
from fetch import FetchError, PermanentAPIError
from image_host import (
    APILAYER_ENDPOINT,
    APILayerHost,
    FallbackImageHost,
    FreeImageHost,
    ImgBBHost,
    classify_upload_response,
)
from main import _build_image_host


def _fake_response(status_code: int = 200, json_data: Any = None, text: str = "") -> requests.Response:
    resp = requests.Response()
    resp.status_code = status_code
    resp._content = b""
    if json_data is not None:
        import json
        resp._content = json.dumps(json_data).encode("utf-8")
    elif text:
        resp._content = text.encode("utf-8")
    return resp


class DummyHost:
    def __init__(self, name: str, responses: list[Any]) -> None:
        self.name = name
        self.responses = list(responses)
        self.calls: list[str] = []

    def upload(self, data: bytes, filename: str) -> str:
        self.calls.append(filename)
        if not self.responses:
            raise RuntimeError(f"{self.name} out of responses")
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return str(outcome)


# --------------------------------------------------------------------------- #
# APILayerHost tests
# --------------------------------------------------------------------------- #
def test_apilayer_init_requires_key() -> None:
    with pytest.raises(PermanentAPIError, match="APILAYER_API_KEY is not configured"):
        APILayerHost(api_key="")


@patch("requests.post")
def test_apilayer_successful_upload(mock_post: MagicMock) -> None:
    mock_post.return_value = _fake_response(
        status_code=200,
        json_data={
            "success": True,
            "data": {
                "url": "https://cdn.apilayer.com/image_upload/prod1.jpg",
                "file_name": "prod1.jpg",
            },
        },
    )

    host = APILayerHost(api_key="test-api-key", min_interval_s=0.0)
    url = host.upload(b"jpeg-bytes", "prod1.jpg")

    assert url == "https://cdn.apilayer.com/image_upload/prod1.jpg"
    assert mock_post.call_count == 1
    args, kwargs = mock_post.call_args
    assert args[0] == APILAYER_ENDPOINT
    assert kwargs["headers"] == {"apikey": "test-api-key"}
    assert "upload" in kwargs["files"]


@patch("requests.post")
def test_apilayer_successful_upload_alt_json_shapes(mock_post: MagicMock) -> None:
    mock_post.return_value = _fake_response(
        status_code=200,
        json_data={"url": "https://cdn.apilayer.com/direct_url.jpg"},
    )
    host = APILayerHost(api_key="test-key", min_interval_s=0.0)
    assert host.upload(b"data", "test.jpg") == "https://cdn.apilayer.com/direct_url.jpg"

    mock_post.return_value = _fake_response(
        status_code=200,
        json_data={"data": {"image_url": "https://cdn.apilayer.com/img_url.jpg"}},
    )
    assert host.upload(b"data", "test.jpg") == "https://cdn.apilayer.com/img_url.jpg"


@patch("requests.post")
def test_apilayer_missing_url_raises_transient(mock_post: MagicMock) -> None:
    mock_post.return_value = _fake_response(status_code=200, json_data={"data": {}})
    host = APILayerHost(api_key="test-key", min_interval_s=0.0, max_retries=1, base_delay_s=0.0)
    with pytest.raises(FetchError, match=r"HTTP 200 without data\.url"):
        host.upload(b"data", "test.jpg")


@patch("requests.post")
def test_apilayer_permanent_errors(mock_post: MagicMock) -> None:
    host = APILayerHost(api_key="test-key", min_interval_s=0.0, max_retries=2, base_delay_s=0.0)

    # 401 Invalid Key
    mock_post.return_value = _fake_response(
        status_code=401,
        json_data={"message": "No valid API key provided."},
    )
    with pytest.raises(PermanentAPIError, match="No valid API key provided"):
        host.upload(b"data", "test.jpg")

    # 400 Quota exceeded
    mock_post.return_value = _fake_response(
        status_code=400,
        json_data={"message": "You have exceeded your monthly request quota."},
    )
    with pytest.raises(PermanentAPIError, match="quota"):
        host.upload(b"data", "test.jpg")


@patch("requests.post")
def test_apilayer_transient_error_retries_and_fails(mock_post: MagicMock) -> None:
    # 429 rate limit
    mock_post.return_value = _fake_response(status_code=429, text="Rate limit exceeded")
    host = APILayerHost(api_key="test-key", min_interval_s=0.0, max_retries=2, base_delay_s=0.0)
    with pytest.raises(FetchError, match="apilayer upload HTTP 429"):
        host.upload(b"data", "test.jpg")
    assert mock_post.call_count == 3  # initial + 2 retries


# --------------------------------------------------------------------------- #
# FallbackImageHost multi-host tests
# --------------------------------------------------------------------------- #
def test_fallback_primary_success() -> None:
    h1 = DummyHost("h1", ["https://cdn.com/1.jpg"])
    h2 = DummyHost("h2", ["https://cdn.com/2.jpg"])
    fb = FallbackImageHost(primary=h1, fallback=h2)

    res = fb.upload(b"data", "a.jpg")
    assert res == "https://cdn.com/1.jpg"
    assert h1.calls == ["a.jpg"]
    assert h2.calls == []
    assert not fb._use_fallback


def test_fallback_three_host_failover_chain() -> None:
    # Primary: permanent error on first call
    # Secondary: permanent error on first call
    # Tertiary: succeeds!
    h1 = DummyHost("FreeImage", [PermanentAPIError("FreeImage quota exceeded")])
    h2 = DummyHost("ImgBB", [PermanentAPIError("ImgBB key invalid")])
    h3 = DummyHost("APILayer", ["https://cdn.apilayer.com/success1.jpg", "https://cdn.apilayer.com/success2.jpg"])

    fb = FallbackImageHost(hosts=[h1, h2, h3])

    res1 = fb.upload(b"data1", "first.jpg")
    assert res1 == "https://cdn.apilayer.com/success1.jpg"
    assert h1.calls == ["first.jpg"]
    assert h2.calls == ["first.jpg"]
    assert h3.calls == ["first.jpg"]
    assert fb._use_fallback

    # Second upload should go directly to h3 (APILayer) without re-trying failed h1 or h2
    res2 = fb.upload(b"data2", "second.jpg")
    assert res2 == "https://cdn.apilayer.com/success2.jpg"
    assert h1.calls == ["first.jpg"]
    assert h2.calls == ["first.jpg"]
    assert h3.calls == ["first.jpg", "second.jpg"]


def test_fallback_all_fail_permanent_error() -> None:
    h1 = DummyHost("h1", [PermanentAPIError("h1 bad")])
    h2 = DummyHost("h2", [PermanentAPIError("h2 bad")])
    fb = FallbackImageHost(hosts=[h1, h2])

    with pytest.raises(PermanentAPIError, match="h2 bad"):
        fb.upload(b"data", "test.jpg")


def test_fallback_empty_hosts_error() -> None:
    with pytest.raises(ValueError, match="At least one image host must be provided"):
        FallbackImageHost(hosts=[])


def test_classify_upload_response_with_message_field() -> None:
    assert classify_upload_response({"status_code": 401, "message": "Invalid API key"}) == (
        "permanent",
        "Invalid API key",
    )


# --------------------------------------------------------------------------- #
# PipelineConfig and main._build_image_host tests
# --------------------------------------------------------------------------- #
def test_config_validates_apilayer_alone(tmp_path: Any) -> None:
    cfg = PipelineConfig(
        assets_dir=tmp_path / "assets",
        output_dir=tmp_path / "out",
        state_dir=tmp_path / "state",
        dry_run=False,
        api_key="",
        imgbb_api_key="",
        apilayer_api_key="secret-apilayer-key",
    )
    assert cfg.apilayer_api_key == "secret-apilayer-key"


def test_build_image_host_apilayer_alone(tmp_path: Any) -> None:
    cfg = PipelineConfig(
        assets_dir=tmp_path / "assets",
        output_dir=tmp_path / "out",
        state_dir=tmp_path / "state",
        dry_run=False,
        api_key="",
        imgbb_api_key="",
        apilayer_api_key="secret-apilayer-key",
    )
    host = _build_image_host(cfg)
    assert isinstance(host, APILayerHost)


def test_build_image_host_three_tier_fallback(tmp_path: Any) -> None:
    cfg = PipelineConfig(
        assets_dir=tmp_path / "assets",
        output_dir=tmp_path / "out",
        state_dir=tmp_path / "state",
        dry_run=False,
        api_key="k1",
        imgbb_api_key="k2",
        apilayer_api_key="k3",
    )
    host = _build_image_host(cfg)
    assert isinstance(host, FallbackImageHost)
    assert len(host._hosts) == 3
    assert isinstance(host._hosts[0], FreeImageHost)
    assert isinstance(host._hosts[1], ImgBBHost)
    assert isinstance(host._hosts[2], APILayerHost)

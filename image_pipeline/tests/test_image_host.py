"""Unit tests for FreeImageHost, ImgBBHost, APILayerHost, and FallbackImageHost."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from config import ConfigError, PipelineConfig, parse_api_keys
from fetch import FetchError, PermanentAPIError
from image_host import (
    APILAYER_ENDPOINT,
    IMAGEKIT_ENDPOINT,
    APILayerHost,
    FallbackImageHost,
    FreeImageHost,
    ImageKitHost,
    ImgBBHost,
    RotatingImageHost,
    classify_upload_response,
)
from main import _build_image_host


def _fake_response(
    status_code: int = 200,
    json_data: Any = None,
    text: str = "",
    headers: dict[str, str] | None = None,
) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status_code
    resp._content = b""
    if headers:
        resp.headers.update(headers)
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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ()),
        ("   , ,  ", ()),
        ("single", ("single",)),
        ("k1,k2,k3", ("k1", "k2", "k3")),
        (" k1 , k2 , , k1 , k3 ", ("k1", "k2", "k3")),
    ],
)
def test_parse_api_keys(raw: str, expected: tuple[str, ...]) -> None:
    assert parse_api_keys(raw) == expected


def test_config_rejects_comma_only_keys(tmp_path: Any) -> None:
    with pytest.raises(ConfigError, match="FREEIMAGE_API_KEY"):
        PipelineConfig(
            assets_dir=tmp_path / "assets",
            output_dir=tmp_path / "out",
            state_dir=tmp_path / "state",
            dry_run=False,
            api_key=" , ",
            imgbb_api_key=",",
            apilayer_api_key="   ",
        )


def test_config_exposes_parsed_key_tuples(tmp_path: Any) -> None:
    cfg = PipelineConfig(
        assets_dir=tmp_path / "assets",
        output_dir=tmp_path / "out",
        state_dir=tmp_path / "state",
        dry_run=False,
        api_key="f1, f2, f1",
        imgbb_api_key="i1,i2",
        apilayer_api_key="a1",
    )
    assert cfg.api_keys == ("f1", "f2")
    assert cfg.imgbb_api_keys == ("i1", "i2")
    assert cfg.apilayer_api_keys == ("a1",)


# --------------------------------------------------------------------------- #
# APILayerHost quota-exhausted 429 detection
# --------------------------------------------------------------------------- #
@patch("requests.post")
def test_apilayer_quota_429_raises_permanent_without_retrying(mock_post: MagicMock) -> None:
    mock_post.return_value = _fake_response(
        status_code=429,
        json_data={
            "message": (
                "You have exceeded your daily/monthly API rate limit. "
                "Please review and upgrade your subscription plan."
            )
        },
        headers={"X-RateLimit-Remaining-Month": "0", "X-RateLimit-Remaining-Day": "0"},
    )
    host = APILayerHost(api_key="test-key", min_interval_s=0.0, max_retries=3, base_delay_s=0.0)
    with pytest.raises(PermanentAPIError, match="apilayer quota exhausted"):
        host.upload(b"data", "test.jpg")
    assert mock_post.call_count == 1  # aborted immediately, did not waste retries


@patch("requests.post")
def test_apilayer_header_only_quota_429_raises_permanent(mock_post: MagicMock) -> None:
    mock_post.return_value = _fake_response(
        status_code=429,
        text="Too Many Requests",
        headers={"X-RateLimit-Remaining-Day": "0"},
    )
    host = APILayerHost(api_key="test-key", min_interval_s=0.0, max_retries=3, base_delay_s=0.0)
    with pytest.raises(PermanentAPIError, match="X-RateLimit-Remaining-Day=0"):
        host.upload(b"data", "test.jpg")
    assert mock_post.call_count == 1


# --------------------------------------------------------------------------- #
# RotatingImageHost tests
# --------------------------------------------------------------------------- #
def test_rotating_host_empty_raises() -> None:
    with pytest.raises(ValueError, match="At least one image host must be provided"):
        RotatingImageHost([])


def test_rotating_host_round_robin_distribution() -> None:
    h1 = DummyHost("k1", ["https://cdn.com/1a.jpg", "https://cdn.com/1b.jpg"])
    h2 = DummyHost("k2", ["https://cdn.com/2a.jpg", "https://cdn.com/2b.jpg"])
    h3 = DummyHost("k3", ["https://cdn.com/3a.jpg"])
    pool = RotatingImageHost([h1, h2, h3], provider_name="ImgBBHost")

    urls = [pool.upload(b"data", f"f{i}.jpg") for i in range(5)]

    assert urls == [
        "https://cdn.com/1a.jpg",
        "https://cdn.com/2a.jpg",
        "https://cdn.com/3a.jpg",
        "https://cdn.com/1b.jpg",
        "https://cdn.com/2b.jpg",
    ]
    assert pool.display_name == "ImgBBHost (3 keys)"
    assert pool.total_count == 3
    assert pool.active_count == 3


def test_rotating_host_retires_permanently_failed_key_and_retries_upload() -> None:
    h1 = DummyHost("k1", [PermanentAPIError("daily limit reached")])
    h2 = DummyHost("k2", ["https://cdn.com/2a.jpg", "https://cdn.com/2b.jpg"])
    h3 = DummyHost("k3", ["https://cdn.com/3a.jpg"])
    pool = RotatingImageHost([h1, h2, h3], provider_name="FreeImageHost")

    # First upload hits k1 -> permanent error -> immediately succeeds on k2
    assert pool.upload(b"data", "a.jpg") == "https://cdn.com/2a.jpg"
    assert pool.active_count == 2
    assert h1.calls == ["a.jpg"]
    assert h2.calls == ["a.jpg"]

    # Subsequent uploads rotate only across healthy keys k3 -> k2 (k1 never called again)
    assert pool.upload(b"data", "b.jpg") == "https://cdn.com/3a.jpg"
    assert pool.upload(b"data", "c.jpg") == "https://cdn.com/2b.jpg"
    assert h1.calls == ["a.jpg"]


def test_rotating_host_all_keys_exhausted_raises_permanent() -> None:
    h1 = DummyHost("k1", [PermanentAPIError("k1 limit")])
    h2 = DummyHost("k2", [PermanentAPIError("k2 limit")])
    pool = RotatingImageHost([h1, h2], provider_name="ImgBBHost")

    with pytest.raises(PermanentAPIError, match="k2 limit"):
        pool.upload(b"data", "a.jpg")
    assert pool.active_count == 0

    # Subsequent call fails immediately without hitting exhausted hosts again
    with pytest.raises(PermanentAPIError, match="All 2 ImgBBHost key\\(s\\) failed permanently"):
        pool.upload(b"data", "b.jpg")
    assert h1.calls == ["a.jpg"]
    assert h2.calls == ["a.jpg"]


def test_rotating_host_thread_safe_concurrent_retirement() -> None:
    class FailingHost:
        def upload(self, data: bytes, filename: str) -> str:
            raise PermanentAPIError("rate limit reached")

    class HealthyHost:
        def __init__(self, tag: str) -> None:
            self.tag = tag

        def upload(self, data: bytes, filename: str) -> str:
            return f"https://cdn.com/{self.tag}/{filename}"

    pool = RotatingImageHost(
        [FailingHost(), HealthyHost("k2"), HealthyHost("k3")],
        provider_name="ImgBBHost",
    )

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(lambda i: pool.upload(b"data", f"{i}.jpg"), range(30)))

    assert len(results) == 30
    assert pool.active_count == 2
    assert all(url.startswith(("https://cdn.com/k2/", "https://cdn.com/k3/")) for url in results)


def test_rotating_host_inside_fallback_chain() -> None:
    free_pool = RotatingImageHost(
        [
            DummyHost("f1", [PermanentAPIError("f1 daily limit")]),
            DummyHost("f2", [PermanentAPIError("f2 daily limit")]),
        ],
        provider_name="FreeImageHost",
    )
    imgbb_pool = RotatingImageHost(
        [
            DummyHost("i1", ["https://i.ibb.co/1.jpg"]),
            DummyHost("i2", ["https://i.ibb.co/2.jpg"]),
        ],
        provider_name="ImgBBHost",
    )
    fb = FallbackImageHost(hosts=[free_pool, imgbb_pool])

    assert fb.upload(b"data", "a.jpg") == "https://i.ibb.co/1.jpg"
    assert fb.upload(b"data", "b.jpg") == "https://i.ibb.co/2.jpg"
    assert free_pool.active_count == 0
    assert imgbb_pool.active_count == 2


# --------------------------------------------------------------------------- #
# _build_image_host multi-key rotation wiring tests
# --------------------------------------------------------------------------- #
def test_build_image_host_single_provider_multi_keys(tmp_path: Any, caplog: pytest.LogCaptureFixture) -> None:
    cfg = PipelineConfig(
        assets_dir=tmp_path / "assets",
        output_dir=tmp_path / "out",
        state_dir=tmp_path / "state",
        dry_run=False,
        api_key="",
        imgbb_api_key="secret-imgbb-1, secret-imgbb-2, secret-imgbb-3",
        apilayer_api_key="",
    )
    with caplog.at_level("INFO"):
        host = _build_image_host(cfg)

    assert isinstance(host, RotatingImageHost)
    assert host.total_count == 3
    assert host.display_name == "ImgBBHost (3 keys)"
    assert all(isinstance(h, ImgBBHost) for h in host._hosts)
    # Never log secret key values
    assert "secret-imgbb-1" not in caplog.text
    assert "ImgBBHost (3 keys)" in caplog.text


def test_build_image_host_multi_provider_multi_keys_failover(tmp_path: Any) -> None:
    cfg = PipelineConfig(
        assets_dir=tmp_path / "assets",
        output_dir=tmp_path / "out",
        state_dir=tmp_path / "state",
        dry_run=False,
        api_key="f1, f2, f1",
        imgbb_api_key="i1,i2,i3",
        apilayer_api_key="a1",
    )
    host = _build_image_host(cfg)

    assert isinstance(host, FallbackImageHost)
    assert len(host._hosts) == 3

    free_pool, imgbb_pool, apilayer_single = host._hosts
    assert isinstance(free_pool, RotatingImageHost)
    assert free_pool.total_count == 2
    assert all(isinstance(h, FreeImageHost) for h in free_pool._hosts)

    assert isinstance(imgbb_pool, RotatingImageHost)
    assert imgbb_pool.total_count == 3
    assert all(isinstance(h, ImgBBHost) for h in imgbb_pool._hosts)

    assert isinstance(apilayer_single, APILayerHost)


@patch("requests.post")
def test_build_image_host_multi_key_end_to_end_rotation_and_failover(
    mock_post: MagicMock, tmp_path: Any
) -> None:
    # FreeImage key 1 hits daily limit -> rotates to FreeImage key 2 (succeeds) ->
    # then FreeImage key 2 hits daily limit -> fails over to ImgBB key 1 & 2!
    mock_post.side_effect = [
        _fake_response(
            status_code=400,
            json_data={"status_code": 400, "error": {"message": "You have reached the daily upload limit."}},
        ),
        _fake_response(
            status_code=200,
            json_data={"status_code": 200, "image": {"url": "https://iili.io/from-f2.jpg"}},
        ),
        _fake_response(
            status_code=400,
            json_data={"status_code": 400, "error": {"message": "You have reached the daily upload limit."}},
        ),
        _fake_response(
            status_code=200,
            json_data={"success": True, "data": {"url": "https://i.ibb.co/from-i1.jpg"}},
        ),
        _fake_response(
            status_code=200,
            json_data={"success": True, "data": {"url": "https://i.ibb.co/from-i2.jpg"}},
        ),
    ]
    cfg = PipelineConfig(
        assets_dir=tmp_path / "assets",
        output_dir=tmp_path / "out",
        state_dir=tmp_path / "state",
        dry_run=False,
        api_key="free-1,free-2",
        imgbb_api_key="imgbb-1,imgbb-2",
        apilayer_api_key="",
        upload_min_interval_s=0.0,
    )
    host = _build_image_host(cfg)

    assert host.upload(b"jpg", "1.jpg") == "https://iili.io/from-f2.jpg"
    assert host.upload(b"jpg", "2.jpg") == "https://i.ibb.co/from-i1.jpg"
    assert host.upload(b"jpg", "3.jpg") == "https://i.ibb.co/from-i2.jpg"

    used_keys = [
        call.kwargs["data"]["key"]
        for call in mock_post.call_args_list
    ]
    assert used_keys == ["free-1", "free-2", "free-2", "imgbb-1", "imgbb-2"]


# --------------------------------------------------------------------------- #
# ImageKitHost tests
# --------------------------------------------------------------------------- #
def test_imagekit_init_requires_key() -> None:
    with pytest.raises(PermanentAPIError, match="IMAGEKIT_PRIVATE_KEY is not configured"):
        ImageKitHost(api_key="")


@patch("requests.post")
def test_imagekit_successful_upload(mock_post: MagicMock) -> None:
    mock_post.return_value = _fake_response(
        status_code=200,
        json_data={
            "fileId": "abc123",
            "name": "prod1.jpg",
            "url": "https://ik.imagekit.io/b7sce1xgj/products/prod1.jpg",
            "filePath": "/products/prod1.jpg",
        },
    )

    host = ImageKitHost(
        api_key="private_test_key",
        url_endpoint="https://ik.imagekit.io/b7sce1xgj",
        folder="/products",
        min_interval_s=0.0,
    )
    url = host.upload(b"jpeg-bytes", "prod1.jpg")

    assert url == "https://ik.imagekit.io/b7sce1xgj/products/prod1.jpg"
    assert mock_post.call_count == 1
    args, kwargs = mock_post.call_args
    assert args[0] == IMAGEKIT_ENDPOINT
    assert kwargs["auth"] == ("private_test_key", "")
    assert kwargs["data"] == {"fileName": "prod1.jpg", "folder": "/products"}
    assert "file" in kwargs["files"]


@patch("requests.post")
def test_imagekit_url_endpoint_fallback_from_filepath(mock_post: MagicMock) -> None:
    mock_post.return_value = _fake_response(
        status_code=200,
        json_data={"fileId": "abc123", "filePath": "/folder/hero.jpg"},
    )
    host = ImageKitHost(
        api_key="private_key",
        url_endpoint="https://ik.imagekit.io/b7sce1xgj/",
        min_interval_s=0.0,
    )
    assert host.upload(b"data", "hero.jpg") == "https://ik.imagekit.io/b7sce1xgj/folder/hero.jpg"


@patch("requests.post")
def test_imagekit_missing_url_raises_transient(mock_post: MagicMock) -> None:
    mock_post.return_value = _fake_response(status_code=200, json_data={"fileId": "abc123"})
    host = ImageKitHost(api_key="private_key", min_interval_s=0.0, max_retries=1, base_delay_s=0.0)
    with pytest.raises(FetchError, match="imagekit HTTP 200 without url"):
        host.upload(b"data", "test.jpg")


@patch("requests.post")
def test_imagekit_permanent_errors(mock_post: MagicMock) -> None:
    host = ImageKitHost(api_key="private_key", min_interval_s=0.0, max_retries=2, base_delay_s=0.0)

    mock_post.return_value = _fake_response(
        status_code=401,
        json_data={"message": "Your account cannot be authenticated."},
    )
    with pytest.raises(PermanentAPIError, match="cannot be authenticated"):
        host.upload(b"data", "test.jpg")

    mock_post.return_value = _fake_response(
        status_code=429,
        json_data={"message": "Monthly storage quota exceeded."},
    )
    with pytest.raises(PermanentAPIError, match="imagekit quota exhausted"):
        host.upload(b"data", "test.jpg")
    assert mock_post.call_count == 2


@patch("requests.post")
def test_imagekit_transient_429_retries(mock_post: MagicMock) -> None:
    mock_post.return_value = _fake_response(status_code=429, text="Too many requests, slow down")
    host = ImageKitHost(api_key="private_key", min_interval_s=0.0, max_retries=2, base_delay_s=0.0)
    with pytest.raises(FetchError, match="imagekit upload HTTP 429"):
        host.upload(b"data", "test.jpg")
    assert mock_post.call_count == 3


def test_build_image_host_imagekit_alone_and_multi_key(tmp_path: Any) -> None:
    cfg_single = PipelineConfig(
        assets_dir=tmp_path / "assets",
        output_dir=tmp_path / "out",
        state_dir=tmp_path / "state",
        dry_run=False,
        api_key="",
        imgbb_api_key="",
        imagekit_private_key="private_only",
        imagekit_url_endpoint="https://ik.imagekit.io/b7sce1xgj",
        apilayer_api_key="",
    )
    single_host = _build_image_host(cfg_single)
    assert isinstance(single_host, ImageKitHost)

    cfg_multi = PipelineConfig(
        assets_dir=tmp_path / "assets",
        output_dir=tmp_path / "out",
        state_dir=tmp_path / "state",
        dry_run=False,
        api_key="",
        imgbb_api_key="",
        imagekit_private_key="private_1, private_2",
        imagekit_url_endpoint="https://ik.imagekit.io/b7sce1xgj",
        apilayer_api_key="",
    )
    multi_host = _build_image_host(cfg_multi)
    assert isinstance(multi_host, RotatingImageHost)
    assert multi_host.display_name == "ImageKitHost (2 keys)"


def test_build_image_host_four_tier_fallback(tmp_path: Any) -> None:
    cfg = PipelineConfig(
        assets_dir=tmp_path / "assets",
        output_dir=tmp_path / "out",
        state_dir=tmp_path / "state",
        dry_run=False,
        api_key="f1",
        imgbb_api_key="i1",
        imagekit_private_key="ik1",
        apilayer_api_key="a1",
    )
    host = _build_image_host(cfg)
    assert isinstance(host, FallbackImageHost)
    assert len(host._hosts) == 4
    assert isinstance(host._hosts[0], FreeImageHost)
    assert isinstance(host._hosts[1], ImgBBHost)
    assert isinstance(host._hosts[2], ImageKitHost)
    assert isinstance(host._hosts[3], APILayerHost)

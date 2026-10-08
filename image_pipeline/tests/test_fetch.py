"""Fetch-layer configuration: the IPv4-only curl option (curl 28 IPv6 black-hole)."""

from __future__ import annotations

from curl_cffi import CurlOpt

from config import PipelineConfig
from fetch import fetch_curl_options


def test_fetch_curl_options_forces_ipv4_by_default() -> None:
    assert fetch_curl_options(PipelineConfig()) == {CurlOpt.IPRESOLVE: 1}


def test_fetch_curl_options_empty_when_disabled() -> None:
    assert fetch_curl_options(PipelineConfig(fetch_force_ipv4=False)) == {}

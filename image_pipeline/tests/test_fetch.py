"""Fetch-layer configuration: the IPv4-only curl option (curl 28 IPv6 black-hole)."""

from __future__ import annotations

from curl_cffi import CurlOpt

from config import PipelineConfig
from fetch import fetch_curl_options


def test_fetch_curl_options_forces_ipv4_by_default() -> None:
    assert fetch_curl_options(PipelineConfig(dry_run=True)) == {CurlOpt.IPRESOLVE: 1}


def test_fetch_curl_options_empty_when_disabled() -> None:
    assert fetch_curl_options(PipelineConfig(dry_run=True, fetch_force_ipv4=False)) == {}


def test_domain_rate_limiter_allows_independent_domains() -> None:
    import time
    from concurrent.futures import ThreadPoolExecutor

    from fetch import DomainRateLimiter

    limiter = DomainRateLimiter(min_interval_s=0.2)
    # First hit to host A sets last_hit
    limiter.wait("https://host-a.com/page1")

    # While host-a would require a delay, host-b should complete immediately
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        future_b = pool.submit(limiter.wait, "https://host-b.com/page1")
        future_b.result()
    elapsed = time.monotonic() - t0
    # host-b shouldn't wait for host-a's 0.2s interval
    assert elapsed < 0.15


def test_domain_rate_limiter_prunes_expired() -> None:
    import time

    from fetch import DomainRateLimiter

    limiter = DomainRateLimiter(min_interval_s=0.01)
    # Seed 5001 entries that are expired
    now = time.monotonic()
    limiter._last_hit = {f"host-{i}.com": now - 10.0 for i in range(5002)}
    # Calling wait should trigger opportunistic pruning
    limiter.wait("https://new-host.com")
    assert len(limiter._last_hit) < 100


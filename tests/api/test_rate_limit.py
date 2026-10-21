"""Which address the public API's rate limit counts (no database needed)."""

import pytest
from archive_api.middleware import RateLimiter, client_ip, parse_proxies
from starlette.requests import Request


def _request(peer: str, headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "client": (peer, 1234),
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
    )


CLAIMS = {"CF-Connecting-IP": "203.0.113.9", "X-Real-IP": "198.51.100.7"}


def test_every_peer_is_believed_by_default():
    proxies = parse_proxies(["*"])
    assert proxies is None
    assert client_ip(_request("192.0.2.50", CLAIMS), proxies) == "203.0.113.9"
    assert client_ip(_request("192.0.2.50", {"X-Real-IP": "198.51.100.7"}), proxies) == "198.51.100.7"
    assert client_ip(_request("192.0.2.50", {}), proxies) == "192.0.2.50"


def test_only_listed_proxies_name_the_client():
    proxies = parse_proxies(["172.16.0.0/12", "192.0.2.1"])  # conventions:allow-infra
    assert client_ip(_request("172.17.0.1", CLAIMS), proxies) == "203.0.113.9"  # conventions:allow-infra
    assert client_ip(_request("192.0.2.1", {"X-Real-IP": "198.51.100.7"}), proxies) == "198.51.100.7"
    # Straight to the api: whatever the client claims, it is counted as itself.
    assert client_ip(_request("192.0.2.50", CLAIMS), proxies) == "192.0.2.50"
    assert client_ip(_request("192.0.2.50", CLAIMS), parse_proxies([])) == "192.0.2.50"
    with pytest.raises(ValueError):
        parse_proxies(["not-an-address"])


def test_a_client_cannot_spread_itself_over_buckets_when_proxies_are_listed():
    limiter = RateLimiter(points=3, window=60)
    proxies = parse_proxies(["192.0.2.1"])
    results = [
        limiter.hit(client_ip(_request("192.0.2.50", {"CF-Connecting-IP": f"203.0.113.{i}"}), proxies))[0]
        for i in range(5)
    ]
    assert results == [True, True, True, False, False]

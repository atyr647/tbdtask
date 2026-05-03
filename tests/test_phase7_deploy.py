"""Phase 7 deployment-hardening tests.

Covers:
* TrustedProxyMiddleware — strips untrusted forwarded headers
* InProcessLimiter thread safety and window reset
* Secret-key fallback persistence
"""

from __future__ import annotations

import ipaddress
import os
import threading
import time
from unittest.mock import patch


from app.auth.rate_limit import InProcessLimiter


# ---------------------------------------------------------------------------
# Trusted proxy parsing
# ---------------------------------------------------------------------------


class TestTrustedProxyParsing:
    def test_default_trusted_ranges(self):
        """Without env override, only loopback ranges are trusted."""
        from app.middleware import _parse_trusted_proxies

        nets = _parse_trusted_proxies()
        assert any("127.0.0.0" in str(n) for n in nets)
        assert not any("192.168.0.0" in str(n) for n in nets)

    def test_empty_env_trusts_nothing(self):
        from app.middleware import _parse_trusted_proxies

        with patch.dict(os.environ, {"TBDTASK_TRUSTED_PROXIES": ""}, clear=False):
            nets = _parse_trusted_proxies()
            assert nets == []

    def test_custom_cidr(self):
        from app.middleware import _parse_trusted_proxies

        with patch.dict(
            os.environ, {"TBDTASK_TRUSTED_PROXIES": "10.0.0.0/8"}, clear=False
        ):
            nets = _parse_trusted_proxies()
            assert len(nets) == 1
            assert nets[0] == ipaddress.ip_network("10.0.0.0/8")

    def test_multiple_cidrs(self):
        from app.middleware import _parse_trusted_proxies

        with patch.dict(
            os.environ,
            {"TBDTASK_TRUSTED_PROXIES": "10.0.0.0/8, 172.16.0.0/12"},
            clear=False,
        ):
            nets = _parse_trusted_proxies()
            assert len(nets) == 2


# ---------------------------------------------------------------------------
# _is_trusted_proxy
# ---------------------------------------------------------------------------


class TestIsTrustedProxy:
    def _get_networks(self):
        from app.middleware import _parse_trusted_proxies

        return _parse_trusted_proxies()

    def test_loopback_trusted_by_default(self):
        nets = self._get_networks()
        addr = ipaddress.ip_address("127.0.0.1")
        assert any(addr in n for n in nets)

    def test_private_range_not_trusted_by_default(self):
        nets = self._get_networks()
        addr = ipaddress.ip_address("192.168.1.100")
        assert not any(addr in n for n in nets)

    def test_public_ip_not_trusted_by_default(self):
        nets = self._get_networks()
        addr = ipaddress.ip_address("203.0.113.50")
        assert not any(addr in n for n in nets)

    def test_ipv6_loopback(self):
        nets = self._get_networks()
        addr = ipaddress.ip_address("::1")
        assert any(addr in n for n in nets)

    def test_invalid_ip_returns_false(self):
        from app.middleware import _is_trusted_proxy

        assert not _is_trusted_proxy("not-an-ip")


# ---------------------------------------------------------------------------
# _client_ip
# ---------------------------------------------------------------------------


class TestClientIP:
    def _make_request(self, *, client_host="127.0.0.1", headers=None):
        """Create a minimal fake request object for _client_ip."""

        class FakeClient:
            def __init__(self):
                self.host = client_host

        class HeaderProxy:
            def __init__(self, d):
                self._d = d

            def get(self, key, default=None):
                return self._d.get(key, default)

        class FakeRequest:
            def __init__(self):
                self.client = FakeClient()
                self.headers = HeaderProxy(headers or {})

        return FakeRequest()

    def test_direct_connection_no_forwarded(self):
        from app.middleware import _client_ip

        req = self._make_request(client_host="127.0.0.1")
        assert _client_ip(req) == "127.0.0.1"

    def test_trusted_proxy_uses_forwarded(self):
        from app.middleware import _client_ip

        req = self._make_request(
            client_host="127.0.0.1",
            headers={"x-forwarded-for": "203.0.113.50, 10.0.0.1"},
        )
        assert _client_ip(req) == "203.0.113.50"

    def test_untrusted_direct_ignores_forwarded(self):
        """When the direct connection is not a trusted proxy, the
        ``x-forwarded-for`` header must be ignored even if present."""
        from app.middleware import _client_ip

        with patch("app.middleware.TRUSTED_PROXY_NETWORKS", []):
            req = self._make_request(
                client_host="203.0.113.50",
                headers={"x-forwarded-for": "1.2.3.4"},
            )
            assert _client_ip(req) == "203.0.113.50"

    def test_no_client_returns_unknown(self):
        from app.middleware import _client_ip

        class FakeRequest:
            client = None
            headers = type("H", (), {"get": lambda s, k, d=None: d})()

        assert _client_ip(FakeRequest()) == "unknown"


# ---------------------------------------------------------------------------
# InProcessLimiter


class TestInProcessLimiter:
    def test_allows_up_to_limit(self):
        rl = InProcessLimiter(limit=3, window_seconds=60)
        assert rl.check("x", "a")
        assert rl.check("x", "a")
        assert rl.check("x", "a")
        assert not rl.check("x", "a")

    def test_resets_after_window(self):
        rl = InProcessLimiter(limit=2, window_seconds=1)
        assert rl.check("x", "a")
        assert rl.check("x", "a")
        assert not rl.check("x", "a")
        # Wait for window to expire
        time.sleep(1.1)
        assert rl.check("x", "a")

    def test_different_keys_independent(self):
        rl = InProcessLimiter(limit=1, window_seconds=60)
        assert rl.check("x", "a")
        assert not rl.check("x", "a")
        assert rl.check("x", "b")

    def test_reset_clears_all(self):
        rl = InProcessLimiter(limit=1, window_seconds=60)
        rl.check("x", "a")
        rl.check("x", "b")
        rl.reset()
        assert rl.check("x", "a")
        assert rl.check("x", "b")

    def test_thread_safety(self):
        """Concurrent checks against the same key must not exceed the
        limit even under thread contention."""
        rl = InProcessLimiter(limit=100, window_seconds=60)
        allowed = []
        lock = threading.Lock()

        def worker():
            for _ in range(20):
                if rl.check("x", "shared"):
                    with lock:
                        allowed.append(1)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(allowed) == 100


# ---------------------------------------------------------------------------
# Health check endpoint
# ---------------------------------------------------------------------------


class TestHealthz:
    def test_healthz_returns_ok(self):
        """The /healthz endpoint must return 200 with a JSON body. It
        bypasses auth and CSRF so orchestrators can probe liveness."""
        from fastapi.testclient import TestClient
        from app.main import create_app

        client = TestClient(create_app())
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

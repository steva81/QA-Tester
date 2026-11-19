"""Tests for Phase B2b: Isolated Network & Channel Hard Barrier."""

import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright

from app.qa.isolated_execution_context import IsolatedExecutionContextFactory
from app.qa.isolated_network_barrier import (
    BarrierState,
    IsolatedNetworkBarrier,
    IsolatedNetworkBarrierError,
    IsolatedNetworkBarrierSession,
    NetworkBarrierMetrics,
    _fail_closed_cleanup,
)


class BarrierTrackingHandler(BaseHTTPRequestHandler):
    """Local HTTP handler tracking requests with zero persistent secret retention."""

    held_request_arrived: Optional[threading.Event] = None
    held_request_release: Optional[threading.Event] = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @classmethod
    def reset_state(cls):
        cls.held_request_arrived = None
        cls.held_request_release = None
        cls.counts: Dict[str, int] = {
            "get_main": 0,
            "head_main": 0,
            "post_mutation": 0,
            "put_mutation": 0,
            "patch_mutation": 0,
            "delete_mutation": 0,
            "beacon": 0,
            "cross_origin": 0,
            "redirect_same": 0,
            "target_same": 0,
            "redirect_cross": 0,
            "redirect_proto_rel": 0,
            "redirect_unusual": 0,
            "redirect_loop_hops": 0,
            "held_redirect_initial": 0,
            "held_redirect_target": 0,
            "popup_target": 0,
            "iframe_target": 0,
            "sw_script": 0,
            "lockdown_fetch_get": 0,
            "lockdown_fetch_post": 0,
            "lockdown_xhr_get": 0,
            "lockdown_xhr_post": 0,
            "lockdown_beacon": 0,
            "lockdown_image": 0,
            "lockdown_iframe": 0,
            "lockdown_popup": 0,
            "lockdown_top_nav": 0,
            "post_exit_attempt": 0,
            "exception_post_cleanup": 0,
            "cancel_post_cleanup": 0,
        }

    def do_GET(self):
        if self.path == "/":
            self.__class__.counts["get_main"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<!DOCTYPE html><html><body><h1>Barrier Main</h1></body></html>")
        elif self.path == "/redirect-to-cross":
            self.__class__.counts["redirect_cross"] += 1
            cross_url = getattr(self.server, "cross_target_url", "http://127.0.0.1:9/cross")
            self.send_response(302)
            self.send_header("Location", cross_url)
            self.end_headers()
        elif self.path == "/redirect-to-proto-rel":
            self.__class__.counts["redirect_proto_rel"] += 1
            cross_netloc = getattr(self.server, "cross_netloc", "127.0.0.1:9")
            self.send_response(302)
            self.send_header("Location", f"//{cross_netloc}/cross")
            self.end_headers()
        elif self.path == "/redirect-to-unusual":
            self.__class__.counts["redirect_unusual"] += 1
            self.send_response(302)
            self.send_header("Location", "javascript:void(0)")
            self.end_headers()
        elif self.path.startswith("/redirect-loop"):
            self.__class__.counts["redirect_loop_hops"] += 1
            try:
                hop = int(self.path.split("-")[-1])
            except Exception:
                hop = 1
            next_hop = hop + 1
            self.send_response(302)
            self.send_header("Location", f"/redirect-loop-{next_hop}")
            self.end_headers()
        elif self.path == "/redirect-to-same":
            self.__class__.counts["redirect_same"] += 1
            self.send_response(302)
            self.send_header("Location", "/target-same")
            self.end_headers()
        elif self.path == "/target-same":
            self.__class__.counts["target_same"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<!DOCTYPE html><html><body>Target Same</body></html>")
        elif self.path == "/held-redirect":
            self.__class__.counts["held_redirect_initial"] += 1
            if self.__class__.held_request_arrived:
                self.__class__.held_request_arrived.set()
            if self.__class__.held_request_release:
                self.__class__.held_request_release.wait(timeout=5)
            self.send_response(302)
            self.send_header("Location", "/held-target")
            self.end_headers()
        elif self.path == "/held-target":
            self.__class__.counts["held_redirect_target"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<!DOCTYPE html><html><body>Held Target Body</body></html>")
        elif self.path == "/cross":
            self.__class__.counts["cross_origin"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"cross-origin")
        elif self.path == "/popup-target":
            self.__class__.counts["popup_target"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<!DOCTYPE html><html><body>Popup Content</body></html>")
        elif self.path == "/iframe-target":
            self.__class__.counts["iframe_target"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<!DOCTYPE html><html><body>Iframe Content</body></html>")
        elif self.path == "/sw.js":
            self.__class__.counts["sw_script"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.end_headers()
            self.wfile.write(b"self.addEventListener('fetch', e => {});")
        elif self.path == "/lockdown-fetch-get":
            self.__class__.counts["lockdown_fetch_get"] += 1
            self.send_response(200)
            self.end_headers()
        elif self.path == "/lockdown-xhr-get":
            self.__class__.counts["lockdown_xhr_get"] += 1
            self.send_response(200)
            self.end_headers()
        elif self.path == "/lockdown-image":
            self.__class__.counts["lockdown_image"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
        elif self.path == "/lockdown-iframe":
            self.__class__.counts["lockdown_iframe"] += 1
            self.send_response(200)
            self.end_headers()
        elif self.path == "/lockdown-popup":
            self.__class__.counts["lockdown_popup"] += 1
            self.send_response(200)
            self.end_headers()
        elif self.path == "/lockdown-top-nav":
            self.__class__.counts["lockdown_top_nav"] += 1
            self.send_response(200)
            self.end_headers()
        elif self.path == "/post-exit":
            self.__class__.counts["post_exit_attempt"] += 1
            self.send_response(200)
            self.end_headers()
        elif self.path == "/exception-post-cleanup":
            self.__class__.counts["exception_post_cleanup"] += 1
            self.send_response(200)
            self.end_headers()
        elif self.path == "/cancel-post-cleanup":
            self.__class__.counts["cancel_post_cleanup"] += 1
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self):
        if self.path == "/":
            self.__class__.counts["head_main"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/beacon":
            self.__class__.counts["beacon"] += 1
        elif self.path == "/lockdown-fetch-post":
            self.__class__.counts["lockdown_fetch_post"] += 1
        elif self.path == "/lockdown-xhr-post":
            self.__class__.counts["lockdown_xhr_post"] += 1
        elif self.path == "/lockdown-beacon":
            self.__class__.counts["lockdown_beacon"] += 1
        else:
            self.__class__.counts["post_mutation"] += 1
        self.send_response(200)
        self.end_headers()

    def do_PUT(self):
        self.__class__.counts["put_mutation"] += 1
        self.send_response(200)
        self.end_headers()

    def do_PATCH(self):
        self.__class__.counts["patch_mutation"] += 1
        self.send_response(200)
        self.end_headers()

    def do_DELETE(self):
        self.__class__.counts["delete_mutation"] += 1
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass  # Zero logging to prevent secret or URL leakage


class ManagedServer:
    """Helper managing an ephemeral localhost HTTP server."""

    def __init__(self, handler_cls=BarrierTrackingHandler):
        self.server = HTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def test_servers():
    BarrierTrackingHandler.reset_state()
    s1 = ManagedServer()
    s2 = ManagedServer()
    s1.server.cross_target_url = f"{s2.origin}/cross"
    s1.server.cross_netloc = f"127.0.0.1:{s2.port}"
    try:
        yield s1, s2
    finally:
        s1.close()
        s2.close()


# ==============================================================================
# 1. Constructor and Public API Gating
# ==============================================================================

def test_constructor_no_overrides():
    """Verify IsolatedNetworkBarrier rejects constructor overrides and enforces safety defaults."""
    barrier = IsolatedNetworkBarrier()
    assert repr(barrier) == "<IsolatedNetworkBarrier>"
    assert str(barrier) == "IsolatedNetworkBarrier"

    with pytest.raises(TypeError):
        IsolatedNetworkBarrier(route_handler=lambda: None)  # type: ignore

    with pytest.raises(TypeError):
        IsolatedNetworkBarrier(offline_policy=False)  # type: ignore


def test_empty_context_prerequisite():
    """Verify guard fails closed if disposable context already contains pages or is invalid."""
    barrier = IsolatedNetworkBarrier()

    class FakeContextWithPage:
        def __init__(self):
            self.pages = [object()]

        async def route(self, *args): pass
        async def unroute(self, *args): pass
        async def set_offline(self, *args): pass
        async def add_init_script(self, *args): pass

    async def run_test():
        with pytest.raises(IsolatedNetworkBarrierError, match="Context must have zero pages before barrier installation"):
            async with barrier.guard(FakeContextWithPage(), bootstrap_origin="http://127.0.0.1:8080"):
                pass

        with pytest.raises(IsolatedNetworkBarrierError, match="disposable_context must not be None"):
            async with barrier.guard(None, bootstrap_origin="http://127.0.0.1:8080"):
                pass

        class IncompleteContext:
            pages = []
        with pytest.raises(IsolatedNetworkBarrierError, match="disposable_context must provide required Playwright methods"):
            async with barrier.guard(IncompleteContext(), bootstrap_origin="http://127.0.0.1:8080"):
                pass

    asyncio.run(run_test())


def test_invalid_bootstrap_origin_refused():
    """Verify invalid, non-HTTP(S), opaque, or path-bearing origins fail closed."""
    barrier = IsolatedNetworkBarrier()

    class EmptyContext:
        pages = []
        async def route(self, *args): pass
        async def unroute(self, *args): pass
        async def set_offline(self, *args): pass
        async def add_init_script(self, *args): pass

    invalid_origins = [
        "",
        "   ",
        "javascript:void(0)",
        "data:text/html,test",
        "file:///etc/passwd",
        "blob:http://example.com/uuid",
        "about:blank",
        "ftp://example.com",
        "http://example.com/some/path",
        "http://example.com?query=1",
        "http://example.com#fragment",
        "http://@127.0.0.1:8080",  # empty userinfo
        "http://:@127.0.0.1:8080",  # empty user and pass
        "http://user@127.0.0.1:8080",  # user only
        "http://:pass@127.0.0.1:8080",  # pass only
        "http://user:pass@127.0.0.1:8080",  # user credentials
        "https://@127.0.0.1:8443",  # https empty userinfo
        "127.0.0.1:8080",  # missing scheme
    ]

    async def run_test():
        for bad_origin in invalid_origins:
            with pytest.raises(IsolatedNetworkBarrierError):
                async with barrier.guard(EmptyContext(), bootstrap_origin=bad_origin):
                    pass

    asyncio.run(run_test())


def test_ipv6_bootstrap_origin_normalization():
    """Verify IPv6 bootstrap origins preserve bracket notation and normalize correctly."""
    from app.qa.isolated_network_barrier import _normalize_bootstrap_origin, _extract_origin

    assert _normalize_bootstrap_origin("http://[::1]:8080") == "http://[::1]:8080"
    assert _normalize_bootstrap_origin("http://[::1]:80") == "http://[::1]"
    assert _normalize_bootstrap_origin("https://[2001:db8::1]:443") == "https://[2001:db8::1]"
    assert _normalize_bootstrap_origin("https://[2001:db8::1]:8443") == "https://[2001:db8::1]:8443"

    assert _extract_origin("http://[::1]:8080/path?query=1") == "http://[::1]:8080"
    assert _extract_origin("https://[2001:db8::1]:8443/api/v1") == "https://[2001:db8::1]:8443"
    assert _extract_origin("http://[::1]:80/index.html") == "http://[::1]"
    assert _extract_origin("https://[2001:db8::1]:443/test") == "https://[2001:db8::1]"


# ==============================================================================
# 2. Real Chromium Bootstrap HTTP Channel Filtering
# ==============================================================================

@pytest.mark.asyncio
async def test_bootstrap_get_and_head_allowed(test_servers):
    """Verify real Chromium allows same-origin GET and HEAD during bootstrap."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=server1.origin) as session:
            assert session.state == BarrierState.BOOTSTRAP
            page = await ctx.new_page()
            response = await page.goto(f"{server1.origin}/")
            assert response is not None
            assert response.status == 200

            head_res = await page.evaluate(f"""async () => {{
                const r = await fetch('{server1.origin}/', {{ method: 'HEAD' }});
                return r.status;
            }}""")
            assert head_res == 200

        await browser.close()

    assert BarrierTrackingHandler.counts["get_main"] >= 1
    assert BarrierTrackingHandler.counts["head_main"] == 1


@pytest.mark.asyncio
async def test_bootstrap_mutating_methods_blocked(test_servers):
    """Verify real Chromium blocks same-origin POST, PUT, PATCH, DELETE during bootstrap."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=server1.origin) as session:
            page = await ctx.new_page()
            await page.goto(f"{server1.origin}/")

            methods = ["POST", "PUT", "PATCH", "DELETE"]
            for m in methods:
                res = await page.evaluate(f"""async (method) => {{
                    try {{
                        await fetch('{server1.origin}/mutation', {{ method: method, body: 'data' }});
                        return 'allowed';
                    }} catch (e) {{
                        return 'blocked';
                    }}
                }}""", m)
                assert res == "blocked", f"Method {m} should be blocked"

            metrics = session.metrics
            assert metrics.bootstrap_mutations_blocked == 4

        await browser.close()

    assert BarrierTrackingHandler.counts["post_mutation"] == 0
    assert BarrierTrackingHandler.counts["put_mutation"] == 0
    assert BarrierTrackingHandler.counts["patch_mutation"] == 0
    assert BarrierTrackingHandler.counts["delete_mutation"] == 0


@pytest.mark.asyncio
async def test_bootstrap_cross_origin_blocked(test_servers):
    """Verify real Chromium blocks cross-origin GET requests during bootstrap."""
    server1, server2 = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=server1.origin) as session:
            page = await ctx.new_page()
            await page.goto(f"{server1.origin}/")

            res = await page.evaluate(f"""async () => {{
                try {{
                    await fetch('{server2.origin}/cross');
                    return 'allowed';
                }} catch (e) {{
                    return 'blocked';
                }}
            }}""")
            assert res == "blocked"
            assert session.metrics.cross_origin_blocked >= 1

        await browser.close()

    assert BarrierTrackingHandler.counts["cross_origin"] == 0


@pytest.mark.asyncio
async def test_bootstrap_redirect_to_cross_origin_blocked(test_servers):
    """Verify same-origin request redirecting to cross-origin target is aborted before dispatch."""
    server1, server2 = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=server1.origin) as session:
            page = await ctx.new_page()

            with pytest.raises(Exception):
                await page.goto(f"{server1.origin}/redirect-to-cross")

            assert session.metrics.cross_origin_blocked >= 1

        await browser.close()

    assert BarrierTrackingHandler.counts["redirect_cross"] == 1
    assert BarrierTrackingHandler.counts["cross_origin"] == 0


@pytest.mark.asyncio
async def test_bootstrap_redirect_protocol_relative_cross_origin_blocked(test_servers):
    """Verify same-origin redirect with protocol-relative URL to cross-origin is aborted."""
    server1, server2 = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=server1.origin) as session:
            page = await ctx.new_page()

            with pytest.raises(Exception):
                await page.goto(f"{server1.origin}/redirect-to-proto-rel")

            assert session.metrics.cross_origin_blocked >= 1

        await browser.close()

    assert BarrierTrackingHandler.counts["redirect_proto_rel"] == 1
    assert BarrierTrackingHandler.counts["cross_origin"] == 0


@pytest.mark.asyncio
async def test_bootstrap_redirect_unusual_locations_blocked(test_servers):
    """Verify redirects to unusual or non-HTTP schemes (e.g. javascript:) are aborted."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=server1.origin):
            page = await ctx.new_page()

            with pytest.raises(Exception):
                await page.goto(f"{server1.origin}/redirect-to-unusual")

        await browser.close()

    assert BarrierTrackingHandler.counts["redirect_unusual"] == 1


@pytest.mark.asyncio
async def test_bootstrap_redirect_loop_budget_aborted(test_servers):
    """Verify same-origin redirect loops exceeding hop budget (5) are aborted without 302 escape."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=server1.origin):
            page = await ctx.new_page()

            with pytest.raises(Exception):
                await page.goto(f"{server1.origin}/redirect-loop-1")

        await browser.close()

    # The redirect budget terminates and aborts at hop 5; server should have handled at most 5 hops
    assert BarrierTrackingHandler.counts["redirect_loop_hops"] <= 5


@pytest.mark.asyncio
async def test_bootstrap_same_origin_redirect_allowed(test_servers):
    """Verify same-origin redirect within budget is followed and fulfilled under bootstrap."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=server1.origin) as session:
            page = await ctx.new_page()
            resp = await page.goto(f"{server1.origin}/redirect-to-same")
            assert resp is not None
            assert resp.status == 200
            content = await page.content()
            assert "Target Same" in content
            assert session.metrics.bootstrap_allowed >= 1

        await browser.close()

    assert BarrierTrackingHandler.counts["redirect_same"] == 1
    assert BarrierTrackingHandler.counts["target_same"] == 1


@pytest.mark.asyncio
async def test_bootstrap_sendbeacon_blocked(test_servers):
    """Verify navigator.sendBeacon is aborted during bootstrap."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=server1.origin) as session:
            page = await ctx.new_page()
            await page.goto(f"{server1.origin}/")

            sent = await page.evaluate(f"""() => {{
                return navigator.sendBeacon('{server1.origin}/beacon', 'payload');
            }}""")
            # sendBeacon returns true if queued, but network request is intercepted and aborted
            await page.wait_for_timeout(200)

        await browser.close()

    assert BarrierTrackingHandler.counts["beacon"] == 0


# ==============================================================================
# 3. Channel Initialization Blocks (WebSocket, Workers, EventSource)
# ==============================================================================

@pytest.mark.asyncio
async def test_websocket_constructor_blocked(test_servers):
    """Verify window.WebSocket constructor throws a bounded error and is non-writable."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=server1.origin):
            page = await ctx.new_page()
            await page.goto(f"{server1.origin}/")

            outcome = await page.evaluate("""() => {
                const out = {};
                try {
                    new WebSocket('ws://127.0.0.1:8080');
                    out.constructed = true;
                } catch (e) {
                    out.error = e.message;
                }

                // Check immutability
                try {
                    window.WebSocket = 123;
                } catch (e) {}
                out.typeAfterReassign = typeof window.WebSocket;
                return out;
            }""")
            assert "WebSocket creation blocked by IsolatedNetworkBarrier" in outcome.get("error", "")
            assert outcome.get("typeAfterReassign") == "function"

        await browser.close()


@pytest.mark.asyncio
async def test_worker_and_sharedworker_blocked(test_servers):
    """Verify Worker and SharedWorker constructors are blocked from instantiation."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=server1.origin):
            page = await ctx.new_page()
            await page.goto(f"{server1.origin}/")

            outcome = await page.evaluate("""() => {
                const out = {};
                try {
                    new Worker('data:text/javascript,');
                    out.worker = 'created';
                } catch (e) {
                    out.worker = e.message;
                }

                try {
                    if (typeof SharedWorker !== 'undefined') {
                        new SharedWorker('data:text/javascript,');
                        out.sharedWorker = 'created';
                    } else {
                        out.sharedWorker = 'not_present';
                    }
                } catch (e) {
                    out.sharedWorker = e.message;
                }
                return out;
            }""")
            assert "Worker creation blocked by IsolatedNetworkBarrier" in outcome.get("worker", "")
            if outcome.get("sharedWorker") != "not_present":
                assert "SharedWorker creation blocked by IsolatedNetworkBarrier" in outcome.get("sharedWorker", "")

        await browser.close()


@pytest.mark.asyncio
async def test_eventsource_blocked(test_servers):
    """Verify EventSource constructor is blocked from context inception."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=server1.origin):
            page = await ctx.new_page()
            await page.goto(f"{server1.origin}/")

            outcome = await page.evaluate("""() => {
                try {
                    new EventSource('http://127.0.0.1:8080/stream');
                    return 'created';
                } catch (e) {
                    return e.message;
                }
            }""")
            assert "EventSource creation blocked by IsolatedNetworkBarrier" in outcome

        await browser.close()


@pytest.mark.asyncio
async def test_webtransport_blocked(test_servers):
    """Verify WebTransport constructor is blocked when supported."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=server1.origin):
            page = await ctx.new_page()
            await page.goto(f"{server1.origin}/")

            outcome = await page.evaluate("""() => {
                try {
                    new WebTransport('https://127.0.0.1:8080');
                    return 'created';
                } catch (e) {
                    return e.message;
                }
            }""")
            assert "WebTransport creation blocked by IsolatedNetworkBarrier" in outcome

        await browser.close()


@pytest.mark.asyncio
async def test_service_workers_registration_blocked(test_servers):
    """Verify attempted Service Worker registration under B2a service_workers=block does not control."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        # Context configured with B2a standard service_workers="block"
        ctx = await browser.new_context(service_workers="block")

        async with barrier.guard(ctx, bootstrap_origin=server1.origin):
            page = await ctx.new_page()
            await page.goto(f"{server1.origin}/")

            status = await page.evaluate("""async () => {
                const out = {};
                try {
                    await navigator.serviceWorker.register('/sw.js');
                    out.registered = true;
                } catch (e) {
                    out.registerError = e.message;
                }
                out.controller = navigator.serviceWorker.controller ? 'active' : 'null';
                return out;
            }""")
            assert status.get("controller") == "null"

        await browser.close()

    assert BarrierTrackingHandler.counts["sw_script"] == 0


@pytest.mark.asyncio
async def test_constructor_guards_delete_immutability(test_servers):
    """Verify constructor guards cannot be deleted via delete window[prop]."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=server1.origin):
            page = await ctx.new_page()
            await page.goto(f"{server1.origin}/")

            delete_outcome = await page.evaluate("""() => {
                const results = {};
                const names = ['WebSocket', 'Worker', 'SharedWorker', 'EventSource', 'WebTransport'];
                for (const name of names) {
                    try {
                        const deleted = delete window[name];
                        results[name] = { deleted, typeAfter: typeof window[name] };
                    } catch (e) {
                        results[name] = { error: e.message, typeAfter: typeof window[name] };
                    }
                }
                return results;
            }""")
            for name in ['WebSocket', 'Worker', 'SharedWorker', 'EventSource', 'WebTransport']:
                item = delete_outcome.get(name, {})
                assert item.get("typeAfter") == "function", f"{name} should remain a function"
                assert item.get("deleted") is False or "error" in item, f"{name} delete must fail or throw"

        await browser.close()


# ==============================================================================
# 4. Lockdown Transition and Absolute Network Barrier
# ==============================================================================

@pytest.mark.asyncio
async def test_lockdown_transition_and_irreversibility(test_servers):
    """Verify lock_down transitions irreversibly to LOCKED_DOWN and repeated calls are idempotent."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=server1.origin) as session:
            assert session.state == BarrierState.BOOTSTRAP
            await session.lock_down()
            assert session.state == BarrierState.LOCKED_DOWN

            # Idempotent lock_down
            await session.lock_down()
            assert session.state == BarrierState.LOCKED_DOWN

            assert not hasattr(session, "unlock")
            assert not hasattr(session, "resume")
            assert not hasattr(session, "allow_network")

        assert session.state == BarrierState.CLOSED
        with pytest.raises(IsolatedNetworkBarrierError, match="Barrier session is already closed"):
            await session.lock_down()

        await browser.close()


@pytest.mark.asyncio
async def test_deny_all_and_offline_after_lockdown(test_servers):
    """Verify every network attempt is aborted after lockdown with ZERO server-side requests."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=server1.origin) as session:
            page = await ctx.new_page()
            await page.goto(f"{server1.origin}/")

            await session.lock_down()

            # 1. Attempt fetch GET
            res_get = await page.evaluate(f"""async () => {{
                try {{
                    await fetch('{server1.origin}/lockdown-fetch-get');
                    return 'allowed';
                }} catch (e) {{
                    return 'blocked';
                }}
            }}""")
            assert res_get == "blocked"

            # 2. Attempt fetch POST
            res_post = await page.evaluate(f"""async () => {{
                try {{
                    await fetch('{server1.origin}/lockdown-fetch-post', {{ method: 'POST', body: 'x' }});
                    return 'allowed';
                }} catch (e) {{
                    return 'blocked';
                }}
            }}""")
            assert res_post == "blocked"

            # 3. Attempt XHR GET
            res_xhr_get = await page.evaluate(f"""async () => {{
                return new Promise((resolve) => {{
                    const x = new XMLHttpRequest();
                    x.open('GET', '{server1.origin}/lockdown-xhr-get');
                    x.onload = () => resolve('allowed');
                    x.onerror = () => resolve('blocked');
                    x.send();
                }});
            }}""")
            assert res_xhr_get == "blocked"

            # 4. Attempt XHR POST
            res_xhr_post = await page.evaluate(f"""async () => {{
                return new Promise((resolve) => {{
                    const x = new XMLHttpRequest();
                    x.open('POST', '{server1.origin}/lockdown-xhr-post');
                    x.onload = () => resolve('allowed');
                    x.onerror = () => resolve('blocked');
                    x.send('data');
                }});
            }}""")
            assert res_xhr_post == "blocked"

            # 5. Attempt sendBeacon
            await page.evaluate(f"""() => {{
                navigator.sendBeacon('{server1.origin}/lockdown-beacon', 'data');
            }}""")
            await page.wait_for_timeout(200)

            # 6. Attempt image load
            img_res = await page.evaluate(f"""() => {{
                return new Promise((resolve) => {{
                    const img = new Image();
                    img.onload = () => resolve('allowed');
                    img.onerror = () => resolve('blocked');
                    img.src = '{server1.origin}/lockdown-image';
                }});
            }}""")
            assert img_res == "blocked"

            # 7. Attempt iframe navigation post-lockdown
            await page.evaluate(f"""() => {{
                const ifr = document.createElement('iframe');
                ifr.id = 'lockdown-ifr';
                ifr.src = '{server1.origin}/lockdown-iframe';
                document.body.appendChild(ifr);
            }}""")
            await page.wait_for_timeout(200)
            ifr_frame = page.frame(name="lockdown-ifr") or (page.frames[1] if len(page.frames) > 1 else None)
            if ifr_frame:
                content = await ifr_frame.content()
                assert "Iframe Content" not in content

            # 8. Attempt top-level navigation post-lockdown
            with pytest.raises(Exception):
                await page.goto(f"{server1.origin}/lockdown-top-nav", timeout=1000)

            # Assert ZERO server-side requests across ALL 8 independent paths
            assert BarrierTrackingHandler.counts["lockdown_fetch_get"] == 0
            assert BarrierTrackingHandler.counts["lockdown_fetch_post"] == 0
            assert BarrierTrackingHandler.counts["lockdown_xhr_get"] == 0
            assert BarrierTrackingHandler.counts["lockdown_xhr_post"] == 0
            assert BarrierTrackingHandler.counts["lockdown_beacon"] == 0
            assert BarrierTrackingHandler.counts["lockdown_image"] == 0
            assert BarrierTrackingHandler.counts["lockdown_iframe"] == 0
            assert BarrierTrackingHandler.counts["lockdown_top_nav"] == 0

        await browser.close()


@pytest.mark.asyncio
async def test_in_flight_redirect_race_prevented_by_lockdown(test_servers):
    """Adversarial test: hold redirect hop in-flight, trigger lock_down(), release hop,

    and verify zero server-side requests reach the redirect target and zero post-lockdown
    fulfillment occurs.
    """
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    arrived = threading.Event()
    release = threading.Event()
    BarrierTrackingHandler.held_request_arrived = arrived
    BarrierTrackingHandler.held_request_release = release

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
            ctx = await browser.new_context()

            async with barrier.guard(ctx, bootstrap_origin=server1.origin) as session:
                page = await ctx.new_page()
                await page.goto(f"{server1.origin}/")

                # Dispatch fetch to /held-redirect while in BOOTSTRAP
                fetch_task = asyncio.create_task(page.evaluate(f"""async () => {{
                    try {{
                        const r = await fetch('{server1.origin}/held-redirect');
                        return await r.text();
                    }} catch (e) {{
                        return 'blocked:' + e.message;
                    }}
                }}"""))

                # Wait for initial request to arrive at localhost server
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, arrived.wait, 5.0)
                assert arrived.is_set(), "Server should have received initial /held-redirect request"
                assert session.inflight_count == 1

                # Initiate lock_down() while the redirect response is pending in-flight
                lockdown_task = asyncio.create_task(session.lock_down())
                # Yield control to let lock_down() initiate and wait on drain
                await asyncio.sleep(0)
                assert not lockdown_task.done(), "lock_down should be waiting for in-flight requests to drain"
                assert session.state != BarrierState.LOCKED_DOWN, "State must not report LOCKED_DOWN before drain completes"

                # Release the server response (302 Location: /held-target)
                release.set()

                # Both lock_down and fetch_task must complete cleanly
                await lockdown_task
                assert session.state == BarrierState.LOCKED_DOWN
                fetch_result = await fetch_task

                # Boundary check caught in-flight request, aborted it, and prevented hop to target
                assert "blocked" in fetch_result
                assert BarrierTrackingHandler.counts["held_redirect_initial"] == 1
                assert BarrierTrackingHandler.counts["held_redirect_target"] == 0
                assert session.metrics.lockdown_blocked >= 1
                assert session.inflight_count == 0

            await browser.close()
    finally:
        release.set()
        BarrierTrackingHandler.held_request_arrived = None
        BarrierTrackingHandler.held_request_release = None


@pytest.mark.asyncio
async def test_lockdown_concurrent_calls(test_servers):
    """Verify concurrent lock_down invocations synchronize and neither falsely succeeds while in-flight work is pending."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    arrived = threading.Event()
    release = threading.Event()
    BarrierTrackingHandler.held_request_arrived = arrived
    BarrierTrackingHandler.held_request_release = release

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
            ctx = await browser.new_context()

            async with barrier.guard(ctx, bootstrap_origin=server1.origin) as session:
                page = await ctx.new_page()
                await page.goto(f"{server1.origin}/")

                # Trigger in-flight fetch that blocks on server
                fetch_task = asyncio.create_task(page.evaluate(f"""async () => {{
                    try {{
                        const r = await fetch('{server1.origin}/held-redirect');
                        return await r.text();
                    }} catch (e) {{
                        return 'blocked:' + e.message;
                    }}
                }}"""))

                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, arrived.wait, 5.0)
                assert arrived.is_set()
                assert session.inflight_count == 1

                # Launch two concurrent lock_down invocations
                call1 = asyncio.create_task(session.lock_down())
                call2 = asyncio.create_task(session.lock_down())

                # Yield control: neither concurrent invocation should report success while in-flight work is pending
                await asyncio.sleep(0)
                assert not call1.done()
                assert not call2.done()
                assert session.inflight_count == 1
                assert session.state != BarrierState.LOCKED_DOWN

                # Release the server response
                release.set()

                # Both concurrent invocations must complete successfully after drain
                await asyncio.gather(call1, call2)
                await fetch_task

                assert session.state == BarrierState.LOCKED_DOWN
                assert session.inflight_count == 0
                assert BarrierTrackingHandler.counts["held_redirect_target"] == 0

            await browser.close()
    finally:
        release.set()
        BarrierTrackingHandler.held_request_arrived = None
        BarrierTrackingHandler.held_request_release = None


@pytest.mark.asyncio
async def test_lockdown_offline_failure_terminal(test_servers):
    """Verify that when set_offline fails, lock_down fails terminally and subsequent/concurrent calls cannot report false success."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=server1.origin) as session:
            page = await ctx.new_page()
            await page.goto(f"{server1.origin}/")

            # Simulate set_offline failure on the context
            orig_set_offline = ctx.set_offline
            async def failing_set_offline(offline: bool):
                raise RuntimeError("simulated_cdp_offline_failure")

            ctx.set_offline = failing_set_offline

            # First call raises error
            with pytest.raises(IsolatedNetworkBarrierError, match="Failed to enable offline mode"):
                await session.lock_down()

            assert session.state == BarrierState.FAILED
            assert session.state != BarrierState.LOCKED_DOWN

            # A second call (retry) MUST NOT report false success; it must fail terminally
            with pytest.raises(IsolatedNetworkBarrierError, match="Lockdown failed terminally"):
                await session.lock_down()

            assert session.state != BarrierState.LOCKED_DOWN

            ctx.set_offline = orig_set_offline

        await browser.close()


@pytest.mark.asyncio
async def test_lockdown_timeout_terminal_and_retry(test_servers):
    """Verify that when lock_down times out waiting for in-flight requests, concurrent calls and retries fail terminally."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    arrived = threading.Event()
    release = threading.Event()
    BarrierTrackingHandler.held_request_arrived = arrived
    BarrierTrackingHandler.held_request_release = release

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
            ctx = await browser.new_context()

            async with barrier.guard(ctx, bootstrap_origin=server1.origin) as session:
                page = await ctx.new_page()
                await page.goto(f"{server1.origin}/")

                # In-flight fetch held on server
                fetch_task = asyncio.create_task(page.evaluate(f"fetch('{server1.origin}/held-redirect').catch(() => {{}})"))
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, arrived.wait, 5.0)
                assert arrived.is_set()

                # Call lock_down with short timeout while server response is NOT released
                call1 = asyncio.create_task(session.lock_down(timeout=0.1))
                call2 = asyncio.create_task(session.lock_down(timeout=0.1))

                # Both concurrent calls must fail with timeout; neither reports false success
                with pytest.raises(IsolatedNetworkBarrierError, match="Lockdown timed out waiting"):
                    await call1

                with pytest.raises(IsolatedNetworkBarrierError, match="Lockdown failed terminally"):
                    await call2

                assert session.state == BarrierState.FAILED
                assert session.state != BarrierState.LOCKED_DOWN

                # A retry attempt must also fail terminally
                with pytest.raises(IsolatedNetworkBarrierError, match="Lockdown failed terminally"):
                    await session.lock_down()

                # Release held response and verify target never received requests
                release.set()
                await fetch_task
                assert BarrierTrackingHandler.counts["held_redirect_target"] == 0

            await browser.close()
    finally:
        release.set()
        BarrierTrackingHandler.held_request_arrived = None
        BarrierTrackingHandler.held_request_release = None


@pytest.mark.asyncio
async def test_lockdown_cancellation(test_servers):
    """Verify cancellation during lock_down drain preserves fail-closed isolation and causes concurrent/retry calls to fail terminally."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    arrived = threading.Event()
    release = threading.Event()
    BarrierTrackingHandler.held_request_arrived = arrived
    BarrierTrackingHandler.held_request_release = release

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
            ctx = await browser.new_context()

            async with barrier.guard(ctx, bootstrap_origin=server1.origin) as session:
                page = await ctx.new_page()
                await page.goto(f"{server1.origin}/")

                # In-flight fetch held on server
                fetch_task = asyncio.create_task(page.evaluate(f"fetch('{server1.origin}/held-redirect').catch(() => {{}})"))
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, arrived.wait, 5.0)
                assert arrived.is_set()

                # Call 1 is cancelled while waiting on drain
                call1 = asyncio.create_task(session.lock_down(timeout=5.0))
                call2 = asyncio.create_task(session.lock_down(timeout=5.0))

                await asyncio.sleep(0)
                call1.cancel()

                with pytest.raises(asyncio.CancelledError):
                    await call1

                # Call 2 must fail terminally, not falsely succeed
                with pytest.raises(IsolatedNetworkBarrierError, match="Lockdown failed terminally"):
                    await call2

                assert session.state == BarrierState.FAILED

                # Retry fails terminally
                with pytest.raises(IsolatedNetworkBarrierError, match="Lockdown failed terminally"):
                    await session.lock_down()

                # Release held response: zero target requests
                release.set()
                try:
                    await fetch_task
                except Exception:
                    pass
                assert BarrierTrackingHandler.counts["held_redirect_target"] == 0

            await browser.close()
    finally:
        release.set()
        BarrierTrackingHandler.held_request_arrived = None
        BarrierTrackingHandler.held_request_release = None


@pytest.mark.asyncio
async def test_bootstrap_popup_same_origin_and_guards(test_servers):
    """Verify same-origin popup during bootstrap loads and inherits all constructor guards."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=server1.origin) as session:
            page = await ctx.new_page()
            await page.goto(f"{server1.origin}/")

            async with ctx.expect_page() as popup_info:
                await page.evaluate(f"window.open('{server1.origin}/popup-target', '_blank')")
            popup = await popup_info.value
            await popup.wait_for_load_state()
            content = await popup.content()
            assert "Popup Content" in content

            # Verify constructor guards on popup realm
            guards = await popup.evaluate("""() => {
                const out = {};
                try {
                    new WebSocket('ws://127.0.0.1:8080');
                    out.ws = 'created';
                } catch (e) {
                    out.ws = e.message;
                }
                try {
                    new Worker('data:text/javascript,');
                    out.worker = 'created';
                } catch (e) {
                    out.worker = e.message;
                }
                return out;
            }""")
            assert "WebSocket creation blocked by IsolatedNetworkBarrier" in guards.get("ws", "")
            assert "Worker creation blocked by IsolatedNetworkBarrier" in guards.get("worker", "")
            await popup.close()

        await browser.close()

    assert BarrierTrackingHandler.counts["popup_target"] == 1


@pytest.mark.asyncio
async def test_popup_initial_request_blocked(test_servers):
    """Verify popup (window.open) initial request is intercepted and aborted after lockdown."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=server1.origin) as session:
            page = await ctx.new_page()
            await page.goto(f"{server1.origin}/")

            await session.lock_down()

            async with ctx.expect_page(timeout=2000) as popup_info:
                await page.evaluate(f"""() => {{
                    window.open('{server1.origin}/lockdown-popup', '_blank');
                }}""")
            popup = await popup_info.value
            await popup.wait_for_timeout(200)
            assert "lockdown-popup" not in popup.url or popup.url.startswith("chrome-error:")
            await popup.close()

        await browser.close()

    assert BarrierTrackingHandler.counts["lockdown_popup"] == 0


@pytest.mark.asyncio
async def test_child_frame_and_init_script_coverage(test_servers):
    """Verify same-origin child iframes have all constructor guards and context routing applied."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=server1.origin) as session:
            page = await ctx.new_page()
            await page.goto(f"{server1.origin}/")

            # Attach same-origin iframe
            await page.evaluate(f"""() => {{
                const ifr = document.createElement('iframe');
                ifr.id = 'test-iframe';
                ifr.src = '{server1.origin}/iframe-target';
                document.body.appendChild(ifr);
            }}""")
            await page.wait_for_selector("#test-iframe")
            await page.wait_for_timeout(200)
            assert BarrierTrackingHandler.counts["iframe_target"] == 1

            # Check that child frame has ALL constructor blocks active
            child_frame = page.frame(name="test-iframe") or page.frames[1]
            frame_eval = await child_frame.evaluate("""() => {
                const out = {};
                try {
                    new WebSocket('ws://127.0.0.1:8080');
                    out.ws = 'created';
                } catch (e) {
                    out.ws = e.message;
                }

                try {
                    new Worker('data:text/javascript,');
                    out.worker = 'created';
                } catch (e) {
                    out.worker = e.message;
                }

                try {
                    if (typeof SharedWorker !== 'undefined') {
                        new SharedWorker('data:text/javascript,');
                        out.sharedWorker = 'created';
                    } else {
                        out.sharedWorker = 'not_present';
                    }
                } catch (e) {
                    out.sharedWorker = e.message;
                }

                try {
                    new EventSource('http://127.0.0.1:8080/stream');
                    out.eventSource = 'created';
                } catch (e) {
                    out.eventSource = e.message;
                }

                try {
                    new WebTransport('https://127.0.0.1:8080');
                    out.webTransport = 'created';
                } catch (e) {
                    out.webTransport = e.message;
                }

                return out;
            }""")
            assert "WebSocket creation blocked by IsolatedNetworkBarrier" in frame_eval.get("ws", "")
            assert "Worker creation blocked by IsolatedNetworkBarrier" in frame_eval.get("worker", "")
            if frame_eval.get("sharedWorker") != "not_present":
                assert "SharedWorker creation blocked by IsolatedNetworkBarrier" in frame_eval.get("sharedWorker", "")
            assert "EventSource creation blocked by IsolatedNetworkBarrier" in frame_eval.get("eventSource", "")
            assert "WebTransport creation blocked by IsolatedNetworkBarrier" in frame_eval.get("webTransport", "")

            # Lock down and verify child frame network is also blocked
            await session.lock_down()
            post_lock_frame_res = await child_frame.evaluate(f"""async () => {{
                try {{
                    await fetch('{server1.origin}/iframe-target');
                    return 'allowed';
                }} catch (e) {{
                    return 'blocked';
                }}
            }}""")
            assert post_lock_frame_res == "blocked"

        await browser.close()

    assert BarrierTrackingHandler.counts["iframe_target"] == 1


# ==============================================================================
# 5. Fail-Closed Preservation & Adversarial Shutdown / Cancellation / Exception Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_unrelated_context_routes_preserved(test_servers):
    """Verify that entering and exiting B2b guard does not destroy unrelated context routes."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        # Unrelated route registered on a distinct pattern before guard installation
        unrelated_hit = []
        async def unrelated_route_handler(route):
            unrelated_hit.append(route.request.url)
            await route.fulfill(status=200, body="unrelated_mocked")

        await ctx.route("**/custom-telemetry", unrelated_route_handler)
        initial_route_count = len(ctx._impl_obj._routes)
        assert initial_route_count >= 1

        async with barrier.guard(ctx, bootstrap_origin=server1.origin):
            page = await ctx.new_page()
            await page.goto(f"{server1.origin}/")
            assert len(ctx._impl_obj._routes) == initial_route_count + 1

        # After guard exit, verify the unrelated route is still intact and preserved on ctx
        matching = [r for r in ctx._impl_obj._routes if r.matches("http://127.0.0.1/custom-telemetry")]
        assert len(matching) >= 1
        await browser.close()


@pytest.mark.asyncio
async def test_unrelated_catchall_context_route_preserved(test_servers):
    """Verify entering and exiting B2b guard does not remove unrelated '**/*' routes via general unroute."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async def unrelated_catchall_handler(route):
            await route.fulfill(status=200, body="catchall_ok")

        await ctx.route("**/*", unrelated_catchall_handler)
        initial_route_count = len(ctx._impl_obj._routes)
        assert initial_route_count == 1

        async with barrier.guard(ctx, bootstrap_origin=server1.origin):
            page = await ctx.new_page()
            await page.goto(f"{server1.origin}/")
            assert len(ctx._impl_obj._routes) == initial_route_count + 1

        # Because guard cleanup deliberately preserves fail-closed route handlers
        # and does NOT call context.unroute('**/*'), both the barrier handler and
        # the pre-existing unrelated catch-all route remain present on ctx.
        assert len(ctx._impl_obj._routes) == initial_route_count + 1
        matching = [r for r in ctx._impl_obj._routes if r.matches("http://127.0.0.1/any-target")]
        assert len(matching) == 2

        await browser.close()


@pytest.mark.asyncio
async def test_post_guard_network_denied_through_disposal(test_servers):
    """Verify that after guard exit, fail-closed deny-all protection remains active on disposable context."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        page = None
        async with barrier.guard(ctx, bootstrap_origin=server1.origin) as session:
            page = await ctx.new_page()
            await page.goto(f"{server1.origin}/")

        # Guard has exited, but context is not yet disposed.
        # Ensure that no request can escape to the server!
        post_guard_res = await page.evaluate(f"""async () => {{
            try {{
                await fetch('{server1.origin}/post-exit');
                return 'allowed';
            }} catch (e) {{
                return 'blocked';
            }}
        }}""")
        assert post_guard_res == "blocked"
        assert BarrierTrackingHandler.counts["post_exit_attempt"] == 0

        await browser.close()


@pytest.mark.asyncio
async def test_exception_cleanup_preserves_fail_closed_barrier(test_servers):
    """Verify that an exception raised inside the guard block leaves the context offline and blocked."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        page = None
        with pytest.raises(RuntimeError, match="deliberate_test_exception"):
            async with barrier.guard(ctx, bootstrap_origin=server1.origin) as session:
                page = await ctx.new_page()
                await page.goto(f"{server1.origin}/")
                raise RuntimeError("deliberate_test_exception")

        # After exception exit, context remains fail-closed until closed
        post_exc_res = await page.evaluate(f"""async () => {{
            try {{
                await fetch('{server1.origin}/exception-post-cleanup');
                return 'allowed';
            }} catch (e) {{
                return 'blocked';
            }}
        }}""")
        assert post_exc_res == "blocked"
        assert BarrierTrackingHandler.counts["exception_post_cleanup"] == 0

        await browser.close()


@pytest.mark.asyncio
async def test_installation_failure_cleanup_add_init_script(test_servers):
    """Verify that if add_init_script fails during guard setup, the context is fail-closed and disposed."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async def failing_add_init_script(*args, **kwargs):
            raise RuntimeError("simulated_add_init_script_failure")

        ctx.add_init_script = failing_add_init_script

        with pytest.raises(IsolatedNetworkBarrierError, match="Failed to install channel blocking init script"):
            async with barrier.guard(ctx, bootstrap_origin=server1.origin):
                pass

        # Verify actual cleanup behavior: context is closed and cannot create pages or execute requests
        with pytest.raises(Exception):
            await ctx.new_page()

        await browser.close()


@pytest.mark.asyncio
async def test_installation_failure_cleanup_context_route(test_servers):
    """Verify that if context.route fails during guard setup, the context is fail-closed and disposed."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        async def failing_route(*args, **kwargs):
            raise RuntimeError("simulated_route_failure")

        ctx.route = failing_route

        with pytest.raises(IsolatedNetworkBarrierError, match="Failed to install context route handler"):
            async with barrier.guard(ctx, bootstrap_origin=server1.origin):
                pass

        # Verify actual cleanup behavior: context is closed and cannot create pages or execute requests
        with pytest.raises(Exception):
            await ctx.new_page()

        await browser.close()


@pytest.mark.asyncio
async def test_installation_failure_cleanup_prerequisite(test_servers):
    """Verify that if guard prerequisite validation fails, the context is fail-closed and disposed."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        with pytest.raises(IsolatedNetworkBarrierError, match="Context must have zero pages"):
            async with barrier.guard(ctx, bootstrap_origin=server1.origin):
                pass

        # Verify actual cleanup behavior: context and its page are closed and disposed
        assert page.is_closed()
        with pytest.raises(Exception):
            await ctx.new_page()

        await browser.close()


class _SimulatedDisposableContext:
    def __init__(self):
        self.offline = False
        self.closed = False
        self._pages = []
        self.add_init_script_started = asyncio.Event()
        self.add_init_script_block = asyncio.Event()
        self.route_started = asyncio.Event()
        self.route_block = asyncio.Event()
        self.set_offline_started = asyncio.Event()
        self.set_offline_block = asyncio.Event()
        self.set_offline_block.set()
        self.close_started = asyncio.Event()
        self.close_block = asyncio.Event()
        self.close_block.set()
        self.set_offline_should_fail = False

    def pages(self):
        return list(self._pages)

    async def add_init_script(self, script: str):
        self.add_init_script_started.set()
        await self.add_init_script_block.wait()

    async def route(self, pattern: str, handler: Any):
        self.route_started.set()
        await self.route_block.wait()

    async def unroute(self, pattern: str):
        pass

    async def set_offline(self, offline: bool):
        self.set_offline_started.set()
        await self.set_offline_block.wait()
        if self.set_offline_should_fail:
            raise RuntimeError("simulated set_offline device failure")
        self.offline = offline

    async def close(self):
        self.close_started.set()
        await self.close_block.wait()
        self.closed = True


@pytest.mark.asyncio
async def test_installation_cancellation_during_add_init_script_simulated():
    """Verify cancellation during add_init_script triggers fail-closed cleanup and propagates CancelledError."""
    barrier = IsolatedNetworkBarrier()
    ctx = _SimulatedDisposableContext()

    async def worker():
        async with barrier.guard(ctx, bootstrap_origin="http://127.0.0.1:8000"):
            pass

    task = asyncio.create_task(worker())
    await ctx.add_init_script_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Fail-closed cleanup must have run: offline=True and closed=True
    assert ctx.offline is True
    assert ctx.closed is True


@pytest.mark.asyncio
async def test_installation_cancellation_during_context_route_simulated():
    """Verify cancellation during context.route triggers fail-closed cleanup and propagates CancelledError."""
    barrier = IsolatedNetworkBarrier()
    ctx = _SimulatedDisposableContext()
    # Let add_init_script proceed immediately
    ctx.add_init_script_block.set()

    async def worker():
        async with barrier.guard(ctx, bootstrap_origin="http://127.0.0.1:8000"):
            pass

    task = asyncio.create_task(worker())
    await ctx.route_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Fail-closed cleanup must have run: offline=True and closed=True
    assert ctx.offline is True
    assert ctx.closed is True


@pytest.mark.asyncio
async def test_cleanup_cancellation_resilient_when_set_offline_fails():
    """Verify context closure is attempted and succeeds even if setting offline fails during cleanup."""
    barrier = IsolatedNetworkBarrier()
    ctx = _SimulatedDisposableContext()
    ctx.set_offline_should_fail = True

    async def worker():
        async with barrier.guard(ctx, bootstrap_origin="http://127.0.0.1:8000"):
            pass

    task = asyncio.create_task(worker())
    await ctx.add_init_script_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Offline failed, but context closure was still executed and completed
    assert ctx.offline is False
    assert ctx.closed is True


@pytest.mark.asyncio
async def test_fail_closed_cleanup_avoids_false_disposal_reporting():
    """Verify _fail_closed_cleanup truthfully reports closure status and never falsely reports success."""
    from app.qa.isolated_network_barrier import _fail_closed_cleanup

    # 1. None context returns False
    assert await _fail_closed_cleanup(None) is False

    # 2. Context where close() raises an error reports explicit terminal failure
    class BrokenCloseContext:
        async def set_offline(self, val):
            pass

        async def close(self):
            raise RuntimeError("close failed")

    broken_ctx = BrokenCloseContext()
    with pytest.raises(IsolatedNetworkBarrierError, match="verified context-owner disposal required"):
        await _fail_closed_cleanup(broken_ctx)

    # 3. Context where set_offline fails but close succeeds returns True
    class WorkingCloseContext:
        def __init__(self):
            self.closed = False

        async def set_offline(self, val):
            raise RuntimeError("offline failed")

        async def close(self):
            self.closed = True

    working_ctx = WorkingCloseContext()
    assert await _fail_closed_cleanup(working_ctx) is True
    assert working_ctx.closed is True


@pytest.mark.asyncio
async def test_installation_cancellation_during_add_init_script_real_chromium(test_servers):
    """Verify cancellation during add_init_script on real Chromium context triggers closure and propagates CancelledError."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        orig_add = ctx.add_init_script
        started = asyncio.Event()
        block = asyncio.Event()

        async def pausing_add(*args, **kwargs):
            started.set()
            await block.wait()
            return await orig_add(*args, **kwargs)

        ctx.add_init_script = pausing_add

        async def worker():
            async with barrier.guard(ctx, bootstrap_origin=server1.origin):
                pass

        task = asyncio.create_task(worker())
        await started.wait()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Disposable context must be closed: attempting to create a page must fail
        with pytest.raises(Exception):
            await ctx.new_page()

        await browser.close()


@pytest.mark.asyncio
async def test_installation_cancellation_during_context_route_real_chromium(test_servers):
    """Verify cancellation during context.route on real Chromium context triggers closure and propagates CancelledError."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        orig_route = ctx.route
        started = asyncio.Event()
        block = asyncio.Event()

        async def pausing_route(*args, **kwargs):
            started.set()
            await block.wait()
            return await orig_route(*args, **kwargs)

        ctx.route = pausing_route

        async def worker():
            async with barrier.guard(ctx, bootstrap_origin=server1.origin):
                pass

        task = asyncio.create_task(worker())
        await started.wait()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Disposable context must be closed: attempting to create a page must fail
        with pytest.raises(Exception):
            await ctx.new_page()

        await browser.close()


@pytest.mark.asyncio
async def test_repeated_cancellation_during_installation_cleanup_simulated():
    """Verify repeated cancellation during installation cleanup reliably completes closure and propagates CancelledError."""
    barrier = IsolatedNetworkBarrier()
    ctx = _SimulatedDisposableContext()
    ctx.close_block.clear()  # Ensure close pauses until released

    async def worker():
        async with barrier.guard(ctx, bootstrap_origin="http://127.0.0.1:8000"):
            pass

    task = asyncio.create_task(worker())
    await ctx.add_init_script_started.wait()

    # Cancel 1: during add_init_script installation
    task.cancel()
    await ctx.close_started.wait()

    # Cancel 2: while _fail_closed_cleanup is executing context.close()
    task.cancel()
    ctx.close_block.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    # Closure must have been reliably completed
    assert ctx.offline is True
    assert ctx.closed is True


@pytest.mark.asyncio
async def test_cancellation_during_normal_guard_teardown_simulated():
    """Verify cancellation during normal guard teardown propagates CancelledError and enforces fail-closed closure."""
    barrier = IsolatedNetworkBarrier()
    ctx = _SimulatedDisposableContext()
    ctx.add_init_script_block.set()
    ctx.route_block.set()
    ctx.set_offline_block.clear()  # Ensure set_offline in teardown pauses until released

    async def worker():
        async with barrier.guard(ctx, bootstrap_origin="http://127.0.0.1:8000"):
            pass  # Normal body completion

    task = asyncio.create_task(worker())
    await ctx.set_offline_started.wait()

    # Cancel while teardown is executing set_offline(True)
    task.cancel()
    ctx.set_offline_block.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    # Must NOT report normal completion: CancelledError was raised and context was fail-closed
    assert ctx.closed is True


@pytest.mark.asyncio
async def test_repeated_cancellation_during_installation_cleanup_real_chromium(test_servers):
    """Verify repeated cancellation on real Chromium reliably completes context closure and propagates CancelledError."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        orig_add = ctx.add_init_script
        add_started = asyncio.Event()
        async def pausing_add(*args, **kwargs):
            add_started.set()
            await asyncio.Event().wait()
            return await orig_add(*args, **kwargs)
        ctx.add_init_script = pausing_add

        orig_close = ctx.close
        close_started = asyncio.Event()
        close_release = asyncio.Event()
        async def pausing_close(*args, **kwargs):
            close_started.set()
            await close_release.wait()
            return await orig_close(*args, **kwargs)
        ctx.close = pausing_close

        async def worker():
            async with barrier.guard(ctx, bootstrap_origin=server1.origin):
                pass

        task = asyncio.create_task(worker())
        await add_started.wait()

        # Cancel 1: during add_init_script
        task.cancel()
        await close_started.wait()

        # Cancel 2: during context.close()
        task.cancel()
        close_release.set()

        with pytest.raises(asyncio.CancelledError):
            await task

        # Context must be reliably closed: attempting to create a page must fail with TargetClosedError
        with pytest.raises(Exception):
            await ctx.new_page()

        await browser.close()


@pytest.mark.asyncio
async def test_cancellation_during_normal_guard_teardown_real_chromium(test_servers):
    """Verify cancellation during normal guard teardown on real Chromium propagates CancelledError and enforces closure."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        orig_set_offline = ctx.set_offline
        offline_started = asyncio.Event()
        offline_release = asyncio.Event()
        async def pausing_set_offline(val):
            offline_started.set()
            await offline_release.wait()
            return await orig_set_offline(val)
        ctx.set_offline = pausing_set_offline

        async def worker():
            async with barrier.guard(ctx, bootstrap_origin=server1.origin):
                pass  # Normal body completion

        task = asyncio.create_task(worker())
        await offline_started.wait()

        # Cancel during normal teardown set_offline
        task.cancel()
        offline_release.set()

        with pytest.raises(asyncio.CancelledError):
            await task

        # Must be fail-closed: context was closed and cannot create pages
        with pytest.raises(Exception):
            await ctx.new_page()

        await browser.close()


@pytest.mark.asyncio
async def test_stalled_offline_activation_triggers_closure_and_completes_cleanup():
    """Verify that if offline activation stalls indefinitely, cleanup bounds it and still completes context closure."""
    ctx = _SimulatedDisposableContext()
    ctx.set_offline_block.clear()  # Never releases: stalls indefinitely

    from app.qa.isolated_network_barrier import _fail_closed_cleanup
    res = await _fail_closed_cleanup(ctx, timeout=0.05)

    assert res is True
    assert ctx.offline is False  # Offline stalled
    assert ctx.closed is True   # But context closure was attempted and succeeded!


@pytest.mark.asyncio
async def test_stalled_close_reports_explicit_terminal_failure():
    """Verify that if context close stalls indefinitely, cleanup reports explicit terminal failure requiring owner disposal."""
    from app.qa.isolated_network_barrier import _fail_closed_cleanup
    ctx = _SimulatedDisposableContext()
    ctx.close_block.clear()  # Never releases: stalls indefinitely

    with pytest.raises(IsolatedNetworkBarrierError, match="verified context-owner disposal required"):
        await _fail_closed_cleanup(ctx, timeout=0.05)

    assert ctx.closed is False


@pytest.mark.asyncio
async def test_normal_teardown_offline_failure_propagates_error_and_fails_closed_simulated():
    """Verify that when set_offline fails during normal guard teardown, an explicit error is propagated and context is closed."""
    barrier = IsolatedNetworkBarrier()
    ctx = _SimulatedDisposableContext()
    ctx.add_init_script_block.set()
    ctx.route_block.set()
    ctx.set_offline_should_fail = True  # Raises RuntimeError("simulated set_offline device failure")

    async def worker():
        async with barrier.guard(ctx, bootstrap_origin="http://127.0.0.1:8000"):
            pass  # Normal body completion

    with pytest.raises(IsolatedNetworkBarrierError, match="Teardown offline activation failed"):
        await worker()

    # Must have attempted fail-closed disposal
    assert ctx.closed is True


@pytest.mark.asyncio
async def test_normal_teardown_offline_failure_propagates_error_and_fails_closed_real_chromium(test_servers):
    """Verify on real Chromium that normal teardown offline failure propagates an explicit error and closes context."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        orig_set_offline = ctx.set_offline
        async def failing_set_offline(offline: bool):
            raise RuntimeError("simulated_real_cdp_teardown_offline_failure")

        ctx.set_offline = failing_set_offline

        async def worker():
            async with barrier.guard(ctx, bootstrap_origin=server1.origin):
                pass  # Normal body completion

        with pytest.raises(IsolatedNetworkBarrierError, match="Teardown offline activation failed"):
            await worker()

        # Context must be closed
        with pytest.raises(Exception):
            await ctx.new_page()

        await browser.close()


@pytest.mark.asyncio
async def test_cancellation_during_bounded_cleanup_preserves_cancellation_and_completes():
    """Verify that cancellation delivered while bounded cleanup is executing preserves CancelledError and completes closure."""
    from app.qa.isolated_network_barrier import _fail_closed_cleanup
    ctx = _SimulatedDisposableContext()

    started = asyncio.Event()
    orig_close = ctx.close
    async def slow_close():
        started.set()
        await asyncio.sleep(0.05)
        return await orig_close()
    ctx.close = slow_close

    task = asyncio.create_task(_fail_closed_cleanup(ctx, timeout=0.5))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # Context closure must have completed despite caller cancellation
    assert ctx.closed is True


# ==============================================================================
# 6. Composition, Invariance, Cancellation, and Privacy
# ==============================================================================

@pytest.mark.asyncio
async def test_source_context_invariance(test_servers):
    """Verify source context remains completely open, online, and unaffected by barrier lifecycle."""
    server1, _ = test_servers
    factory = IsolatedExecutionContextFactory()
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()
        await source_page.goto(f"{server1.origin}/")

        async with factory.open(source_ctx) as disp_ctx:
            async with barrier.guard(disp_ctx, bootstrap_origin=server1.origin) as session:
                disp_page = await disp_ctx.new_page()
                await disp_page.goto(f"{server1.origin}/")
                await session.lock_down()

        # Verify source context is unaffected
        assert len(source_ctx.pages) == 1
        assert source_page.url == f"{server1.origin}/"
        source_fetch = await source_page.evaluate(f"""async () => {{
            const r = await fetch('{server1.origin}/');
            return r.status;
        }}""")
        assert source_fetch == 200

        await browser.close()


@pytest.mark.asyncio
async def test_b2a_and_b2b_composition(test_servers):
    """Verify seamless end-to-end composition of B2a factory and B2b barrier."""
    server1, _ = test_servers
    factory = IsolatedExecutionContextFactory()
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        source_ctx = await browser.new_context()
        await source_ctx.new_page()

        async with factory.open(source_ctx) as disp_ctx:
            async with barrier.guard(disp_ctx, bootstrap_origin=server1.origin) as session:
                assert session.state == BarrierState.BOOTSTRAP
                disp_page = await disp_ctx.new_page()
                await disp_page.goto(f"{server1.origin}/")
                await session.lock_down()
                assert session.state == BarrierState.LOCKED_DOWN

        await browser.close()


@pytest.mark.asyncio
async def test_cancellation_cleanup(test_servers):
    """Verify asyncio cancellation during barrier lifecycle cleanly closes context without leaks."""
    server1, _ = test_servers
    factory = IsolatedExecutionContextFactory()
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        started_event = asyncio.Event()
        block_forever = asyncio.Event()

        async def worker():
            async with factory.open(source_ctx) as disp_ctx:
                async with barrier.guard(disp_ctx, bootstrap_origin=server1.origin) as session:
                    started_event.set()
                    await block_forever.wait()

        task = asyncio.create_task(worker())
        await started_event.wait()

        assert len(browser.contexts) == 2
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Disposable context must have been closed
        assert len(browser.contexts) == 1
        assert source_ctx in browser.contexts
        assert not source_page.is_closed()

        await browser.close()


@pytest.mark.asyncio
async def test_cancellation_preserves_fail_closed_barrier(test_servers):
    """Verify cancellation inside barrier guard leaves context fail-closed with zero network escape."""
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        ctx = await browser.new_context()

        started_event = asyncio.Event()
        block_forever = asyncio.Event()
        page_ref = []

        async def worker():
            async with barrier.guard(ctx, bootstrap_origin=server1.origin) as session:
                page = await ctx.new_page()
                page_ref.append(page)
                await page.goto(f"{server1.origin}/")
                started_event.set()
                await block_forever.wait()

        task = asyncio.create_task(worker())
        await started_event.wait()

        page = page_ref[0]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Guard exited via cancellation, but ctx is still open. Verify network request fails closed!
        res = await page.evaluate(f"""async () => {{
            try {{
                await fetch('{server1.origin}/cancel-post-cleanup');
                return 'allowed';
            }} catch (e) {{
                return 'blocked';
            }}
        }}""")
        assert res == "blocked"
        assert BarrierTrackingHandler.counts["cancel_post_cleanup"] == 0

        await browser.close()


@pytest.mark.asyncio
async def test_in_flight_cancellation_and_exception_drain(test_servers):
    """Verify that cancellation and exception cleanup while in-flight requests are pending

    drains gracefully, fails closed, and prevents any network request from escaping to target.
    """
    server1, _ = test_servers
    barrier = IsolatedNetworkBarrier()

    arrived = threading.Event()
    release = threading.Event()
    BarrierTrackingHandler.held_request_arrived = arrived
    BarrierTrackingHandler.held_request_release = release

    try:
        # Case A: Cancellation with pending in-flight request
        async with async_playwright() as p:
            browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
            ctx = await browser.new_context()

            started = asyncio.Event()

            async def worker():
                async with barrier.guard(ctx, bootstrap_origin=server1.origin) as session:
                    page = await ctx.new_page()
                    await page.goto(f"{server1.origin}/")
                    started.set()
                    # Trigger in-flight fetch that will block on server
                    await page.evaluate(f"fetch('{server1.origin}/held-redirect').catch(() => {{}})")

            task = asyncio.create_task(worker())
            await started.wait()

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, arrived.wait, 5.0)
            assert arrived.is_set(), "Request must be actively pending on server before cancellation"

            # Cancel while in-flight request is pending on server
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # Only release server response AFTER cancellation has completed to verify post-cancellation isolation
            release.set()

            assert BarrierTrackingHandler.counts["held_redirect_target"] == 0
            await browser.close()

        # Case B: Exception with pending in-flight request
        arrived.clear()
        release.clear()
        async with async_playwright() as p:
            browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
            ctx = await browser.new_context()

            eval_task = None

            async def worker_with_exc():
                nonlocal eval_task
                async with barrier.guard(ctx, bootstrap_origin=server1.origin) as session:
                    page = await ctx.new_page()
                    await page.goto(f"{server1.origin}/")
                    eval_task = asyncio.create_task(page.evaluate(f"fetch('{server1.origin}/held-redirect').catch(() => {{}})"))
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, arrived.wait, 5.0)
                    assert arrived.is_set(), "Request must be actively pending on server before exception"
                    raise RuntimeError("simulated_error_during_inflight")

            task = asyncio.create_task(worker_with_exc())
            with pytest.raises(RuntimeError, match="simulated_error_during_inflight"):
                await task

            # Only release server response AFTER exception cleanup has completed to verify post-exception isolation
            release.set()
            if eval_task is not None:
                try:
                    await eval_task
                except Exception:
                    pass

            assert BarrierTrackingHandler.counts["held_redirect_target"] == 0
            await browser.close()
    finally:
        release.set()
        BarrierTrackingHandler.held_request_arrived = None
        BarrierTrackingHandler.held_request_release = None


def test_privacy_and_no_unbounded_metadata():
    """Verify string representations and metrics omit URLs, cookies, and secrets."""
    barrier = IsolatedNetworkBarrier()
    assert "IsolatedNetworkBarrier" in repr(barrier)
    assert "IsolatedNetworkBarrier" in str(barrier)

    metrics = NetworkBarrierMetrics(
        bootstrap_allowed=1,
        bootstrap_mutations_blocked=2,
        cross_origin_blocked=3,
        lockdown_blocked=4,
    )
    assert metrics.bootstrap_allowed == 1
    assert "password" not in repr(metrics)

    err = IsolatedNetworkBarrierError("Context must have zero pages before barrier installation")
    assert "zero pages" in str(err)


@pytest.mark.asyncio
async def test_cancellation_resistant_offline_does_not_block_cleanup():
    """Verify that a cancellation-resistant set_offline operation cannot block cleanup or prevent context closure."""
    class ResistantOfflineContext:
        def __init__(self):
            self.closed = False
            self.offline_cancelled = False

        async def set_offline(self, val):
            while True:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    self.offline_cancelled = True
                    # Suppress cancellation and continue looping!
                    await asyncio.sleep(10)

        async def close(self):
            self.closed = True

    ctx = ResistantOfflineContext()
    # External strict deadline: must finish well before 0.15s with timeout=0.03
    t0 = asyncio.get_running_loop().time()
    res = await asyncio.wait_for(_fail_closed_cleanup(ctx, timeout=0.03), timeout=0.10)
    elapsed = asyncio.get_running_loop().time() - t0

    assert res is True
    assert ctx.closed is True
    assert ctx.offline_cancelled is True
    assert elapsed < 0.10


@pytest.mark.asyncio
async def test_cancellation_resistant_close_reports_terminal_failure_within_deadline():
    """Verify that a cancellation-resistant close operation cannot block cleanup indefinitely and reports terminal failure."""
    class ResistantCloseContext:
        def __init__(self):
            self.close_cancelled = False

        async def set_offline(self, val):
            pass

        async def close(self):
            while True:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    self.close_cancelled = True
                    # Suppress cancellation and continue looping!
                    await asyncio.sleep(10)

    ctx = ResistantCloseContext()
    t0 = asyncio.get_running_loop().time()
    with pytest.raises(IsolatedNetworkBarrierError, match="verified context-owner disposal required"):
        await asyncio.wait_for(_fail_closed_cleanup(ctx, timeout=0.03), timeout=0.10)
    elapsed = asyncio.get_running_loop().time() - t0

    assert ctx.close_cancelled is True
    assert elapsed < 0.10


@pytest.mark.asyncio
async def test_simultaneous_teardown_offline_and_close_failure_explicit():
    """Verify that when both teardown set_offline and context close fail, the guard raises an explicit error and never claims disposal."""
    barrier = IsolatedNetworkBarrier()

    class BothFailingContext:
        def __init__(self):
            self.closed = False
            self._pages = []

        def pages(self):
            return list(self._pages)

        async def add_init_script(self, script: str):
            pass

        async def route(self, pattern: str, handler: Any):
            pass

        async def unroute(self, pattern: str):
            pass

        async def set_offline(self, val):
            raise RuntimeError("teardown_offline_failure")

        async def close(self):
            raise RuntimeError("teardown_close_failure")

    ctx = BothFailingContext()

    async def run_guard():
        async with barrier.guard(ctx, bootstrap_origin="http://127.0.0.1:8000"):
            pass

    t0 = asyncio.get_running_loop().time()
    with pytest.raises(IsolatedNetworkBarrierError) as exc_info:
        await asyncio.wait_for(run_guard(), timeout=0.5)

    elapsed = asyncio.get_running_loop().time() - t0
    assert elapsed < 0.5

    msg = str(exc_info.value)
    # Must report BOTH failures and require verified owner disposal
    assert "Teardown offline activation failed" in msg
    assert "context closure failed terminally" in msg
    assert "verified context-owner disposal required" in msg
    # Must NEVER falsely claim that the context was disposed
    assert "fail-closed and disposed" not in msg


@pytest.mark.asyncio
async def test_installation_plus_close_failure_preserves_cause_and_reports_disposal_failure():
    """Verify that when installation fails and cleanup close also fails, both errors are explicit and cause is preserved."""
    barrier = IsolatedNetworkBarrier()

    class InstallAndCloseFailingContext:
        def __init__(self):
            self._pages = []

        def pages(self):
            return list(self._pages)

        async def add_init_script(self, script: str):
            raise RuntimeError("simulated_init_script_failure")

        async def route(self, pattern: str, handler: Any):
            pass

        async def unroute(self, pattern: str):
            pass

        async def set_offline(self, val):
            pass

        async def close(self):
            raise RuntimeError("simulated_cleanup_close_failure")

    ctx = InstallAndCloseFailingContext()

    async def run_guard():
        async with barrier.guard(ctx, bootstrap_origin="http://127.0.0.1:8000"):
            pass

    t0 = asyncio.get_running_loop().time()
    with pytest.raises(IsolatedNetworkBarrierError) as exc_info:
        await asyncio.wait_for(run_guard(), timeout=0.5)

    elapsed = asyncio.get_running_loop().time() - t0
    assert elapsed < 0.5

    msg = str(exc_info.value)
    # Must report both installation failure and cleanup closure failure
    assert "Barrier installation failed" in msg
    assert "cleanup closure failed terminally" in msg
    assert "verified context-owner disposal required" in msg
    # Original cause must be preserved as __cause__
    assert exc_info.value.__cause__ is not None
    assert "Failed to install channel blocking init script" in str(exc_info.value.__cause__)


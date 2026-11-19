"""Tests for Phase B2c: State Reconstruction & Exact Revalidation.

Validates that StateReconstructor safely and deterministically reconstructs browser state
within an isolated, disposable BrowserContext (B2a) under an irreversible network/channel
barrier (B2b), and performs exact read-only revalidation (A2/A3/A4/B1) without invoking
any UI action or mutating the source context.
"""

import asyncio
from contextlib import asynccontextmanager
import dataclasses
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from typing import Any, Dict, Optional
from urllib.parse import urlsplit
import pytest
from playwright.async_api import async_playwright

from app.qa.candidate_intent_graph import CandidateActionIntent
from app.qa.candidate_intent_factory import CandidateActionIntentFactory
from app.qa.browser_snapshot_adapter import ReadOnlyBrowserSnapshotAdapter, CancellationToken
from app.qa.semantic_normalizer import SemanticNormalizer
from app.qa.state_fingerprint import CanonicalStateFingerprint
from app.qa.interaction_policy import RiskClassification
from app.qa.isolated_network_barrier import BarrierState
from app.qa.state_reconstruction import (
    ReconstructedStateSession,
    ReconstructionStatus,
    StateReconstructionError,
    StateReconstructionResult,
    StateReconstructor,
    _verify_context_closed,
)


class B2cTrackingHandler(BaseHTTPRequestHandler):
    """Local HTTP handler with deterministic endpoints for Phase B2c testing."""

    counts: Dict[str, int] = {
        "get_dashboard": 0,
        "get_stale": 0,
        "get_disabled": 0,
        "get_interactive": 0,
        "get_secure": 0,
        "get_login": 0,
        "redirect_cross": 0,
        "cross_target": 0,
        "post_action": 0,
        "click_action": 0,
    }

    @classmethod
    def reset_counts(cls):
        for k in cls.counts:
            cls.counts[k] = 0

    def do_GET(self):
        req_path = urlsplit(self.path).path
        if req_path == "/dashboard":
            self.__class__.counts["get_dashboard"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Dashboard</title></head>
<body>
  <h1>User Dashboard</h1>
  <button id="save-btn" aria-label="Save Settings">Save Settings</button>
</body>
</html>""")
        elif req_path == "/dashboard-stale":
            self.__class__.counts["get_stale"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Stale Dashboard</title></head>
<body>
  <h1>Different Layout</h1>
  <button id="submit-btn" aria-label="Submit Form">Submit Form</button>
</body>
</html>""")
        elif req_path == "/dashboard-disabled":
            self.__class__.counts["get_disabled"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Dashboard</title></head>
<body>
  <h1>User Dashboard</h1>
  <button id="save-btn" aria-label="Save Settings" disabled>Save Settings</button>
</body>
</html>""")
        elif req_path == "/interactive-probe":
            self.__class__.counts["get_interactive"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Interactive Probe</title></head>
<body>
  <h1>Interactive Elements</h1>
  <form id="probe-form" onsubmit="window.__submitted = (window.__submitted || 0) + 1; event.preventDefault();">
    <input id="probe-input" type="text" oninput="window.__inputs = (window.__inputs || 0) + 1;" />
    <button id="probe-btn" type="submit" onclick="window.__clicks = (window.__clicks || 0) + 1;">Probe Submit</button>
  </form>
</body>
</html>""")
        elif req_path == "/secure-dashboard":
            self.__class__.counts["get_secure"] += 1
            cookie_header = self.headers.get("Cookie", "")
            if "session_token=secret_val_123" in cookie_header:
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Secure Dashboard</title></head>
<body>
  <h1>Secure Dashboard</h1>
  <button id="secure-btn" aria-label="Logout">Logout</button>
</body>
</html>""")
            else:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
        elif req_path == "/login":
            self.__class__.counts["get_login"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Login</title></head>
<body>
  <h1>Login Required</h1>
  <input type="text" placeholder="Username" />
</body>
</html>""")
        elif req_path == "/redirect-to-cross":
            self.__class__.counts["redirect_cross"] += 1
            cross_url = getattr(self.server, "cross_target_url", "http://127.0.0.1:9/cross")
            self.send_response(302)
            self.send_header("Location", cross_url)
            self.end_headers()
        elif req_path == "/cross-target":
            self.__class__.counts["cross_target"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<!DOCTYPE html><html><body>Cross Target</body></html>")
        elif req_path == "/prefix-sequence":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Prefix Sequence Probe</title></head>
<body>
  <div id="tabs">
    <button id="tab-1" role="tab" aria-selected="true" aria-controls="panel-1">Tab 1</button>
    <button id="tab-2" role="tab" aria-selected="false" aria-controls="panel-2" onclick="this.setAttribute('aria-selected', 'true'); document.getElementById('panel-2').style.display='block';">Tab 2</button>
    <button id="btn-ordinary">Ordinary Safe Button</button>
    <button id="btn-delete" class="danger">Delete Account</button>
    <button id="btn-unrelated-class" role="tab" aria-selected="false" aria-controls="panel-2" onclick="this.className='active-tab';">Unrelated Class Only</button>
    <button id="btn-dangling-panel" role="tab" aria-selected="false" aria-controls="panel-nonexistent" onclick="this.setAttribute('aria-selected', 'true');">Dangling Panel Tab</button>
    <button id="btn-hidden-panel" role="tab" aria-selected="false" aria-controls="panel-hidden" onclick="this.setAttribute('aria-selected', 'true');">Hidden Panel Tab</button>
    <button role="tab" aria-selected="false" aria-controls="panel-autoid" onclick="this.setAttribute('aria-selected', 'true'); document.getElementById('panel-autoid').style.display='block';">Auto ID Tab</button>
    <button id="btn-delayed-ok" role="tab" aria-selected="false" aria-controls="panel-delayed-ok" onclick="setTimeout(() => { this.setAttribute('aria-selected', 'true'); document.getElementById('panel-delayed-ok').style.display='block'; }, 200);">Delayed Legitimate</button>
    <button id="btn-slow" role="tab" aria-selected="false" aria-controls="panel-slow" onclick="setTimeout(() => { this.setAttribute('aria-selected', 'true'); }, 10000);">Slow Tab</button>
  </div>
  <div id="panel-1">Initial Panel 1 Content</div>
  <div id="panel-2" style="display:none;">
    <button id="revealed-action" role="button" aria-expanded="false" onclick="this.setAttribute('aria-expanded', 'true');">Revealed Action</button>
  </div>
  <div id="panel-hidden" style="display:none;">Never Revealed Content</div>
  <div id="panel-autoid" style="display:none;">
    <button id="revealed-autoid-action" role="button" aria-expanded="false">Revealed Auto ID Action</button>
  </div>
  <div id="panel-delayed-ok" style="display:none;">
    <button id="revealed-delayed-action" role="button" aria-expanded="false">Revealed Delayed Action</button>
  </div>
  <div id="panel-slow" style="display:none;">Slow Panel</div>
  <iframe id="sub-frame" src="/frame-content"></iframe>
</body>
</html>""")
        elif req_path == "/frame-content":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html><html><body><button id="frame-tab" role="tab" aria-selected="false">Frame Tab</button></body></html>""")
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        self.__class__.counts["post_action"] += 1
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass


class ManagedServer:
    def __init__(self, handler_cls=B2cTrackingHandler):
        self.server = HTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def b2c_servers():
    B2cTrackingHandler.reset_counts()
    s1 = ManagedServer()
    s2 = ManagedServer()
    s1.server.cross_target_url = f"{s2.origin}/cross-target"
    try:
        yield s1, s2
    finally:
        s1.close()
        s2.close()


async def _derive_intent_from_page(page: Any, target_control_name: str) -> CandidateActionIntent:
    """Helper to derive real CandidateActionIntent directly from active page."""
    adapter = ReadOnlyBrowserSnapshotAdapter()
    normalizer = SemanticNormalizer()
    factory = CandidateActionIntentFactory()

    contract = {
        "qa_owned": True,
        "manual_visible_page": False,
        "expected_context": page.context,
    }
    snapshot = await adapter.capture_snapshot(page, contract)
    assert snapshot.coverage.terminal_status == "COMPLETE"
    norm = normalizer.normalize_snapshot(snapshot)
    assert norm.terminal_status == "COMPLETE"
    derivation = factory.derive(norm)
    assert derivation.terminal_status == "COMPLETE"

    for intent in derivation.intents:
        if target_control_name in intent.control_fingerprint or (
            hasattr(intent, "accessible_name") and intent.accessible_name == target_control_name
        ):
            return intent

    # Fallback to first available intent
    assert len(derivation.intents) > 0, "No intents derived from test page"
    return derivation.intents[0]


# ==============================================================================
# Tests for Phase B2c
# ==============================================================================

@pytest.mark.asyncio
async def test_state_reconstruction_success_real_chromium(b2c_servers):
    """Verify B2c successfully reconstructs state, locks down, and revalidates intent in disposable context."""
    s1, _ = b2c_servers
    reconstructor = StateReconstructor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        # Establish authenticated source state
        target_url = f"{s1.origin}/dashboard"
        await source_page.goto(target_url)

        # Derive genuine CandidateActionIntent from source state
        intent = await _derive_intent_from_page(source_page, "Save Settings")
        assert intent is not None
        assert intent.expected_action_type == "click"

        # Execute B2c reconstruction
        async with reconstructor.reconstruct(source_ctx, target_url, intent) as session:
            assert session.is_revalidated is True
            assert session.is_disposed is False
            assert session.result.status == ReconstructionStatus.REVALIDATED
            assert session.result.is_revalidated is True
            assert session.result.parent_state_fingerprint == intent.parent_state_fingerprint
            assert session.barrier_session.state == BarrierState.LOCKED_DOWN

            # Page is accessible inside context manager
            disposable_page = session.page
            assert disposable_page.url == target_url
            assert not disposable_page.is_closed()
            assert disposable_page.context != source_ctx

        # After block exit, session is disposed and cannot access page or context
        assert session.is_disposed is True
        with pytest.raises(StateReconstructionError, match="Cannot access page on disposed session"):
            _ = session.page
        with pytest.raises(StateReconstructionError, match="Cannot access context on disposed session"):
            _ = session.disposable_context

        # Verify source context remained completely untouched
        assert len(source_ctx.pages) == 1
        assert source_page.url == target_url
        assert not source_page.is_closed()

        await browser.close()


@pytest.mark.asyncio
async def test_state_reconstruction_stale_state_mismatch(b2c_servers):
    """Verify B2c fails closed with STALE_STATE when reconstructed state does not match intent parent fingerprint."""
    s1, _ = b2c_servers
    reconstructor = StateReconstructor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        # Derive intent from original dashboard
        await source_page.goto(f"{s1.origin}/dashboard")
        intent = await _derive_intent_from_page(source_page, "Save Settings")

        # Now attempt reconstruction against a changed/stale dashboard URL
        stale_target_url = f"{s1.origin}/dashboard-stale"
        result = await reconstructor.revalidate(source_ctx, stale_target_url, intent)

        assert result.is_revalidated is False
        assert result.status == ReconstructionStatus.STALE_STATE
        assert result.parent_state_fingerprint != intent.parent_state_fingerprint
        assert result.is_execution_control is True

        await browser.close()


@pytest.mark.asyncio
async def test_state_reconstruction_intent_not_present(b2c_servers):
    """Verify B2c fails closed with INTENT_NOT_PRESENT when target control is disabled or missing on reconstructed page."""
    s1, _ = b2c_servers
    reconstructor = StateReconstructor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        # Derive intent from active dashboard
        await source_page.goto(f"{s1.origin}/dashboard")
        intent = await _derive_intent_from_page(source_page, "Save Settings")

        # Reconstruct on page where button is disabled (A4 will not derive click intent on disabled button)
        disabled_url = f"{s1.origin}/dashboard-disabled"
        result = await reconstructor.revalidate(source_ctx, disabled_url, intent)

        assert result.is_revalidated is False
        # Disabled control either changes fingerprint or omits intent from derived candidates
        assert result.status in (ReconstructionStatus.INTENT_NOT_PRESENT, ReconstructionStatus.STALE_STATE)

        await browser.close()


@pytest.mark.asyncio
async def test_state_reconstruction_proves_no_interaction_actions(b2c_servers):
    """Verify B2c performs ZERO clicks, form submissions, or inputs during reconstruction and revalidation."""
    s1, _ = b2c_servers
    reconstructor = StateReconstructor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{s1.origin}/interactive-probe"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "Probe Submit")

        async with reconstructor.reconstruct(source_ctx, target_url, intent) as session:
            assert session.is_revalidated is True
            disp_page = session.page

            # Inspect DOM interaction flags on disposable page
            probe_results = await disp_page.evaluate("""() => ({
                clicks: window.__clicks || 0,
                submitted: window.__submitted || 0,
                inputs: window.__inputs || 0
            })""")

            assert probe_results["clicks"] == 0
            assert probe_results["submitted"] == 0
            assert probe_results["inputs"] == 0

        # Server-side mutation handlers must also have 0 records
        assert B2cTrackingHandler.counts["post_action"] == 0
        assert B2cTrackingHandler.counts["click_action"] == 0

        await browser.close()


@pytest.mark.asyncio
async def test_state_reconstruction_barrier_installed_before_page_creation(b2c_servers):
    """Verify B2b barrier is installed before page creation and channel constructors are blocked immediately."""
    s1, _ = b2c_servers
    reconstructor = StateReconstructor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{s1.origin}/dashboard"
        await source_page.goto(target_url)
        intent = await _derive_intent_from_page(source_page, "Save Settings")

        async with reconstructor.reconstruct(source_ctx, target_url, intent) as session:
            assert session.is_revalidated is True
            page = session.page

            # Verify all communication channels are disabled by init script
            channels_blocked = await page.evaluate("""() => {
                const results = {};
                for (const name of ['WebSocket', 'Worker', 'SharedWorker', 'EventSource', 'WebTransport']) {
                    try {
                        if (name === 'WebSocket') new window.WebSocket('wss://127.0.0.1:8080');
                        else if (name === 'Worker') new window.Worker('data:text/javascript,');
                        else if (name === 'SharedWorker') new window.SharedWorker('data:text/javascript,');
                        else if (name === 'EventSource') new window.EventSource('http://127.0.0.1:8080');
                        else if (name === 'WebTransport') new window.WebTransport('https://127.0.0.1:8080');
                        results[name] = false;
                    } catch (e) {
                        results[name] = e.message.includes('blocked by IsolatedNetworkBarrier');
                    }
                }
                return results;
            }""")

            assert channels_blocked["WebSocket"] is True
            assert channels_blocked["Worker"] is True
            assert channels_blocked["SharedWorker"] is True
            assert channels_blocked["EventSource"] is True
            assert channels_blocked["WebTransport"] is True

        await browser.close()


@pytest.mark.asyncio
async def test_state_reconstruction_url_authorization_rejection():
    """Verify B2c fails closed without navigation when target URL is unapproved or malformed."""
    reconstructor = StateReconstructor()

    class DummyContext:
        pass

    ctx = DummyContext()
    intent = CandidateActionIntent(
        parent_state_fingerprint="fp1",
        control_fingerprint="ctrl1",
        expected_action_type="click",
        risk_classification="SAFE_CANDIDATE",
    )

    invalid_urls = [
        "",
        "   ",
        "ftp://127.0.0.1/path",
        "http://user:pass@127.0.0.1:8080/path",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "http://",
    ]

    for bad_url in invalid_urls:
        res = await reconstructor.revalidate(ctx, bad_url, intent)
        assert res.is_revalidated is False
        assert res.status == ReconstructionStatus.URL_NOT_APPROVED
        assert res.is_execution_control is True


@pytest.mark.asyncio
async def test_state_reconstruction_cross_origin_redirect_blocked(b2c_servers):
    """Verify B2c fails closed when navigation attempts cross-origin redirect, and target receives zero requests."""
    s1, s2 = b2c_servers
    reconstructor = StateReconstructor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        await source_page.goto(f"{s1.origin}/dashboard")
        intent = await _derive_intent_from_page(source_page, "Save Settings")

        # Attempt navigation to an endpoint that redirects to cross-origin server 2
        redirect_url = f"{s1.origin}/redirect-to-cross"
        result = await reconstructor.revalidate(source_ctx, redirect_url, intent)

        assert result.is_revalidated is False
        assert result.status in (ReconstructionStatus.CROSS_ORIGIN_BLOCKED, ReconstructionStatus.BOOTSTRAP_FAILED)
        assert B2cTrackingHandler.counts["cross_target"] == 0

        await browser.close()


@pytest.mark.asyncio
async def test_state_reconstruction_auth_loss_redirect_detected(b2c_servers):
    """Verify B2c detects authentication loss when navigation redirects to login interface."""
    s1, _ = b2c_servers
    reconstructor = StateReconstructor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()

        # Set valid cookie in source context
        await source_ctx.add_cookies([{
            "name": "session_token",
            "value": "secret_val_123",
            "domain": "127.0.0.1",
            "path": "/",
        }])

        source_page = await source_ctx.new_page()
        secure_url = f"{s1.origin}/secure-dashboard"
        await source_page.goto(secure_url)

        intent = await _derive_intent_from_page(source_page, "Logout")

        # Now create an unauthenticated source context without the cookie
        unauth_source_ctx = await browser.new_context()

        # Reconstruct from unauthenticated context -> server redirects to /login
        result = await reconstructor.revalidate(unauth_source_ctx, secure_url, intent)

        assert result.is_revalidated is False
        assert result.status in (ReconstructionStatus.AUTH_LOSS, ReconstructionStatus.STALE_STATE)

        await browser.close()


@pytest.mark.asyncio
async def test_state_reconstruction_source_context_invariance(b2c_servers):
    """Verify that source context cookies, active pages, and online state remain unchanged after B2c."""
    s1, _ = b2c_servers
    reconstructor = StateReconstructor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()

        await source_ctx.add_cookies([{
            "name": "auth_cookie",
            "value": "val_source_1",
            "domain": "127.0.0.1",
            "path": "/",
        }])

        source_page = await source_ctx.new_page()
        target_url = f"{s1.origin}/dashboard"
        await source_page.goto(target_url)

        intent = await _derive_intent_from_page(source_page, "Save Settings")

        # Execute B2c
        result = await reconstructor.revalidate(source_ctx, target_url, intent)
        assert result.is_revalidated is True

        # Source context invariants:
        # 1. Cookies untouched
        cookies = await source_ctx.cookies()
        assert len(cookies) == 1
        assert cookies[0]["name"] == "auth_cookie"
        assert cookies[0]["value"] == "val_source_1"

        # 2. Source context is NOT offline (can still navigate)
        await source_page.goto(f"{s1.origin}/dashboard")
        assert source_page.url == target_url

        # 3. Exactly one page in source context
        assert len(source_ctx.pages) == 1

        await browser.close()


@pytest.mark.asyncio
async def test_state_reconstruction_failure_and_cancellation_cleanup(b2c_servers):
    """Verify cancellation during reconstruction triggers complete cleanup and propagates CancelledError with explicit sync."""
    s1, _ = b2c_servers
    reconstructor = StateReconstructor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{s1.origin}/dashboard"
        await source_page.goto(target_url)
        intent = await _derive_intent_from_page(source_page, "Save Settings")

        # Explicit synchronization event before cancelling
        entered_session = asyncio.Event()

        async def worker():
            async with reconstructor.reconstruct(source_ctx, target_url, intent) as session:
                entered_session.set()
                await asyncio.sleep(10)

        task = asyncio.create_task(worker())
        await entered_session.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        # Disposable resources are cleaned up; only source context remains
        assert browser.contexts == [source_ctx]
        assert not source_page.is_closed()
        assert len(source_ctx.pages) == 1

        await browser.close()


@pytest.mark.asyncio
async def test_state_reconstruction_session_disposal_invariants(b2c_servers):
    """Verify session properties enforce disposal contracts and prevent access to disposed/unvalidated resources."""
    s1, _ = b2c_servers
    reconstructor = StateReconstructor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        await source_page.goto(f"{s1.origin}/dashboard")
        intent = await _derive_intent_from_page(source_page, "Save Settings")

        # 1. Failed reconstruction session shuts down resources before yielding, and handles raise StateReconstructionError
        bad_url = f"{s1.origin}/nonexistent-404"
        async with reconstructor.reconstruct(source_ctx, bad_url, intent) as session:
            assert session.is_revalidated is False
            assert session.is_disposed is True
            # Browser context from reconstruction was already cleaned up and removed before body entered:
            assert browser.contexts == [source_ctx]
            with pytest.raises(StateReconstructionError, match="Cannot access page on (unvalidated|disposed)"):
                _ = session.page
            with pytest.raises(StateReconstructionError, match="Cannot access context on disposed"):
                _ = session.disposable_context
            with pytest.raises(StateReconstructionError, match="Cannot access barrier session on disposed"):
                _ = session.barrier_session

        # 2. Exited successful session cannot access page or context
        target_url = f"{s1.origin}/dashboard"
        async with reconstructor.reconstruct(source_ctx, target_url, intent) as session:
            assert session.is_revalidated is True
            page_ref = session.page
            assert not page_ref.is_closed()

        # Session is now disposed
        assert session.is_disposed is True
        with pytest.raises(StateReconstructionError, match="Cannot access page on disposed session"):
            _ = session.page
        with pytest.raises(StateReconstructionError, match="Cannot access context on disposed session"):
            _ = session.disposable_context
        with pytest.raises(StateReconstructionError, match="Cannot access barrier session on disposed session"):
            _ = session.barrier_session

        await browser.close()


def test_state_reconstruction_type_validation():
    """Verify type checking for source_context and candidate_intent."""
    reconstructor = StateReconstructor()

    # None source context raises StateReconstructionError
    with pytest.raises(StateReconstructionError, match="source_context must not be None"):
        asyncio.run(reconstructor.revalidate(None, "http://127.0.0.1:8080", None))  # type: ignore

    # Non-intent raises TypeError
    class DummyContext:
        pass

    with pytest.raises(TypeError, match="candidate_intent must be a CandidateActionIntent"):
        asyncio.run(reconstructor.revalidate(DummyContext(), "http://127.0.0.1:8080", "not_an_intent"))  # type: ignore


def test_state_reconstruction_privacy_and_representations():
    """Verify representations omit tokens and secrets, and results are tagged as execution control."""
    reconstructor = StateReconstructor()
    assert "StateReconstructor" in repr(reconstructor)
    assert "StateReconstructor" in str(reconstructor)

    result = StateReconstructionResult(
        status=ReconstructionStatus.REVALIDATED,
        is_revalidated=True,
        target_url="http://127.0.0.1:8080/dashboard",
        reconstructed_url="http://127.0.0.1:8080/dashboard",
        parent_state_fingerprint="fp123456",
        preflight_result=None,
        limitations=(),
        error_message=None,
    )

    assert result.is_execution_control is True
    assert "cookie" not in repr(result)
    assert "secret" not in str(result)


@pytest.mark.asyncio
async def test_state_reconstruction_privacy_credentials_and_query_redaction(b2c_servers):
    """Regression test: Verify synthetic credentials, query tokens, and fragments are never leaked."""
    s1, _ = b2c_servers
    reconstructor = StateReconstructor()

    class DummyContext:
        pass

    ctx = DummyContext()
    intent = CandidateActionIntent(
        parent_state_fingerprint="fp1",
        control_fingerprint="ctrl1",
        expected_action_type="click",
        risk_classification="SAFE_CANDIDATE",
    )

    sentinels = [
        "synthetic_password_987",
        "synthetic_token_xyz_456",
        "synthetic_frag_123",
        "synthetic_user_abc",
    ]

    malicious_url = (
        "http://synthetic_user_abc:synthetic_password_987@127.0.0.1:8080/dashboard"
        "?token=synthetic_token_xyz_456#synthetic_frag_123"
    )

    # 1. Test via revalidate()
    res = await reconstructor.revalidate(ctx, malicious_url, intent)
    assert res.status == ReconstructionStatus.URL_NOT_APPROVED
    assert res.is_revalidated is False

    for s in sentinels:
        assert s not in res.target_url, f"Sentinel {s} leaked in res.target_url: {res.target_url}"
        assert s not in repr(res), f"Sentinel {s} leaked in repr(res): {repr(res)}"
        assert s not in str(res), f"Sentinel {s} leaked in str(res): {str(res)}"
        assert s not in str(dataclasses.asdict(res)), f"Sentinel {s} leaked in asdict(res)"
        if res.error_message:
            assert s not in res.error_message, f"Sentinel {s} leaked in error_message: {res.error_message}"

    # 2. Test via reconstruct() context manager
    async with reconstructor.reconstruct(ctx, malicious_url, intent) as session:
        session_res = session.result
        assert session_res.status == ReconstructionStatus.URL_NOT_APPROVED
        for s in sentinels:
            assert s not in session_res.target_url
            assert s not in repr(session_res)
            assert s not in str(dataclasses.asdict(session_res))
            if session_res.error_message:
                assert s not in session_res.error_message

    # 3. Test on real server with query parameters on an approved URL
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        query_sentinel = "synthetic_query_token_live_789"
        approved_url_with_query = f"{s1.origin}/dashboard?token={query_sentinel}"

        await source_page.goto(approved_url_with_query)
        real_intent = await _derive_intent_from_page(source_page, "Save Settings")

        valid_res = await reconstructor.revalidate(source_ctx, approved_url_with_query, real_intent)
        assert valid_res.is_revalidated is True
        assert valid_res.status == ReconstructionStatus.REVALIDATED

        # Metadata must strictly strip query string for privacy
        assert query_sentinel not in valid_res.target_url
        assert query_sentinel not in (valid_res.reconstructed_url or "")
        assert query_sentinel not in repr(valid_res)
        assert query_sentinel not in str(dataclasses.asdict(valid_res))

        await browser.close()


@pytest.mark.asyncio
async def test_state_reconstruction_navigation_exception_privacy(b2c_servers):
    """Regression test: Navigation exceptions with embedded synthetic secrets do not leak into results."""
    s1, _ = b2c_servers

    secret_sentinel = "SYNTHETIC_EXCEPTION_SECRET_SENTINEL_XYZ_456"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        await source_page.goto(f"{s1.origin}/dashboard")
        intent = await _derive_intent_from_page(source_page, "Save Settings")

        reconstructor = StateReconstructor()
        orig_factory = reconstructor._context_factory

        class MockFactory:
            @asynccontextmanager
            async def open(self, src_ctx, *args, **kwargs):
                async with orig_factory.open(src_ctx, *args, **kwargs) as disp_ctx:
                    orig_new_page = disp_ctx.new_page

                    async def mock_new_page():
                        page = await orig_new_page()

                        async def mock_goto(*args, **kwargs):
                            raise RuntimeError(f"Connection failed at {s1.origin}/path with token={secret_sentinel}")

                        page.goto = mock_goto
                        return page

                    disp_ctx.new_page = mock_new_page
                    yield disp_ctx

        reconstructor._context_factory = MockFactory()

        target_url = f"{s1.origin}/dashboard"
        result = await reconstructor.revalidate(source_ctx, target_url, intent)

        assert result.status == ReconstructionStatus.BOOTSTRAP_FAILED
        assert result.is_revalidated is False
        assert result.error_message is not None

        # Verify the secret sentinel was not leaked
        assert secret_sentinel not in result.error_message
        assert secret_sentinel not in repr(result)
        assert secret_sentinel not in str(result)
        assert secret_sentinel not in str(dataclasses.asdict(result))

        await browser.close()


@pytest.mark.asyncio
async def test_state_reconstruction_failure_shuts_down_resources_before_yielding(b2c_servers):
    """Regression test: Before yielding any failure session, bootstrap admission ends and resources are shut down."""
    s1, _ = b2c_servers

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        await source_page.goto(f"{s1.origin}/dashboard")
        intent = await _derive_intent_from_page(source_page, "Save Settings")

        captured_barrier: Optional[Any] = None
        captured_context: Optional[Any] = None

        reconstructor = StateReconstructor()
        orig_barrier = reconstructor._barrier

        class MockBarrier:
            @asynccontextmanager
            async def guard(self, disp_ctx, *, bootstrap_origin):
                nonlocal captured_context
                captured_context = disp_ctx
                async with orig_barrier.guard(disp_ctx, bootstrap_origin=bootstrap_origin) as b_sess:
                    nonlocal captured_barrier
                    captured_barrier = b_sess
                    yield b_sess

        reconstructor._barrier = MockBarrier()

        orig_factory = reconstructor._context_factory

        class MockFactory:
            @asynccontextmanager
            async def open(self, src_ctx, *args, **kwargs):
                async with orig_factory.open(src_ctx, *args, **kwargs) as disp_ctx:
                    orig_new_page = disp_ctx.new_page

                    async def mock_new_page():
                        page = await orig_new_page()

                        async def failing_goto(*args, **kwargs):
                            raise RuntimeError("Simulated Page.goto() failure during bootstrap")

                        page.goto = failing_goto
                        return page

                    disp_ctx.new_page = mock_new_page
                    yield disp_ctx

        reconstructor._context_factory = MockFactory()

        target_url = f"{s1.origin}/dashboard"
        async with reconstructor.reconstruct(source_ctx, target_url, intent) as session:
            # INSIDE caller block before exit:
            assert session.result.status == ReconstructionStatus.BOOTSTRAP_FAILED
            assert session.is_revalidated is False
            assert session.is_disposed is True

            # Handles must NOT be accessible
            with pytest.raises(StateReconstructionError):
                _ = session.page
            with pytest.raises(StateReconstructionError):
                _ = session.disposable_context
            with pytest.raises(StateReconstructionError):
                _ = session.barrier_session

            # Disposable resources must ALREADY be safely shut down:
            assert captured_context is not None
            assert captured_context not in browser.contexts
            assert browser.contexts == [source_ctx]
            assert captured_barrier is not None
            assert captured_barrier.state == BarrierState.CLOSED
            assert captured_barrier._enter_inflight() is False
            with pytest.raises(Exception):
                await captured_context.new_page()

        await browser.close()


@pytest.mark.asyncio
async def test_state_reconstruction_cancellation_during_page_close_real_chromium(b2c_servers):
    """Regression test: Cancellation during page.close() does not skip context/barrier cleanup."""
    s1, _ = b2c_servers
    reconstructor = StateReconstructor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        await source_page.goto(f"{s1.origin}/dashboard")
        intent = await _derive_intent_from_page(source_page, "Save Settings")

        page_close_entered = asyncio.Event()
        page_close_release = asyncio.Event()

        orig_factory = reconstructor._context_factory

        class MockFactory:
            @asynccontextmanager
            async def open(self, src_ctx, *args, **kwargs):
                async with orig_factory.open(src_ctx, *args, **kwargs) as disp_ctx:
                    orig_new_page = disp_ctx.new_page

                    async def mock_new_page():
                        page = await orig_new_page()
                        orig_close = page.close

                        async def delayed_close():
                            page_close_entered.set()
                            await page_close_release.wait()
                            return await orig_close()

                        page.close = delayed_close

                        async def failing_goto(*args, **kwargs):
                            raise RuntimeError("Simulated navigation failure")

                        page.goto = failing_goto
                        return page

                    disp_ctx.new_page = mock_new_page
                    yield disp_ctx

        reconstructor._context_factory = MockFactory()

        target_url = f"{s1.origin}/dashboard"
        task = asyncio.create_task(reconstructor.revalidate(source_ctx, target_url, intent))

        # Wait until page.close() is entered
        await page_close_entered.wait()

        # Cancel the task while awaiting shielded page.close()
        task.cancel()

        # Release page.close() so it finishes
        page_close_release.set()

        # Verify CancelledError propagates
        with pytest.raises(asyncio.CancelledError):
            await task

        # Crucial invariant: disposable context was NOT skipped; it was cleaned up and closed
        assert browser.contexts == [source_ctx]
        assert not source_page.is_closed()

        await browser.close()


@pytest.mark.asyncio
async def test_state_reconstruction_repeated_cancellation_during_cleanup(b2c_servers):
    """Regression test: Repeated cancellation across page.close() and stack.aclose() does not abandon cleanup."""
    s1, _ = b2c_servers
    reconstructor = StateReconstructor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        await source_page.goto(f"{s1.origin}/dashboard")
        intent = await _derive_intent_from_page(source_page, "Save Settings")

        close_entered_1 = asyncio.Event()
        close_release_1 = asyncio.Event()
        barrier_entered_2 = asyncio.Event()
        barrier_release_2 = asyncio.Event()

        orig_factory = reconstructor._context_factory
        orig_barrier = reconstructor._barrier

        class MockBarrier:
            @asynccontextmanager
            async def guard(self, disp_ctx, *, bootstrap_origin):
                async with orig_barrier.guard(disp_ctx, bootstrap_origin=bootstrap_origin) as b_sess:
                    try:
                        yield b_sess
                    finally:
                        barrier_entered_2.set()
                        await barrier_release_2.wait()

        reconstructor._barrier = MockBarrier()

        class MockFactory:
            @asynccontextmanager
            async def open(self, src_ctx, *args, **kwargs):
                async with orig_factory.open(src_ctx, *args, **kwargs) as disp_ctx:
                    orig_new_page = disp_ctx.new_page

                    async def mock_new_page():
                        page = await orig_new_page()
                        orig_close = page.close

                        async def delayed_close():
                            close_entered_1.set()
                            await close_release_1.wait()
                            return await orig_close()

                        page.close = delayed_close

                        async def failing_goto(*args, **kwargs):
                            raise RuntimeError("Simulated navigation failure")

                        page.goto = failing_goto
                        return page

                    disp_ctx.new_page = mock_new_page
                    yield disp_ctx

        reconstructor._context_factory = MockFactory()

        target_url = f"{s1.origin}/dashboard"
        task = asyncio.create_task(reconstructor.revalidate(source_ctx, target_url, intent))

        # 1. First cancellation during page.close()
        await close_entered_1.wait()
        task.cancel()
        close_release_1.set()

        # 2. Second cancellation during stack.aclose() / barrier teardown
        await barrier_entered_2.wait()
        task.cancel()
        barrier_release_2.set()

        # Verify CancelledError propagates cleanly
        with pytest.raises(asyncio.CancelledError):
            await task

        # Verified disposal despite repeated cancellations
        assert browser.contexts == [source_ctx]
        assert not source_page.is_closed()

        await browser.close()


@pytest.mark.asyncio
async def test_state_reconstruction_context_close_failure_without_is_closed(b2c_servers):
    """Regression test: Context closure failure on a context without is_closed() raises terminal failure.

    Verifies that when context.close() fails on a context matching Playwright's API shape
    (which lacks is_closed()), B2a suppresses the ordinary close exception, but B2c detects
    that closure cannot be verified, surfaces terminal cleanup failure, and does NOT yield
    a false normally disposed result (such as BOOTSTRAP_FAILED with session.is_disposed == True).
    Asserts resource state before browser.close() to ensure leaks cannot be masked.
    """
    s1, _ = b2c_servers
    reconstructor = StateReconstructor()

    secret_sentinel = "SYNTHETIC_CLOSE_FAILURE_SECRET_TOKEN_999"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        await source_page.goto(f"{s1.origin}/dashboard")
        intent = await _derive_intent_from_page(source_page, "Save Settings")

        orig_factory = reconstructor._context_factory
        captured_disp_ctx = None
        orig_close_method = None

        class MockFactory:
            @asynccontextmanager
            async def open(self, src_ctx, *args, **kwargs):
                nonlocal captured_disp_ctx, orig_close_method
                async with orig_factory.open(src_ctx, *args, **kwargs) as disp_ctx:
                    captured_disp_ctx = disp_ctx
                    orig_close_method = disp_ctx.close

                    # Verify that real Playwright BrowserContext does NOT provide is_closed()
                    assert getattr(disp_ctx, "is_closed", None) is None

                    # Make disp_ctx.close() raise, simulating close failure in B2a
                    async def failing_close():
                        raise RuntimeError(f"Context close failed with {secret_sentinel}")

                    disp_ctx.close = failing_close

                    orig_new_page = disp_ctx.new_page
                    async def mock_new_page():
                        page = await orig_new_page()
                        async def failing_goto(*args, **kwargs):
                            raise RuntimeError("Simulated navigation failure during bootstrap")
                        page.goto = failing_goto
                        return page

                    disp_ctx.new_page = mock_new_page
                    yield disp_ctx

        reconstructor._context_factory = MockFactory()

        target_url = f"{s1.origin}/dashboard"

        # Verify terminal cleanup failure is surfaced instead of a normally yielded disposed result
        yielded_sessions = []
        with pytest.raises(StateReconstructionError) as exc_info:
            async with reconstructor.reconstruct(source_ctx, target_url, intent) as session:
                yielded_sessions.append(session)

        # No session was yielded (terminal cleanup failure was surfaced instead of a normally yielded disposed result)
        assert len(yielded_sessions) == 0

        # Terminal cleanup failure is surfaced
        err_msg = str(exc_info.value)
        assert "Terminal cleanup failure" in err_msg or "disposal required" in err_msg

        # Privacy invariant: secret sentinel is NOT leaked in the error message
        assert secret_sentinel not in err_msg

        # Assert resource state before browser.close() can mask a leak:
        # The unclosed context is STILL in browser.contexts because close() failed!
        assert captured_disp_ctx is not None
        assert captured_disp_ctx in browser.contexts

        # Explicit harness recovery to clean up the retained context before browser.close()
        await orig_close_method()
        assert captured_disp_ctx not in browser.contexts
        assert browser.contexts == [source_ctx]

        await browser.close()


@pytest.mark.asyncio
async def test_state_reconstruction_real_chromium_closure_evidence_verification(b2c_servers):
    """Regression test: Closure verification accurately tracks real Chromium context lifecycle."""
    s1, _ = b2c_servers

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()

        # Before creation:
        disp_ctx = await browser.new_context()
        close_event = asyncio.Event()
        disp_ctx.on("close", lambda *args: close_event.set())

        # When open:
        assert not close_event.is_set()
        assert disp_ctx in browser.contexts
        verified, available = _verify_context_closed(disp_ctx, close_event)
        assert available is True
        assert verified is False  # Still open!

        # After close:
        await disp_ctx.close()
        assert close_event.is_set()
        assert disp_ctx not in browser.contexts
        verified, available = _verify_context_closed(disp_ctx, close_event)
        assert available is True
        assert verified is True  # Verified closed!

        await source_ctx.close()
        await browser.close()


def instrument_disposable_context_clicks(reconstructor):
    """Instruments reconstructor._context_factory to capture click dispatch directly in the disposable context."""
    disposable_clicks = []
    orig_open = reconstructor._context_factory.open

    @asynccontextmanager
    async def spied_open(src_ctx, *args, **kwargs):
        async with orig_open(src_ctx, *args, **kwargs) as disp_ctx:
            orig_new_page = disp_ctx.new_page

            async def spied_new_page(*args, **kwargs):
                page = await orig_new_page(*args, **kwargs)

                wrapped_containers = set()

                def wrap_container(container):
                    if id(container) in wrapped_containers:
                        return
                    wrapped_containers.add(id(container))
                    orig_locator = container.locator

                    def spied_locator(selector, **loc_kwargs):
                        loc = orig_locator(selector, **loc_kwargs)
                        orig_click = loc.click

                        async def spied_click(*c_args, **c_kwargs):
                            disposable_clicks.append({
                                "selector": selector,
                                "args": c_args,
                                "kwargs": c_kwargs,
                            })
                            return await orig_click(*c_args, **c_kwargs)

                        loc.click = spied_click
                        return loc

                    orig_get_by_role = container.get_by_role

                    def spied_get_by_role(role, **role_kwargs):
                        loc = orig_get_by_role(role, **role_kwargs)
                        orig_click = loc.click

                        async def spied_click(*c_args, **c_kwargs):
                            disposable_clicks.append({
                                "role": role,
                                "role_kwargs": role_kwargs,
                                "args": c_args,
                                "kwargs": c_kwargs,
                            })
                            return await orig_click(*c_args, **c_kwargs)

                        loc.click = spied_click
                        return loc

                    container.locator = spied_locator
                    container.get_by_role = spied_get_by_role

                wrap_container(page)
                if hasattr(page, "main_frame") and page.main_frame is not None:
                    wrap_container(page.main_frame)
                if hasattr(page, "frames"):
                    for f in page.frames:
                        wrap_container(f)
                return page

            disp_ctx.new_page = spied_new_page
            yield disp_ctx

    reconstructor._context_factory.open = spied_open
    return disposable_clicks


@pytest.mark.asyncio
async def test_guarded_prefix_replay_success(b2c_servers):
    """Guarded sequence contract: preceding tab action is validated, dispatched, observed in disposable context, and revealed target revalidated."""
    s1, _ = b2c_servers
    target_url = f"{s1.origin}/prefix-sequence"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()
        await source_page.goto(target_url)

        snap_adapter = ReadOnlyBrowserSnapshotAdapter()
        normalizer = SemanticNormalizer()
        initial_snap = await snap_adapter.capture_snapshot(
            source_page,
            {"qa_owned": True, "manual_visible_page": False, "expected_context": source_ctx},
        )
        initial_norm = normalizer.normalize_snapshot(initial_snap)
        initial_fp = CanonicalStateFingerprint.generate(initial_norm.semantic_state)

        prec_intent = CandidateActionIntent(
            parent_state_fingerprint=initial_fp,
            control_fingerprint="0:tab-2",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )

        probe_page = await source_ctx.new_page()
        await probe_page.goto(target_url)
        await probe_page.locator("#tab-2").click()
        probe_snap = await snap_adapter.capture_snapshot(
            probe_page,
            {"qa_owned": True, "manual_visible_page": False, "expected_context": source_ctx},
        )
        probe_norm = normalizer.normalize_snapshot(probe_snap)
        revealed_fp = CanonicalStateFingerprint.generate(probe_norm.semantic_state)
        await probe_page.close()

        candidate_intent = CandidateActionIntent(
            parent_state_fingerprint=revealed_fp,
            control_fingerprint="0:revealed-action",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
            preceding_intents=(prec_intent,),
        )

        reconstructor = StateReconstructor()
        disposable_clicks = instrument_disposable_context_clicks(reconstructor)

        async with reconstructor.reconstruct(source_ctx, target_url, candidate_intent) as session:
            assert session.result.is_revalidated is True
            assert session.result.status == ReconstructionStatus.REVALIDATED
            # Proved: exactly 1 click dispatched in disposable context on tab-2
            assert len(disposable_clicks) == 1
            assert "tab-2" in str(disposable_clicks[0])

        await source_ctx.close()
        await browser.close()


@pytest.mark.asyncio
async def test_guarded_prefix_replay_stale_parent_fingerprint_rejects_without_click(b2c_servers):
    """Guarded sequence contract: stale preceding state fingerprint fails closed; click is NOT dispatched in disposable context."""
    s1, _ = b2c_servers
    target_url = f"{s1.origin}/prefix-sequence"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()
        await source_page.goto(target_url)

        prec_intent = CandidateActionIntent(
            parent_state_fingerprint="stale_fingerprint_000000000000",
            control_fingerprint="0:tab-2",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )

        candidate_intent = CandidateActionIntent(
            parent_state_fingerprint=None,
            control_fingerprint="0:revealed-action",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
            preceding_intents=(prec_intent,),
        )

        reconstructor = StateReconstructor()
        disposable_clicks = instrument_disposable_context_clicks(reconstructor)

        async with reconstructor.reconstruct(source_ctx, target_url, candidate_intent) as session:
            assert session.result.is_revalidated is False
            assert session.result.status == ReconstructionStatus.STALE_STATE
            assert "stale state" in session.result.error_message.lower()

        # Proved: zero clicks dispatched in disposable context
        assert len(disposable_clicks) == 0

        await source_ctx.close()
        await browser.close()


@pytest.mark.asyncio
async def test_guarded_prefix_replay_unsupported_ordinary_button_rejects_without_click(b2c_servers):
    """Guarded sequence contract: ordinary safe button not matching disclosure/tab pattern is rejected without click in disposable context."""
    s1, _ = b2c_servers
    target_url = f"{s1.origin}/prefix-sequence"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()
        await source_page.goto(target_url)

        snap_adapter = ReadOnlyBrowserSnapshotAdapter()
        normalizer = SemanticNormalizer()
        initial_snap = await snap_adapter.capture_snapshot(
            source_page,
            {"qa_owned": True, "manual_visible_page": False, "expected_context": source_ctx},
        )
        initial_norm = normalizer.normalize_snapshot(initial_snap)
        initial_fp = CanonicalStateFingerprint.generate(initial_norm.semantic_state)

        # Preceding intent targeting ordinary button
        prec_intent = CandidateActionIntent(
            parent_state_fingerprint=initial_fp,
            control_fingerprint="0:btn-ordinary",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )

        candidate_intent = CandidateActionIntent(
            parent_state_fingerprint=None,
            control_fingerprint="0:revealed-action",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
            preceding_intents=(prec_intent,),
        )

        reconstructor = StateReconstructor()
        disposable_clicks = instrument_disposable_context_clicks(reconstructor)

        async with reconstructor.reconstruct(source_ctx, target_url, candidate_intent) as session:
            assert session.result.is_revalidated is False
            assert session.result.status == ReconstructionStatus.TARGET_NOT_ELIGIBLE
            assert "unsupported ordinary control" in session.result.error_message.lower()

        # Proved: ordinary button never clicked in disposable context
        assert len(disposable_clicks) == 0

        await source_ctx.close()
        await browser.close()


@pytest.mark.asyncio
async def test_guarded_prefix_replay_prohibited_prefix_rejects_without_click(b2c_servers):
    """Guarded sequence contract: prohibited prefix (Delete Account) fails closed; click is NOT dispatched in disposable context."""
    s1, _ = b2c_servers
    target_url = f"{s1.origin}/prefix-sequence"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()
        await source_page.goto(target_url)

        snap_adapter = ReadOnlyBrowserSnapshotAdapter()
        normalizer = SemanticNormalizer()
        initial_snap = await snap_adapter.capture_snapshot(
            source_page,
            {"qa_owned": True, "manual_visible_page": False, "expected_context": source_ctx},
        )
        initial_norm = normalizer.normalize_snapshot(initial_snap)
        initial_fp = CanonicalStateFingerprint.generate(initial_norm.semantic_state)

        # Preceding intent targeting Delete Account button
        prec_intent = CandidateActionIntent(
            parent_state_fingerprint=initial_fp,
            control_fingerprint="0:btn-delete",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )

        candidate_intent = CandidateActionIntent(
            parent_state_fingerprint=None,
            control_fingerprint="0:revealed-action",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
            preceding_intents=(prec_intent,),
        )

        reconstructor = StateReconstructor()
        disposable_clicks = instrument_disposable_context_clicks(reconstructor)

        async with reconstructor.reconstruct(source_ctx, target_url, candidate_intent) as session:
            assert session.result.is_revalidated is False
            assert session.result.status == ReconstructionStatus.TARGET_NOT_ELIGIBLE

        # Proved: zero clicks dispatched in disposable context
        assert len(disposable_clicks) == 0

        await source_ctx.close()
        await browser.close()


@pytest.mark.asyncio
async def test_guarded_prefix_replay_wrong_frame_rejects_without_fallback_or_click(b2c_servers):
    """Guarded sequence contract: missing/wrong frame fails closed without falling back to main page or clicking in disposable context."""
    s1, _ = b2c_servers
    target_url = f"{s1.origin}/prefix-sequence"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()
        await source_page.goto(target_url)

        snap_adapter = ReadOnlyBrowserSnapshotAdapter()
        normalizer = SemanticNormalizer()
        initial_snap = await snap_adapter.capture_snapshot(
            source_page,
            {"qa_owned": True, "manual_visible_page": False, "expected_context": source_ctx},
        )
        initial_norm = normalizer.normalize_snapshot(initial_snap)
        initial_fp = CanonicalStateFingerprint.generate(initial_norm.semantic_state)

        # Preceding intent targeting non-existent child frame 0.99
        prec_intent = CandidateActionIntent(
            parent_state_fingerprint=initial_fp,
            control_fingerprint="0.99:tab-2",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )

        candidate_intent = CandidateActionIntent(
            parent_state_fingerprint=None,
            control_fingerprint="0:revealed-action",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
            preceding_intents=(prec_intent,),
        )

        reconstructor = StateReconstructor()
        disposable_clicks = instrument_disposable_context_clicks(reconstructor)

        async with reconstructor.reconstruct(source_ctx, target_url, candidate_intent) as session:
            assert session.result.is_revalidated is False
            assert session.result.status in (ReconstructionStatus.INTENT_NOT_PRESENT, ReconstructionStatus.BOOTSTRAP_FAILED)

        # Proved: zero clicks dispatched in disposable context
        assert len(disposable_clicks) == 0

        await source_ctx.close()
        await browser.close()


@pytest.mark.asyncio
async def test_guarded_prefix_replay_missing_target_rejects_without_click(b2c_servers):
    """Guarded sequence contract: missing target in semantic state fails closed without click in disposable context."""
    s1, _ = b2c_servers
    target_url = f"{s1.origin}/prefix-sequence"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()
        await source_page.goto(target_url)

        snap_adapter = ReadOnlyBrowserSnapshotAdapter()
        normalizer = SemanticNormalizer()
        initial_snap = await snap_adapter.capture_snapshot(
            source_page,
            {"qa_owned": True, "manual_visible_page": False, "expected_context": source_ctx},
        )
        initial_norm = normalizer.normalize_snapshot(initial_snap)
        initial_fp = CanonicalStateFingerprint.generate(initial_norm.semantic_state)

        prec_intent = CandidateActionIntent(
            parent_state_fingerprint=initial_fp,
            control_fingerprint="0:btn-ghost-nonexistent",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )

        candidate_intent = CandidateActionIntent(
            parent_state_fingerprint=None,
            control_fingerprint="0:revealed-action",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
            preceding_intents=(prec_intent,),
        )

        reconstructor = StateReconstructor()
        disposable_clicks = instrument_disposable_context_clicks(reconstructor)

        async with reconstructor.reconstruct(source_ctx, target_url, candidate_intent) as session:
            assert session.result.is_revalidated is False
            assert session.result.status == ReconstructionStatus.INTENT_NOT_PRESENT

        # Proved: zero clicks in disposable context
        assert len(disposable_clicks) == 0

        await source_ctx.close()
        await browser.close()


@pytest.mark.asyncio
async def test_guarded_prefix_replay_already_selected_tab_rejects_without_click(b2c_servers):
    """Guarded sequence contract: tab already selected requires no transition; fails closed without click in disposable context."""
    s1, _ = b2c_servers
    target_url = f"{s1.origin}/prefix-sequence"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()
        await source_page.goto(target_url)

        snap_adapter = ReadOnlyBrowserSnapshotAdapter()
        normalizer = SemanticNormalizer()
        initial_snap = await snap_adapter.capture_snapshot(
            source_page,
            {"qa_owned": True, "manual_visible_page": False, "expected_context": source_ctx},
        )
        initial_norm = normalizer.normalize_snapshot(initial_snap)
        initial_fp = CanonicalStateFingerprint.generate(initial_norm.semantic_state)

        # Preceding intent targeting tab-1 (which has aria-selected="true" initially)
        prec_intent = CandidateActionIntent(
            parent_state_fingerprint=initial_fp,
            control_fingerprint="0:tab-1",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )

        candidate_intent = CandidateActionIntent(
            parent_state_fingerprint=None,
            control_fingerprint="0:revealed-action",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
            preceding_intents=(prec_intent,),
        )

        reconstructor = StateReconstructor()
        disposable_clicks = instrument_disposable_context_clicks(reconstructor)

        async with reconstructor.reconstruct(source_ctx, target_url, candidate_intent) as session:
            assert session.result.is_revalidated is False
            assert session.result.status == ReconstructionStatus.TARGET_NOT_ELIGIBLE
            assert "already selected" in session.result.error_message.lower()

        # Proved: zero clicks dispatched on already-selected tab
        assert len(disposable_clicks) == 0

        await source_ctx.close()
        await browser.close()


@pytest.mark.asyncio
async def test_guarded_prefix_replay_unrelated_class_change_fails_closed(b2c_servers):
    """Guarded sequence contract: unrelated class change alone does not count as positive transition; fails closed."""
    s1, _ = b2c_servers
    target_url = f"{s1.origin}/prefix-sequence"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()
        await source_page.goto(target_url)

        snap_adapter = ReadOnlyBrowserSnapshotAdapter()
        normalizer = SemanticNormalizer()
        initial_snap = await snap_adapter.capture_snapshot(
            source_page,
            {"qa_owned": True, "manual_visible_page": False, "expected_context": source_ctx},
        )
        initial_norm = normalizer.normalize_snapshot(initial_snap)
        initial_fp = CanonicalStateFingerprint.generate(initial_norm.semantic_state)

        # Preceding intent targeting btn-unrelated-class
        prec_intent = CandidateActionIntent(
            parent_state_fingerprint=initial_fp,
            control_fingerprint="0:btn-unrelated-class",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )

        candidate_intent = CandidateActionIntent(
            parent_state_fingerprint=None,
            control_fingerprint="0:revealed-action",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
            preceding_intents=(prec_intent,),
        )

        reconstructor = StateReconstructor()
        async with reconstructor.reconstruct(source_ctx, target_url, candidate_intent, timeout=2.0) as session:
            assert session.result.is_revalidated is False
            assert session.result.status == ReconstructionStatus.BOOTSTRAP_FAILED
            assert "expected state transition" in session.result.error_message.lower()

        await source_ctx.close()
        await browser.close()


@pytest.mark.asyncio
async def test_guarded_prefix_replay_missing_or_hidden_panel_fails_closed(b2c_servers):
    """Guarded sequence contract: tab transitioning aria-selected but whose panel is missing or remains hidden fails closed."""
    s1, _ = b2c_servers
    target_url = f"{s1.origin}/prefix-sequence"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()
        await source_page.goto(target_url)

        snap_adapter = ReadOnlyBrowserSnapshotAdapter()
        normalizer = SemanticNormalizer()
        initial_snap = await snap_adapter.capture_snapshot(
            source_page,
            {"qa_owned": True, "manual_visible_page": False, "expected_context": source_ctx},
        )
        initial_norm = normalizer.normalize_snapshot(initial_snap)
        initial_fp = CanonicalStateFingerprint.generate(initial_norm.semantic_state)

        # Case 1: Dangling panel
        prec_dangling = CandidateActionIntent(
            parent_state_fingerprint=initial_fp,
            control_fingerprint="0:btn-dangling-panel",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )
        cand_dangling = CandidateActionIntent(
            parent_state_fingerprint=None,
            control_fingerprint="0:revealed-action",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
            preceding_intents=(prec_dangling,),
        )
        reconstructor = StateReconstructor()
        async with reconstructor.reconstruct(source_ctx, target_url, cand_dangling, timeout=2.0) as session:
            assert session.result.is_revalidated is False
            assert session.result.status == ReconstructionStatus.BOOTSTRAP_FAILED

        # Case 2: Panel stays hidden
        prec_hidden = CandidateActionIntent(
            parent_state_fingerprint=initial_fp,
            control_fingerprint="0:btn-hidden-panel",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )
        cand_hidden = CandidateActionIntent(
            parent_state_fingerprint=None,
            control_fingerprint="0:revealed-action",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
            preceding_intents=(prec_hidden,),
        )
        async with reconstructor.reconstruct(source_ctx, target_url, cand_hidden, timeout=2.0) as session:
            assert session.result.is_revalidated is False
            assert session.result.status == ReconstructionStatus.BOOTSTRAP_FAILED

        await source_ctx.close()
        await browser.close()


@pytest.mark.asyncio
async def test_guarded_prefix_replay_control_without_explicit_id_succeeds(b2c_servers):
    """Guarded sequence contract: control without explicit ID resolved via role/name and directly observed on locator succeeds."""
    s1, _ = b2c_servers
    target_url = f"{s1.origin}/prefix-sequence"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()
        await source_page.goto(target_url)

        snap_adapter = ReadOnlyBrowserSnapshotAdapter()
        normalizer = SemanticNormalizer()
        initial_snap = await snap_adapter.capture_snapshot(
            source_page,
            {"qa_owned": True, "manual_visible_page": False, "expected_context": source_ctx},
        )
        initial_norm = normalizer.normalize_snapshot(initial_snap)
        initial_fp = CanonicalStateFingerprint.generate(initial_norm.semantic_state)

        # Find the auto-ID control corresponding to "Auto ID Tab"
        auto_ctrl = next(c for c in initial_norm.semantic_state.controls if c.accessible_name == "Auto ID Tab")
        assert "auto-" in auto_ctrl.control_id

        prec_intent = CandidateActionIntent(
            parent_state_fingerprint=initial_fp,
            control_fingerprint=auto_ctrl.control_id,
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )

        probe_page = await source_ctx.new_page()
        await probe_page.goto(target_url)
        await probe_page.get_by_role("tab", name="Auto ID Tab").click()
        probe_snap = await snap_adapter.capture_snapshot(
            probe_page,
            {"qa_owned": True, "manual_visible_page": False, "expected_context": source_ctx},
        )
        probe_norm = normalizer.normalize_snapshot(probe_snap)
        revealed_fp = CanonicalStateFingerprint.generate(probe_norm.semantic_state)
        await probe_page.close()

        candidate_intent = CandidateActionIntent(
            parent_state_fingerprint=revealed_fp,
            control_fingerprint="0:revealed-autoid-action",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
            preceding_intents=(prec_intent,),
        )

        reconstructor = StateReconstructor()
        disposable_clicks = instrument_disposable_context_clicks(reconstructor)

        async with reconstructor.reconstruct(source_ctx, target_url, candidate_intent) as session:
            assert session.result.is_revalidated is True
            assert session.result.status == ReconstructionStatus.REVALIDATED
            assert len(disposable_clicks) == 1

        await source_ctx.close()
        await browser.close()


@pytest.mark.asyncio
async def test_guarded_prefix_replay_delayed_legitimate_transition_succeeds(b2c_servers):
    """Guarded sequence contract: legitimate 200ms delayed transition settles within observation budget and succeeds."""
    s1, _ = b2c_servers
    target_url = f"{s1.origin}/prefix-sequence"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()
        await source_page.goto(target_url)

        snap_adapter = ReadOnlyBrowserSnapshotAdapter()
        normalizer = SemanticNormalizer()
        initial_snap = await snap_adapter.capture_snapshot(
            source_page,
            {"qa_owned": True, "manual_visible_page": False, "expected_context": source_ctx},
        )
        initial_norm = normalizer.normalize_snapshot(initial_snap)
        initial_fp = CanonicalStateFingerprint.generate(initial_norm.semantic_state)

        prec_intent = CandidateActionIntent(
            parent_state_fingerprint=initial_fp,
            control_fingerprint="0:btn-delayed-ok",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )

        probe_page = await source_ctx.new_page()
        await probe_page.goto(target_url)
        await probe_page.locator("#btn-delayed-ok").click()
        await asyncio.sleep(0.3)
        probe_snap = await snap_adapter.capture_snapshot(
            probe_page,
            {"qa_owned": True, "manual_visible_page": False, "expected_context": source_ctx},
        )
        probe_norm = normalizer.normalize_snapshot(probe_snap)
        revealed_fp = CanonicalStateFingerprint.generate(probe_norm.semantic_state)
        await probe_page.close()

        candidate_intent = CandidateActionIntent(
            parent_state_fingerprint=revealed_fp,
            control_fingerprint="0:revealed-delayed-action",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
            preceding_intents=(prec_intent,),
        )

        reconstructor = StateReconstructor()
        disposable_clicks = instrument_disposable_context_clicks(reconstructor)

        async with reconstructor.reconstruct(source_ctx, target_url, candidate_intent) as session:
            assert session.result.is_revalidated is True
            assert session.result.status == ReconstructionStatus.REVALIDATED
            assert len(disposable_clicks) == 1

        await source_ctx.close()
        await browser.close()


@pytest.mark.asyncio
async def test_guarded_prefix_replay_sequence_budget_exhaustion_fails_closed(b2c_servers):
    """Guarded sequence contract: preceding action transition exceeding sequence budget fails closed cleanly."""
    s1, _ = b2c_servers
    target_url = f"{s1.origin}/prefix-sequence"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()
        await source_page.goto(target_url)

        snap_adapter = ReadOnlyBrowserSnapshotAdapter()
        normalizer = SemanticNormalizer()
        initial_snap = await snap_adapter.capture_snapshot(
            source_page,
            {"qa_owned": True, "manual_visible_page": False, "expected_context": source_ctx},
        )
        initial_norm = normalizer.normalize_snapshot(initial_snap)
        initial_fp = CanonicalStateFingerprint.generate(initial_norm.semantic_state)

        # btn-slow delays transition by 10s
        prec_intent = CandidateActionIntent(
            parent_state_fingerprint=initial_fp,
            control_fingerprint="0:btn-slow",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )

        candidate_intent = CandidateActionIntent(
            parent_state_fingerprint=None,
            control_fingerprint="0:revealed-action",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
            preceding_intents=(prec_intent,),
        )

        reconstructor = StateReconstructor()
        # Bound sequence to 1.5s total budget
        async with reconstructor.reconstruct(source_ctx, target_url, candidate_intent, timeout=1.5) as session:
            assert session.result.is_revalidated is False
            assert session.result.status == ReconstructionStatus.BOOTSTRAP_FAILED

        await source_ctx.close()
        await browser.close()


@pytest.mark.asyncio
async def test_guarded_prefix_replay_cancellation_during_discovery_before_dispatch(b2c_servers):
    """Guarded sequence contract: cancellation token pre-triggered aborts before click dispatch in disposable context."""
    s1, _ = b2c_servers
    target_url = f"{s1.origin}/prefix-sequence"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()
        await source_page.goto(target_url)

        snap_adapter = ReadOnlyBrowserSnapshotAdapter()
        normalizer = SemanticNormalizer()
        initial_snap = await snap_adapter.capture_snapshot(
            source_page,
            {"qa_owned": True, "manual_visible_page": False, "expected_context": source_ctx},
        )
        initial_norm = normalizer.normalize_snapshot(initial_snap)
        initial_fp = CanonicalStateFingerprint.generate(initial_norm.semantic_state)

        prec_intent = CandidateActionIntent(
            parent_state_fingerprint=initial_fp,
            control_fingerprint="0:tab-2",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
        )

        candidate_intent = CandidateActionIntent(
            parent_state_fingerprint=None,
            control_fingerprint="0:revealed-action",
            expected_action_type="click",
            risk_classification=RiskClassification.SAFE_CANDIDATE,
            preceding_intents=(prec_intent,),
        )

        cancel_token = CancellationToken()
        cancel_token.cancel()

        reconstructor = StateReconstructor()
        disposable_clicks = instrument_disposable_context_clicks(reconstructor)

        async with reconstructor.reconstruct(
            source_ctx, target_url, candidate_intent, cancellation_token=cancel_token
        ) as session:
            assert session.result.is_revalidated is False
            assert session.result.status == ReconstructionStatus.CANCELLED

        # Proved: zero clicks dispatched in disposable context
        assert len(disposable_clicks) == 0

        await source_ctx.close()
        await browser.close()




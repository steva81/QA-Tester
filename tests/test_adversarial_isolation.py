"""Tests for Phase B2e: Real-Chromium Adversarial Isolation Suite.

Validates adversarial security, channel blocking, storage isolation, source invariance,
and bounded resource cleanup across the composed B2a/B2b/B2c/B2d architecture under real
Chromium execution against localhost fixtures.

Tested Invariants & Scenarios:
1. Storage Isolation:
   - Disposable cookie, localStorage, sessionStorage, IndexedDB, and CacheStorage mutations
     never pollute the source context.
   - Verifies the documented invariant that sessionStorage and IndexedDB are NOT cloned
     at disposable context inception (Playwright storage_state only clones cookies and localStorage).
2. Source Context & Manual Page Invariance:
   - Source context page URL, page count, storage, and network online connectivity remain
     completely unchanged during and after guarded execution.
3. Adversarial Network Mutation Containment:
   - POST, PUT, PATCH, DELETE, fetch, XHR, sendBeacon, keepalive, and redirect races
     are strictly blocked by the barrier during bootstrap.
   - Under lockdown, 100% of network traffic (including GET/HEAD) is blocked.
4. Popup & Child-Frame Containment:
   - Initial requests from popups and child-frames are intercepted and contained.
   - Cross-origin popups/iframes are aborted; lockdown aborts all popup navigation.
5. Channel API Blocking:
   - WebSocket, Worker, SharedWorker, EventSource, WebTransport, and Service Worker
     constructors/registrations are blocked using supported browser APIs.
   - Constructor overrides and deletions are rejected or prevented.
6. Non-Claimed Channels Explicit Documentation:
   - Explicitly documents and verifies that WebRTC (RTCPeerConnection) and BroadcastChannel
     fall outside the established B2b monkeypatching contract, avoiding unverified claims.
7. Exact Target Identity Across Frames & Adversarial DOM Mutation:
   - Frame-qualified control IDs resolve strictly within the target child frame, avoiding
     duplicate-ID collisions with the parent frame.
   - Adversarial DOM changes (fingerprint mismatch) trigger truthful STALE_STATE rejection
     with zero click attempts.
   - Adversarial target removal triggers fail-closed TARGET_NOT_FOUND with zero clicks.
8. Single Guarded Action & No Application Retries/Fallbacks:
   - Enforces at most one Playwright click attempt without retry, fallback, force, or JS click.
   - Occluded elements fail closed as ACTION_FAILED without force or JS fallback.
9. Distinguishable Action States & Preserved Teardown History:
   - Action attempted vs confirmed vs not-attempted remain clearly distinguishable.
   - Action history is preserved across teardown verification failure (CLEANUP_FAILED
     retains action_executed=True, action_confirmed=True).
10. Bounded Resource Cleanup Before Browser Close:
    - Verifies that disposable contexts and pages are completely closed BEFORE browser.close()
      on success, failure, cancellation, and repeated cancellation.
11. Privacy-Safe Diagnostics:
    - URL userinfo (passwords) and query tokens are strictly redacted in representations,
      error messages, and diagnostic outputs.
"""

import asyncio
from contextlib import asynccontextmanager
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from app.qa.browser_snapshot_adapter import ReadOnlyBrowserSnapshotAdapter
from app.qa.candidate_intent_factory import CandidateActionIntentFactory
from app.qa.candidate_intent_graph import CandidateActionIntent
from app.qa.guarded_execution import (
    GuardedActionExecutor,
    GuardedExecutionResult,
    GuardedExecutionStatus,
)
from app.qa.interaction_policy import RiskClassification
from app.qa.isolated_execution_context import IsolatedExecutionContextFactory
from app.qa.isolated_network_barrier import (
    BarrierState,
    IsolatedNetworkBarrier,
    IsolatedNetworkBarrierSession,
)
from app.qa.semantic_normalizer import SemanticNormalizer
from app.qa.state_reconstruction import (
    ReconstructionStatus,
    StateReconstructionError,
    StateReconstructor,
)


STORAGE_FIXTURE_JS = """
window.setupStorage = function(lsKey, lsVal, ssKey, ssVal) {
    if (lsKey) localStorage.setItem(lsKey, lsVal);
    if (ssKey) sessionStorage.setItem(ssKey, ssVal);
};

window.setupIDB = function(dbName, storeName, key, val) {
    return new Promise((resolve, reject) => {
        const req = indexedDB.open(dbName, 1);
        req.onupgradeneeded = function(e) {
            const db = e.target.result;
            db.createObjectStore(storeName, { keyPath: 'id' });
        };
        req.onsuccess = function(e) {
            const db = e.target.result;
            const tx = db.transaction(storeName, 'readwrite');
            tx.objectStore(storeName).put({ id: key, val: val });
            tx.oncomplete = () => { db.close(); resolve(true); };
            tx.onerror = () => { db.close(); reject(tx.error); };
        };
        req.onerror = () => reject(req.error);
    });
};

window.getIDBVal = function(dbName, storeName, key) {
    return new Promise((resolve) => {
        const req = indexedDB.open(dbName, 1);
        req.onsuccess = function(e) {
            const db = e.target.result;
            if (!db.objectStoreNames.contains(storeName)) {
                db.close();
                resolve(null);
                return;
            }
            const tx = db.transaction(storeName, 'readonly');
            const getReq = tx.objectStore(storeName).get(key);
            getReq.onsuccess = () => { db.close(); resolve(getReq.result ? getReq.result.val : null); };
            getReq.onerror = () => { db.close(); resolve(null); };
        };
        req.onerror = () => resolve(null);
    });
};

window.setupCache = async function(cacheName, url, content) {
    if (!window.caches) return false;
    const cache = await caches.open(cacheName);
    await cache.put(url, new Response(content));
    return true;
};

window.getCacheVal = async function(cacheName, url) {
    if (!window.caches) return null;
    const cache = await caches.open(cacheName);
    const res = await cache.match(url);
    return res ? await res.text() : null;
};
"""


class AdversarialTrackingHandler(BaseHTTPRequestHandler):
    """Local HTTP handler tracking adversarial attempts with zero secret retention."""

    counts: Dict[str, int] = {
        "get_root": 0,
        "get_disclosure": 0,
        "get_source_landing": 0,
        "get_plain_button": 0,
        "get_ping": 0,
        "get_frame_parent": 0,
        "get_frame_child": 0,
        "get_popup_opener": 0,
        "get_popup_target": 0,
        "get_occluded": 0,
        "post_mutate": 0,
        "put_mutate": 0,
        "patch_mutate": 0,
        "delete_mutate": 0,
        "beacon": 0,
        "keepalive": 0,
        "redirect_mutate": 0,
        "redirect_cross": 0,
        "redirect_unusual": 0,
        "cross_origin_hit": 0,
        "sw_script": 0,
        "lockdown_hit": 0,
    }

    @classmethod
    def reset_counts(cls):
        for k in cls.counts:
            cls.counts[k] = 0

    def do_GET(self):
        req_path = urlsplit(self.path).path
        if req_path == "/":
            self.__class__.counts["get_root"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<!DOCTYPE html><html><body><h1>Root</h1></body></html>")
        elif req_path == "/ping":
            self.__class__.counts["get_ping"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"pong")
        elif req_path == "/plain-button":
            self.__class__.counts["get_plain_button"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html>
<head><title>Plain Button</title></head>
<body>
  <h1>Plain Button</h1>
  <button id="plain-btn">Plain Button</button>
</body>
</html>""")
        elif req_path == "/disclosure":
            self.__class__.counts["get_disclosure"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            content = f"""<!DOCTYPE html>
<html>
<head><title>Disclosure Page</title></head>
<body>
  <h1>FAQ Section</h1>
  <button id="faq-toggle" aria-expanded="false" onclick="
    var isExp = this.getAttribute('aria-expanded') === 'true';
    this.setAttribute('aria-expanded', isExp ? 'false' : 'true');
    var content = document.getElementById('faq-content');
    if (content) {{ content.hidden = isExp; }}
    window.__click_count = (window.__click_count || 0) + 1;
  ">Show FAQ Details</button>
  <div id="faq-content" hidden>
    <p>Detailed FAQ disclosure answer revealed.</p>
  </div>
  <script>{STORAGE_FIXTURE_JS}</script>
</body>
</html>"""
            self.wfile.write(content.encode("utf-8"))
        elif req_path == "/source-landing":
            self.__class__.counts["get_source_landing"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            content = f"""<!DOCTYPE html>
<html>
<head><title>Source Landing</title></head>
<body>
  <h1>Source Landing Page</h1>
  <button id="source-btn">Source Control</button>
  <script>{STORAGE_FIXTURE_JS}</script>
</body>
</html>"""
            self.wfile.write(content.encode("utf-8"))
        elif req_path == "/frame-parent":
            self.__class__.counts["get_frame_parent"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            content = """<!DOCTYPE html>
<html>
<head><title>Frame Parent</title></head>
<body>
  <h1>Parent Document</h1>
  <button id="same-id" onclick="window.__wrong_clicks = (window.__wrong_clicks || 0) + 1;">Parent Button</button>
  <iframe id="child-iframe" src="/frame-child" style="width:400px; height:300px;"></iframe>
</body>
</html>"""
            self.wfile.write(content.encode("utf-8"))
        elif req_path == "/frame-child":
            self.__class__.counts["get_frame_child"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            content = """<!DOCTYPE html>
<html>
<head><title>Child Frame</title></head>
<body>
  <h2>Child Frame</h2>
  <button id="same-id" aria-expanded="false" onclick="
    var isExp = this.getAttribute('aria-expanded') === 'true';
    this.setAttribute('aria-expanded', isExp ? 'false' : 'true');
    window.__child_clicks = (window.__child_clicks || 0) + 1;
  ">Child Disclosure Button</button>
</body>
</html>"""
            self.wfile.write(content.encode("utf-8"))
        elif req_path == "/popup-opener":
            self.__class__.counts["get_popup_opener"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            content = """<!DOCTYPE html>
<html>
<head><title>Popup Opener</title></head>
<body>
  <h1>Popup Opener</h1>
  <button id="open-popup-btn" onclick="window.__popup = window.open('/popup-target', '_blank');">Open Popup</button>
</body>
</html>"""
            self.wfile.write(content.encode("utf-8"))
        elif req_path == "/popup-target":
            self.__class__.counts["get_popup_target"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<!DOCTYPE html><html><body><h1>Popup Target</h1></body></html>")
        elif req_path == "/occluded":
            self.__class__.counts["get_occluded"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            content = """<!DOCTYPE html>
<html>
<head><title>Occluded Page</title></head>
<body>
  <h1>Occluded Control</h1>
  <button id="faq-toggle" aria-expanded="false" onclick="
    this.setAttribute('aria-expanded', 'true');
    window.__click_count = (window.__click_count || 0) + 1;
  ">Show FAQ Details</button>
  <!-- Full-screen translucent blocking overlay that intercepts pointer events -->
  <div id="overlay" style="position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.1); z-index:99999;"></div>
</body>
</html>"""
            self.wfile.write(content.encode("utf-8"))
        elif req_path == "/redirect-to-cross":
            self.__class__.counts["redirect_cross"] += 1
            target = getattr(self.server, "cross_target_url", "http://127.0.0.1:9/cross")
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
        elif req_path == "/redirect-to-unusual":
            self.__class__.counts["redirect_unusual"] += 1
            self.send_response(302)
            self.send_header("Location", "javascript:void(0)")
            self.end_headers()
        elif req_path == "/cross":
            self.__class__.counts["cross_origin_hit"] += 1
            self.send_response(200)
            self.end_headers()
        elif req_path == "/sw.js":
            self.__class__.counts["sw_script"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.end_headers()
            self.wfile.write(b"// Service worker script")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        req_path = urlsplit(self.path).path
        if req_path == "/mutate":
            self.__class__.counts["post_mutate"] += 1
            self.send_response(200)
            self.end_headers()
        elif req_path == "/beacon":
            self.__class__.counts["beacon"] += 1
            self.send_response(200)
            self.end_headers()
        elif req_path == "/keepalive":
            self.__class__.counts["keepalive"] += 1
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_PUT(self):
        req_path = urlsplit(self.path).path
        if req_path == "/mutate":
            self.__class__.counts["put_mutate"] += 1
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_PATCH(self):
        req_path = urlsplit(self.path).path
        if req_path == "/mutate":
            self.__class__.counts["patch_mutate"] += 1
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_DELETE(self):
        req_path = urlsplit(self.path).path
        if req_path == "/mutate":
            self.__class__.counts["delete_mutate"] += 1
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Zero logging to prevent secret leakage


class ManagedAdversarialServer:
    """Helper managing an ephemeral localhost HTTP server for adversarial isolation."""

    def __init__(self, handler_cls=AdversarialTrackingHandler):
        self.server = HTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def adversarial_servers():
    AdversarialTrackingHandler.reset_counts()
    s1 = ManagedAdversarialServer()
    s2 = ManagedAdversarialServer()
    s1.server.cross_target_url = f"{s2.origin}/cross"
    try:
        yield s1, s2
    finally:
        s1.close()
        s2.close()


async def _derive_intent_from_page(page: Any, target_name: str) -> CandidateActionIntent:
    """Helper to derive CandidateActionIntent from an active page matching target_name."""
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
        if target_name in intent.control_fingerprint:
            return intent
    for control in norm.semantic_state.controls:
        if target_name in control.accessible_name or target_name in control.control_id:
            for intent in derivation.intents:
                if intent.control_fingerprint == control.control_id:
                    return intent

    assert len(derivation.intents) > 0, f"No intents derived for {target_name}"
    return derivation.intents[0]


# ==============================================================================
# 1. Storage Isolation Suite (Cookies, Local, Session, IDB, Cache)
# ==============================================================================


@pytest.mark.asyncio
async def test_storage_isolation_cookies_and_localstorage_do_not_leak_to_source(adversarial_servers):
    """Verify disposable cookie and localStorage mutations never pollute source context."""
    s1, _ = adversarial_servers

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()

        # Seed source context with cookies and localStorage
        source_page = await source_ctx.new_page()
        await source_page.goto(f"{s1.origin}/source-landing")
        await source_ctx.add_cookies([
            {"name": "source_auth", "value": "source_secret_cookie", "url": s1.origin}
        ])
        await source_page.evaluate("""() => {
            localStorage.setItem("source_setting", "dark_mode");
            localStorage.setItem("user_id", "42");
        }""")

        # Prepare valid disclosure intent for target URL
        target_url = f"{s1.origin}/disclosure"
        temp_page = await source_ctx.new_page()
        await temp_page.goto(target_url)
        disclosure_intent = await _derive_intent_from_page(temp_page, "faq-toggle")
        await temp_page.close()

        executor = GuardedActionExecutor()
        orig_reconstruct = executor._reconstructor.reconstruct

        # Intercept reconstruction to mutate disposable storage during the active session
        @asynccontextmanager
        async def mutating_reconstruct(*args, **kwargs):
            async with orig_reconstruct(*args, **kwargs) as sess:
                disp_page = sess.page
                # Disposable page attempts hostile storage mutations
                await disp_page.evaluate("""() => {
                    document.cookie = "disposable_rogue=rogue_val; path=/";
                    localStorage.setItem("source_setting", "HACKED_VAL");
                    localStorage.setItem("disposable_only", "injected_data");
                }""")
                yield sess

        executor._reconstructor.reconstruct = mutating_reconstruct

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=disclosure_intent,
        )

        assert res.status == GuardedExecutionStatus.EXECUTED
        assert res.action_executed is True

        # Inspect source context storage
        source_cookies = await source_ctx.cookies([s1.origin])
        cookie_map = {c["name"]: c["value"] for c in source_cookies}
        assert cookie_map.get("source_auth") == "source_secret_cookie"
        assert "disposable_rogue" not in cookie_map, "Disposable cookie leaked into source context!"

        ls_values = await source_page.evaluate("""() => {
            return {
                source_setting: localStorage.getItem("source_setting"),
                user_id: localStorage.getItem("user_id"),
                disposable_only: localStorage.getItem("disposable_only")
            };
        }""")
        assert ls_values["source_setting"] == "dark_mode", "Source localStorage was overwritten!"
        assert ls_values["user_id"] == "42"
        assert ls_values["disposable_only"] is None, "Disposable localStorage leaked into source context!"

        assert browser.contexts == [source_ctx]
        await browser.close()


@pytest.mark.asyncio
async def test_storage_isolation_sessionstorage_and_idb_not_cloned_and_do_not_leak(adversarial_servers):
    """Verify sessionStorage and IndexedDB are NOT cloned to disposable context and do not leak to source."""
    s1, _ = adversarial_servers

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()
        await source_page.goto(f"{s1.origin}/source-landing")

        # Setup source sessionStorage and IndexedDB
        await source_page.evaluate("""async () => {
            sessionStorage.setItem("source_ss_token", "source_session_secret");
            await window.setupIDB("SourceDB", "records", "key1", "source_val_1");
        }""")

        source_idb_val = await source_page.evaluate("() => window.getIDBVal('SourceDB', 'records', 'key1')")
        assert source_idb_val == "source_val_1"

        target_url = f"{s1.origin}/disclosure"
        temp_page = await source_ctx.new_page()
        await temp_page.goto(target_url)
        intent = await _derive_intent_from_page(temp_page, "faq-toggle")
        await temp_page.close()

        executor = GuardedActionExecutor()
        orig_reconstruct = executor._reconstructor.reconstruct

        observed_disp_state = {}

        @asynccontextmanager
        async def inspecting_reconstruct(*args, **kwargs):
            async with orig_reconstruct(*args, **kwargs) as sess:
                disp_page = sess.page
                # Invariant verification: sessionStorage and IDB are NOT cloned from source
                disp_ss = await disp_page.evaluate("() => sessionStorage.getItem('source_ss_token')")
                disp_dbs = await disp_page.evaluate("async () => (await window.indexedDB.databases()).map(d => d.name)")
                observed_disp_state["disp_ss_at_start"] = disp_ss
                observed_disp_state["source_db_in_disp"] = "SourceDB" in disp_dbs

                # Disposable context mutates its own sessionStorage and creates its own DisposableDB
                await disp_page.evaluate("""async () => {
                    sessionStorage.setItem("disposable_ss_rogue", "rogue_ss_data");
                    await window.setupIDB("DisposableDB", "records", "dkey", "disp_data");
                }""")
                yield sess

        executor._reconstructor.reconstruct = inspecting_reconstruct

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert res.status == GuardedExecutionStatus.EXECUTED

        # 1. Documented non-cloning invariant verified:
        assert observed_disp_state["disp_ss_at_start"] is None, "sessionStorage was unexpectedly cloned!"
        assert observed_disp_state["source_db_in_disp"] is False, "IndexedDB was unexpectedly cloned!"

        # 2. Source isolation invariant verified:
        source_ss = await source_page.evaluate("() => sessionStorage.getItem('source_ss_token')")
        source_rogue_ss = await source_page.evaluate("() => sessionStorage.getItem('disposable_ss_rogue')")
        assert source_ss == "source_session_secret", "Source sessionStorage was mutated!"
        assert source_rogue_ss is None, "Disposable sessionStorage leaked to source!"

        source_idb_final = await source_page.evaluate("() => window.getIDBVal('SourceDB', 'records', 'key1')")
        source_disp_dbs = await source_page.evaluate("async () => (await window.indexedDB.databases()).map(d => d.name)")
        assert source_idb_final == "source_val_1", "Source IndexedDB record was mutated!"
        assert "DisposableDB" not in source_disp_dbs, "Disposable IndexedDB database leaked to source context!"

        assert browser.contexts == [source_ctx]
        await browser.close()


@pytest.mark.asyncio
async def test_storage_isolation_cachestorage_does_not_leak_to_source(adversarial_servers):
    """Verify CacheStorage mutations in disposable context do not pollute source context."""
    s1, _ = adversarial_servers

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()
        await source_page.goto(f"{s1.origin}/source-landing")

        # Setup source CacheStorage
        await source_page.evaluate("""async () => {
            await window.setupCache("source-cache", "/cached-item", "source_cached_content");
        }""")
        cached_val = await source_page.evaluate("() => window.getCacheVal('source-cache', '/cached-item')")
        assert cached_val == "source_cached_content"

        target_url = f"{s1.origin}/disclosure"
        temp_page = await source_ctx.new_page()
        await temp_page.goto(target_url)
        intent = await _derive_intent_from_page(temp_page, "faq-toggle")
        await temp_page.close()

        executor = GuardedActionExecutor()
        orig_reconstruct = executor._reconstructor.reconstruct

        @asynccontextmanager
        async def cache_mutating_reconstruct(*args, **kwargs):
            async with orig_reconstruct(*args, **kwargs) as sess:
                disp_page = sess.page
                await disp_page.evaluate("""async () => {
                    await window.setupCache("source-cache", "/cached-item", "OVERWRITTEN_CONTENT");
                    await window.setupCache("disposable-cache", "/rogue", "rogue_content");
                }""")
                yield sess

        executor._reconstructor.reconstruct = cache_mutating_reconstruct

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert res.status == GuardedExecutionStatus.EXECUTED

        # Source CacheStorage must be pristine
        source_cached = await source_page.evaluate("() => window.getCacheVal('source-cache', '/cached-item')")
        source_rogue = await source_page.evaluate("() => window.getCacheVal('disposable-cache', '/rogue')")
        assert source_cached == "source_cached_content", "Source CacheStorage item was overwritten!"
        assert source_rogue is None, "Disposable CacheStorage leaked into source context!"

        assert browser.contexts == [source_ctx]
        await browser.close()


# ==============================================================================
# 2. Source Context & Manual Page Invariance
# ==============================================================================


@pytest.mark.asyncio
async def test_source_page_url_pages_and_online_behavior_invariant(adversarial_servers):
    """Verify source page URL, page list, and online connectivity remain strictly unchanged."""
    s1, _ = adversarial_servers

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()
        landing_url = f"{s1.origin}/source-landing"
        await source_page.goto(landing_url)

        initial_pages = list(source_ctx.pages)
        assert len(initial_pages) == 1
        assert source_page.url == landing_url

        target_url = f"{s1.origin}/disclosure"
        temp_page = await source_ctx.new_page()
        await temp_page.goto(target_url)
        intent = await _derive_intent_from_page(temp_page, "faq-toggle")
        await temp_page.close()

        executor = GuardedActionExecutor()
        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert res.status == GuardedExecutionStatus.EXECUTED
        assert res.action_executed is True

        # 1. Source page URL was never navigated
        assert source_page.url == landing_url

        # 2. Source context page count is preserved
        assert source_ctx.pages == initial_pages

        # 3. Source context network is still online (not set offline by B2b lockdown on disposable context)
        ping_res = await source_page.evaluate("""async () => {
            const resp = await fetch('/ping');
            return { status: resp.status, text: await resp.text() };
        }""")
        assert ping_res["status"] == 200
        assert ping_res["text"] == "pong"
        assert AdversarialTrackingHandler.counts["get_ping"] >= 1

        assert browser.contexts == [source_ctx]
        await browser.close()


# ==============================================================================
# 3. Adversarial Network Mutation Escape Attempts
# ==============================================================================


@pytest.mark.asyncio
async def test_adversarial_network_mutations_blocked_during_bootstrap(adversarial_servers):
    """Verify POST, PUT, PATCH, DELETE, fetch, XHR, sendBeacon, and keepalive are blocked."""
    s1, _ = adversarial_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=s1.origin) as session:
            page = await ctx.new_page()
            await page.goto(f"{s1.origin}/disclosure")

            # 1. Fetch mutations
            outcomes = await page.evaluate("""async () => {
                const results = {};
                for (const m of ['POST', 'PUT', 'PATCH', 'DELETE']) {
                    try {
                        await fetch('/mutate', { method: m, body: 'data' });
                        results[m] = 'success';
                    } catch (e) {
                        results[m] = 'blocked';
                    }
                }
                return results;
            }""")
            for m in ['POST', 'PUT', 'PATCH', 'DELETE']:
                assert outcomes.get(m) == "blocked", f"fetch {m} was not blocked!"

            # 2. XHR mutation
            xhr_outcome = await page.evaluate("""() => {
                return new Promise((resolve) => {
                    const req = new XMLHttpRequest();
                    req.open('POST', '/mutate');
                    req.onload = () => resolve('success');
                    req.onerror = () => resolve('blocked');
                    req.send('xhr_payload');
                });
            }""")
            assert xhr_outcome == "blocked", "XHR POST was not blocked!"

            # 3. sendBeacon
            await page.evaluate("""() => {
                navigator.sendBeacon('/beacon', 'beacon_payload');
            }""")
            await page.wait_for_timeout(100)

            # 4. keepalive fetch
            keepalive_outcome = await page.evaluate("""async () => {
                try {
                    await fetch('/keepalive', { method: 'POST', body: 'keepalive_data', keepalive: true });
                    return 'success';
                } catch (e) {
                    return 'blocked';
                }
            }""")
            assert keepalive_outcome == "blocked", "keepalive fetch POST was not blocked!"

        await browser.close()

    # Verify server received zero mutating requests
    assert AdversarialTrackingHandler.counts["post_mutate"] == 0
    assert AdversarialTrackingHandler.counts["put_mutate"] == 0
    assert AdversarialTrackingHandler.counts["patch_mutate"] == 0
    assert AdversarialTrackingHandler.counts["delete_mutate"] == 0
    assert AdversarialTrackingHandler.counts["beacon"] == 0
    assert AdversarialTrackingHandler.counts["keepalive"] == 0


@pytest.mark.asyncio
async def test_adversarial_network_redirect_races_blocked(adversarial_servers):
    """Verify redirects attempting to escape to cross-origin or unusual endpoints are blocked."""
    s1, s2 = adversarial_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=s1.origin) as session:
            # 1. Cross-origin redirect is aborted by the barrier
            page1 = await ctx.new_page()
            with pytest.raises(Exception):
                await page1.goto(f"{s1.origin}/redirect-to-cross")

            # 2. Unusual scheme redirect (javascript:void(0)) is aborted
            page2 = await ctx.new_page()
            with pytest.raises(Exception):
                await page2.goto(f"{s1.origin}/redirect-to-unusual")

            # 3. Mutating fetch request with redirect is blocked
            page3 = await ctx.new_page()
            await page3.goto(f"{s1.origin}/")
            fetch_post_outcome = await page3.evaluate("""async () => {
                try {
                    await fetch('/redirect-to-cross', { method: 'POST' });
                    return 'success';
                } catch (e) {
                    return 'blocked';
                }
            }""")
            assert fetch_post_outcome == "blocked"

        await browser.close()

    assert AdversarialTrackingHandler.counts["redirect_cross"] == 1
    assert AdversarialTrackingHandler.counts["cross_origin_hit"] == 0
    assert AdversarialTrackingHandler.counts["redirect_unusual"] == 1


@pytest.mark.asyncio
async def test_adversarial_network_all_traffic_blocked_under_lockdown(adversarial_servers):
    """Verify 100% of network traffic (including same-origin GET/HEAD) is blocked under lockdown."""
    s1, _ = adversarial_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=s1.origin) as session:
            page = await ctx.new_page()
            await page.goto(f"{s1.origin}/disclosure")
            get_count_before = AdversarialTrackingHandler.counts["get_disclosure"]

            # Lock down network
            await session.lock_down()
            assert session.state == BarrierState.LOCKED_DOWN

            # Attempt same-origin GET fetch
            get_outcome = await page.evaluate("""async () => {
                try {
                    await fetch('/disclosure');
                    return 'allowed';
                } catch (e) {
                    return 'blocked';
                }
            }""")
            assert get_outcome == "blocked"

            # Attempt same-origin POST fetch
            post_outcome = await page.evaluate("""async () => {
                try {
                    await fetch('/mutate', { method: 'POST', body: 'lockdown_leak' });
                    return 'allowed';
                } catch (e) {
                    return 'blocked';
                }
            }""")
            assert post_outcome == "blocked"

        await browser.close()

    assert AdversarialTrackingHandler.counts["get_disclosure"] == get_count_before
    assert AdversarialTrackingHandler.counts["post_mutate"] == 0


# ==============================================================================
# 4. Popup & Child-Frame Network Containment
# ==============================================================================


@pytest.mark.asyncio
async def test_popup_initial_requests_contained_under_barrier(adversarial_servers):
    """Verify popup initial requests are guarded and lockdown prevents popup network escapes."""
    s1, s2 = adversarial_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=s1.origin) as session:
            page = await ctx.new_page()
            await page.goto(f"{s1.origin}/popup-opener")

            # 1. Cross-origin popup is blocked from reaching server
            cross_target = f"{s2.origin}/cross"
            popup_cross_promise = ctx.wait_for_event("page")
            await page.evaluate(f"url => {{ window.open(url, '_blank'); }}", cross_target)
            cross_popup = await popup_cross_promise
            await page.wait_for_timeout(200)
            assert AdversarialTrackingHandler.counts["cross_origin_hit"] == 0

            # 2. Under lockdown, popup requests are aborted
            await session.lock_down()
            popup_promise = ctx.wait_for_event("page")
            await page.click("#open-popup-btn")
            lockdown_popup = await popup_promise
            await page.wait_for_timeout(200)

            # Server should receive 0 hits on /popup-target under lockdown
            assert AdversarialTrackingHandler.counts["get_popup_target"] == 0

        await browser.close()


@pytest.mark.asyncio
async def test_child_frame_requests_contained_under_barrier(adversarial_servers):
    """Verify child frame requests are guarded and cross-origin frames are aborted."""
    s1, s2 = adversarial_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=s1.origin) as session:
            page = await ctx.new_page()
            # Loading frame-parent loads same-origin frame-child
            await page.goto(f"{s1.origin}/frame-parent")
            assert AdversarialTrackingHandler.counts["get_frame_parent"] == 1
            assert AdversarialTrackingHandler.counts["get_frame_child"] == 1

            # Inject cross-origin iframe
            await page.evaluate(f"""() => {{
                const ifr = document.createElement('iframe');
                ifr.src = '{s2.origin}/cross';
                document.body.appendChild(ifr);
            }}""")
            await page.wait_for_timeout(200)
            assert AdversarialTrackingHandler.counts["cross_origin_hit"] == 0

            # Transition to lockdown
            await session.lock_down()
            # Dynamic iframe in lockdown
            await page.evaluate("""() => {
                const ifr = document.createElement('iframe');
                ifr.src = '/disclosure';
                document.body.appendChild(ifr);
            }""")
            await page.wait_for_timeout(200)

        await browser.close()

    assert AdversarialTrackingHandler.counts["cross_origin_hit"] == 0


# ==============================================================================
# 5. Channel API Blocking & Non-Claimed Channels Documentation
# ==============================================================================


@pytest.mark.asyncio
async def test_channel_apis_blocked_and_immutable(adversarial_servers):
    """Verify WebSocket, Worker, SharedWorker, EventSource, WebTransport, and ServiceWorker are blocked."""
    s1, _ = adversarial_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # B2a standard configuration: service_workers="block"
        ctx = await browser.new_context(service_workers="block")

        async with barrier.guard(ctx, bootstrap_origin=s1.origin):
            page = await ctx.new_page()
            await page.goto(f"{s1.origin}/")

            outcomes = await page.evaluate("""() => {
                const res = {};
                try {
                    new WebSocket('ws://127.0.0.1:9');
                    res.ws = 'created';
                } catch (e) {
                    res.ws = e.message;
                }

                try {
                    new Worker('data:text/javascript,');
                    res.worker = 'created';
                } catch (e) {
                    res.worker = e.message;
                }

                if (typeof SharedWorker !== 'undefined') {
                    try {
                        new SharedWorker('data:text/javascript,');
                        res.sharedWorker = 'created';
                    } catch (e) {
                        res.sharedWorker = e.message;
                    }
                } else {
                    res.sharedWorker = 'not_present';
                }

                try {
                    new EventSource('http://127.0.0.1:9/stream');
                    res.eventsource = 'created';
                } catch (e) {
                    res.eventsource = e.message;
                }

                if (typeof WebTransport !== 'undefined') {
                    try {
                        new WebTransport('https://127.0.0.1:9');
                        res.webtransport = 'created';
                    } catch (e) {
                        res.webtransport = e.message;
                    }
                } else {
                    res.webtransport = 'not_present';
                }

                // Check constructor immutability
                try {
                    window.WebSocket = 999;
                } catch (e) {}
                res.wsTypeAfter = typeof window.WebSocket;

                return res;
            }""")

            assert "WebSocket creation blocked" in outcomes.get("ws", "")
            assert "Worker creation blocked" in outcomes.get("worker", "")
            if outcomes.get("sharedWorker") != "not_present":
                assert "SharedWorker creation blocked" in outcomes.get("sharedWorker", "")
            assert "EventSource creation blocked" in outcomes.get("eventsource", "")
            if outcomes.get("webtransport") != "not_present":
                assert "WebTransport creation blocked" in outcomes.get("webtransport", "")
            assert outcomes.get("wsTypeAfter") == "function"

            # Service Worker registration check
            sw_status = await page.evaluate("""async () => {
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
            assert sw_status.get("controller") == "null"
            assert AdversarialTrackingHandler.counts["sw_script"] == 0

        await browser.close()


@pytest.mark.asyncio
async def test_document_channels_outside_blocking_contract_explicitly(adversarial_servers):
    """Explicitly verify and document that WebRTC and BroadcastChannel fall outside B2b monkeypatch contract.

    This test confirms that B2b does not make false claims of blocking WebRTC (RTCPeerConnection)
    or BroadcastChannel via init script stubs. The isolation contract relies on OS/process-level
    boundaries and absence of authorization rather than unsupported channel monkeypatching.
    """
    s1, _ = adversarial_servers
    barrier = IsolatedNetworkBarrier()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()

        async with barrier.guard(ctx, bootstrap_origin=s1.origin):
            page = await ctx.new_page()
            await page.goto(f"{s1.origin}/")

            channel_status = await page.evaluate("""() => {
                return {
                    rtcPresent: typeof window.RTCPeerConnection !== 'undefined',
                    rtcIsFunction: typeof window.RTCPeerConnection === 'function',
                    broadcastChannelPresent: typeof window.BroadcastChannel !== 'undefined',
                    broadcastChannelIsFunction: typeof window.BroadcastChannel === 'function',
                };
            }""")

            # Documented boundary: RTCPeerConnection and BroadcastChannel are standard browser APIs
            # that are NOT monkeypatched or claimed as blocked by IsolatedNetworkBarrier scripts.
            assert channel_status["rtcPresent"] is True
            assert channel_status["broadcastChannelPresent"] is True

        await browser.close()


# ==============================================================================
# 6. Target Identity Across Frames & Adversarial DOM Changes
# ==============================================================================


@pytest.mark.asyncio
async def test_exact_target_identity_duplicate_id_in_child_frame_vs_parent(adversarial_servers):
    """Regression 1: Verify duplicate ID in main frame vs child iframe resolves strictly to the approved frame."""
    s1, _ = adversarial_servers
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{s1.origin}/frame-parent"
        await source_page.goto(target_url)

        # Wait for child iframe to load
        child_el = await source_page.wait_for_selector("#child-iframe")
        assert child_el is not None

        # Derive intent pointing specifically to child frame button "0.0:same-id"
        intent = await _derive_intent_from_page(source_page, "0.0:same-id")
        assert intent.control_fingerprint == "0.0:same-id"
        assert intent.risk_classification == RiskClassification.SAFE_CANDIDATE

        observed_disposable_parent_clicks = []
        observed_disposable_child_clicks = []
        tracking_errors = []

        orig_reconstruct = executor._reconstructor.reconstruct

        @asynccontextmanager
        async def tracking_reconstruct(*args, **kwargs):
            async with orig_reconstruct(*args, **kwargs) as sess:
                disp_page = sess.page
                orig_close = disp_page.close

                # Observe the disposable page before teardown / page closure
                async def tracking_close(*c_args, **c_kwargs):
                    try:
                        p_clicks = await disp_page.evaluate("() => window.__wrong_clicks || 0")
                        observed_disposable_parent_clicks.append(p_clicks)
                        child_frame = next((f for f in disp_page.frames if "frame-child" in f.url), None)
                        if child_frame is None and len(disp_page.frames) > 1:
                            child_frame = disp_page.frames[1]
                        if child_frame:
                            c_clicks = await child_frame.evaluate("() => window.__child_clicks || 0")
                            observed_disposable_child_clicks.append(c_clicks)
                        else:
                            tracking_errors.append("Child frame not found in disposable page")
                    except Exception as exc:
                        tracking_errors.append(f"Observation error: {exc}")
                    return await orig_close(*c_args, **c_kwargs)

                disp_page.close = tracking_close
                yield sess

        executor._reconstructor.reconstruct = tracking_reconstruct

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert not tracking_errors, f"Pre-teardown observation failed with errors: {tracking_errors}"
        assert res.status == GuardedExecutionStatus.EXECUTED
        assert res.action_executed is True
        assert res.action_confirmed is True
        assert res.target_control_id == "0.0:same-id"

        # 1. Observed in disposable page before teardown:
        # Intended child-frame button receives exactly one click; main-frame duplicate receives zero clicks
        assert observed_disposable_parent_clicks == [0], (
            f"Disposable main-frame duplicate received unexpected clicks: {observed_disposable_parent_clicks}"
        )
        assert observed_disposable_child_clicks == [1], (
            f"Intended child-frame button did not receive exactly 1 click: {observed_disposable_child_clicks}"
        )

        # 2. Source-context invariance check:
        # Neither parent nor child buttons were ever clicked in source page
        source_parent_clicks = await source_page.evaluate("() => window.__wrong_clicks || 0")
        assert source_parent_clicks == 0
        source_child_frame = source_page.frames[1] if len(source_page.frames) > 1 else None
        assert source_child_frame is not None
        source_child_clicks = await source_child_frame.evaluate("() => window.__child_clicks || 0")
        assert source_child_clicks == 0

        assert browser.contexts == [source_ctx]
        await browser.close()


@pytest.mark.asyncio
async def test_adversarial_dom_mutation_fingerprint_mismatch_fails_closed(adversarial_servers):
    """Regression 2: Verify pre-action DOM change triggers truthful STALE_STATE with zero click attempts."""
    s1, _ = adversarial_servers
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{s1.origin}/disclosure"
        await source_page.goto(target_url)
        intent = await _derive_intent_from_page(source_page, "faq-toggle")

        orig_reconstruct = executor._reconstructor.reconstruct

        # Intercept reconstruction to mutate DOM text prior to yielding to executor
        @asynccontextmanager
        async def mutating_reconstruct(*args, **kwargs):
            async with orig_reconstruct(*args, **kwargs) as sess:
                disp_page = sess.page
                # Mutate button text to invalidate state fingerprint
                await disp_page.evaluate("""() => {
                    const btn = document.getElementById('faq-toggle');
                    if (btn) btn.innerText = 'ADVERSARIAL_MUTATED_TEXT';
                }""")
                yield sess

        executor._reconstructor.reconstruct = mutating_reconstruct

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        # Must fail closed with STALE_STATE
        assert res.status == GuardedExecutionStatus.STALE_STATE
        assert res.action_executed is False
        assert res.action_attempted is False
        assert res.action_confirmed is False
        assert "stale state" in (res.error_message or "").lower()

        assert browser.contexts == [source_ctx]
        await browser.close()


@pytest.mark.asyncio
async def test_adversarial_dom_target_removed_fails_closed(adversarial_servers):
    """Verify target element removed prior to execution fails closed with TARGET_NOT_FOUND and zero clicks."""
    s1, _ = adversarial_servers
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{s1.origin}/disclosure"
        await source_page.goto(target_url)
        intent = await _derive_intent_from_page(source_page, "faq-toggle")

        orig_reconstruct = executor._reconstructor.reconstruct

        @asynccontextmanager
        async def removing_reconstruct(*args, **kwargs):
            async with orig_reconstruct(*args, **kwargs) as sess:
                disp_page = sess.page
                await disp_page.evaluate("""() => {
                    const btn = document.getElementById('faq-toggle');
                    if (btn) btn.remove();
                }""")
                yield sess

        executor._reconstructor.reconstruct = removing_reconstruct

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        # STALE_STATE or TARGET_NOT_FOUND (both fail closed with zero clicks)
        assert res.status in (GuardedExecutionStatus.STALE_STATE, GuardedExecutionStatus.TARGET_NOT_FOUND)
        assert res.action_executed is False
        assert res.action_attempted is False

        assert browser.contexts == [source_ctx]
        await browser.close()


# ==============================================================================
# 7. Single Guarded Action & No Retries/Fallbacks
# ==============================================================================


@pytest.mark.asyncio
async def test_one_guarded_action_no_application_retry_or_js_click_on_occluded_target(adversarial_servers):
    """Verify occluded target fails closed as ACTION_FAILED without force click, JS click, or fallback."""
    s1, _ = adversarial_servers
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{s1.origin}/occluded"
        await source_page.goto(target_url)
        intent = await _derive_intent_from_page(source_page, "faq-toggle")

        click_calls = []
        observed_js_clicks = []
        tracking_errors = []
        frame_locator_instrumented = False
        locator_click_instrumented = False

        orig_reconstruct = executor._reconstructor.reconstruct

        @asynccontextmanager
        async def instrumented_reconstruct(*args, **kwargs):
            nonlocal frame_locator_instrumented, locator_click_instrumented
            async with orig_reconstruct(*args, **kwargs) as sess:
                disp_page = sess.page
                target_frame = disp_page.main_frame

                # Track any synthetic JS clicks on the target element
                await disp_page.evaluate("""() => {
                    window.__js_click_count = 0;
                    const el = document.getElementById('faq-toggle');
                    if (el) {
                        const origClick = el.click.bind(el);
                        el.click = function(...args) {
                            window.__js_click_count += 1;
                            return origClick(...args);
                        };
                    }
                }""")

                # Instrument the actual Frame.locator execution path
                orig_frame_locator = target_frame.locator

                def instrumented_frame_locator(selector, **loc_kwargs):
                    nonlocal frame_locator_instrumented
                    frame_locator_instrumented = True
                    loc = orig_frame_locator(selector, **loc_kwargs)
                    orig_click = loc.click

                    async def instrumented_click(*c_args, **c_kwargs):
                        nonlocal locator_click_instrumented
                        locator_click_instrumented = True
                        click_calls.append({
                            "args": c_args,
                            "kwargs": dict(c_kwargs),
                            "force": c_kwargs.get("force", False),
                        })
                        # Bound Playwright actionability wait to 1000ms for deterministic test execution.
                        # Note: Playwright's locator.click() internally performs actionability checks
                        # (visible, stable, enabled, receiving pointer events). Because the button is
                        # occluded by a full-screen overlay, Playwright's single click call will wait for
                        # actionability until timeout, which is internal engine waiting, NOT an
                        # application-level retry.
                        c_kwargs["timeout"] = 1000
                        return await orig_click(*c_args, **c_kwargs)

                    loc.click = instrumented_click
                    return loc

                target_frame.locator = instrumented_frame_locator

                # Track before disposable page closes
                orig_close = disp_page.close

                async def tracking_close(*c_args, **c_kwargs):
                    try:
                        js_clicks = await disp_page.evaluate("() => window.__js_click_count || 0")
                        observed_js_clicks.append(js_clicks)
                    except Exception as exc:
                        tracking_errors.append(f"Observation error: {exc}")
                    return await orig_close(*c_args, **c_kwargs)

                disp_page.close = tracking_close
                yield sess

        executor._reconstructor.reconstruct = instrumented_reconstruct

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert not tracking_errors, f"Pre-teardown observation encountered errors: {tracking_errors}"
        # 1. Assert instrumentation was reached on the actual Frame.locator execution path
        assert frame_locator_instrumented is True, "Frame.locator instrumentation was not reached!"
        assert locator_click_instrumented is True, "locator.click instrumentation was not reached!"

        # 2. Count application-level click calls (must be exactly 1; zero application-level retries)
        assert len(click_calls) == 1, (
            f"Expected exactly 1 application-level click call, got {len(click_calls)}"
        )

        # 3. Verify no force click was attempted
        assert click_calls[0]["force"] is False, "Application improperly used {force: True} click!"

        # 4. Verify no synthetic JavaScript click fallback was executed
        assert observed_js_clicks == [0], (
            f"Application fell back to synthetic JavaScript click: {observed_js_clicks}"
        )

        # 5. Verify truthful status outcomes and cleanup
        assert res.status == GuardedExecutionStatus.ACTION_FAILED
        assert res.action_executed is False
        assert res.action_attempted is True
        assert res.action_confirmed is False

        assert browser.contexts == [source_ctx]
        await browser.close()


@pytest.mark.asyncio
async def test_one_guarded_action_single_click_dispatched_on_valid_target(adversarial_servers):
    """Verify exactly one click is dispatched to a valid disclosure control, with zero extra clicks."""
    s1, _ = adversarial_servers
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{s1.origin}/disclosure"
        await source_page.goto(target_url)
        intent = await _derive_intent_from_page(source_page, "faq-toggle")

        observed_clicks = []
        orig_reconstruct = executor._reconstructor.reconstruct

        @asynccontextmanager
        async def tracking_reconstruct(*args, **kwargs):
            async with orig_reconstruct(*args, **kwargs) as sess:
                disp_page = sess.page
                orig_close = disp_page.close

                async def tracking_close(*c_args, **c_kwargs):
                    try:
                        clicks = await disp_page.evaluate("() => window.__click_count || 0")
                        observed_clicks.append(clicks)
                    except Exception:
                        pass
                    return await orig_close(*c_args, **c_kwargs)

                disp_page.close = tracking_close
                yield sess

        executor._reconstructor.reconstruct = tracking_reconstruct

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert res.status == GuardedExecutionStatus.EXECUTED
        assert res.action_executed is True
        assert len(observed_clicks) == 1
        assert observed_clicks[0] == 1, f"Expected exactly 1 click, got {observed_clicks[0]}"

        assert browser.contexts == [source_ctx]
        await browser.close()


# ==============================================================================
# 8. Distinguishable Action States & Preserved Teardown History
# ==============================================================================


@pytest.mark.asyncio
async def test_distinguishable_action_attempted_vs_confirmed_on_dispatch_failure(adversarial_servers):
    """Verify action_attempted is distinguished from action_confirmed when click times out or fails."""
    s1, _ = adversarial_servers
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{s1.origin}/occluded"
        await source_page.goto(target_url)
        intent = await _derive_intent_from_page(source_page, "faq-toggle")

        orig_reconstruct = executor._reconstructor.reconstruct

        # Intercept reconstruction to use a 1s timeout for fast deterministic timeout failure
        @asynccontextmanager
        async def fast_occluded_reconstruct(*args, **kwargs):
            async with orig_reconstruct(*args, **kwargs) as sess:
                disp_page = sess.page
                orig_frame_locator = disp_page.main_frame.locator

                def fast_frame_locator(selector, **loc_kwargs):
                    loc = orig_frame_locator(selector, **loc_kwargs)
                    orig_click = loc.click

                    async def fast_click(*c_args, **c_kwargs):
                        c_kwargs["timeout"] = 1000
                        return await orig_click(*c_args, **c_kwargs)

                    loc.click = fast_click
                    return loc

                disp_page.main_frame.locator = fast_frame_locator
                yield sess

        executor._reconstructor.reconstruct = fast_occluded_reconstruct

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert res.status == GuardedExecutionStatus.ACTION_FAILED
        # Invariant: action_executed=False does NOT mean nothing was attempted.
        # action_attempted=True proves an attempt occurred, while action_confirmed=False proves no completion.
        assert res.action_executed is False
        assert res.action_attempted is True
        assert res.action_confirmed is False

        assert browser.contexts == [source_ctx]
        await browser.close()


@pytest.mark.asyncio
async def test_action_history_preserved_when_teardown_fails_after_confirmed_click(adversarial_servers):
    """Regression 3: Cleanup failure after confirmed click surfaces CLEANUP_FAILED without erasing action history."""
    s1, _ = adversarial_servers
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{s1.origin}/disclosure"
        await source_page.goto(target_url)
        intent = await _derive_intent_from_page(source_page, "faq-toggle")

        orig_reconstruct = executor._reconstructor.reconstruct

        @asynccontextmanager
        async def failing_teardown_reconstruct(*args, **kwargs):
            async with orig_reconstruct(*args, **kwargs) as sess:
                yield sess
            # Terminal verification failure upon exit
            raise StateReconstructionError("Simulated terminal context disposal failure")

        executor._reconstructor.reconstruct = failing_teardown_reconstruct

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=target_url,
            candidate_intent=intent,
        )

        assert res.status == GuardedExecutionStatus.CLEANUP_FAILED
        # Invariant: Confirmed action is NOT erased by cleanup failure
        assert res.action_executed is True
        assert res.action_attempted is True
        assert res.action_confirmed is True
        assert "Terminal teardown failure after action execution" in (res.error_message or "")

        assert browser.contexts == [source_ctx]
        await browser.close()


# ==============================================================================
# 9. Resource Disposal Verification Before Browser Close
# ==============================================================================


@pytest.mark.asyncio
async def test_resource_cleanup_verified_before_browser_close_on_success_and_failures(adversarial_servers):
    """Verify disposable context is closed BEFORE browser.close() across success and various failure modes."""
    s1, _ = adversarial_servers
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{s1.origin}/disclosure"
        await source_page.goto(target_url)
        valid_intent = await _derive_intent_from_page(source_page, "faq-toggle")

        # 1. Successful execution
        res1 = await executor.execute(source_ctx, target_url, valid_intent)
        assert res1.status == GuardedExecutionStatus.EXECUTED
        assert browser.contexts == [source_ctx], "Disposable context was not closed after success!"

        # 2. Risk tier rejection (DISALLOWED_RISK_TIER)
        mutating_intent = CandidateActionIntent(
            parent_state_fingerprint=valid_intent.parent_state_fingerprint,
            control_fingerprint=valid_intent.control_fingerprint,
            expected_action_type="click",
            risk_classification=RiskClassification.POTENTIALLY_MUTATING,
        )
        res2 = await executor.execute(source_ctx, target_url, mutating_intent)
        assert res2.status == GuardedExecutionStatus.DISALLOWED_RISK_TIER
        assert browser.contexts == [source_ctx], "Disposable context leaked on risk tier rejection!"

        # 3. Non-disclosure control rejection (NOT_DISCLOSURE_CONTROL)
        plain_page = await source_ctx.new_page()
        await plain_page.goto(f"{s1.origin}/plain-button")
        plain_intent = await _derive_intent_from_page(plain_page, "plain-btn")
        await plain_page.close()

        res3 = await executor.execute(source_ctx, f"{s1.origin}/plain-button", plain_intent)
        assert res3.status == GuardedExecutionStatus.NOT_DISCLOSURE_CONTROL
        assert browser.contexts == [source_ctx], "Disposable context leaked on non-disclosure rejection!"

        await browser.close()


@pytest.mark.asyncio
async def test_resource_cleanup_verified_on_cancellation_and_repeated_cancellation(adversarial_servers):
    """Verify disposable resources are reliably closed when execution task is repeatedly cancelled.

    Synchronizes the first cancellation during the active reconstruction session and the second
    cancellation while resource cleanup is actively in progress. Verifies that disposable context
    is closed before browser.close() and CancelledError propagates without hanging.
    """
    s1, _ = adversarial_servers
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        target_url = f"{s1.origin}/disclosure"
        await source_page.goto(target_url)
        intent = await _derive_intent_from_page(source_page, "faq-toggle")

        session_active = asyncio.Event()
        cleanup_in_progress = asyncio.Event()
        cleanup_release = asyncio.Event()

        orig_reconstruct = executor._reconstructor.reconstruct

        @asynccontextmanager
        async def cancellable_reconstruct(*args, **kwargs):
            async with orig_reconstruct(*args, **kwargs) as sess:
                disp_page = sess.page
                orig_close = disp_page.close

                # Synchronize second cancellation during page cleanup
                async def delayed_close(*c_args, **c_kwargs):
                    cleanup_in_progress.set()
                    # Bounded wait for second cancellation and release
                    await asyncio.wait_for(cleanup_release.wait(), timeout=5.0)
                    return await orig_close(*c_args, **c_kwargs)

                disp_page.close = delayed_close
                session_active.set()

                # Await sleep until first cancellation occurs
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    raise
                yield sess

        executor._reconstructor.reconstruct = cancellable_reconstruct

        exec_task = asyncio.create_task(executor.execute(source_ctx, target_url, intent))

        # 1. Bounded wait for active reconstruction session
        await asyncio.wait_for(session_active.wait(), timeout=5.0)

        # 2. First cancellation: cancel task during active session
        exec_task.cancel()

        # 3. Bounded wait until cleanup is confirmed to be in progress
        await asyncio.wait_for(cleanup_in_progress.wait(), timeout=5.0)

        # 4. Second cancellation: cancel task AGAIN while cleanup is actively in progress
        exec_task.cancel()

        # 5. Release cleanup and await task completion with bounded timeout
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(exec_task, timeout=5.0)

        # 6. Crucial invariant: disposable context was completely closed before browser.close()
        assert browser.contexts == [source_ctx], "Disposable context leaked upon repeated cancellation!"
        assert not source_page.is_closed(), "Source page was unexpectedly closed!"

        await browser.close()


# ==============================================================================
# 10. Privacy-Safe Diagnostics and Credential Redaction
# ==============================================================================


@pytest.mark.asyncio
async def test_privacy_safe_diagnostics_and_no_auth_artifacts(adversarial_servers):
    """Verify synthetic credentials in URLs and query tokens are strictly redacted from results and reprs."""
    s1, _ = adversarial_servers
    executor = GuardedActionExecutor()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        source_ctx = await browser.new_context()
        source_page = await source_ctx.new_page()

        # Target URL containing sensitive credentials and query token
        target_url = f"{s1.origin}/disclosure"
        sensitive_url = f"http://admin:synthetic_secret_password@127.0.0.1:{s1.port}/disclosure?secret_token=abc123xyz&user=admin"

        await source_page.goto(target_url)
        intent = await _derive_intent_from_page(source_page, "faq-toggle")

        res = await executor.execute(
            source_context=source_ctx,
            approved_target_url=sensitive_url,
            candidate_intent=intent,
        )

        res_repr = repr(res)
        res_str = str(res)
        err_msg = res.error_message or ""

        # Sensitive passwords and tokens must never appear in representation or messages
        assert "synthetic_secret_password" not in res_repr, "Password leaked in repr(result)!"
        assert "synthetic_secret_password" not in res_str, "Password leaked in str(result)!"
        assert "synthetic_secret_password" not in err_msg, "Password leaked in error_message!"

        assert "secret_token=abc123xyz" not in res_repr, "Query token leaked in repr(result)!"
        assert "secret_token=abc123xyz" not in res_str, "Query token leaked in str(result)!"
        assert "secret_token=abc123xyz" not in err_msg, "Query token leaked in error_message!"

        assert browser.contexts == [source_ctx]
        await browser.close()

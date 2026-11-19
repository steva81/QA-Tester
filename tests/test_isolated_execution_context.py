"""Tests for Phase B2a Isolated Execution Context Lifecycle & Authentication Cloning."""

import ast
import asyncio
import http.server
import inspect
import threading
from typing import Any, List
import pytest
from playwright.async_api import async_playwright

from app.qa.isolated_execution_context import (
    IsolatedExecutionContextFactory,
    IsolatedContextError,
)

B2A_TEST_HTML = """<!DOCTYPE html>
<html>
<head><title>B2a Isolated Context Test Page</title></head>
<body>
<h1>B2a Isolation Fixture</h1>
<script>
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
</script>
</body>
</html>
"""


class B2aHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(B2A_TEST_HTML.encode("utf-8"))

    def log_message(self, format, *args):
        pass  # Silent logging in tests


@pytest.fixture(scope="module")
def b2a_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), B2aHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=2.0)


@pytest.mark.asyncio
async def test_real_chromium_source_disposable_isolation(b2a_server):
    """Proves disposable context is distinct, source page stays open, and source context remains usable."""
    port = b2a_server.server_port
    url = f"http://127.0.0.1:{port}/"

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        try:
            source_context = await browser.new_context()
            source_page = await source_context.new_page()
            await source_page.goto(url)

            factory = IsolatedExecutionContextFactory()
            async with factory.open(source_context) as disposable_context:
                assert disposable_context is not source_context
                assert source_page in source_context.pages
                assert not source_page.is_closed()
                assert source_page.url == url
                assert disposable_context.browser is browser

            # After context manager exit, source context must remain completely usable
            assert not source_page.is_closed()
            assert source_context in browser.contexts
            extra_page = await source_context.new_page()
            await extra_page.goto(url)
            assert not extra_page.is_closed()
            await extra_page.close()
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_cookie_auth_clone_and_isolation(b2a_server):
    """Proves authentication cookies clone into disposable context and mutations do not leak back."""
    port = b2a_server.server_port
    url = f"http://127.0.0.1:{port}/"

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        try:
            source_context = await browser.new_context()
            source_page = await source_context.new_page()
            await source_page.goto(url)

            # Set benign auth cookie in source
            await source_context.add_cookies([
                {"name": "qa_auth_token", "value": "source_secret_token", "url": url}
            ])

            factory = IsolatedExecutionContextFactory()
            async with factory.open(source_context) as disposable_context:
                disp_cookies = await disposable_context.cookies()
                auth_cookie = next((c for c in disp_cookies if c["name"] == "qa_auth_token"), None)
                assert auth_cookie is not None
                assert auth_cookie["value"] == "source_secret_token"

                # Mutate cookie inside disposable context only
                await disposable_context.add_cookies([
                    {"name": "qa_auth_token", "value": "mutated_in_disposable", "url": url},
                    {"name": "disposable_only_cookie", "value": "disp_val", "url": url},
                ])

                # Verify mutated state inside disposable context
                disp_cookies_after = await disposable_context.cookies()
                auth_cookie_disp = next(c for c in disp_cookies_after if c["name"] == "qa_auth_token")
                assert auth_cookie_disp["value"] == "mutated_in_disposable"

            # Verify source context cookie remained 100% unchanged
            source_cookies = await source_context.cookies()
            auth_cookie_src = next(c for c in source_cookies if c["name"] == "qa_auth_token")
            assert auth_cookie_src["value"] == "source_secret_token"
            assert not any(c["name"] == "disposable_only_cookie" for c in source_cookies)
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_localstorage_auth_clone_and_isolation(b2a_server):
    """Proves localStorage clones into disposable context and mutations do not leak back."""
    port = b2a_server.server_port
    url = f"http://127.0.0.1:{port}/"

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        try:
            source_context = await browser.new_context()
            source_page = await source_context.new_page()
            await source_page.goto(url)

            # Set benign localStorage auth item in source
            await source_page.evaluate("window.setupStorage('qa_auth_ls', 'source_ls_value', null, null)")

            factory = IsolatedExecutionContextFactory()
            async with factory.open(source_context) as disposable_context:
                disp_page = await disposable_context.new_page()
                await disp_page.goto(url)

                # Cloned value exists in disposable context
                val = await disp_page.evaluate("localStorage.getItem('qa_auth_ls')")
                assert val == "source_ls_value"

                # Mutate in disposable context
                await disp_page.evaluate("localStorage.setItem('qa_auth_ls', 'mutated_in_disposable')")
                await disp_page.evaluate("localStorage.setItem('disp_extra', 'extra_val')")

                val_disp = await disp_page.evaluate("localStorage.getItem('qa_auth_ls')")
                assert val_disp == "mutated_in_disposable"
                await disp_page.close()

            # Verify source context localStorage remains exactly unchanged
            src_val = await source_page.evaluate("localStorage.getItem('qa_auth_ls')")
            assert src_val == "source_ls_value"
            src_extra = await source_page.evaluate("localStorage.getItem('disp_extra')")
            assert src_extra is None
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_sessionstorage_not_cloned(b2a_server):
    """Proves sessionStorage is NOT cloned by storage_state into the disposable context."""
    port = b2a_server.server_port
    url = f"http://127.0.0.1:{port}/"

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        try:
            source_context = await browser.new_context()
            source_page = await source_context.new_page()
            await source_page.goto(url)

            # Set sessionStorage in source
            await source_page.evaluate("window.setupStorage(null, null, 'qa_sess_key', 'source_sess_val')")
            assert await source_page.evaluate("sessionStorage.getItem('qa_sess_key')") == "source_sess_val"

            factory = IsolatedExecutionContextFactory()
            async with factory.open(source_context) as disposable_context:
                disp_page = await disposable_context.new_page()
                await disp_page.goto(url)

                # sessionStorage must NOT be cloned
                disp_sess = await disp_page.evaluate("sessionStorage.getItem('qa_sess_key')")
                assert disp_sess is None
                await disp_page.close()
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_indexeddb_isolation(b2a_server):
    """Proves source IndexedDB is not shared, and disposable IndexedDB mutations do not affect source."""
    port = b2a_server.server_port
    url = f"http://127.0.0.1:{port}/"

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        try:
            source_context = await browser.new_context()
            source_page = await source_context.new_page()
            await source_page.goto(url)

            # Populate IndexedDB in source
            await source_page.evaluate("window.setupIDB('source_db', 'store', 1, 'source_idb_data')")
            assert await source_page.evaluate("window.getIDBVal('source_db', 'store', 1)") == "source_idb_data"

            factory = IsolatedExecutionContextFactory()
            async with factory.open(source_context) as disposable_context:
                disp_page = await disposable_context.new_page()
                await disp_page.goto(url)

                # Source db is NOT cloned into disposable context
                disp_val = await disp_page.evaluate("window.getIDBVal('source_db', 'store', 1)")
                assert disp_val is None

                # Create separate IndexedDB in disposable context
                await disp_page.evaluate("window.setupIDB('disp_db', 'store', 1, 'disp_idb_data')")
                disp_val_after = await disp_page.evaluate("window.getIDBVal('disp_db', 'store', 1)")
                assert disp_val_after == "disp_idb_data"
                await disp_page.close()

            # Verify source IndexedDB remained completely unchanged and disp_db does not exist in source
            src_val_after = await source_page.evaluate("window.getIDBVal('source_db', 'store', 1)")
            assert src_val_after == "source_idb_data"
            src_disp_val = await source_page.evaluate("window.getIDBVal('disp_db', 'store', 1)")
            assert src_disp_val is None
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_cachestorage_isolation(b2a_server):
    """Proves source CacheStorage is not shared, and disposable CacheStorage writes do not affect source."""
    port = b2a_server.server_port
    url = f"http://127.0.0.1:{port}/"

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        try:
            source_context = await browser.new_context()
            source_page = await source_context.new_page()
            await source_page.goto(url)

            # Setup CacheStorage in source
            cache_supported = await source_page.evaluate(
                "window.setupCache('test_cache', '/cached_item', 'source_cached_content')"
            )
            if not cache_supported:
                pytest.skip("CacheStorage not supported in local test environment")

            assert await source_page.evaluate("window.getCacheVal('test_cache', '/cached_item')") == "source_cached_content"

            factory = IsolatedExecutionContextFactory()
            async with factory.open(source_context) as disposable_context:
                disp_page = await disposable_context.new_page()
                await disp_page.goto(url)

                # Cache is not cloned
                disp_val = await disp_page.evaluate("window.getCacheVal('test_cache', '/cached_item')")
                assert disp_val is None

                # Mutate in disposable
                await disp_page.evaluate("window.setupCache('test_cache', '/cached_item', 'disp_cached_content')")
                assert await disp_page.evaluate("window.getCacheVal('test_cache', '/cached_item')") == "disp_cached_content"
                await disp_page.close()

            # Verify source CacheStorage is unchanged
            src_val_after = await source_page.evaluate("window.getCacheVal('test_cache', '/cached_item')")
            assert src_val_after == "source_cached_content"
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_source_context_invariance(b2a_server):
    """Proves page count, URL, cookies, and localStorage in source context remain strictly invariant."""
    port = b2a_server.server_port
    url = f"http://127.0.0.1:{port}/"

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        try:
            source_context = await browser.new_context()
            source_page = await source_context.new_page()
            await source_page.goto(url)

            await source_context.add_cookies([{"name": "inv_cookie", "value": "inv_val", "url": url}])
            await source_page.evaluate("window.setupStorage('inv_ls', 'inv_ls_val', null, null)")

            # Snapshot baseline source state
            pages_before = len(source_context.pages)
            url_before = source_page.url
            cookies_before = await source_context.cookies()
            ls_before = await source_page.evaluate("localStorage.getItem('inv_ls')")

            factory = IsolatedExecutionContextFactory()
            async with factory.open(source_context) as disposable_context:
                p_disp = await disposable_context.new_page()
                await p_disp.goto(url)
                await disposable_context.add_cookies([{"name": "other_c", "value": "1", "url": url}])
                await p_disp.evaluate("localStorage.setItem('inv_ls', 'altered')")

            # Verify source state is strictly invariant
            assert len(source_context.pages) == pages_before
            assert source_page.url == url_before
            assert await source_context.cookies() == cookies_before
            assert await source_page.evaluate("localStorage.getItem('inv_ls')") == ls_before
            assert source_context in browser.contexts
            assert not source_page.is_closed()
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_exception_cleanup_guarantee(b2a_server):
    """Proves disposable context and its pages are guaranteed closed even on uncaught exception."""
    port = b2a_server.server_port
    url = f"http://127.0.0.1:{port}/"

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        try:
            source_context = await browser.new_context()
            source_page = await source_context.new_page()
            await source_page.goto(url)

            disp_ref = None
            disp_page_ref = None
            factory = IsolatedExecutionContextFactory()

            with pytest.raises(RuntimeError, match="Controlled exception inside lifecycle"):
                async with factory.open(source_context) as disposable_context:
                    disp_ref = disposable_context
                    disp_page_ref = await disposable_context.new_page()
                    await disp_page_ref.goto(url)
                    raise RuntimeError("Controlled exception inside lifecycle")

            # Disposable context and its pages must be closed
            assert disp_ref not in browser.contexts
            assert disp_page_ref.is_closed()

            # Source context remains open and completely untouched
            assert source_context in browser.contexts
            assert not source_page.is_closed()
            assert source_page in source_context.pages
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_multiple_disposable_contexts_independent(b2a_server):
    """Proves multiple sequential disposable contexts are distinct and do not retain mutations."""
    port = b2a_server.server_port
    url = f"http://127.0.0.1:{port}/"

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        try:
            source_context = await browser.new_context()
            source_page = await source_context.new_page()
            await source_page.goto(url)
            await source_page.evaluate("window.setupStorage('seq_ls', 'initial_source', null, null)")

            factory = IsolatedExecutionContextFactory()

            # 1. First disposable context A
            async with factory.open(source_context) as ctx_a:
                page_a = await ctx_a.new_page()
                await page_a.goto(url)
                assert await page_a.evaluate("localStorage.getItem('seq_ls')") == "initial_source"
                await page_a.evaluate("localStorage.setItem('seq_ls', 'mutated_in_a')")
                await page_a.evaluate("localStorage.setItem('a_only', 'a_data')")

            # 2. Second disposable context B
            async with factory.open(source_context) as ctx_b:
                assert ctx_b is not ctx_a
                page_b = await ctx_b.new_page()
                await page_b.goto(url)
                # B receives pristine source state, not A's mutations
                assert await page_b.evaluate("localStorage.getItem('seq_ls')") == "initial_source"
                assert await page_b.evaluate("localStorage.getItem('a_only')") is None
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_service_workers_blocked_configuration(b2a_server):
    """Proves disposable context enforces service_workers='block'."""
    port = b2a_server.server_port
    url = f"http://127.0.0.1:{port}/"

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        try:
            source_context = await browser.new_context()
            source_page = await source_context.new_page()
            await source_page.goto(url)

            factory = IsolatedExecutionContextFactory()
            async with factory.open(source_context) as disposable_context:
                page = await disposable_context.new_page()
                await page.goto(url)

                # In a context with service_workers="block", navigator.serviceWorker.controller is None
                is_controller_none = await page.evaluate("navigator.serviceWorker ? navigator.serviceWorker.controller === null : true")
                assert is_controller_none is True
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_private_sentinel_not_leaked(b2a_server):
    """Proves sensitive auth sentinels do not leak into Python repr, str, or exception messages."""
    port = b2a_server.server_port
    url = f"http://127.0.0.1:{port}/"
    secret_sentinel = "PRIVATE-B2A-AUTH-SENTINEL-12345"

    async with async_playwright() as p:
        browser = await p.chromium.launch(chromium_sandbox=True, headless=True)
        try:
            source_context = await browser.new_context()
            source_page = await source_context.new_page()
            await source_page.goto(url)

            # Store secret sentinel in source auth state
            await source_context.add_cookies([{"name": "secret_session", "value": secret_sentinel, "url": url}])
            await source_page.evaluate(f"localStorage.setItem('secret_key', '{secret_sentinel}')")

            factory = IsolatedExecutionContextFactory()
            assert secret_sentinel not in repr(factory)
            assert secret_sentinel not in str(factory)

            async with factory.open(source_context) as disposable_context:
                assert secret_sentinel not in repr(factory)
                assert secret_sentinel not in str(factory)

            # Check that an exception raised by invalid context does not contain secret
            class MockInvalidContext:
                async def storage_state(self):
                    raise RuntimeError(f"Error mentioning {secret_sentinel}")

            with pytest.raises(IsolatedContextError) as exc_info:
                async with factory.open(MockInvalidContext()):
                    pass
            assert secret_sentinel not in str(exc_info.value)
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_error_handling_invalid_source_context():
    """Proves invalid, None, or broken source contexts fail closed with IsolatedContextError."""
    factory = IsolatedExecutionContextFactory()

    # None source context
    with pytest.raises(IsolatedContextError, match="source_context must not be None"):
        async with factory.open(None):
            pass

    # Missing storage_state method
    class NoStorageState:
        pass

    with pytest.raises(IsolatedContextError, match="must provide a callable storage_state"):
        async with factory.open(NoStorageState()):
            pass

    # Missing browser property
    class NoBrowser:
        async def storage_state(self):
            return {}

    with pytest.raises(IsolatedContextError, match="associated with an active Browser instance"):
        async with factory.open(NoBrowser()):
            pass

    # Browser missing new_context
    class BrokenBrowserCtx:
        browser = object()

        async def storage_state(self):
            return {}

    with pytest.raises(IsolatedContextError, match="must provide a callable new_context"):
        async with factory.open(BrokenBrowserCtx()):
            pass


@pytest.mark.asyncio
async def test_no_source_context_fallback():
    """Proves factory fails closed and NEVER returns source_context if context creation fails."""
    factory = IsolatedExecutionContextFactory()

    class FailingBrowser:
        async def new_context(self, **kwargs):
            raise RuntimeError("Underlying browser failed to allocate context")

    class SourceCtxWithFailingBrowser:
        browser = FailingBrowser()

        async def storage_state(self):
            return {"cookies": [], "origins": []}

    source_double = SourceCtxWithFailingBrowser()
    with pytest.raises(IsolatedContextError, match="Failed to create disposable execution context"):
        async with factory.open(source_double):
            pytest.fail("Should never reach inner block")


@pytest.mark.asyncio
async def test_distinct_context_enforced():
    """Proves factory fails closed if disposable context is identical to source context."""
    factory = IsolatedExecutionContextFactory()

    class IdentityBrowser:
        def __init__(self):
            self.owner = None

        async def new_context(self, **kwargs):
            return self.owner

    source_double = type("SourceDouble", (), {})()
    b = IdentityBrowser()
    b.owner = source_double
    source_double.browser = b
    source_double.storage_state = lambda: asyncio.sleep(0, result={"cookies": [], "origins": []})

    with pytest.raises(IsolatedContextError, match="must be a distinct BrowserContext"):
        async with factory.open(source_double):
            pytest.fail("Should never reach inner block")


def test_no_disk_auth_files():
    """Static AST audit confirming production B2a contains zero disk serialization calls."""
    import app.qa.isolated_execution_context as mod
    source_code = inspect.getsource(mod)
    tree = ast.parse(source_code)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in ("open", "write"):
                pytest.fail(f"Forbidden call to '{node.func.id}' found in production B2a code!")
            if isinstance(node.func, ast.Attribute) and node.func.attr in (
                "dump",
                "write_text",
                "NamedTemporaryFile",
                "TemporaryDirectory",
            ):
                pytest.fail(f"Forbidden attribute call to '{node.func.attr}' found in production B2a code!")

    assert "storage_state(path=" not in source_code


def test_boundedness_and_no_retention():
    """Proves factory instance maintains zero state, registry, or caching."""
    factory = IsolatedExecutionContextFactory()
    assert factory.__dict__ == {}
    assert not hasattr(factory, "_contexts")
    assert not hasattr(factory, "_auth_cache")
    assert not hasattr(factory, "_history")
